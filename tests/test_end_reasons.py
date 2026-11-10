import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks

from app.i18n import SUPPORTED_LOCALES, translate
from app.models.reservation import EndReason, ReservationStatus
from app.routers import reservations
from app.services.end_reasons import public_end_reason
from app.services.reservation import end_reservation


def ended(end_reason, **extra):
    return SimpleNamespace(
        status=ReservationStatus.ENDED,
        end_reason=end_reason,
        player_joined=False,
        **extra,
    )


class PublicEndReasonTests(unittest.TestCase):
    def test_each_recorded_reason_has_its_own_message(self):
        messages = {
            reason: public_end_reason(ended(reason), 15)
            for reason in (
                EndReason.OWNER,
                EndReason.OWNER_IN_GAME,
                EndReason.ADMIN,
                EndReason.EXPIRED,
                EndReason.EMPTY,
                EndReason.SERVER_REMOVED,
            )
        }

        self.assertEqual(
            translate("status.end_reason_owner", "en"), messages[EndReason.OWNER]
        )
        self.assertIn("!end", messages[EndReason.OWNER_IN_GAME])
        self.assertEqual(
            translate("status.end_reason_expired", "en"),
            messages[EndReason.EXPIRED],
        )
        self.assertEqual(len(messages), len(set(messages.values())))

    def test_empty_server_message_says_whether_anyone_joined(self):
        never_joined = public_end_reason(ended(EndReason.EMPTY), 15)
        emptied = public_end_reason(
            SimpleNamespace(
                status=ReservationStatus.ENDED,
                end_reason=EndReason.EMPTY,
                player_joined=True,
            ),
            15,
        )

        self.assertEqual(
            translate("status.end_reason_never_joined", "en", minutes=15),
            never_joined,
        )
        self.assertEqual(
            translate("status.end_reason_empty", "en", minutes=15), emptied
        )
        self.assertIn("15", never_joined)
        self.assertIn("15", emptied)

    def test_no_message_without_a_known_reason(self):
        # Reservations that ended before reasons were recorded have none.
        self.assertIsNone(public_end_reason(ended(None), 15))
        self.assertIsNone(public_end_reason(ended("something_new"), 15))
        # The owner can no longer open a reservation after deleting their
        # account, so that reason has no message.
        self.assertIsNone(public_end_reason(ended(EndReason.ACCOUNT_DELETED), 15))

    def test_no_message_while_the_reservation_is_running(self):
        reservation = SimpleNamespace(
            status=ReservationStatus.ACTIVE,
            end_reason=EndReason.EMPTY,
            player_joined=True,
        )

        self.assertIsNone(public_end_reason(reservation, 15))

    def test_every_locale_translates_every_message(self):
        keys = (
            "owner", "in_game", "admin", "expired",
            "empty", "never_joined", "server_removed",
        )
        for locale in SUPPORTED_LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    full_key = f"status.end_reason_{key}"
                    message = translate(full_key, locale, minutes=15)
                    self.assertNotEqual(full_key, message)
                    if locale != "en":
                        self.assertNotEqual(
                            translate(full_key, "en", minutes=15), message
                        )

    def test_ended_page_shows_the_message(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")

        self.assertIn("{% if is_ended and end_reason_message %}", template)
        self.assertIn("{{ end_reason_message }}", template)


class RecordEndReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_reservation_stores_the_reason(self):
        reservation = SimpleNamespace(
            can_be_ended=True,
            status=ReservationStatus.ACTIVE,
            ended_at=None,
            end_reason=None,
            reservation_number=1000,
        )
        db = SimpleNamespace(commit=AsyncMock())

        await end_reservation(reservation, db, end_reason=EndReason.OWNER)

        self.assertEqual(ReservationStatus.ENDED, reservation.status)
        self.assertEqual(EndReason.OWNER, reservation.end_reason)
        self.assertIsNotNone(reservation.ended_at)
        db.commit.assert_awaited_once()

    async def end_from_website(self, user):
        reservation = SimpleNamespace(
            id=42,
            user_id=7,
            can_be_ended=True,
            status=ReservationStatus.ACTIVE,
            started_at=None,
            ended_at=None,
            end_reason=None,
            reservation_number=1000,
        )
        db = SimpleNamespace(commit=AsyncMock())

        with (
            patch.object(
                reservations,
                "get_reservation_by_id",
                AsyncMock(return_value=reservation),
            ),
            patch.object(reservations, "end_reservation_runtime", AsyncMock()),
            patch("app.services.timer.cancel_expiry_timer"),
            patch("app.routers.internal.clear_player_data"),
        ):
            await reservations.end_reservation_endpoint(
                reservation_id=42,
                background_tasks=BackgroundTasks(),
                user=user,
                db=db,
            )
        return reservation

    async def test_website_end_by_owner_is_recorded_as_owner(self):
        reservation = await self.end_from_website(
            SimpleNamespace(id=7, is_admin=False)
        )

        self.assertEqual(EndReason.OWNER, reservation.end_reason)

    async def test_website_end_by_admin_is_recorded_as_admin(self):
        reservation = await self.end_from_website(
            SimpleNamespace(id=1, is_admin=True)
        )

        self.assertEqual(EndReason.ADMIN, reservation.end_reason)


if __name__ == "__main__":
    unittest.main()

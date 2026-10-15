import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every model and relationship
from app.database import Base
from app.models.feedback import Feedback
from app.models.instance import CloudInstance, EnabledLocation, Provider
from app.models.instant import InstantAssignment, InstantHost, InstantSlot
from app.models.reservation import Reservation, ReservationStatus, RuntimeKind
from app.models.user import User
from app.routers import admin as admin_router
from app.routers import feedback as feedback_router
from app.routers import pages
from app.routers.admin import UpdateFeedbackRequest, delete_feedback, update_feedback
from app.routers.auth import delete_account, export_account_data
from app.routers.feedback import FeedbackRequest, submit_feedback
from app.services import feedback as feedback_service


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_URL = "https://discord.test/api/webhooks/1/secret-token"


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient and records posted payloads."""

    calls: list = []
    response_status = 204
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json):
        type(self).calls.append((url, json))
        if type(self).error is not None:
            raise type(self).error
        return httpx.Response(type(self).response_status, request=httpx.Request("POST", url))


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record):
            # Match production so foreign keys block deleting referenced users.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            db.add(Provider(code="vultr", name="Vultr"))
            await db.flush()
            db.add(EnabledLocation(
                code="hel", name="Helsinki", city="Helsinki",
                provider="vultr", provider_region="hel",
            ))
            self.owner = User(steam_id="76561198000000001", display_name="Owner")
            self.other = User(steam_id="76561198000000002", display_name="Other")
            db.add_all([self.owner, self.other])
            await db.flush()
            self.ended = self._reservation(1001, self.owner, config_file="rgl_6s_koth")
            self.foreign = self._reservation(1002, self.other)
            db.add_all([self.ended, self.foreign])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    @staticmethod
    def _reservation(number: int, user: User, **overrides) -> Reservation:
        created_at = datetime(2026, 10, 1, 12, 0)
        values = dict(
            reservation_number=number,
            user_id=user.id,
            location="hel",
            starts_at=created_at,
            ends_at=created_at + timedelta(hours=2),
            password="password",
            rcon_password="rcon-password",
            tv_password="tv-password",
            first_map="koth_product_final",
            motd_token=f"motd-{number}",
            logsecret=f"log-{number}",
            plugin_api_key=f"plugin-{number}",
            status=ReservationStatus.ENDED,
            created_at=created_at,
            started_at=created_at + timedelta(minutes=2),
            ended_at=created_at + timedelta(hours=1),
            peak_player_count=12,
        )
        values.update(overrides)
        return Reservation(**values)

    async def _submit(self, db, *, background_tasks=None, **body):
        payload = {"category": "lag", "message": "Rubber-banding on mid."}
        payload.update(body)
        return await submit_feedback(
            body=FeedbackRequest(**payload),
            background_tasks=background_tasks or BackgroundTasks(),
            user=self.owner,
            db=db,
        )

    async def _feedback_rows(self) -> list[Feedback]:
        async with self.session_maker() as db:
            return list((await db.execute(select(Feedback).order_by(Feedback.id))).scalars())

    # -- Submission ---------------------------------------------------------

    async def test_general_feedback_is_stored_without_context(self):
        async with self.session_maker() as db:
            response = await self._submit(db, category="suggestion", message="  Add koth_lakeside  ")

        [row] = await self._feedback_rows()
        self.assertEqual({"id": row.id}, response)
        self.assertEqual(self.owner.id, row.user_id)
        self.assertIsNone(row.reservation_id)
        self.assertIsNone(row.context)
        self.assertEqual("suggestion", row.category)
        self.assertEqual("Add koth_lakeside", row.message)
        self.assertEqual("new", row.status)

    async def test_reservation_feedback_snapshots_context_with_location_provider_fallback(self):
        async with self.session_maker() as db:
            await self._submit(db, reservation_id=self.ended.id)

        [row] = await self._feedback_rows()
        self.assertEqual(self.ended.id, row.reservation_id)
        context = row.context
        self.assertEqual(1001, context["reservation_number"])
        self.assertEqual("ended", context["status"])
        self.assertEqual("cloud", context["runtime_kind"])
        self.assertEqual("hel", context["location"])
        self.assertEqual("Helsinki", context["location_city"])
        self.assertEqual("koth_product_final", context["first_map"])
        self.assertEqual("rgl_6s_koth", context["config_file"])
        self.assertEqual(12, context["peak_player_count"])
        self.assertEqual("2026-10-01T12:02:00+00:00", context["started_at"])
        # The CloudInstance row is gone after teardown, so the location mapping is used.
        self.assertEqual("vultr", context["provider"])
        self.assertEqual("hel", context["provider_region"])
        self.assertEqual("location", context["provider_source"])

    async def test_context_prefers_the_live_cloud_instance(self):
        async with self.session_maker() as db:
            db.add(CloudInstance(
                id="vm-1", instance_id="agent-1", location="hel", auth_token="token",
                provider_code="gcore", provider_region="180",
            ))
            active = self._reservation(
                1003, self.owner, status=ReservationStatus.ACTIVE,
                instance_id="vm-1", ended_at=None,
            )
            db.add(active)
            await db.commit()
            reservation = await feedback_service.get_reservation_for_feedback(active.id, db)
            context = await feedback_service.build_reservation_context(reservation, db)

        self.assertEqual("gcore", context["provider"])
        self.assertEqual("180", context["provider_region"])
        self.assertEqual("instance", context["provider_source"])
        self.assertEqual("vm-1", context["instance_id"])

    async def test_instant_context_names_the_latest_host(self):
        async with self.session_maker() as db:
            old_host = InstantHost(name="hel-old", location="hel")
            new_host = InstantHost(name="hel-new", location="hel")
            db.add_all([old_host, new_host])
            await db.flush()
            old_slot = InstantSlot(host_id=old_host.id, slot_index=0, game_port=27015, tv_port=27020)
            new_slot = InstantSlot(host_id=new_host.id, slot_index=0, game_port=27015, tv_port=27020)
            db.add_all([old_slot, new_slot])
            instant = self._reservation(1004, self.owner, runtime_kind=RuntimeKind.INSTANT)
            db.add(instant)
            await db.flush()
            now = datetime(2026, 10, 1, 12, 5)
            db.add_all([
                InstantAssignment(
                    reservation_id=instant.id, slot_id=old_slot.id, generation=1,
                    command_id="cmd-1", closed_at=now,
                ),
                InstantAssignment(
                    reservation_id=instant.id, slot_id=new_slot.id, generation=2,
                    command_id="cmd-2", closed_at=now,
                ),
            ])
            await db.commit()
            reservation = await feedback_service.get_reservation_for_feedback(instant.id, db)
            context = await feedback_service.build_reservation_context(reservation, db)

        self.assertEqual("instant", context["runtime_kind"])
        self.assertEqual("hel-new", context["instant_host"])
        self.assertNotIn("provider", context)

    async def test_another_users_reservation_is_not_found(self):
        async with self.session_maker() as db:
            with self.assertRaises(HTTPException) as raised:
                await self._submit(db, reservation_id=self.foreign.id)

        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("Reservation not found", raised.exception.detail)
        self.assertEqual([], await self._feedback_rows())

    async def test_invalid_submissions_are_rejected_with_translated_errors(self):
        cases = [
            ({"category": "praise"}, "Choose a feedback category."),
            ({"category": ""}, "Choose a feedback category."),
            ({"message": " \n\t "}, "Write a message before sending."),
            (
                {"message": "x" * (feedback_service.MAX_MESSAGE_LENGTH + 1)},
                "Your message is too long. The limit is 2000 characters.",
            ),
        ]
        async with self.session_maker() as db:
            for body, detail in cases:
                with self.subTest(body=body):
                    with self.assertRaises(HTTPException) as raised:
                        await self._submit(db, **body)
                    self.assertEqual(400, raised.exception.status_code)
                    self.assertEqual(detail, raised.exception.detail)

        self.assertEqual([], await self._feedback_rows())

    async def test_message_at_the_limit_is_accepted(self):
        async with self.session_maker() as db:
            await self._submit(db, message="x" * feedback_service.MAX_MESSAGE_LENGTH)

        self.assertEqual(1, len(await self._feedback_rows()))

    async def test_sixth_submission_within_an_hour_is_rate_limited(self):
        async with self.session_maker() as db:
            # Older feedback falls outside the window.
            db.add(Feedback(
                user_id=self.owner.id, category="other", message="old",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
            ))
            await db.commit()
            for _ in range(feedback_service.FEEDBACK_PER_HOUR):
                await self._submit(db)
            with self.assertRaises(HTTPException) as raised:
                await self._submit(db)

        self.assertEqual(429, raised.exception.status_code)
        self.assertEqual(feedback_service.FEEDBACK_PER_HOUR + 1, len(await self._feedback_rows()))

    async def test_rate_limit_is_per_user(self):
        async with self.session_maker() as db:
            for _ in range(feedback_service.FEEDBACK_PER_HOUR):
                db.add(Feedback(user_id=self.other.id, category="other", message="spam"))
            await db.commit()
            await self._submit(db)

        self.assertEqual(feedback_service.FEEDBACK_PER_HOUR + 1, len(await self._feedback_rows()))

    # -- Discord webhook ----------------------------------------------------

    async def test_webhook_is_scheduled_only_when_configured(self):
        tasks = BackgroundTasks()
        with patch.object(
            feedback_router, "settings", SimpleNamespace(discord_feedback_webhook_configured=False)
        ):
            async with self.session_maker() as db:
                await self._submit(db, background_tasks=tasks)
        self.assertEqual([], tasks.tasks)

        tasks = BackgroundTasks()
        with patch.object(
            feedback_router, "settings", SimpleNamespace(discord_feedback_webhook_configured=True)
        ):
            async with self.session_maker() as db:
                await self._submit(db, background_tasks=tasks, reservation_id=self.ended.id)
        [task] = tasks.tasks
        self.assertIs(feedback_service.send_feedback_webhook, task.func)
        [payload] = task.args
        embed = payload["embeds"][0]
        self.assertEqual("Lag or performance · Reservation #1001", embed["title"])
        self.assertEqual("Rubber-banding on mid.", embed["description"])
        self.assertEqual({"parse": []}, payload["allowed_mentions"])
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(
            "[Owner](https://steamcommunity.com/profiles/76561198000000001)", fields["User"]
        )
        self.assertEqual("Helsinki (hel)", fields["Location"])
        self.assertEqual("vultr · hel (location default)", fields["Server"])
        self.assertEqual(r"koth\_product\_final · rgl\_6s\_koth", fields["Map / config"])

    def test_general_webhook_payload_escapes_user_markdown(self):
        user = SimpleNamespace(steam_id="76561198000000003", display_name="[evil](https://x) @everyone")
        payload = feedback_service.build_webhook_payload(
            category="website", message="@everyone look", user=user,
            reservation_number=None, context=None,
        )
        embed = payload["embeds"][0]
        self.assertEqual("Website · General", embed["title"])
        self.assertTrue(embed["url"].endswith("/admin#feedback"))
        self.assertEqual(
            r"[\[evil\]\(https://x\) @everyone](https://steamcommunity.com/profiles/76561198000000003)",
            embed["fields"][0]["value"],
        )
        self.assertEqual({"parse": []}, payload["allowed_mentions"])

    async def test_send_feedback_webhook_posts_payload_without_mentions(self):
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.response_status = 204
        _FakeAsyncClient.error = None
        payload = feedback_service.build_webhook_payload(
            category="lag", message="lag", user=self.owner,
            reservation_number=None, context=None,
        )
        with (
            patch.object(feedback_service, "settings", SimpleNamespace(discord_feedback_webhook_url=WEBHOOK_URL)),
            patch.object(feedback_service.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            await feedback_service.send_feedback_webhook(payload)

        [(url, posted)] = _FakeAsyncClient.calls
        self.assertEqual(WEBHOOK_URL, url)
        self.assertEqual([], posted["allowed_mentions"]["parse"])

    async def test_send_feedback_webhook_swallows_errors_without_logging_the_token(self):
        failures = [
            (500, None, "HTTP 500"),
            (204, httpx.ConnectError("connection refused"), "ConnectError"),
        ]
        for status, error, expected in failures:
            with self.subTest(expected=expected):
                _FakeAsyncClient.calls = []
                _FakeAsyncClient.response_status = status
                _FakeAsyncClient.error = error
                with (
                    patch.object(feedback_service, "settings", SimpleNamespace(discord_feedback_webhook_url=WEBHOOK_URL)),
                    patch.object(feedback_service.httpx, "AsyncClient", _FakeAsyncClient),
                    self.assertLogs("app.services.feedback", "WARNING") as logs,
                ):
                    await feedback_service.send_feedback_webhook({"embeds": []})

                output = "\n".join(logs.output)
                self.assertIn(expected, output)
                self.assertNotIn("secret-token", output)
        _FakeAsyncClient.error = None

    # -- Admin --------------------------------------------------------------

    async def test_admin_can_resolve_reopen_and_delete_feedback(self):
        admin = SimpleNamespace(is_admin=True)
        async with self.session_maker() as db:
            created = await self._submit(db)
            feedback_id = created["id"]

            self.assertEqual(
                {"id": feedback_id, "status": "resolved"},
                await update_feedback(feedback_id, UpdateFeedbackRequest(status="resolved"), user=admin, db=db),
            )
            self.assertEqual(
                {"id": feedback_id, "status": "new"},
                await update_feedback(feedback_id, UpdateFeedbackRequest(status="new"), user=admin, db=db),
            )
            with self.assertRaises(HTTPException) as raised:
                await update_feedback(feedback_id, UpdateFeedbackRequest(status="ignored"), user=admin, db=db)
            self.assertEqual(400, raised.exception.status_code)

            self.assertEqual(
                {"deleted": True, "id": feedback_id},
                await delete_feedback(feedback_id, user=admin, db=db),
            )

        self.assertEqual([], await self._feedback_rows())

    async def test_admin_endpoints_return_404_for_unknown_feedback(self):
        admin = SimpleNamespace(is_admin=True)
        async with self.session_maker() as db:
            for call in (
                update_feedback(999, UpdateFeedbackRequest(status="resolved"), user=admin, db=db),
                delete_feedback(999, user=admin, db=db),
            ):
                with self.subTest(call=call):
                    with self.assertRaises(HTTPException) as raised:
                        await call
                    self.assertEqual(404, raised.exception.status_code)
                    self.assertEqual("Feedback not found", raised.exception.detail)

    async def test_admin_panel_lists_feedback_with_user_and_reservation(self):
        async with self.session_maker() as db:
            await self._submit(db, reservation_id=self.ended.id, message="Lag spike at 20:00")
            with patch.object(admin_router.templates, "TemplateResponse", return_value="admin") as render:
                await admin_router.admin_panel(
                    request=SimpleNamespace(), user=SimpleNamespace(is_admin=True), db=db,
                )

        [item] = render.call_args.args[2]["feedback"]
        self.assertEqual("Lag spike at 20:00", item["message"])
        self.assertEqual("Owner", item["user_name"])
        self.assertEqual("76561198000000001", item["user_steam_id"])
        self.assertEqual(self.ended.id, item["reservation_id"])
        self.assertEqual(1001, item["reservation_number"])
        self.assertEqual("Helsinki", item["context"]["location_city"])
        self.assertTrue(item["created_at"].endswith("+00:00"))

    # -- Privacy ------------------------------------------------------------

    async def test_export_includes_feedback(self):
        async with self.session_maker() as db:
            await self._submit(db, reservation_id=self.ended.id, category="config", message="Wrong cfg")
            response = await export_account_data(user=self.owner, db=db)

        [item] = json.loads(response.body)["feedback"]
        self.assertEqual("config", item["category"])
        self.assertEqual("Wrong cfg", item["message"])
        self.assertEqual("new", item["status"])
        self.assertEqual(1001, item["reservation_number"])
        self.assertIsNotNone(item["created_at"])

    async def test_delete_account_removes_feedback(self):
        async with self.session_maker() as db:
            await self._submit(db, reservation_id=self.ended.id)
            owner = await db.get(User, self.owner.id)
            response = await delete_account(user=owner, db=db)

        self.assertEqual(200, response.status_code)
        self.assertEqual([], await self._feedback_rows())
        async with self.session_maker() as db:
            self.assertIsNone(await db.get(User, self.owner.id))
            reservation = await db.get(Reservation, self.ended.id)
            self.assertIsNone(reservation.user_id)

    # -- Page ---------------------------------------------------------------

    async def _render_page(self, user, reservation=None):
        async with self.session_maker() as db:
            with (
                patch.object(pages, "get_current_user", AsyncMock(return_value=user)),
                patch.object(pages, "has_beta_access", return_value=True),
                patch.object(pages.templates, "TemplateResponse", return_value="feedback page") as render,
            ):
                response = await pages.feedback_page(
                    request=SimpleNamespace(), reservation=reservation, db=db,
                )
        self.assertEqual("feedback page", response)
        self.assertEqual("feedback.html", render.call_args.args[1])
        return render.call_args.args[2]

    async def test_feedback_page_lists_only_the_users_reservations(self):
        context = await self._render_page(self.owner)

        self.assertTrue(context["can_submit"])
        self.assertIsNone(context["preselected_id"])
        self.assertEqual(
            [{"id": self.ended.id, "reservation_number": 1001, "city": "Helsinki"}],
            [{k: r[k] for k in ("id", "reservation_number", "city")} for r in context["reservations"]],
        )
        self.assertEqual(feedback_service.FEEDBACK_CATEGORIES, context["categories"])
        self.assertEqual(feedback_service.MAX_MESSAGE_LENGTH, context["max_length"])

    async def test_feedback_page_preselects_owned_reservation_and_ignores_others(self):
        for value, expected in (
            (str(self.ended.id), self.ended.id),
            (str(self.foreign.id), None),
            ("abc", None),
            ("²", None),
            ("9" * 30, None),
        ):
            with self.subTest(value=value):
                context = await self._render_page(self.owner, reservation=value)
                self.assertEqual(expected, context["preselected_id"])
                self.assertNotIn(self.foreign.id, [r["id"] for r in context["reservations"]])

    async def test_feedback_page_appends_owned_reservation_outside_recent_list(self):
        async with self.session_maker() as db:
            for number in range(2000, 2020):
                db.add(self._reservation(
                    number, self.owner, created_at=datetime(2026, 10, 2) + timedelta(minutes=number),
                ))
            await db.commit()

        context = await self._render_page(self.owner, reservation=str(self.ended.id))

        self.assertEqual(self.ended.id, context["preselected_id"])
        self.assertEqual(21, len(context["reservations"]))
        self.assertEqual(self.ended.id, context["reservations"][-1]["id"])

    async def test_feedback_page_hides_form_for_banned_and_logged_out_users(self):
        banned = SimpleNamespace(id=self.owner.id, is_banned=True)
        context = await self._render_page(banned)
        self.assertFalse(context["can_submit"])
        self.assertEqual([], context["reservations"])

        context = await self._render_page(None)
        self.assertFalse(context["can_submit"])
        self.assertIsNone(context["user"])

    async def test_robots_disallows_feedback(self):
        response = await pages.robots_txt()
        self.assertIn("Disallow: /feedback\n", response.body.decode())


class FeedbackTemplateTests(unittest.TestCase):
    @staticmethod
    def _template(name: str) -> str:
        return (PROJECT_ROOT / "templates" / name).read_text(encoding="utf-8")

    def test_footer_links_to_feedback(self):
        self.assertIn('<a href="/feedback" class="hover:text-slate-800">{{ _(\'footer.feedback\') }}</a>',
                      self._template("base.html"))

    def test_status_page_offers_feedback_only_to_owner_after_terminal_status(self):
        template = self._template("status.html")
        link = template.index("_('status.send_feedback')")
        block = template[template.rindex("{% if user.id == reservation.user_id %}", 0, link):link]
        self.assertIn('href="/feedback?reservation={{ reservation.id }}"', block)
        self.assertIn(
            "x-show=\"['ended', 'failed', 'cancelled', 'no_show'].includes(status)\"", block
        )

    def test_my_reservations_links_terminal_rows_to_feedback(self):
        template = self._template("my_reservations.html")
        self.assertIn(
            "{% if reservation.status.value in ['ended', 'failed', 'cancelled', 'no_show'] %}", template
        )
        self.assertIn('href="/feedback?reservation={{ reservation.id }}"', template)

    def test_admin_renders_feedback_text_without_html(self):
        template = self._template("admin/index.html")
        self.assertIn('x-text="f.message"', template)
        self.assertNotIn('x-html="f.', template)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.reservation import Reservation, ReservationStatus
from app.routers import internal, reservations
from app.services import server_liveness

USER = SimpleNamespace(id=7, is_admin=False)
NUMBER = 408
START = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)


def naive(value):
    return value.replace(tzinfo=None) if value is not None else None


class RconEmptyTimerTests(unittest.IsolatedAsyncioTestCase):
    """Lobby sites poll a waiting server over RCON; that holds off the empty auto-end."""

    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "rcon_timer.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as db:
            db.add(Reservation(
                id=NUMBER, reservation_number=NUMBER, user_id=USER.id,
                location="vultr-fra",
                starts_at=START, ends_at=START + timedelta(hours=4),
                first_map="koth_product_final", current_map="koth_product_final",
                password="pw", rcon_password="rcon", tv_password="tv",
                motd_token="motd", logsecret="log",
                status=ReservationStatus.ACTIVE,
                enable_direct_connect=True, enable_rcon=True,
                # Set when the server became ready.
                empty_since=START,
            ))
            await db.commit()

        server_liveness._servers.clear()
        self.addCleanup(server_liveness._servers.clear)
        internal.player_data.clear()
        self.addCleanup(internal.player_data.clear)

        session_patch = patch.object(internal, "async_session_maker", self.sessions)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        key_patch = patch.object(
            internal, "validate_reservation_api_key", AsyncMock(return_value=True)
        )
        key_patch.start()
        self.addCleanup(key_patch.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def empty_since(self):
        async with self.sessions() as db:
            return (await db.get(Reservation, NUMBER)).empty_since

    async def report(self, at, player_count=0, rcon_idle_seconds=None):
        body = internal.PlayerUpdateRequest(
            player_count=player_count, rcon_idle_seconds=rcon_idle_seconds
        )
        with patch.object(internal, "datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            await internal.update_players(NUMBER, body, x_api_key="key")

    async def reset_by_rcon(self):
        async with self.sessions() as db:
            view = await reservations.get_reservation_players(NUMBER, user=USER, db=db)
        return view["empty_timer_reset_by_rcon"]

    async def test_polling_lobby_site_keeps_restarting_the_timer(self):
        # Validation and setup, then a poll about every five minutes.
        for minutes in (2, 7, 12, 47, 82):
            await self.report(START + minutes * MINUTE, rcon_idle_seconds=4)

            self.assertEqual(
                naive(START + minutes * MINUTE - 4 * SECOND), await self.empty_since()
            )
            self.assertTrue(await self.reset_by_rcon())

    async def test_reports_about_the_same_command_leave_the_timer_alone(self):
        command_at = START + 10 * MINUTE
        await self.report(command_at + 3 * SECOND, rcon_idle_seconds=3)
        timer = await self.empty_since()
        self.assertEqual(naive(command_at), timer)

        # The age is in whole seconds, so later reports put the command up to
        # a second later.
        await self.report(command_at + 13.9 * SECOND, rcon_idle_seconds=13)
        await self.report(command_at + 24.2 * SECOND, rcon_idle_seconds=24)

        self.assertEqual(timer, await self.empty_since())
        self.assertTrue(await self.reset_by_rcon())

    async def test_timer_runs_out_once_the_polling_stops(self):
        last_poll = START + 40 * MINUTE
        await self.report(last_poll, rcon_idle_seconds=0)

        for minutes in range(1, 31):
            await self.report(
                last_poll + minutes * MINUTE, rcon_idle_seconds=minutes * 60
            )

        self.assertEqual(naive(last_poll), await self.empty_since())

    async def test_players_leaving_after_the_last_command_start_the_timer(self):
        await self.report(START + 5 * MINUTE, player_count=12, rcon_idle_seconds=0)
        self.assertIsNone(await self.empty_since())
        self.assertFalse(await self.reset_by_rcon())

        game_over = START + 40 * MINUTE
        await self.report(game_over, rcon_idle_seconds=35 * 60)

        self.assertEqual(naive(game_over), await self.empty_since())
        self.assertFalse(await self.reset_by_rcon())

    async def test_commands_while_players_are_on_keep_the_timer_off(self):
        await self.report(START + 5 * MINUTE, player_count=3, rcon_idle_seconds=1)

        self.assertIsNone(await self.empty_since())
        self.assertFalse(await self.reset_by_rcon())

    async def test_without_rcon_commands_the_timer_counts_from_server_ready(self):
        # Older plugins and servers no RCON tool has used send no age.
        await self.report(START + 5 * MINUTE)

        self.assertEqual(naive(START), await self.empty_since())
        self.assertFalse(await self.reset_by_rcon())

    async def test_age_from_a_skewed_clock_never_moves_the_timer_ahead(self):
        await self.report(START + 5 * MINUTE, rcon_idle_seconds=-30)

        self.assertEqual(naive(START + 5 * MINUTE), await self.empty_since())


if __name__ == "__main__":
    unittest.main()

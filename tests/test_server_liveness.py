import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.reservation import EndReason, Reservation, ReservationStatus
from app.routers import internal, reservations
from app.routers.internal import RconRequestTimeout, RconRequestUnavailable
from app.services import server_liveness
from app.services.end_reasons import public_end_reason

USER = SimpleNamespace(id=7, is_admin=False)
NUMBER = 401
START = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)

ANSWERED = {"output": "summon_liveness_check", "error": None}
AGENT_ERROR = {"output": "", "error": "context deadline exceeded"}


def naive(value):
    return value.replace(tzinfo=None) if value is not None else None


class ServerLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "liveness.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as db:
            db.add(Reservation(
                id=NUMBER, reservation_number=NUMBER, user_id=USER.id,
                location="vultr-fra",
                starts_at=START, ends_at=START + timedelta(hours=3),
                first_map="cp_process_final", current_map="cp_process_final",
                password="pw", rcon_password="rcon", tv_password="tv",
                motd_token="motd", logsecret="log",
                status=ReservationStatus.ACTIVE,
            ))
            await db.commit()

        server_liveness._servers.clear()
        self.addCleanup(server_liveness._servers.clear)
        internal.player_data.clear()
        self.addCleanup(internal.player_data.clear)

        for target in (server_liveness, internal):
            session_patch = patch.object(target, "async_session_maker", self.sessions)
            session_patch.start()
            self.addCleanup(session_patch.stop)
        key_patch = patch.object(
            internal, "validate_reservation_api_key", AsyncMock(return_value=True)
        )
        key_patch.start()
        self.addCleanup(key_patch.stop)

        self.rcon = AsyncMock(return_value=AGENT_ERROR)
        rcon_patch = patch(
            "app.services.runtime.awaited_rcon_reservation_runtime", self.rcon
        )
        rcon_patch.start()
        self.addCleanup(rcon_patch.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def stored(self):
        async with self.sessions() as db:
            return await db.get(Reservation, NUMBER)

    async def set_fields(self, **fields):
        async with self.sessions() as db:
            reservation = await db.get(Reservation, NUMBER)
            for name, value in fields.items():
                setattr(reservation, name, value)
            await db.commit()

    async def report(self, at, player_count=1):
        body = internal.PlayerUpdateRequest(player_count=player_count)
        with patch.object(internal, "datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            await internal.update_players(NUMBER, body, x_api_key="key")

    async def check(self, at):
        await server_liveness.check_active_servers(now=at)

    async def players_view(self):
        async with self.sessions() as db:
            return await reservations.get_reservation_players(NUMBER, user=USER, db=db)

    async def make_unresponsive(self, last_report=START, player_count=1):
        await self.report(last_report, player_count=player_count)
        await self.check(last_report + 3 * MINUTE)
        await self.check(last_report + 4 * MINUTE)
        self.assertTrue(server_liveness.is_unresponsive(NUMBER))

    async def test_server_that_keeps_reporting_is_never_checked(self):
        for minute in range(0, 10):
            await self.report(START + minute * MINUTE)
            await self.check(START + minute * MINUTE + timedelta(seconds=30))

        self.rcon.assert_not_awaited()
        self.assertFalse(server_liveness.is_unresponsive(NUMBER))

    async def test_silent_server_that_misses_two_checks_starts_the_empty_timer(self):
        await self.report(START)

        await self.check(START + 2 * MINUTE)
        self.rcon.assert_not_awaited()

        await self.check(START + 3 * MINUTE)
        self.assertEqual(1, self.rcon.await_count)
        self.assertEqual(
            server_liveness.CHECK_COMMAND, self.rcon.await_args.args[2]
        )
        # One missed answer is not enough.
        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)

        await self.check(START + 4 * MINUTE)
        self.assertEqual(2, self.rcon.await_count)
        self.assertTrue(server_liveness.is_unresponsive(NUMBER))
        # The timer counts from the last report, when the server stopped.
        self.assertEqual(naive(START), (await self.stored()).empty_since)
        self.assertEqual(EndReason.UNRESPONSIVE, server_liveness.auto_end_reason(NUMBER))

        view = await self.players_view()
        self.assertEqual(START.isoformat(), view["unresponsive_since"])
        self.assertIsNotNone(view["empty_since"])

    async def test_rcon_timeout_counts_as_unanswered(self):
        self.rcon.side_effect = RconRequestTimeout("timed out")

        await self.make_unresponsive()

    async def test_server_already_empty_keeps_its_timer(self):
        empty_at = START - 10 * MINUTE
        await self.set_fields(empty_since=empty_at)

        await self.make_unresponsive(player_count=0)

        self.assertEqual(naive(empty_at), (await self.stored()).empty_since)
        self.assertFalse(server_liveness._servers[NUMBER].started_empty_timer)

    async def test_server_that_answers_rcon_is_left_alone(self):
        # For example, the plugin cannot reach the website but players can
        # still play.
        self.rcon.return_value = ANSWERED
        await self.report(START)

        for minute in range(3, 8):
            await self.check(START + minute * MINUTE)

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)
        # Asked once, then again only after the recheck delay.
        self.assertEqual(1, self.rcon.await_count)
        await self.check(START + 3 * MINUTE + server_liveness.ANSWERED_RECHECK_DELAY)
        self.assertEqual(2, self.rcon.await_count)
        self.assertEqual(EndReason.EMPTY, server_liveness.auto_end_reason(NUMBER))

    async def test_one_slow_answer_does_not_mark_the_server(self):
        self.rcon.side_effect = [AGENT_ERROR, ANSWERED, AGENT_ERROR]
        await self.report(START)

        await self.check(START + 3 * MINUTE)
        await self.check(START + 4 * MINUTE)
        await self.check(START + 4 * MINUTE + server_liveness.ANSWERED_RECHECK_DELAY)

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)

    async def test_server_with_unreachable_agent_is_not_marked(self):
        self.rcon.side_effect = RconRequestUnavailable("Cloud agent is not connected")
        await self.report(START)

        for minute in range(3, 20):
            await self.check(START + minute * MINUTE)

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)

    async def test_report_with_players_clears_the_mark_and_timer(self):
        await self.make_unresponsive()

        await self.report(START + 6 * MINUTE, player_count=4)

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)
        self.assertIsNone((await self.players_view())["unresponsive_since"])

    async def test_server_that_recovers_empty_gets_a_fresh_timer(self):
        await self.make_unresponsive()
        recovered_at = START + 12 * MINUTE

        await self.report(recovered_at, player_count=0)

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertEqual(naive(recovered_at), (await self.stored()).empty_since)

    async def test_rcon_answer_after_the_mark_undoes_the_timer(self):
        await self.make_unresponsive()
        self.rcon.return_value = ANSWERED

        # A marked server is asked again less often.
        await self.check(START + 5 * MINUTE)
        self.assertEqual(2, self.rcon.await_count)
        self.assertTrue(server_liveness.is_unresponsive(NUMBER))

        await self.check(START + 4 * MINUTE + server_liveness.MARKED_RECHECK_DELAY)

        self.assertEqual(3, self.rcon.await_count)
        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)

    async def test_web_restart_measures_silence_from_first_sight(self):
        # A new process has no reports: the reservation is not judged by
        # anything from before it started.
        first_seen = START + 30 * MINUTE

        await self.check(first_seen)
        await self.check(first_seen + 2 * MINUTE)
        self.rcon.assert_not_awaited()

        await self.check(first_seen + 3 * MINUTE)
        await self.check(first_seen + 4 * MINUTE)
        self.assertTrue(server_liveness.is_unresponsive(NUMBER))
        self.assertEqual(naive(first_seen), (await self.stored()).empty_since)

    async def test_restart_is_not_flagged(self):
        await self.report(START)
        await self.check(START + 3 * MINUTE)

        # The restart endpoint clears player data before the server stops.
        internal.clear_player_data(NUMBER)
        await self.set_fields(status=ReservationStatus.PROVISIONING)
        for minute in range(4, 10):
            await self.check(START + minute * MINUTE)
        self.assertEqual(1, self.rcon.await_count)

        # Back up: silence counts from when it is next seen active.
        await self.set_fields(status=ReservationStatus.ACTIVE)
        await self.check(START + 10 * MINUTE)
        await self.check(START + 12 * MINUTE)
        self.assertEqual(1, self.rcon.await_count)
        self.assertFalse(server_liveness.is_unresponsive(NUMBER))

    async def test_reports_while_not_active_do_not_count(self):
        await self.set_fields(status=ReservationStatus.PROVISIONING)
        await self.report(START)

        self.assertNotIn(NUMBER, server_liveness._servers)

    async def test_ended_reservation_is_forgotten(self):
        await self.make_unresponsive()
        await self.set_fields(status=ReservationStatus.ENDED)

        await self.check(START + 5 * MINUTE)

        self.assertNotIn(NUMBER, server_liveness._servers)

    async def test_report_during_a_check_wins(self):
        await self.report(START)
        await self.check(START + 3 * MINUTE)
        answer = asyncio.Event()

        async def slow_unanswered(*args, **kwargs):
            await answer.wait()
            return AGENT_ERROR

        self.rcon.side_effect = slow_unanswered
        check = asyncio.create_task(self.check(START + 4 * MINUTE))
        while self.rcon.await_count < 2:
            await asyncio.sleep(0)
        await self.report(START + 4 * MINUTE, player_count=3)
        answer.set()
        await check

        self.assertFalse(server_liveness.is_unresponsive(NUMBER))
        self.assertIsNone((await self.stored()).empty_since)


class UnresponsiveEndReasonTests(unittest.TestCase):
    def test_unresponsive_end_has_its_own_message(self):
        reservation = SimpleNamespace(
            status=ReservationStatus.ENDED,
            end_reason=EndReason.UNRESPONSIVE,
            player_joined=True,
        )
        empty = SimpleNamespace(
            status=ReservationStatus.ENDED,
            end_reason=EndReason.EMPTY,
            player_joined=True,
        )

        message = public_end_reason(reservation, 30)

        self.assertIsNotNone(message)
        self.assertNotEqual(public_end_reason(empty, 30), message)
        self.assertIn("stopped responding", message)


if __name__ == "__main__":
    unittest.main()

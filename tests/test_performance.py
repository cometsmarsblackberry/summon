import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Response

from app.database import engine
from app.main import _templates
from app.models.reservation import Reservation
from app.routers import admin, motd, pages, status
from app.routers.internal import _update_persisted_player_state


class TemplateCacheTests(unittest.TestCase):
    def test_template_environments_keep_their_compilation_cache(self):
        environments = (
            _templates.env,
            pages.templates.env,
            admin.templates.env,
            motd.templates.env,
        )

        for environment in environments:
            with self.subTest(environment=id(environment)):
                self.assertIsNotNone(environment.cache)

    def test_homepage_reuses_compiled_template(self):
        first = pages.templates.get_template("home.html")
        second = pages.templates.get_template("home.html")
        self.assertIs(first, second)


class StatusCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        status._status_cache = None
        status._status_cache_time = 0
        status._status_cache_lock = asyncio.Lock()

    async def test_concurrent_cache_misses_build_status_once(self):
        responses = [Response() for _ in range(10)]

        async def slow_build(_db):
            await asyncio.sleep(0.01)
            return {"helsinki": {"available": 1}}

        build_status = AsyncMock(side_effect=slow_build)

        with patch.object(status, "_build_status", build_status):
            results = await asyncio.gather(
                *(status.get_status(response, object()) for response in responses)
            )

        self.assertEqual(1, build_status.await_count)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertTrue(
            all(
                response.headers["cache-control"] == status._STATUS_CACHE_CONTROL
                for response in responses
            )
        )


class SQLitePerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_connections_use_concurrency_pragmas(self):
        async with engine.connect() as connection:
            journal_mode = (
                await connection.exec_driver_sql("PRAGMA journal_mode")
            ).scalar_one()
            synchronous = (
                await connection.exec_driver_sql("PRAGMA synchronous")
            ).scalar_one()
            busy_timeout = (
                await connection.exec_driver_sql("PRAGMA busy_timeout")
            ).scalar_one()

        self.assertEqual("wal", journal_mode.lower())
        self.assertEqual(1, synchronous)  # NORMAL
        self.assertEqual(10000, busy_timeout)

    def test_rate_limit_indexes_are_declared(self):
        index_names = {index.name for index in Reservation.__table__.indexes}
        self.assertTrue(
            {
                "ix_reservations_created_at",
                "ix_reservations_user_created_at",
                "ix_reservations_status_created_at",
            }.issubset(index_names)
        )


class PlayerUpdateTests(unittest.TestCase):
    def test_unchanged_periodic_update_does_not_require_commit(self):
        reservation = SimpleNamespace(
            player_joined=True,
            peak_player_count=12,
            empty_since=None,
        )

        changed = _update_persisted_player_state(
            reservation,
            player_count=6,
            now=datetime.now(timezone.utc),
        )

        self.assertFalse(changed)

    def test_player_state_transitions_require_commit(self):
        empty_since = datetime.now(timezone.utc)
        reservation = SimpleNamespace(
            player_joined=False,
            peak_player_count=0,
            empty_since=empty_since,
        )

        changed = _update_persisted_player_state(
            reservation,
            player_count=6,
            now=datetime.now(timezone.utc),
        )

        self.assertTrue(changed)
        self.assertTrue(reservation.player_joined)
        self.assertEqual(6, reservation.peak_player_count)
        self.assertIsNone(reservation.empty_since)


if __name__ == "__main__":
    unittest.main()

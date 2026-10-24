import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Reservation, ReservationStatus
from app.models.instance import GameMap
from app.routers import reservations
from app.services.maps import get_map_download_url

FASTDL = "https://fastdl.example/tf/maps/"
USER = SimpleNamespace(id=7, is_admin=False)


class LiveServerStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "live-server-state.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.now(timezone.utc)
        async with self.sessions() as db:
            db.add(GameMap(name="cp_process_final", display_name="Process"))
            db.add(GameMap(name="koth_product_final", display_name="Product"))
            db.add(Reservation(
                id=301, reservation_number=301, user_id=USER.id, location="vultr-fra",
                starts_at=now, ends_at=now + timedelta(hours=2),
                first_map="cp_process_final", current_map="cp_process_final",
                password="pw", rcon_password="rcon", tv_password="tv",
                motd_token="motd-301", logsecret="log",
                status=ReservationStatus.ACTIVE,
            ))
            await db.commit()

        fastdl = patch(
            "app.services.settings.get_fastdl_url", AsyncMock(return_value=FASTDL)
        )
        fastdl.start()
        self.addCleanup(fastdl.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def get_status(self):
        async with self.sessions() as db:
            return await reservations.get_reservation(301, user=USER, db=db)

    async def change_level(self, map_name, rcon_ok=True):
        async with self.sessions() as db:
            with patch.object(
                reservations, "rcon_reservation_runtime", AsyncMock(return_value=rcon_ok)
            ):
                return await reservations.change_level(
                    301,
                    reservations.ChangeLevelRequest(map_name=map_name),
                    user=USER,
                    db=db,
                )

    async def test_download_url_only_for_listed_maps(self):
        async with self.sessions() as db:
            self.assertIsNone(await get_map_download_url("ctf_2fort", db))
            self.assertEqual(
                "https://fastdl.example/tf/maps/koth_product_final.bsp",
                await get_map_download_url("koth_product_final", db),
            )

    async def test_status_reports_map_and_config_changed_in_game(self):
        async with self.sessions() as db:
            reservation = await db.get(Reservation, 301)
            reservation.current_map = "koth_product_final"
            reservation.config_file = "rgl_6s_koth"
            await db.commit()

        response = await self.get_status()

        self.assertEqual("koth_product_final", response.current_map)
        self.assertEqual("rgl_6s_koth", response.config_file)
        self.assertEqual(
            "https://fastdl.example/tf/maps/koth_product_final.bsp",
            response.map_download_url,
        )

    async def test_status_skips_download_lookup_when_not_active(self):
        async with self.sessions() as db:
            reservation = await db.get(Reservation, 301)
            reservation.status = ReservationStatus.PROVISIONING
            reservation.current_map = "koth_product_final"
            await db.commit()

        response = await self.get_status()

        self.assertIsNone(response.map_download_url)

    async def test_changelevel_shows_the_new_map_before_the_plugin_reports_it(self):
        await self.change_level("koth_product_final")

        response = await self.get_status()
        self.assertEqual("koth_product_final", response.current_map)
        self.assertIsNotNone(response.map_download_url)

    async def test_failed_changelevel_keeps_the_current_map(self):
        with self.assertRaises(HTTPException) as failed:
            await self.change_level("koth_product_final", rcon_ok=False)
        self.assertEqual(503, failed.exception.status_code)

        response = await self.get_status()
        self.assertEqual("cp_process_final", response.current_map)


class StatusPagePollingTests(unittest.TestCase):
    def test_poll_applies_map_config_and_download_link(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")
        fetch_status = template[
            template.index("async fetchStatus()"):
            template.index("stopPolling()")
        ]

        self.assertIn("this.currentMap = data.current_map || data.first_map;", fetch_status)
        self.assertIn("this.mapDownloadUrl = data.map_download_url || null;", fetch_status)
        self.assertIn("this.initialConfig = data.config_file || '';", fetch_status)
        self.assertIn('x-show="mapDownloadUrl"', template)
        self.assertIn(':href="mapDownloadUrl"', template)


if __name__ == "__main__":
    unittest.main()

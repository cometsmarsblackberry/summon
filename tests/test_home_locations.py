import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Reservation, ReservationStatus
from app.models.user import User
from app.routers import pages
from app.services.reservation import get_user_recent_locations


class RecentLocationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "recent-locations.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def test_lists_each_location_once_most_recent_first(self):
        start = datetime(2026, 10, 1)
        async with self.sessions() as db:
            user = User(id=1, steam_id="76561198000000001", display_name="Player")
            other = User(id=2, steam_id="76561198000000002", display_name="Other")
            db.add_all([user, other])
            await db.commit()
            history = [
                (1, "vultr-fra", ReservationStatus.ENDED),
                (1, "vultr-ams", ReservationStatus.ENDED),
                (1, "vultr-sea", ReservationStatus.FAILED),
                (1, "vultr-fra", ReservationStatus.ENDED),
                (2, "vultr-syd", ReservationStatus.ENDED),
            ]
            for number, (user_id, location, status) in enumerate(history, start=1):
                created = start + timedelta(days=number)
                db.add(Reservation(
                    reservation_number=number, user_id=user_id, location=location,
                    starts_at=created, ends_at=created + timedelta(hours=2),
                    created_at=created, status=status,
                    password="pw", rcon_password="rcon", tv_password="tv",
                    motd_token=f"motd-{number}", logsecret="log",
                ))
            await db.commit()

            recent = await get_user_recent_locations(user, db)

        self.assertEqual(["vultr-fra", "vultr-sea", "vultr-ams"], recent)


class HomeRecentLocationsTests(unittest.IsolatedAsyncioTestCase):
    async def render_home(self, user, recent):
        locations = [
            SimpleNamespace(code=code, city=code, provider="vultr",
                            provider_region=None, ping_url=None)
            for code in ("fra", "ams", "sea", "lax")
        ]
        recent_lookup = AsyncMock(return_value=recent)
        with (
            patch.object(pages, "get_current_user", AsyncMock(return_value=user)),
            patch.object(pages, "get_enabled_locations", AsyncMock(return_value=locations)),
            patch.object(pages, "has_beta_access", return_value=True),
            patch.object(pages, "get_user_active_reservation", AsyncMock(return_value=None)),
            patch.object(pages, "get_user_recent_locations", recent_lookup),
            patch.object(pages, "get_status_snapshot", AsyncMock(return_value={})),
            patch("app.services.maps.get_selectable_maps", AsyncMock(return_value=[])),
            patch("app.services.settings.get_reservation_settings", AsyncMock(return_value={})),
            patch.object(pages.templates, "TemplateResponse", return_value="home") as render,
        ):
            await pages.home(request=SimpleNamespace(), db=object())
        return render.call_args.args[2], recent_lookup

    async def test_recent_locations_skip_disabled_and_are_capped(self):
        user = SimpleNamespace(id=7)
        context, _ = await self.render_home(
            user, ["gone", "sea", "fra", "lax", "ams"],
        )

        self.assertEqual(["sea", "fra", "lax"], context["recent_locations"])

    async def test_anonymous_visitors_get_no_recent_locations(self):
        context, recent_lookup = await self.render_home(None, ["fra"])

        self.assertEqual([], context["recent_locations"])
        recent_lookup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

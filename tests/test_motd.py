import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.main  # noqa: F401 - registers template globals
from app.database import Base
from app.models.instance import EnabledLocation, Provider
from app.models.reservation import Reservation, ReservationStatus
from app.models.user import User
from app.routers import internal, motd
from app.routers.internal import PlayerUpdateRequest


class MotdPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime(2026, 10, 19, 18, 0)
        async with self.session_maker() as db:
            db.add(Provider(code="vultr", name="Vultr"))
            await db.flush()
            db.add(EnabledLocation(
                code="hel", name="Helsinki", city="Helsinki",
                provider="vultr", provider_region="hel",
            ))
            owner = User(steam_id="76561198000000001", display_name="Scrim Owner")
            db.add(owner)
            await db.flush()
            db.add(Reservation(
                reservation_number=212, user_id=owner.id, location="hel",
                starts_at=now, ends_at=now + timedelta(hours=2),
                password="join-secret", rcon_password="rcon", tv_password="stv-secret",
                sdr_ip="169.254.1.2", sdr_port=41234, sdr_tv_port=41235,
                direct_ip="203.0.113.9", direct_port=27015, direct_tv_port=27020,
                first_map="cp_badlands", current_map="cp_badlands",
                config_file="etf2l_6v6_5cp", motd_token="motd-212",
                logsecret="log", plugin_api_key="plugin-212",
                status=ReservationStatus.ACTIVE,
            ))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def render(self) -> str:
        request = Request({
            "type": "http", "method": "GET", "path": "/motd/motd-212",
            "headers": [], "query_string": b"",
        })
        async with self.session_maker() as db:
            response = await motd.motd_page(request, "motd-212", db)
        self.assertEqual(200, response.status_code)
        return response.body.decode()

    async def report(self, **body):
        with patch.object(internal, "async_session_maker", self.session_maker):
            await internal.update_players(
                212,
                PlayerUpdateRequest(player_count=1, **body),
                x_api_key="plugin-212",
            )

    async def test_shows_owner_map_and_config(self):
        html = await self.render()

        self.assertIn("Reserved by", html)
        self.assertIn("Scrim Owner", html)
        self.assertIn("cp_badlands", html)
        self.assertIn("etf2l_6v6_5cp", html)

    async def test_connection_details_are_left_out(self):
        # SourceTV demos record the MOTD URL and are public mid-reservation.
        html = await self.render()

        for detail in ("join-secret", "stv-secret", "169.254.1.2", "203.0.113.9", "connect "):
            self.assertNotIn(detail, html)

    async def test_owner_is_left_out_after_account_deletion(self):
        async with self.session_maker() as db:
            reservation = await db.get(Reservation, 1)
            reservation.user_id = None
            await db.commit()

        html = await self.render()

        self.assertNotIn("Reserved by", html)
        self.assertIn("etf2l_6v6_5cp", html)

    async def test_map_and_config_changed_in_game_are_shown(self):
        await self.report(map="cp_process_f12", config_file="rgl_6s_5cp_match_half1")

        html = await self.render()

        self.assertIn("cp_process_f12", html)
        self.assertIn("rgl_6s_5cp_match_half1", html)
        self.assertNotIn("cp_badlands", html)
        self.assertNotIn("etf2l_6v6_5cp", html)

    async def test_empty_config_clears_the_loaded_config(self):
        await self.report(map="cp_badlands", config_file="")

        async with self.session_maker() as db:
            reservation = await db.get(Reservation, 1)
        self.assertIsNone(reservation.config_file)

    async def test_older_plugins_leave_map_and_config_unchanged(self):
        await self.report()

        async with self.session_maker() as db:
            reservation = await db.get(Reservation, 1)
        self.assertEqual("cp_badlands", reservation.current_map)
        self.assertEqual("etf2l_6v6_5cp", reservation.config_file)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401 - registers template globals
from app.database import Base
from app.models import CloudInstance, EnabledLocation, Provider, Reservation, ReservationStatus
from app.routers import internal, motd
from app.routers.reservations import reservation_to_response
from app.services.cloud_provider import CloudInstanceData

CLOUD_ID = "6f0c2b1e-0d6a-4a57-9a3e-1f5f4a2c9b10"
AGENT_ID = "tf2-212-0a1b2c3d"
PUBLIC_IP = "203.0.113.9"


class MotdConnectCommandTests(unittest.TestCase):
    def render(self, connection):
        return motd.templates.get_template("motd.html").render(
            request=SimpleNamespace(state=SimpleNamespace(locale="en")),
            reservation=SimpleNamespace(
                reservation_number=212,
                status=SimpleNamespace(value="active"),
                ends_at=datetime(2026, 10, 19, 20, 0),
            ),
            connection=connection,
            trivia=None,
            location_display="Frankfurt",
        )

    def test_default_game_port_is_left_out(self):
        html = self.render({
            "sdr_ip": "169.254.1.2", "sdr_port": 41234, "sdr_tv_port": 41235,
            "password": "pw", "tv_password": "tv",
            "direct_ip": PUBLIC_IP, "direct_port": 27015, "direct_tv_port": 27020,
            "ip_address": PUBLIC_IP,
        })

        self.assertIn(f'data-command="connect {PUBLIC_IP}; password pw"', html)
        self.assertIn(f'data-command="connect {PUBLIC_IP}:27020; password tv"', html)
        self.assertIn('data-command="connect 169.254.1.2:41234; password pw"', html)
        self.assertNotIn(":27015", html)

    def test_other_game_ports_are_kept(self):
        html = self.render({
            "sdr_ip": None, "password": "pw", "tv_password": "tv",
            "direct_ip": PUBLIC_IP, "direct_port": 27115, "direct_tv_port": 27120,
            "ip_address": PUBLIC_IP,
        })

        self.assertIn(f'data-command="connect {PUBLIC_IP}:27115; password pw"', html)


class StatusPageConnectCommandTests(unittest.TestCase):
    def test_commands_share_the_port_aware_helper(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Number(port) === 27015 ? ip : ip + ':' + port", template)
        self.assertNotIn("'connect ' + connection.", template)
        self.assertEqual(8, template.count("connectCommand(connection."))

    def test_direct_details_show_while_provisioning(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "status === 'active' || (status === 'provisioning' && connection && connection.direct_ip)",
            template,
        )
        self.assertIn("_('status.direct_connect_starting')", template)


class BootingPublicIpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "direct-connect.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.utcnow()
        async with self.sessions() as db:
            db.add(Provider(code="vultr", name="Vultr", billing_model="hourly"))
            await db.commit()
            db.add(EnabledLocation(code="vultr-fra", name="Frankfurt", city="Frankfurt",
                                   provider="vultr", provider_region="fra"))
            db.add(Reservation(
                id=212, reservation_number=212, location="vultr-fra",
                starts_at=now, ends_at=now + timedelta(hours=2),
                password="pw", rcon_password="rcon", tv_password="tv",
                motd_token="motd-212", logsecret="log",
                status=ReservationStatus.PROVISIONING,
                enable_direct_connect=True, instance_id=CLOUD_ID,
            ))
            await db.commit()
            db.add(CloudInstance(
                id=CLOUD_ID, instance_id=AGENT_ID, location="vultr-fra",
                provider_code="vultr", provider_region="fra", auth_token="token",
                status="pending", current_reservation_id=212, is_available=False,
            ))
            await db.commit()

        self.client = SimpleNamespace(get_instance=AsyncMock(return_value=CloudInstanceData(
            id=CLOUD_ID, region="fra", plan="vhf-2c-4gb", main_ip=PUBLIC_IP,
            status="active", power_status="running", date_created="",
        )))

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def record(self):
        with (patch.object(internal, "async_session_maker", self.sessions),
              patch("app.services.cloud_provider.get_cloud_client", return_value=self.client)):
            await internal._record_booting_public_ip(AGENT_ID)

    async def test_ip_is_stored_and_shown_before_the_server_is_ready(self):
        await self.record()

        async with self.sessions() as db:
            instance = await db.get(CloudInstance, CLOUD_ID)
            reservation = await db.get(Reservation, 212)
            self.assertEqual(PUBLIC_IP, instance.ip_address)
            response = reservation_to_response(
                reservation, include_secrets=True, cloud_instance=instance,
            )
        self.assertEqual("provisioning", response.status)
        self.assertEqual(PUBLIC_IP, response.direct_ip)
        self.assertEqual(27015, response.direct_port)

    async def test_skipped_without_direct_connect(self):
        async with self.sessions() as db:
            (await db.get(Reservation, 212)).enable_direct_connect = False
            await db.commit()

        await self.record()

        self.client.get_instance.assert_not_awaited()
        async with self.sessions() as db:
            self.assertIsNone((await db.get(CloudInstance, CLOUD_ID)).ip_address)


if __name__ == "__main__":
    unittest.main()

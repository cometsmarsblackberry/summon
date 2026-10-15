import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import CloudInstance, EnabledLocation, Provider, Reservation, ReservationStatus
from app.models.user import User
from app.routers import internal
from app.services import orchestrator
from app.services.runtime import end_reservation_runtime

CLOUD_ID = "3dc390dc-fcb3-4d26-8c86-be082efe1a65"
OLD_AGENT_ID = "tf2-135-6ad0fccc"


def pulling(progress, message=None):
    return {
        "type": "boot_progress",
        "stage": "pulling_container",
        "progress": progress,
        "message": message or f"Downloading layers ({progress} MB / 900 MB)...",
    }


class BillingBoundaryTests(unittest.TestCase):
    def test_boundary_is_the_end_of_the_running_billing_hour(self):
        created = datetime(2026, 10, 15, 16, 18, 21)
        utc = timezone.utc
        self.assertEqual(
            datetime(2026, 10, 15, 17, 18, 21, tzinfo=utc),
            orchestrator.next_billing_boundary(created, datetime(2026, 10, 15, 16, 22, 57, tzinfo=utc)),
        )
        self.assertEqual(
            datetime(2026, 10, 15, 18, 18, 21, tzinfo=utc),
            orchestrator.next_billing_boundary(created, datetime(2026, 10, 15, 17, 30, tzinfo=utc)),
        )
        # Provider clocks may put creation slightly after now; it is still the first hour.
        self.assertEqual(
            datetime(2026, 10, 15, 17, 18, 21, tzinfo=utc),
            orchestrator.next_billing_boundary(created, datetime(2026, 10, 15, 16, 18, 11, tzinfo=utc)),
        )


class BootHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.tempdir.name) / 'handover.db'}"
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.utcnow()
        self.created_at = (now - timedelta(minutes=12)).replace(microsecond=0)
        async with self.sessions() as db:
            db.add(Provider(code="vultr", name="Vultr", billing_model="hourly", container_image="image"))
            await db.commit()
            db.add(EnabledLocation(code="vultr-fra", name="Frankfurt", city="Frankfurt",
                                   provider="vultr", provider_region="fra"))
            db.add(User(steam_id="76561197960287930", display_name="Admin", is_admin=True))
            await db.commit()
            for number, status in ((135, ReservationStatus.ENDED), (138, ReservationStatus.PROVISIONING)):
                db.add(Reservation(
                    id=number, reservation_number=number, location="vultr-fra",
                    starts_at=now, ends_at=now + timedelta(hours=4),
                    password=f"password-{number}", rcon_password=f"rcon-{number}",
                    tv_password="tv", motd_token=f"motd-{number}", logsecret="log",
                    status=status,
                    instance_id=CLOUD_ID if number == 135 else None,
                    ended_at=now if status == ReservationStatus.ENDED else None,
                ))
            await db.commit()
            db.add(CloudInstance(
                id=CLOUD_ID, instance_id=OLD_AGENT_ID, location="vultr-fra",
                provider_code="vultr", provider_region="fra", auth_token="old-token",
                status="active", current_reservation_id=135, is_available=False,
                billing_hour_ends_at=now + timedelta(hours=4), created_at=self.created_at,
            ))
            await db.commit()

        self.saved_state = {
            name: dict(getattr(internal, name))
            for name in ("connected_agents", "agent_instance_ids", "agent_features",
                         "boot_progress", "boot_activity")
        }
        for name in self.saved_state:
            getattr(internal, name).clear()
        self.agent = SimpleNamespace(send_json=AsyncMock())
        self.connect_agent(OLD_AGENT_ID, frozenset({internal.BOOT_HANDOVER_FEATURE}))

    async def asyncTearDown(self):
        for name, saved in self.saved_state.items():
            getattr(internal, name).clear()
            getattr(internal, name).update(saved)
        await self.engine.dispose()
        self.tempdir.cleanup()

    def connect_agent(self, instance_id, features):
        internal.connected_agents[instance_id] = self.agent
        internal.agent_instance_ids[id(self.agent)] = instance_id
        internal.agent_features[id(self.agent)] = features

    async def booting_instance(self):
        async with self.sessions() as db:
            return await orchestrator.get_booting_instance("vultr-fra", db, provider_code="vultr")

    async def provision_138(self, client):
        providers = [SimpleNamespace(provider_code="vultr", provider_region="fra", instance_plan=None)]
        with (patch.object(orchestrator, "get_cloud_client", return_value=client),
              patch.object(orchestrator, "get_providers_for_location", AsyncMock(return_value=providers)),
              patch.object(orchestrator, "is_provider_suspended", return_value=False)):
            async with self.sessions() as db:
                return await orchestrator.provision_instance_for_reservation(
                    await db.get(Reservation, 138), db,
                )

    async def test_cancel_during_boot_bills_only_to_the_next_hour(self):
        async with self.sessions() as db:
            reservation = await db.get(Reservation, 135)
            await end_reservation_runtime(reservation, db, was_active=False, had_started=False)
            instance = await db.get(CloudInstance, CLOUD_ID)
            self.assertEqual(self.created_at + timedelta(hours=1), instance.billing_hour_ends_at)
            self.assertFalse(instance.is_available)
        self.agent.send_json.assert_not_awaited()

    async def test_billing_cap_leaves_a_handed_over_server_alone(self):
        async with self.sessions() as db:
            instance = await db.get(CloudInstance, CLOUD_ID)
            instance.current_reservation_id = 138
            billing_end = instance.billing_hour_ends_at
            await db.commit()
            await orchestrator.end_cancelled_boot_billing(await db.get(Reservation, 135), db)
            self.assertEqual(billing_end, (await db.get(CloudInstance, CLOUD_ID)).billing_hour_ends_at)

    async def test_progressing_pull_of_cancelled_reservation_can_be_handed_over(self):
        self.assertIsNone(await self.booting_instance())  # no progress seen yet
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        self.assertEqual(CLOUD_ID, (await self.booting_instance()).id)

    async def test_servers_that_cannot_be_handed_over(self):
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        self.assertIsNotNone(await self.booting_instance())

        with self.subTest("agent without boot handover"):
            internal.agent_features[id(self.agent)] = frozenset()
            self.assertIsNone(await self.booting_instance())
            internal.agent_features[id(self.agent)] = frozenset({internal.BOOT_HANDOVER_FEATURE})

        with self.subTest("stalled pull"):
            internal.boot_activity[OLD_AGENT_ID]["changed_at"] -= timedelta(minutes=2)
            self.assertIsNone(await self.booting_instance())
            await internal.handle_agent_message(OLD_AGENT_ID, pulling(41))

        with self.subTest("container already starting"):
            await internal.handle_agent_message(OLD_AGENT_ID, {
                "type": "boot_progress", "stage": "starting_container", "progress": 80,
                "message": "Starting TF2 server...",
            })
            self.assertIsNone(await self.booting_instance())
            await internal.handle_agent_message(OLD_AGENT_ID, pulling(42))

        with self.subTest("billing hour about to end"):
            async with self.sessions() as db:
                instance = await db.get(CloudInstance, CLOUD_ID)
                instance.billing_hour_ends_at = datetime.utcnow() + timedelta(minutes=3)
                await db.commit()
            self.assertIsNone(await self.booting_instance())
            async with self.sessions() as db:
                instance = await db.get(CloudInstance, CLOUD_ID)
                instance.billing_hour_ends_at = datetime.utcnow() + timedelta(minutes=30)
                await db.commit()

        with self.subTest("reservation had started"):
            async with self.sessions() as db:
                reservation = await db.get(Reservation, 135)
                reservation.started_at = datetime.utcnow()
                await db.commit()
            self.assertIsNone(await self.booting_instance())
            async with self.sessions() as db:
                reservation = await db.get(Reservation, 135)
                reservation.started_at = None
                await db.commit()

        with self.subTest("agent disconnected"):
            internal.connected_agents.pop(OLD_AGENT_ID)
            self.assertIsNone(await self.booting_instance())
            internal.connected_agents[OLD_AGENT_ID] = self.agent

        self.assertIsNotNone(await self.booting_instance())

    async def test_heartbeats_are_not_progress_and_pull_retries_disqualify(self):
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        changed_at = internal.boot_activity[OLD_AGENT_ID]["changed_at"]
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        self.assertEqual(changed_at, internal.boot_activity[OLD_AGENT_ID]["changed_at"])
        self.assertNotIn("changed_at", internal.boot_progress[OLD_AGENT_ID])

        await internal.handle_agent_message(
            OLD_AGENT_ID, pulling(5, "Retrying image download (attempt 2)..."),
        )
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(30))
        self.assertTrue(internal.boot_activity[OLD_AGENT_ID]["pull_retried"])
        self.assertIsNone(await self.booting_instance())

    async def test_new_reservation_takes_over_the_booting_server(self):
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        client = SimpleNamespace(create_instance=AsyncMock())

        result = await self.provision_138(client)

        self.assertEqual(CLOUD_ID, result.id)
        client.create_instance.assert_not_awaited()
        async with self.sessions() as db:
            instance = await db.get(CloudInstance, CLOUD_ID)
            reservation = await db.get(Reservation, 138)
            self.assertEqual(138, instance.current_reservation_id)
            self.assertEqual(CLOUD_ID, reservation.instance_id)
            self.assertTrue(instance.instance_id.startswith("tf2-138-"))
            self.assertNotEqual("old-token", instance.auth_token)
            self.assertEqual(reservation.ends_at + timedelta(minutes=5), instance.billing_hour_ends_at)

        message = self.agent.send_json.await_args.args[0]
        self.assertEqual("container.reconfigure", message["type"])
        config = message["config"]
        self.assertEqual(138, config["reservation_number"])
        self.assertEqual("password-138", config["password"])
        self.assertEqual(instance.instance_id, config["instance_id"])
        self.assertEqual(instance.auth_token, config["auth_token"])
        self.assertTrue(config["motd_url"].endswith("/motd/motd-138"))
        self.assertEqual(1, len(config["admin_steam_ids"]))

        # Progress follows the new reservation so its boot bar continues.
        self.assertIs(self.agent, internal.connected_agents[instance.instance_id])
        self.assertNotIn(OLD_AGENT_ID, internal.connected_agents)
        self.assertEqual(40, internal.get_boot_progress(instance.instance_id)["progress"])

    async def test_ready_report_after_handover_does_not_release_the_server(self):
        lock = orchestrator.cloud_instance_lock(CLOUD_ID)
        await lock.acquire()
        with patch.object(internal, "async_session_maker", self.sessions):
            ready = asyncio.create_task(internal.handle_server_ready(
                OLD_AGENT_ID, {"sdr_ip": "169.254.1.1", "sdr_port": 27015},
            ))
            for _ in range(200):
                if lock._waiters:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(lock._waiters, "readiness handling did not wait for the handover")

            # A handover commits while the readiness report waits for the lock.
            async with self.sessions() as db:
                instance = await db.get(CloudInstance, CLOUD_ID)
                instance.current_reservation_id = 138
                await db.commit()
            lock.release()
            await ready

        async with self.sessions() as db:
            instance = await db.get(CloudInstance, CLOUD_ID)
            self.assertFalse(instance.is_available)
            self.assertEqual(138, instance.current_reservation_id)
        self.agent.send_json.assert_not_awaited()

    async def test_boot_failure_after_handover_retries_the_new_reservation(self):
        await internal.handle_agent_message(OLD_AGENT_ID, pulling(40))
        instance = await self.provision_138(SimpleNamespace(create_instance=AsyncMock()))
        client = SimpleNamespace(destroy_instance=AsyncMock())

        with (patch.object(internal, "async_session_maker", self.sessions),
              patch("app.services.cloud_provider.get_cloud_client", return_value=client),
              patch.object(internal, "retry_provision_after_boot_failure", AsyncMock()) as retry):
            await internal.handle_boot_failed(instance.instance_id, "podman pull stalled")
            await asyncio.sleep(0)

        client.destroy_instance.assert_awaited_once_with(CLOUD_ID, region="fra")
        retry.assert_awaited_once_with(138)
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Reservation, 138)).instance_id)
            self.assertEqual(ReservationStatus.ENDED, (await db.get(Reservation, 135)).status)


if __name__ == "__main__":
    unittest.main()

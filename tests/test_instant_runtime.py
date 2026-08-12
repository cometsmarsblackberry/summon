import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, _create_and_migrate
from app.models.instance import EnabledLocation, LocationProvider, Provider
from app.models.instant import InstantAssignment, InstantHost, InstantSlot
from app.models.reservation import Reservation, ReservationStatus, RuntimeKind
from app.models.setting import SiteSetting
from app.models.upload_link import UploadLink  # noqa: F401 - relationship registration
from app.services.instant_hosts import (
    InstantHostError,
    configure_slots,
    create_host,
    exchange_enrollment_token,
    generate_slot_ports,
    serialize_host,
    verify_host_secret,
)
from app.services.runtime import (
    _cloud_capacity,
    awaited_rcon_reservation_runtime,
    claim_instant_slot,
    configure_uploads_runtime,
    count_available_instant_slots,
    dispatch_instant_start,
    end_reservation_runtime,
    get_runtime_stats,
    handle_instant_start_failure,
    rcon_reservation_runtime,
    restart_reservation_runtime,
)
from app.routers.internal import (
    _handle_host_status,
    _handle_instant_ready,
    _reconcile_host_inventory,
    connected_instant_hosts,
    handle_instant_host_message,
    pending_instant_rcon,
    send_instant_command,
)
from app.routers.admin import deregister_instant_host, update_instant_host_agent
from app.routers.status import _build_status
from scripts.migrate import _export_instant_hosts, _import_instant_hosts


class InstantRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "instant.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(EnabledLocation(
                code="instant-only", name="Instant only", provider=None,
                provider_region=None, enabled=True,
            ))
            db.add(SiteSetting(key="instant_hosts_enabled", value="true"))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    @staticmethod
    def reservation(number: int) -> Reservation:
        now = datetime.now(timezone.utc)
        return Reservation(
            reservation_number=number,
            location="instant-only",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
            password="password",
            rcon_password="rcon-password",
            tv_password="tv-password",
            first_map="cp_badlands",
            motd_token=f"motd-{number}",
            logsecret=f"log-{number}",
            plugin_api_key=f"plugin-{number}",
            status=ReservationStatus.PENDING,
        )

    async def add_ready_host(self, db, address: str, name: str, *, slots: int = 1):
        host = InstantHost(
            name=name,
            location="instant-only",
            public_ipv4=address,
            enabled=True,
            credential_hash="enrolled",
            health_status="ready",
            last_heartbeat_at=datetime.now(timezone.utc),
            protocol_version=1,
            protocol_min=1,
            protocol_max=1,
            ready_image_digest="sha256:prepared",
            image_status="ready",
        )
        db.add(host)
        await db.flush()
        for index, game, tv in generate_slot_ports(slots):
            db.add(InstantSlot(
                host_id=host.id,
                slot_index=index,
                game_port=game,
                tv_port=tv,
                enabled=True,
            ))
        await db.commit()
        return host

    async def test_enrollment_is_one_use_and_serialization_excludes_secrets(self):
        async with self.sessions() as db:
            host, token = await create_host(
                db,
                location="instant-only",
                slot_count=2,
            )
            self.assertIsNone(host.public_ipv4)
            self.assertEqual(f"instant-only-instant-{host.id}", host.name)

            with self.assertRaisesRegex(InstantHostError, "globally routable"):
                await exchange_enrollment_token(
                    db, token, public_ipv4="192.168.1.10"
                )
            enrolled, credential = await exchange_enrollment_token(
                db,
                token,
                public_ipv4="8.8.4.10",
                hostname="helsinki edge/01",
            )
            self.assertEqual("8.8.4.10", enrolled.public_ipv4)
            self.assertEqual("instant-only-helsinki-edge-01", enrolled.name)
            self.assertTrue(verify_host_secret(credential, enrolled.credential_hash))
            host_id = enrolled.id
            with self.assertRaises(InstantHostError):
                await exchange_enrollment_token(db, token)
            enrolled = await db.get(InstantHost, host_id)

            rows = list((await db.execute(
                select(InstantSlot)
                .where(InstantSlot.host_id == host_id)
                .order_by(InstantSlot.slot_index)
            )).scalars().all())
            observed_at = datetime(2026, 8, 12, tzinfo=timezone.utc)
            enrolled.last_heartbeat_at = observed_at
            rows[0].quarantined_at = observed_at
            rows[1].last_used_at = observed_at
            payload = serialize_host(enrolled, slots=rows)
            json.dumps(payload)
            flattened = repr(payload).lower()
            self.assertNotIn("credential_hash", flattened)
            self.assertNotIn("enrollment_token", flattened)
            self.assertNotIn(credential, flattened)
            self.assertEqual("2026-08-12T00:00:00+00:00", payload["last_heartbeat_at"])
            self.assertEqual("2026-08-12T00:00:00+00:00", payload["slots"][0]["quarantined_at"])
            self.assertEqual("2026-08-12T00:00:00+00:00", payload["slots"][1]["last_used_at"])
            self.assertEqual([(27015, 27020), (27025, 27030)], [
                (item["game_port"], item["tv_port"]) for item in payload["slots"]
            ])

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            exported = _export_instant_hosts(connection)
        finally:
            connection.close()
        flattened_export = repr(exported).lower()
        self.assertEqual(1, len(exported))
        self.assertNotIn("credential", flattened_export)
        self.assertNotIn("enrollment", flattened_export)
        self.assertNotIn("assignment", flattened_export)

    async def test_duplicate_observed_ipv4_does_not_consume_enrollment_token(self):
        async with self.sessions() as db:
            _, first_token = await create_host(
                db, location="instant-only", slot_count=1
            )
            second, second_token = await create_host(
                db, location="instant-only", slot_count=1
            )
            await exchange_enrollment_token(
                db, first_token, public_ipv4="1.1.1.1"
            )

            with self.assertRaisesRegex(InstantHostError, "already uses"):
                await exchange_enrollment_token(
                    db, second_token, public_ipv4="1.1.1.1"
                )

            enrolled, _ = await exchange_enrollment_token(
                db, second_token, public_ipv4="8.8.8.8"
            )
            self.assertEqual(second.id, enrolled.id)
            self.assertEqual("8.8.8.8", enrolled.public_ipv4)

    async def test_generated_port_range_rejects_cross_slot_overlap_and_overflow(self):
        with self.assertRaises(InstantHostError):
            generate_slot_ports(2, 27015, stride=5, tv_offset=5)
        with self.assertRaises(InstantHostError):
            generate_slot_ports(2, 65525)

    async def test_admin_can_deregister_idle_instant_host(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(
                db, "203.0.113.26", "retired-host", slots=2
            )

            response = await deregister_instant_host(host.id, user=None, db=db)

            self.assertEqual({"id": host.id, "deleted": True}, response)
            self.assertFalse(host.enabled)
            self.assertTrue(host.draining)
            self.assertIsNotNone(host.deleted_at)
            self.assertIsNone(host.credential_hash)
            self.assertEqual("deleted", host.health_status)

    async def test_repeated_agent_update_preserves_active_phase_and_auto_drain(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(
                db, "203.0.113.30", "updating-host",
            )
            host.draining = True
            host.update_auto_drained = True
            host.update_status = "waiting_for_idle"
            await db.commit()

            with patch(
                "app.routers.internal.send_instant_command",
                new_callable=AsyncMock,
            ) as send_command:
                response = await update_instant_host_agent(
                    host.id, user=None, db=db,
                )

            send_command.assert_not_awaited()
            self.assertEqual("waiting_for_idle", response["update_status"])
            self.assertTrue(response["draining"])
            self.assertTrue(host.update_auto_drained)

    async def test_agent_update_only_claims_drain_when_host_was_not_drained(self):
        async with self.sessions() as db:
            automatic = await self.add_ready_host(
                db, "203.0.113.31", "automatic-update-host",
            )
            manual = await self.add_ready_host(
                db, "203.0.113.32", "manual-drain-host",
            )
            manual.draining = True
            manual.update_status = "failed"
            await db.commit()

            with patch(
                "app.routers.internal.send_instant_command",
                new_callable=AsyncMock,
                return_value=True,
            ) as send_command:
                automatic_response = await update_instant_host_agent(
                    automatic.id, user=None, db=db,
                )
                manual_response = await update_instant_host_agent(
                    manual.id, user=None, db=db,
                )

            self.assertEqual(2, send_command.await_count)
            self.assertEqual("queued", automatic_response["update_status"])
            self.assertEqual("queued", manual_response["update_status"])
            self.assertTrue(automatic.update_auto_drained)
            self.assertFalse(manual.update_auto_drained)

    async def test_drained_capacity_shift_is_atomic_despite_old_unique_ports(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(
                db, "203.0.113.27", "port-shift", slots=2
            )
            host.draining = True
            await db.commit()
            await configure_slots(db, host, slot_count=2, base_port=27025)
            slots = list((await db.execute(
                select(InstantSlot)
                .where(InstantSlot.host_id == host.id)
                .order_by(InstantSlot.slot_index)
            )).scalars().all())

        self.assertEqual(
            [(27025, 27030), (27035, 27040)],
            [(slot.game_port, slot.tv_port) for slot in slots],
        )

    async def test_public_status_exposes_instant_capacity_without_changing_alias(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.18", "status-host")
            snapshot = await _build_status(db)

        location = snapshot["instant-only"]
        self.assertEqual(1, location["instant_slots_available"])
        self.assertTrue(location["instant_available"])
        self.assertFalse(location["cloud_available"])
        self.assertTrue(location["reservable"])
        self.assertFalse(location["warm_cloud_available"])
        self.assertFalse(location["instant"])

    async def test_image_preparation_temporarily_removes_host_from_scheduling(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.30", "refreshing-host")
            reservation = self.reservation(180)
            db.add(reservation)
            host.image_status = "preparing"
            await db.commit()

            self.assertEqual(
                0, await count_available_instant_slots("instant-only", db)
            )
            self.assertIsNone(await claim_instant_slot(reservation, db))
            snapshot = await _build_status(db)
            self.assertEqual(
                0, snapshot["instant-only"]["instant_slots_available"]
            )

            # A failed refresh may continue serving its preserved known-good
            # digest; only an in-progress storage operation is excluded.
            host.image_status = "failed"
            await db.commit()
            self.assertEqual(
                1, await count_available_instant_slots("instant-only", db)
            )
            self.assertIsNotNone(await claim_instant_slot(reservation, db))

    async def test_version_pin_removes_mismatched_host_from_capacity(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.29", "pinned-host")
            host.version_pin = "0.2.0"
            host.agent_version = "0.1.0"
            await db.commit()
            self.assertEqual(
                0, await count_available_instant_slots("instant-only", db)
            )

            host.agent_version = "0.2.0"
            await db.commit()
            self.assertEqual(
                1, await count_available_instant_slots("instant-only", db)
            )

    async def test_agent_health_error_removes_host_until_recovery(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.33", "unhealthy-host")
            reservation = self.reservation(181)
            db.add(reservation)
            host.health_error = "podman ps: signal: killed"
            await db.commit()

            self.assertEqual(
                0, await count_available_instant_slots("instant-only", db)
            )
            self.assertIsNone(await claim_instant_slot(reservation, db))

            host.health_error = None
            await db.commit()
            self.assertEqual(
                1, await count_available_instant_slots("instant-only", db)
            )
            self.assertIsNotNone(await claim_instant_slot(reservation, db))

    async def test_host_websocket_writes_are_serialized_per_host(self):
        class FakeWebSocket:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.messages = []

            async def send_json(self, message):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.messages.append(message)
                self.active -= 1

        websocket = FakeWebSocket()
        connected_instant_hosts[987654] = websocket
        try:
            results = await asyncio.gather(
                send_instant_command(987654, {"sequence": 1}),
                send_instant_command(987654, {"sequence": 2}),
            )
        finally:
            connected_instant_hosts.pop(987654, None)

        self.assertEqual([True, True], results)
        self.assertEqual(1, websocket.max_active)
        self.assertEqual(2, len(websocket.messages))

    async def test_cloud_capacity_recheck_does_not_count_current_reservation(self):
        async with self.sessions() as db:
            db.add(Provider(
                code="test-cloud",
                name="Test cloud",
                billing_model="hourly",
                instance_plan="small",
                container_image="example.invalid/tf2:test",
                instance_limit=1,
                enabled=True,
            ))
            await db.flush()
            db.add(LocationProvider(
                location_code="instant-only",
                provider_code="test-cloud",
                provider_region="region-1",
                priority=0,
                enabled=True,
            ))
            reservation = self.reservation(90)
            reservation.runtime_kind = RuntimeKind.CLOUD
            reservation.status = ReservationStatus.PENDING
            db.add(reservation)
            await db.commit()

            with patch("app.services.runtime.get_cloud_client", return_value=object()):
                without_exclusion, _ = await _cloud_capacity("instant-only", db)
                with_exclusion, _ = await _cloud_capacity(
                    "instant-only", db, exclude_reservation_id=reservation.id
                )

        self.assertEqual(0, without_exclusion)
        self.assertEqual(1, with_exclusion)

    async def test_partial_indexes_prevent_duplicate_open_leases(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.11", "constraint-host", slots=2)
            reservations = [self.reservation(100), self.reservation(101)]
            db.add_all(reservations)
            await db.commit()
            slots = list((await db.execute(
                select(InstantSlot).where(InstantSlot.host_id == host.id)
                .order_by(InstantSlot.slot_index)
            )).scalars().all())
            first_reservation_id = reservations[0].id
            second_reservation_id = reservations[1].id
            first_slot_id = slots[0].id
            second_slot_id = slots[1].id

            db.add(InstantAssignment(
                reservation_id=first_reservation_id,
                slot_id=first_slot_id,
                generation=1,
                command_id="first",
                state="claimed",
            ))
            await db.commit()

            db.add(InstantAssignment(
                reservation_id=second_reservation_id,
                slot_id=first_slot_id,
                generation=1,
                command_id="same-slot",
                state="claimed",
            ))
            with self.assertRaises(IntegrityError):
                await db.commit()
            await db.rollback()

            db.add(InstantAssignment(
                reservation_id=first_reservation_id,
                slot_id=second_slot_id,
                generation=2,
                command_id="same-reservation",
                state="claimed",
            ))
            with self.assertRaises(IntegrityError):
                await db.commit()

    async def test_concurrent_claims_cannot_lease_one_slot_twice(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.17", "atomic-host")
            reservations = [self.reservation(110), self.reservation(111)]
            db.add_all(reservations)
            await db.commit()
            reservation_ids = [item.id for item in reservations]

        async def claim(reservation_id):
            for attempt in range(3):
                async with self.sessions() as session:
                    reservation = await session.get(Reservation, reservation_id)
                    try:
                        return await claim_instant_slot(reservation, session)
                    except OperationalError:
                        await session.rollback()
                await asyncio.sleep(0.02 * (attempt + 1))
            return None

        results = await asyncio.gather(*(claim(item) for item in reservation_ids))
        async with self.sessions() as db:
            open_count = (await db.execute(
                select(func.count(InstantAssignment.id))
                .where(InstantAssignment.closed_at.is_(None))
            )).scalar_one()
            instant_count = (await db.execute(
                select(func.count(Reservation.id)).where(
                    Reservation.runtime_kind == RuntimeKind.INSTANT
                )
            )).scalar_one()

        self.assertEqual(1, open_count)
        self.assertEqual(1, instant_count)
        self.assertEqual(1, sum(item is not None for item in results))

    async def test_scheduler_prefers_less_loaded_host_and_control_stays_instant(self):
        async with self.sessions() as db:
            first = await self.add_ready_host(db, "203.0.113.12", "first")
            second = await self.add_ready_host(db, "203.0.113.13", "second")
            one, two = self.reservation(200), self.reservation(201)
            db.add_all([one, two])
            await db.commit()

            assignment_one = await claim_instant_slot(one, db)
            self.assertIsNotNone(assignment_one)
            assignment_two = await claim_instant_slot(two, db)
            self.assertIsNotNone(assignment_two)
            self.assertEqual({first.id, second.id}, {
                assignment_one.slot.host_id, assignment_two.slot.host_id,
            })

            two.status = ReservationStatus.ENDED
            with (
                patch("app.routers.internal.send_instant_command", new=AsyncMock(return_value=True)) as send,
                patch("app.services.orchestrator.destroy_instance", new=AsyncMock()) as destroy,
                patch("app.services.orchestrator.release_to_warm_pool", new=AsyncMock()) as release,
            ):
                result = await end_reservation_runtime(two, db)
            self.assertTrue(result)
            send.assert_awaited_once()
            destroy.assert_not_awaited()
            release.assert_not_awaited()
            self.assertEqual("stopping", assignment_two.state)
            self.assertIsNone(assignment_two.closed_at)

    async def test_instant_controls_and_stats_never_enter_cloud_adapter(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.26", "control-host")
            reservation = self.reservation(250)
            reservation.status = ReservationStatus.ACTIVE
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)
            reservation.status = ReservationStatus.ACTIVE
            await db.commit()

            with (
                patch("app.routers.internal.send_instant_command", new=AsyncMock(return_value=True)) as instant_send,
                patch("app.routers.internal.send_container_restart", new=AsyncMock()) as cloud_restart,
                patch("app.routers.internal.send_rcon_command", new=AsyncMock()) as cloud_rcon,
                patch("app.routers.internal.send_upload_settings", new=AsyncMock()) as cloud_uploads,
                patch("app.routers.internal.get_agent_stats") as cloud_stats,
                patch("app.routers.internal.get_instant_container_stats", return_value={"cpu": "1%"}),
            ):
                self.assertTrue(await restart_reservation_runtime(reservation, db, {}))
                self.assertTrue(await rcon_reservation_runtime(reservation, db, "status"))
                self.assertTrue(await configure_uploads_runtime(
                    reservation, db, logs_tf=True, demos_tf=False
                ))
                stats = await get_runtime_stats(reservation, db)

            self.assertEqual(3, instant_send.await_count)
            cloud_restart.assert_not_awaited()
            cloud_rcon.assert_not_awaited()
            cloud_uploads.assert_not_awaited()
            cloud_stats.assert_not_called()
            self.assertTrue(stats["host_shared"])
            self.assertEqual({"cpu": "1%"}, stats["container"])
            self.assertEqual(assignment.slot.host.system_stats, stats["host"])

    async def test_instant_rcon_awaits_only_valid_correlated_result(self):
        class FakeWebSocket:
            def __init__(self):
                self.messages = []

            async def send_json(self, message):
                self.messages.append(message)

        async with self.sessions() as db:
            host = await self.add_ready_host(
                db, "203.0.113.30", "correlated-host"
            )
            reservation = self.reservation(251)
            reservation.status = ReservationStatus.ACTIVE
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)
            reservation.status = ReservationStatus.ACTIVE
            await db.commit()

            websocket = FakeWebSocket()
            connected_instant_hosts[host.id] = websocket
            try:
                task = asyncio.create_task(awaited_rcon_reservation_runtime(
                    reservation, db, "status", timeout=1
                ))
                while not websocket.messages:
                    await asyncio.sleep(0)
                envelope = websocket.messages[0]

                stale = dict(envelope)
                stale.update({
                    "type": "server.rcon.result",
                    "command_id": "stale-command-id",
                    "output": "stale",
                })
                with patch("app.routers.internal.async_session_maker", self.sessions):
                    await handle_instant_host_message(host.id, stale)
                    self.assertFalse(task.done())

                    result_event = dict(envelope)
                    result_event.update({
                        "type": "server.rcon.result",
                        "output": "correlated",
                    })
                    await handle_instant_host_message(host.id, result_event)
                self.assertEqual(
                    {"output": "correlated", "error": None}, await task
                )
                self.assertFalse(pending_instant_rcon)
                self.assertEqual(assignment.id, envelope["assignment_id"])
            finally:
                connected_instant_hosts.pop(host.id, None)
                pending_instant_rcon.clear()

    async def test_retry_uses_different_host_and_terminal_error_does_not_retry(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.14", "retry-one")
            await self.add_ready_host(db, "203.0.113.15", "retry-two")
            reservation = self.reservation(300)
            db.add(reservation)
            await db.commit()
            first = await claim_instant_slot(reservation, db)
            first_host = first.slot.host_id

            with (
                patch("app.services.runtime.dispatch_instant_start", new=AsyncMock(return_value=True)) as dispatch,
                patch("app.services.runtime._fallback_to_cloud", new=AsyncMock()) as cloud,
            ):
                await handle_instant_start_failure(
                    first,
                    reservation,
                    db,
                    failure_class="infrastructure",
                    failure_code="podman_start",
                    failure_message="podman failed",
                )
            dispatch.assert_awaited_once()
            cloud.assert_not_awaited()
            retry = dispatch.await_args.args[1]
            self.assertNotEqual(first_host, retry.slot.host_id)

            await self.add_ready_host(db, "203.0.113.16", "terminal-host")
            terminal = self.reservation(301)
            db.add(terminal)
            await db.commit()
            terminal_assignment = await claim_instant_slot(terminal, db)
            with (
                patch("app.services.runtime.dispatch_instant_start", new=AsyncMock()) as terminal_dispatch,
                patch("app.services.runtime._fallback_to_cloud", new=AsyncMock()) as terminal_cloud,
            ):
                await handle_instant_start_failure(
                    terminal_assignment,
                    terminal,
                    db,
                    failure_class="configuration",
                    failure_code="bad_config",
                    failure_message="invalid reservation data",
                )
            terminal_dispatch.assert_not_awaited()
            terminal_cloud.assert_not_awaited()
            self.assertEqual(ReservationStatus.FAILED, terminal.status)
            self.assertIsNone(terminal_assignment.slot.quarantined_at)
            self.assertIsNone(terminal_assignment.slot.error_code)

    async def test_deferred_cloud_fallback_persists_decision_without_provider_io(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.21", "deferred-fallback")
            reservation = self.reservation(320)
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)

            with (
                patch(
                    "app.services.runtime._prepare_cloud_fallback",
                    new=AsyncMock(return_value=True),
                ) as prepare,
                patch(
                    "app.services.runtime._fallback_to_cloud",
                    new=AsyncMock(),
                ) as provision,
            ):
                deferred = await handle_instant_start_failure(
                    assignment,
                    reservation,
                    db,
                    failure_class="infrastructure",
                    failure_code="container_start",
                    failure_message="failed",
                    defer_cloud=True,
                )

            self.assertTrue(deferred)
            prepare.assert_awaited_once()
            provision.assert_not_awaited()

    async def test_start_dispatch_schedules_backend_expiry_while_provisioning(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.22", "expiry-host")
            reservation = self.reservation(330)
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)

            with (
                patch("app.services.runtime._instant_config", new=AsyncMock(return_value={})),
                patch("app.routers.internal.send_instant_command", new=AsyncMock(return_value=True)),
                patch("app.services.timer.schedule_expiry_timer") as schedule,
            ):
                self.assertTrue(await dispatch_instant_start(reservation, assignment, db))

            schedule.assert_called_once_with(
                reservation.id,
                reservation.reservation_number,
                reservation.ends_at,
                None,
            )

    async def test_backend_restart_restores_provisioning_instant_expiry(self):
        async with self.sessions() as db:
            reservation = self.reservation(340)
            reservation.runtime_kind = RuntimeKind.INSTANT
            reservation.status = ReservationStatus.PROVISIONING
            db.add(reservation)
            await db.commit()

        from app.services.timer import restore_expiry_timers
        with (
            patch("app.services.timer.async_session_maker", self.sessions),
            patch("app.services.timer.schedule_expiry_timer") as schedule,
        ):
            await restore_expiry_timers()

        schedule.assert_called_once()
        scheduled = schedule.call_args.args
        self.assertEqual((reservation.id, reservation.reservation_number), scheduled[:2])
        self.assertEqual(
            reservation.ends_at.replace(tzinfo=None),
            scheduled[2].replace(tzinfo=None),
        )
        self.assertIsNone(scheduled[3])

    async def test_late_ready_does_not_resurrect_ended_reservation(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.19", "late-ready")
            reservation = self.reservation(350)
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)
            reservation.status = ReservationStatus.ENDED
            assignment.state = "stopping"
            await db.commit()

            with patch(
                "app.routers.internal.send_instant_command",
                new=AsyncMock(return_value=True),
            ) as send:
                await _handle_instant_ready(
                    assignment,
                    reservation,
                    {"container_id": "late-container", "map": "cp_badlands"},
                    db,
                )

            self.assertEqual(ReservationStatus.ENDED, reservation.status)
            self.assertEqual("stopping", assignment.state)
            self.assertEqual("late-container", assignment.container_id)
            command = send.await_args.args[1]
            self.assertEqual("server.stop", command["type"])
            self.assertEqual(assignment.id, command["assignment_id"])

    async def test_ready_for_closed_assignment_is_stopped(self):
        async with self.sessions() as db:
            await self.add_ready_host(db, "203.0.113.26", "closed-ready")
            reservation = self.reservation(351)
            db.add(reservation)
            await db.commit()
            assignment = await claim_instant_slot(reservation, db)
            reservation.status = ReservationStatus.ENDED
            assignment.state = "stopped"
            assignment.closed_at = datetime.now(timezone.utc)
            await db.commit()

            with patch(
                "app.routers.internal.send_instant_command",
                new=AsyncMock(return_value=True),
            ) as send:
                await _handle_instant_ready(
                    assignment,
                    reservation,
                    {"container_id": "late-closed-container"},
                    db,
                )

            send.assert_awaited_once()
            command = send.await_args.args[1]
            self.assertEqual("server.stop", command["type"])
            self.assertEqual("closed_assignment", command["reason"])

    async def test_heartbeat_cannot_restore_protocol_incompatible_host(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.20", "protocol-host")
            host_id = host.id

        with patch("app.routers.internal.async_session_maker", self.sessions):
            await _handle_host_status(host_id, {
                "type": "host.hello",
                "agent_version": "test",
                "protocol_version": 1,
                "protocol_min": 1,
                "protocol_max": 1,
                "preflight_ok": True,
                "image": {"status": "ready", "ready_digest": "sha256:prepared"},
            }, hello=True)
            await _handle_host_status(host_id, {
                "type": "host.status",
                "protocol_version": 2,
                "protocol_min": 2,
                "protocol_max": 2,
                "preflight_ok": True,
                "image": {"status": "ready", "ready_digest": "sha256:prepared"},
            }, hello=False)

        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            self.assertEqual("incompatible", host.health_status)
            self.assertEqual(2, host.protocol_min)

    async def test_reconnect_stays_unschedulable_until_hello(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.30", "hello-gated")
            host.health_status = "connecting"
            host.agent_version = "old"
            await db.commit()
            host_id = host.id

        status = {
            "type": "host.status",
            "agent_version": "0.2.0",
            "protocol_version": 1,
            "protocol_min": 1,
            "protocol_max": 1,
            "preflight_ok": True,
            "image": {"status": "ready", "ready_digest": "sha256:prepared"},
        }
        with patch("app.routers.internal.async_session_maker", self.sessions):
            await _handle_host_status(host_id, status, hello=False)

        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            self.assertEqual("connecting", host.health_status)
            self.assertEqual("0.2.0", host.agent_version)
            self.assertEqual(
                0, await count_available_instant_slots("instant-only", db)
            )

        status["type"] = "host.hello"
        with patch("app.routers.internal.async_session_maker", self.sessions):
            await _handle_host_status(host_id, status, hello=True)

        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            self.assertEqual("ready", host.health_status)
            self.assertEqual(
                1, await count_available_instant_slots("instant-only", db)
            )

    async def test_failed_image_heartbeat_keeps_last_known_good_host_degraded(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.23", "image-host")
            host_id = host.id

        with patch("app.routers.internal.async_session_maker", self.sessions):
            await _handle_host_status(host_id, {
                "type": "host.status",
                "protocol_version": 1,
                "protocol_min": 1,
                "protocol_max": 1,
                "preflight_ok": True,
                "image": {
                    "status": "failed",
                    "ready_digest": "sha256:last-known-good",
                    "error": "registry unavailable",
                },
                "slots": [],
            }, hello=False)

        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            self.assertEqual("degraded", host.health_status)
            self.assertEqual("sha256:last-known-good", host.ready_image_digest)

    async def test_reconciliation_conflict_quarantines_host(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.24", "conflict-host")
            slot = (await db.execute(select(InstantSlot).where(
                InstantSlot.host_id == host.id
            ))).scalar_one()
            await _reconcile_host_inventory(host, [{
                "assignment_id": 999,
                "slot_id": slot.id,
                "generation": 1,
                "container_id": "stale-container",
            }], db)
            self.assertEqual("quarantined", host.health_status)
            self.assertIsNotNone(host.reconciliation_error)
            self.assertEqual("reconciliation_conflict", slot.error_code)

    async def test_import_refuses_to_rewrite_host_with_open_assignment(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.25", "live-import")
            reservation = self.reservation(360)
            db.add(reservation)
            await db.commit()
            await claim_instant_slot(reservation, db)
            host_id = host.id

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            result = _import_instant_hosts(connection, [{
                "name": "rewritten",
                "location": "instant-only",
                "public_ipv4": "203.0.113.25",
                "slots": [{
                    "slot_index": 0, "game_port": 28015,
                    "tv_port": 28020, "enabled": 1,
                }],
            }], "update")
            connection.commit()
        finally:
            connection.close()

        self.assertEqual({"created": 0, "updated": 0, "skipped": 1}, result)
        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            self.assertEqual("live-import", host.name)
            self.assertEqual("enrolled", host.credential_hash)

    async def test_import_port_shift_preserves_slot_ids_without_unique_collision(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(
                db, "203.0.113.28", "import-port-shift", slots=2
            )
            host_id = host.id
            original_slots = list((await db.execute(
                select(InstantSlot)
                .where(InstantSlot.host_id == host_id)
                .order_by(InstantSlot.slot_index)
            )).scalars().all())
            original_ids = [slot.id for slot in original_slots]

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            result = _import_instant_hosts(connection, [{
                "name": "import-port-shift",
                "location": "instant-only",
                "public_ipv4": "203.0.113.28",
                "slots": [
                    {
                        "slot_index": 0, "game_port": 27025,
                        "tv_port": 27030, "enabled": 1,
                    },
                    {
                        "slot_index": 1, "game_port": 27035,
                        "tv_port": 27040, "enabled": 1,
                    },
                ],
            }], "update")
            connection.commit()
        finally:
            connection.close()

        self.assertEqual({"created": 0, "updated": 1, "skipped": 0}, result)
        async with self.sessions() as db:
            host = await db.get(InstantHost, host_id)
            slots = list((await db.execute(
                select(InstantSlot)
                .where(InstantSlot.host_id == host_id)
                .order_by(InstantSlot.slot_index)
            )).scalars().all())

        self.assertFalse(host.enabled)
        self.assertIsNone(host.credential_hash)
        self.assertEqual(original_ids, [slot.id for slot in slots])
        self.assertEqual(
            [(27025, 27030), (27035, 27040)],
            [(slot.game_port, slot.tv_port) for slot in slots],
        )


class InstantMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_rows_keep_ids_and_backfill_cloud_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(directory) / 'legacy.db'}"
            )
            async with engine.begin() as connection:
                # A compact representation of the legacy tables needed by the
                # lightweight migration. Other tables are created by metadata.
                await connection.execute(text("""
                    CREATE TABLE providers (
                        code VARCHAR(32) PRIMARY KEY,
                        name VARCHAR(64) NOT NULL,
                        billing_model VARCHAR(32) NOT NULL,
                        instance_plan VARCHAR(32) NOT NULL,
                        container_image VARCHAR(255) NOT NULL,
                        instance_limit INTEGER NOT NULL,
                        enabled BOOLEAN NOT NULL,
                        display_order INTEGER NOT NULL
                    )
                """))
                await connection.execute(text("""
                    CREATE TABLE enabled_locations (
                        code VARCHAR(32) PRIMARY KEY,
                        name VARCHAR(64) NOT NULL,
                        provider VARCHAR(32) NOT NULL REFERENCES providers(code),
                        provider_region VARCHAR(32) NOT NULL,
                        vultr_region VARCHAR(32), billing_model VARCHAR(32),
                        city VARCHAR(64), country VARCHAR(64), continent VARCHAR(32),
                        subdivision VARCHAR(64), recommended BOOLEAN, enabled BOOLEAN,
                        display_order INTEGER, instance_plan VARCHAR(32),
                        region_instance_limit INTEGER
                    )
                """))
                await connection.execute(text("""
                    CREATE TABLE reservations (
                        id INTEGER PRIMARY KEY,
                        reservation_number INTEGER NOT NULL UNIQUE,
                        user_id INTEGER,
                        location VARCHAR(32) NOT NULL,
                        instance_id VARCHAR(64),
                        status VARCHAR(32) NOT NULL,
                        created_at DATETIME NOT NULL,
                        motd_token VARCHAR(64) DEFAULT ''
                    )
                """))
                await connection.execute(text("""
                    CREATE TABLE instant_hosts (
                        id INTEGER PRIMARY KEY,
                        name VARCHAR(64) NOT NULL,
                        location VARCHAR(32) NOT NULL REFERENCES enabled_locations(code),
                        public_ipv4 VARCHAR(15) NOT NULL,
                        enabled BOOLEAN NOT NULL,
                        draining BOOLEAN NOT NULL,
                        health_status VARCHAR(32) NOT NULL,
                        image_status VARCHAR(32) NOT NULL,
                        update_status VARCHAR(32) NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                    )
                """))
                await connection.execute(text(
                    "CREATE UNIQUE INDEX uq_instant_hosts_active_public_ipv4 "
                    "ON instant_hosts(public_ipv4)"
                ))
                await connection.execute(text(
                    "INSERT INTO providers VALUES "
                    "('vultr','Vultr','hourly','plan','image',10,1,0)"
                ))
                await connection.execute(text(
                    "INSERT INTO enabled_locations "
                    "(code,name,provider,provider_region,enabled,display_order) "
                    "VALUES ('legacy','Legacy','vultr','ewr',1,0)"
                ))
                await connection.execute(text(
                    "INSERT INTO reservations "
                    "(id,reservation_number,location,status,created_at,motd_token) "
                    "VALUES (42,99,'legacy','ENDED',CURRENT_TIMESTAMP,'legacy-token')"
                ))
                await connection.execute(text(
                    "INSERT INTO instant_hosts "
                    "(id,name,location,public_ipv4,enabled,draining,health_status,image_status,update_status) "
                    "VALUES (7,'legacy-host','legacy','8.8.4.4',0,0,'offline','ready','idle')"
                ))
                await _create_and_migrate(connection)

                location_columns = {
                    row[1]: row for row in (
                        await connection.execute(text("PRAGMA table_info(enabled_locations)"))
                    ).all()
                }
                row = (await connection.execute(text(
                    "SELECT id,reservation_number,location,runtime_kind "
                    "FROM reservations WHERE id=42"
                ))).one()
                instant_columns = {
                    row[1]: row for row in (
                        await connection.execute(text("PRAGMA table_info(instant_hosts)"))
                    ).all()
                }
                instant_row = (await connection.execute(text(
                    "SELECT id,name,location,public_ipv4 FROM instant_hosts WHERE id=7"
                ))).one()
                violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()

            await engine.dispose()
            self.assertEqual((42, 99, "legacy", "cloud"), tuple(row))
            self.assertEqual(0, location_columns["provider"][3])
            self.assertEqual(0, location_columns["provider_region"][3])
            self.assertIn("ping_url", location_columns)
            self.assertEqual(0, instant_columns["public_ipv4"][3])
            self.assertEqual(
                (7, "legacy-host", "legacy", "8.8.4.4"), tuple(instant_row)
            )
            self.assertEqual([], violations)

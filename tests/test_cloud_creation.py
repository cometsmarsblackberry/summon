import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import CloudCreationAttempt, CloudInstance, EnabledLocation, Provider, Reservation, ReservationStatus
from app.services import orchestrator
from app.services.cloud_creation import reconcile_creation_attempt, record_created_instance
from app.services.cloud_provider import CloudCreationUncertain, CloudInstanceData, CloudProviderError
from app.services.vultr import VultrClient


def provider_data(instance_id="provider-vm", **changes):
    data = dict(id=instance_id, region="mad", plan="vhf-2c-2gb",
                main_ip="192.0.2.1", status="active", power_status="running",
                date_created="2026-09-15T16:58:16Z", label="Summon #88", tags=[])
    data.update(changes)
    return data


class VultrCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_uncertainty_is_distinct_from_rejection(self):
        for error in (httpx.ReadTimeout("lost response"),
                      CloudProviderError("gateway failure", 502),
                      CloudProviderError("request timeout", 408),
                      ValueError("invalid JSON")):
            with self.subTest(error=error):
                client = VultrClient("test")
                client.get_fcos_id = AsyncMock(return_value=123)
                client._request = AsyncMock(side_effect=error)
                with self.assertRaises(CloudCreationUncertain):
                    await client.create_instance("mad", "Summon #88", "ignition",
                                                 creation_key="summon-create-test")
                self.assertEqual(["summon-create-test"],
                                 client._request.await_args.kwargs["json"]["tags"])
                self.assertEqual(1, client._request.await_count)

    async def test_definitive_rejections_and_preflight_errors_remain_retryable(self):
        for status in (400, 401, 429):
            client = VultrClient("test")
            client.get_fcos_id = AsyncMock(return_value=123)
            client._request = AsyncMock(side_effect=CloudProviderError("rejected", status))
            with self.assertRaises(CloudProviderError):
                await client.create_instance("mad", "label", "ignition")
        client = VultrClient("test")
        client.get_fcos_id = AsyncMock(side_effect=httpx.ReadTimeout("OS lookup"))
        client._request = AsyncMock()
        with self.assertRaises(httpx.ReadTimeout):
            await client.create_instance("mad", "label", "ignition")
        client._request.assert_not_awaited()

    async def test_missing_id_is_an_uncertain_create(self):
        client = VultrClient("test")
        client.get_fcos_id = AsyncMock(return_value=123)
        client._request = AsyncMock(return_value={"instance": {}})
        with self.assertRaises(CloudCreationUncertain):
            await client.create_instance("mad", "label", "ignition")

    async def test_inventory_reads_all_pages_and_preserves_identity(self):
        client = VultrClient("test")
        client._request = AsyncMock(side_effect=[
            {"instances": [provider_data("unrelated", label="Other")],
             "meta": {"links": {"next": "cursor+/="}}},
            {"instances": [provider_data(tags=["summon-create-test"])],
             "meta": {"links": {"next": ""}}},
        ])
        instances = await client.list_instances(label_prefix="Summon #")
        self.assertEqual(["provider-vm"], [i.id for i in instances])
        self.assertEqual(("summon-create-test",), instances[0].tags)
        self.assertEqual("Summon #88", instances[0].label)
        self.assertEqual("/instances?per_page=100&cursor=cursor%2B%2F%3D",
                         client._request.await_args.args[1])

    async def test_repeated_cursor_fails_instead_of_returning_partial_inventory(self):
        client = VultrClient("test")
        client._request = AsyncMock(return_value={
            "instances": [], "meta": {"links": {"next": "same"}},
        })
        with self.assertRaises(CloudProviderError):
            await client.list_instances()
        self.assertEqual(2, client._request.await_count)


class DurableCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.tempdir.name) / 'creation.db'}"
        )
        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.utcnow()
        async with self.sessions() as db:
            db.add(Provider(code="vultr", name="Vultr"))
            await db.commit()
            db.add(Reservation(
                id=88, reservation_number=88, location="vultr-mad",
                starts_at=now, ends_at=now + timedelta(hours=4),
                password="password", rcon_password="rcon", tv_password="tv",
                motd_token="motd", logsecret="log",
                status=ReservationStatus.PROVISIONING,
            ))
            db.add(EnabledLocation(code="vultr-mad", name="Madrid",
                                   provider="vultr", provider_region="mad"))
            await db.commit()
        self.client = SimpleNamespace(
            create_instance=AsyncMock(side_effect=CloudCreationUncertain("lost response")),
            list_instances=AsyncMock(return_value=[]),
            destroy_instance=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def create(self, db):
        reservation = await db.get(Reservation, 88)
        with patch.object(orchestrator, "generate_ignition_config", return_value="ignition"):
            return await orchestrator._create_new_instance(
                reservation, self.client,
                SimpleNamespace(provider_code="vultr", provider_region="mad", instance_plan=None),
                SimpleNamespace(instance_plan="vhf-2c-2gb"),
                "original-token", "tf2-88-original", "Madrid", "image", "", "", "", db,
            )

    async def pending(self, db):
        return (await db.execute(select(CloudCreationAttempt))).scalar_one()

    async def make_uncertain(self):
        async with self.sessions() as db:
            with self.assertRaises(CloudCreationUncertain):
                await self.create(db)

    async def test_intent_is_committed_before_post_and_survives_restart(self):
        async def timeout(**kwargs):
            async with self.sessions() as other:
                attempt = await self.pending(other)
                self.assertEqual(kwargs["creation_key"], attempt.id)
                self.assertEqual("original-token", attempt.auth_token)
            raise CloudCreationUncertain("timeout")
        self.client.create_instance.side_effect = timeout
        await self.make_uncertain()
        # A new session and a new client simulate restart; empty inventory must
        # not authorize another POST, even if location/provider selection changes.
        restarted_client = SimpleNamespace(list_instances=AsyncMock(return_value=[]),
                                           create_instance=AsyncMock())
        with patch.object(orchestrator, "get_cloud_client", return_value=restarted_client):
            async with self.sessions() as db:
                reservation = await db.get(Reservation, 88)
                self.assertIsNone(await orchestrator.provision_instance_for_reservation(reservation, db))
                await self.pending(db)
        restarted_client.create_instance.assert_not_awaited()
        self.assertEqual(1, self.client.create_instance.await_count)

    async def test_late_result_reuses_original_agent_credentials(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            cloud = CloudInstanceData(**provider_data(tags=(attempt.id,)))
            self.client.list_instances.return_value = [cloud]
            with patch.object(orchestrator, "get_cloud_client", return_value=self.client):
                instance = await orchestrator.provision_instance_for_reservation(
                    await db.get(Reservation, 88), db,
                )
            self.assertEqual("tf2-88-original", instance.instance_id)
            self.assertEqual("original-token", instance.auth_token)
            self.assertEqual(88, instance.current_reservation_id)
            self.assertFalse(instance.is_available)
            self.assertEqual(datetime(2026, 9, 15, 16, 58, 16), instance.created_at)
            self.assertIsNone(await db.get(CloudCreationAttempt, attempt.id))
            self.assertEqual("provider-vm", (await db.get(Reservation, 88)).instance_id)
        self.assertEqual(1, self.client.create_instance.await_count)

    async def test_definitive_failure_removes_intent(self):
        self.client.create_instance.side_effect = CloudProviderError("quota rejected", 429)
        async with self.sessions() as db:
            with self.assertRaises(CloudProviderError):
                await self.create(db)
            self.assertEqual([], list((await db.execute(select(CloudCreationAttempt))).scalars()))

    async def test_process_cancellation_leaves_durable_intent(self):
        self.client.create_instance.side_effect = asyncio.CancelledError()
        async with self.sessions() as db:
            with self.assertRaises(asyncio.CancelledError):
                await self.create(db)
        async with self.sessions() as db:
            await self.pending(db)

    async def test_database_failure_after_acceptance_preserves_intent(self):
        self.client.create_instance.side_effect = None
        self.client.create_instance.return_value = CloudInstanceData(**provider_data())
        async with self.sessions() as db:
            with (patch.object(orchestrator, "record_created_instance",
                               AsyncMock(side_effect=RuntimeError("database unavailable"))),
                  self.assertRaises(CloudCreationUncertain)):
                await self.create(db)
        async with self.sessions() as db:
            await self.pending(db)
        self.assertEqual(1, self.client.create_instance.await_count)

    async def test_upgrade_adds_attempt_table_without_changing_reservations(self):
        from app import database
        async with self.engine.begin() as conn:
            await conn.execute(text("DROP TABLE cloud_creation_attempts"))
        with patch.object(database, "engine", self.engine):
            await database.create_tables()
        async with self.sessions() as db:
            self.assertIsNotNone(await db.get(Reservation, 88))
            self.assertEqual([], list((await db.execute(select(CloudCreationAttempt))).scalars()))

    async def test_success_atomically_records_instance(self):
        self.client.create_instance.side_effect = None
        self.client.create_instance.return_value = CloudInstanceData(**provider_data())
        async with self.sessions() as db:
            instance = await self.create(db)
            self.assertEqual("vhf-2c-2gb", instance.shape)
            self.assertEqual([], list((await db.execute(select(CloudCreationAttempt))).scalars()))
            self.assertEqual("provider-vm", (await db.get(Reservation, 88)).instance_id)

    async def test_cancellation_before_response_does_not_resurrect_reservation(self):
        async def cancel(**kwargs):
            async with self.sessions() as other:
                reservation = await other.get(Reservation, 88)
                reservation.status = ReservationStatus.ENDED
                await other.commit()
            return CloudInstanceData(**provider_data())
        self.client.create_instance.side_effect = cancel
        async with self.sessions() as db:
            instance = await self.create(db)
            self.assertTrue(instance.is_available)
            self.assertIsNone(instance.current_reservation_id)
            self.assertLessEqual(instance.billing_hour_ends_at, datetime.utcnow())
            self.assertEqual(ReservationStatus.ENDED, (await db.get(Reservation, 88)).status)

    async def test_unresolved_attempt_times_out_without_losing_ownership(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            attempt.created_at = datetime.utcnow() - timedelta(minutes=6)
            await db.commit()
            await reconcile_creation_attempt(attempt, self.client, db)
            self.assertEqual(ReservationStatus.FAILED, (await db.get(Reservation, 88)).status)
            self.assertIsNotNone(await db.get(CloudCreationAttempt, attempt.id))
            # It may still appear later, after the reservation failed.
            self.client.list_instances.return_value = [
                CloudInstanceData(**provider_data(tags=(attempt.id,))),
            ]
            instance = await reconcile_creation_attempt(attempt, self.client, db)
            self.assertTrue(instance.is_available)
            self.assertIsNone(instance.current_reservation_id)
        self.assertEqual(1, self.client.create_instance.await_count)

    async def test_wrong_tag_or_region_cannot_be_adopted(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            self.client.list_instances.return_value = [
                CloudInstanceData(**provider_data(tags=("different",))),
                CloudInstanceData(**provider_data("other-region", region="ams", tags=(attempt.id,))),
            ]
            self.assertIsNone(await reconcile_creation_attempt(attempt, self.client, db))
            self.assertIsNotNone(await db.get(CloudCreationAttempt, attempt.id))

    async def test_list_outage_does_not_authorize_retry(self):
        await self.make_uncertain()
        self.client.list_instances.side_effect = httpx.ReadTimeout("list unavailable")
        async with self.sessions() as db:
            attempt = await self.pending(db)
            attempt.created_at = datetime.utcnow() - timedelta(minutes=6)
            await db.commit()
            self.assertIsNone(await reconcile_creation_attempt(attempt, self.client, db))
            self.assertEqual(ReservationStatus.FAILED, (await db.get(Reservation, 88)).status)
            self.assertIsNotNone(await db.get(CloudCreationAttempt, attempt.id))
        self.assertEqual(1, self.client.create_instance.await_count)

    async def test_expired_reservation_is_ended_on_recovery(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            reservation = await db.get(Reservation, 88)
            reservation.ends_at = datetime.utcnow() - timedelta(minutes=1)
            await db.commit()
            self.client.list_instances.return_value = [
                CloudInstanceData(**provider_data(tags=(attempt.id,))),
            ]
            instance = await reconcile_creation_attempt(attempt, self.client, db)
            self.assertTrue(instance.is_available)
            self.assertEqual(ReservationStatus.ENDED, reservation.status)

    async def test_sync_recovers_without_any_tracked_instances_and_cleanup_deletes(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            attempt.created_at = datetime.utcnow() - timedelta(minutes=2)
            reservation = await db.get(Reservation, 88)
            reservation.status = ReservationStatus.ENDED
            await db.commit()
            self.client.list_instances.return_value = [
                CloudInstanceData(**provider_data(tags=(attempt.id,))),
            ]
        with (patch("app.database.async_session_maker", self.sessions),
              patch.object(orchestrator, "get_cloud_client", return_value=self.client)):
            await orchestrator.sync_cloud_instances()
            self.assertEqual(1, await orchestrator.cleanup_expired_instances())
        self.client.destroy_instance.assert_awaited_once_with("provider-vm", region="mad")

    async def test_untracked_legacy_instance_is_reported_without_deletion(self):
        self.client.list_instances.return_value = [
            CloudInstanceData(**provider_data(label=f"{orchestrator.settings.site_name} #88")),
        ]
        with (patch("app.database.async_session_maker", self.sessions),
              patch.object(orchestrator, "get_cloud_client", return_value=self.client),
              self.assertLogs(orchestrator.logger, level="WARNING") as logs):
            await orchestrator.sync_cloud_instances()
        self.assertTrue(any("Untracked Vultr instance provider-vm" in line for line in logs.output))
        self.client.destroy_instance.assert_not_awaited()

    async def test_atomic_recovery_is_idempotent(self):
        await self.make_uncertain()
        async with self.sessions() as db:
            attempt = await self.pending(db)
            cloud = CloudInstanceData(**provider_data(tags=(attempt.id,)))
            first = await record_created_instance(attempt, cloud, db)
            second = await record_created_instance(attempt, cloud, db)
            self.assertEqual(first.id, second.id)
            self.assertEqual(1, len(list((await db.execute(select(CloudInstance))).scalars())))

    async def test_uncertain_create_does_not_fall_back_to_another_provider(self):
        fallback = SimpleNamespace(create_instance=AsyncMock())
        def get_client(provider):
            return self.client if provider == "vultr" else fallback
        providers = [
            SimpleNamespace(provider_code="vultr", provider_region="mad", instance_plan=None),
            SimpleNamespace(provider_code="gcore", provider_region="1", instance_plan=None),
        ]
        with (patch.object(orchestrator, "get_cloud_client", side_effect=get_client),
              patch.object(orchestrator, "get_providers_for_location", AsyncMock(return_value=providers)),
              patch.object(orchestrator, "generate_ignition_config", return_value="ignition"),
              patch.object(orchestrator, "is_provider_suspended", return_value=False)):
            async with self.sessions() as db:
                result = await orchestrator.provision_instance_for_reservation(
                    await db.get(Reservation, 88), db,
                )
                self.assertIsNone(result)
                await self.pending(db)
        fallback.create_instance.assert_not_awaited()
        self.assertEqual(1, self.client.create_instance.await_count)


if __name__ == "__main__":
    unittest.main()

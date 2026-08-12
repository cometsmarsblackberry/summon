import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.instance import CloudInstance
from app.services import orchestrator
from app.services.cloud_provider import CloudInstanceData, CloudProviderError


class CloudInstanceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "cloud-sync.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        self.instance_id = "b83c5f37-e317-46d1-92e2-7423b4a39f9f"
        async with self.sessions() as db:
            db.add(CloudInstance(
                id=self.instance_id,
                instance_id="tf2-47-test",
                location="vultr-mad",
                shape="vhf-2c-2gb",
                provider_code="vultr",
                provider_region="mad",
                auth_token="test-token",
                status="pending",
            ))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def _sync_with(self, client):
        with (
            patch("app.database.async_session_maker", self.sessions),
            patch.object(orchestrator, "get_cloud_client", return_value=client),
        ):
            return await orchestrator.sync_cloud_instances()

    async def _stored_instance(self):
        async with self.sessions() as db:
            return await db.get(CloudInstance, self.instance_id)

    async def test_list_omission_is_preserved_when_direct_lookup_succeeds(self):
        provider_instance = CloudInstanceData(
            id=self.instance_id,
            region="mad",
            plan="vhf-2c-2gb",
            main_ip="208.85.21.192",
            status="active",
            power_status="running",
            date_created="2026-08-12T17:00:07+00:00",
        )
        client = SimpleNamespace(
            list_instances=AsyncMock(return_value=[]),
            get_instance=AsyncMock(return_value=provider_instance),
        )

        removed = await self._sync_with(client)
        stored = await self._stored_instance()

        self.assertEqual(0, removed)
        self.assertIsNotNone(stored)
        self.assertEqual("208.85.21.192", stored.ip_address)
        self.assertEqual("active", stored.status)
        client.get_instance.assert_awaited_once_with(self.instance_id)

    async def test_list_omission_is_removed_after_confirmed_404(self):
        client = SimpleNamespace(
            list_instances=AsyncMock(return_value=[]),
            get_instance=AsyncMock(
                side_effect=CloudProviderError("instance not found", status_code=404)
            ),
        )

        removed = await self._sync_with(client)

        self.assertEqual(1, removed)
        self.assertIsNone(await self._stored_instance())
        client.get_instance.assert_awaited_once_with(self.instance_id)

    async def test_list_omission_is_preserved_when_direct_lookup_fails(self):
        client = SimpleNamespace(
            list_instances=AsyncMock(return_value=[]),
            get_instance=AsyncMock(side_effect=RuntimeError("provider unavailable")),
        )

        removed = await self._sync_with(client)

        self.assertEqual(0, removed)
        self.assertIsNotNone(await self._stored_instance())
        client.get_instance.assert_awaited_once_with(self.instance_id)


if __name__ == "__main__":
    unittest.main()

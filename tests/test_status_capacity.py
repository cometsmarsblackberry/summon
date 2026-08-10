import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import status


class _QueryResult:
    def __init__(self, *, rows=None, scalar_values=None):
        self._rows = rows or []
        self._scalar_values = scalar_values or []

    def all(self):
        return self._rows

    def scalars(self):
        return _ScalarResult(self._scalar_values)


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


def _status_db_fixture():
    location = SimpleNamespace(
        code="helsinki",
        name="Helsinki",
        city="Helsinki",
        country="Finland",
        continent="Europe",
        subdivision=None,
        recommended=True,
        enabled=True,
        provider="vultr",
        provider_region="hel",
        instance_plan=None,
        region_instance_limit=None,
    )
    location_provider = SimpleNamespace(
        location_code="helsinki",
        provider_code="vultr",
        provider_region="hel",
        region_instance_limit=None,
    )
    provider = SimpleNamespace(code="vultr", instance_limit=30)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _QueryResult(rows=[]),
                _QueryResult(scalar_values=[location_provider]),
                _QueryResult(scalar_values=[location]),
                _QueryResult(scalar_values=[]),
                _QueryResult(scalar_values=[provider]),
            ]
        )
    )
    return location, db


class VultrAccountCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        status._vultr_account_instance_ids = None
        status._vultr_account_refresh_after = 0
        status._vultr_account_refresh_task = None

    async def asyncTearDown(self):
        await status.stop_vultr_account_refresh()

    async def test_account_count_includes_untracked_vultr_instances(self):
        client = SimpleNamespace(
            list_instances=AsyncMock(
                return_value=[
                    SimpleNamespace(id="tracked"),
                    SimpleNamespace(id="other-deployment"),
                ]
            )
        )

        with patch.object(status, "get_cloud_client", return_value=client):
            await status._refresh_vultr_account_instance_ids()
            count = status._get_vultr_account_instance_count({"tracked"})

        self.assertEqual(2, count)
        client.list_instances.assert_awaited_once_with()

    async def test_account_count_returns_local_state_while_refresh_is_slow(self):
        refresh_finished = asyncio.Event()

        async def slow_list_instances():
            await refresh_finished.wait()
            return [SimpleNamespace(id="other-deployment")]

        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=slow_list_instances)
        )

        with patch.object(status, "get_cloud_client", return_value=client):
            count = status._get_vultr_account_instance_count({"tracked"})
            await asyncio.sleep(0)

        self.assertEqual(1, count)
        self.assertIsNotNone(status._vultr_account_refresh_task)
        self.assertFalse(status._vultr_account_refresh_task.done())
        client.list_instances.assert_awaited_once_with()

    async def test_account_count_preserves_stale_state_on_api_error(self):
        status._vultr_account_instance_ids = frozenset({"other-deployment"})
        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=RuntimeError("Vultr unavailable"))
        )

        with patch.object(status, "get_cloud_client", return_value=client):
            await status._refresh_vultr_account_instance_ids()
            status._vultr_account_refresh_after = float("inf")
            count = status._get_vultr_account_instance_count({"tracked"})

        self.assertEqual(2, count)

    async def test_refresh_timeout_keeps_requests_on_local_state(self):
        async def never_finishes():
            await asyncio.sleep(60)

        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=never_finishes)
        )

        with (
            patch.object(status, "get_cloud_client", return_value=client),
            patch.object(status, "_VULTR_ACCOUNT_REFRESH_TIMEOUT", 0.01),
        ):
            status.schedule_vultr_account_refresh()
            task = status._vultr_account_refresh_task
            self.assertIsNotNone(task)
            await task
            count = status._get_vultr_account_instance_count({"tracked"})

        self.assertEqual(1, count)

    async def test_concurrent_reads_schedule_only_one_refresh(self):
        refresh_finished = asyncio.Event()

        async def slow_list_instances():
            await refresh_finished.wait()
            return []

        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=slow_list_instances)
        )

        with patch.object(status, "get_cloud_client", return_value=client):
            counts = [
                status._get_vultr_account_instance_count({"tracked"})
                for _ in range(10)
            ]
            task = status._vultr_account_refresh_task
            await asyncio.sleep(0)

        self.assertEqual([1] * 10, counts)
        self.assertIs(task, status._vultr_account_refresh_task)
        client.list_instances.assert_awaited_once_with()

    async def test_slow_vultr_does_not_block_status_build(self):
        async def slow_list_instances():
            await asyncio.sleep(60)

        location, db = _status_db_fixture()
        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=slow_list_instances)
        )

        with (
            patch.object(
                status,
                "get_enabled_locations",
                AsyncMock(return_value=[location]),
            ),
            patch.object(status, "get_cloud_client", return_value=client),
        ):
            result = await asyncio.wait_for(status._build_status(db), timeout=0.5)
            await asyncio.sleep(0)

        self.assertEqual(30, result["helsinki"]["available"])
        client.list_instances.assert_awaited_once_with()

    async def test_status_subtracts_instances_owned_by_another_deployment(self):
        location, db = _status_db_fixture()

        with (
            patch.object(
                status,
                "get_enabled_locations",
                AsyncMock(return_value=[location]),
            ),
            patch.object(
                status,
                "_get_vultr_account_instance_count",
                return_value=1,
            ) as account_count,
        ):
            result = await status._build_status(db)

        self.assertEqual(29, result["helsinki"]["available"])
        account_count.assert_called_once_with(set())


if __name__ == "__main__":
    unittest.main()

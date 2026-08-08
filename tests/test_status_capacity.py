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


class VultrAccountCapacityTests(unittest.IsolatedAsyncioTestCase):
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
            count = await status._get_vultr_account_instance_count({"tracked"})

        self.assertEqual(2, count)
        client.list_instances.assert_awaited_once_with()

    async def test_account_count_falls_back_to_local_state_on_api_error(self):
        client = SimpleNamespace(
            list_instances=AsyncMock(side_effect=RuntimeError("Vultr unavailable"))
        )

        with patch.object(status, "get_cloud_client", return_value=client):
            count = await status._get_vultr_account_instance_count(
                {"tracked-a", "tracked-b"}
            )

        self.assertEqual(2, count)

    async def test_status_subtracts_instances_owned_by_another_deployment(self):
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

        with (
            patch.object(
                status,
                "get_enabled_locations",
                AsyncMock(return_value=[location]),
            ),
            patch.object(
                status,
                "_get_vultr_account_instance_count",
                AsyncMock(return_value=1),
            ) as account_count,
        ):
            result = await status._build_status(db)

        self.assertEqual(29, result["helsinki"]["available"])
        account_count.assert_awaited_once_with(set())


if __name__ == "__main__":
    unittest.main()

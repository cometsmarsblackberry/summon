import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.models.upload_link import UploadLink  # noqa: F401 - registers the relationship
from app.services import orchestrator
from app.services.cloud_provider import CloudInstanceData


class InstancePlanTests(unittest.IsolatedAsyncioTestCase):
    async def _create_instance(
        self,
        *,
        location_plan: str | None,
        provider_plan: str | None,
        returned_plan: str,
    ):
        reservation = SimpleNamespace(
            id=35,
            reservation_number=35,
            location="vultr-mad",
            instance_id=None,
        )
        loc_provider = SimpleNamespace(
            provider_code="vultr",
            provider_region="mad",
            instance_plan=location_plan,
        )
        provider_record = SimpleNamespace(instance_plan=provider_plan)
        client = SimpleNamespace(
            create_instance=AsyncMock(
                return_value=CloudInstanceData(
                    id="cloud-instance-id",
                    region="mad",
                    plan=returned_plan,
                    main_ip="192.0.2.1",
                    status="active",
                    power_status="running",
                    date_created="2026-08-09T00:00:00+00:00",
                )
            )
        )
        db = SimpleNamespace(add=Mock(), commit=AsyncMock())

        with patch.object(
            orchestrator,
            "generate_ignition_config",
            return_value="encoded-ignition",
        ):
            instance = await orchestrator._create_new_instance(
                reservation=reservation,
                client=client,
                loc_provider=loc_provider,
                provider_record=provider_record,
                auth_token="auth-token",
                instance_id="tf2-35-test",
                location_city="Madrid",
                container_image="example.invalid/tf2:latest",
                owner_steam_id="steam-id",
                owner_name="Player",
                fastdl_url="https://fastdl.example.invalid",
                db=db,
            )

        return instance, client

    async def test_provider_plan_is_used_when_location_override_is_unset(self):
        instance, client = await self._create_instance(
            location_plan=None,
            provider_plan="vhf-2c-2gb",
            returned_plan="vhf-2c-2gb",
        )

        self.assertEqual(
            "vhf-2c-2gb",
            client.create_instance.await_args.kwargs["plan"],
        )
        self.assertEqual("vhf-2c-2gb", instance.shape)

    async def test_location_plan_override_takes_precedence(self):
        _, client = await self._create_instance(
            location_plan="vhp-1c-1gb",
            provider_plan="vhf-2c-2gb",
            returned_plan="vhp-1c-1gb",
        )

        self.assertEqual(
            "vhp-1c-1gb",
            client.create_instance.await_args.kwargs["plan"],
        )


if __name__ == "__main__":
    unittest.main()

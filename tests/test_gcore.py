import unittest
from unittest.mock import AsyncMock

from app.services.cloud_provider import CloudInstanceData, CloudProviderError
from app.services.gcore import GcoreClient


def region_request(volume_types):
    async def request(method, endpoint, **kwargs):
        if endpoint.startswith("/regions/"):
            return {"id": 152, "available_volume_types": volume_types}
        if method == "POST" and endpoint.startswith("/instances/"):
            return {"tasks": ["task-1"]}
        raise AssertionError(f"unexpected request {method} {endpoint}")
    return request


class BootVolumeTypeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_low_latency_then_high_iops_then_standard(self):
        cases = [
            (["standard", "ssd_hiiops", "ssd_lowlatency"], "ssd_lowlatency"),
            (["ssd_hiiops", "standard"], "ssd_hiiops"),
            (["standard"], "standard"),
            (["cold", "ultra"], "standard"),
            (None, "standard"),
        ]
        for volume_types, expected in cases:
            with self.subTest(volume_types=volume_types):
                client = GcoreClient("test", 1)
                client._request = AsyncMock(side_effect=region_request(volume_types))
                self.assertEqual(await client._get_boot_volume_type(152), expected)
                client._request.assert_awaited_once_with("GET", "/regions/152")

    async def test_region_lookup_failure_falls_back_to_standard(self):
        client = GcoreClient("test", 1)
        client._request = AsyncMock(side_effect=CloudProviderError("unavailable", 503))
        with self.assertLogs("app.services.gcore", level="WARNING"):
            self.assertEqual(await client._get_boot_volume_type(152), "standard")

    async def test_create_instance_uses_region_boot_volume_type(self):
        client = GcoreClient("test", 1)
        client._request = AsyncMock(side_effect=region_request(["ssd_hiiops", "standard"]))
        client._get_fcos_image_id = AsyncMock(return_value="fcos-image")
        client._wait_for_task = AsyncMock(
            return_value={"created_resources": {"instances": ["vm-1"]}}
        )
        client.get_instance = AsyncMock(return_value=CloudInstanceData(
            id="vm-1", region="152", plan="g2-cpu-2-2", main_ip="192.0.2.1",
            status="ACTIVE", power_status="active", date_created="",
        ))

        await client.create_instance("152", "Summon #88", "ignition", plan="g2-cpu-2-2")

        post = next(c for c in client._request.await_args_list if c.args[0] == "POST")
        self.assertEqual(post.args[1], "/instances/1/152")
        self.assertEqual(post.kwargs["json"]["volumes"], [{
            "source": "image",
            "image_id": "fcos-image",
            "size": 20,
            "boot_index": 0,
            "type_name": "ssd_hiiops",
        }])


if __name__ == "__main__":
    unittest.main()

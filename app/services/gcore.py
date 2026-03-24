"""Gcore Cloud API client for cloud instance management."""

import asyncio
import logging
from typing import Optional

import httpx

from app.config import get_settings
from app.services.cloud_provider import (
    CloudProvider,
    CloudInstanceData,
    CloudProviderError,
)


logger = logging.getLogger(__name__)

settings = get_settings()

GCORE_API_BASE = "https://api.gcore.com/cloud"

# Default flavor: 1 vCPU, 2GB RAM, Intel Xeon 2nd Gen
GCORE_FLAVOR = "g1-standard-1-2"

# Fedora CoreOS 42 x64 image ID (available across regions)
GCORE_FCOS_IMAGE = "fedora-coreos-42-x64"

# Boot volume size in GB
GCORE_BOOT_VOLUME_SIZE = 20

# Security group for direct connect (UDP game ports)
DIRECT_CONNECT_SG_NAME = "tf2-direct-connect"

# Task polling config
TASK_POLL_INTERVAL = 3  # seconds
TASK_POLL_TIMEOUT = 300  # seconds


class GcoreClient(CloudProvider):
    """Gcore Cloud API client."""

    def __init__(self, api_key: str, project_id: int):
        self.api_key = api_key
        self.project_id = project_id
        self.headers = {
            "Authorization": f"apikey {api_key}",
            "Content-Type": "application/json",
        }
        self._sg_cache: dict[int, str] = {}  # region_id -> security group ID

    async def _request(
        self,
        method: str,
        endpoint: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        api_version: str = "v1",
    ) -> dict:
        """Make API request to Gcore Cloud."""
        url = f"{GCORE_API_BASE}/{api_version}{endpoint}"

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=self.headers,
                json=json,
                params=params,
                timeout=30.0,
            )

        if response.status_code >= 400:
            error_msg = response.text
            try:
                error_data = response.json()
                error_msg = error_data.get("message", response.text)
            except Exception:
                pass
            raise CloudProviderError(error_msg, response.status_code)

        if response.status_code == 204:
            return {}

        return response.json()

    async def _get_fcos_image_id(self, region_id: int) -> str:
        """Find the Fedora CoreOS image ID for a given region."""
        data = await self._request(
            "GET", f"/images/{self.project_id}/{region_id}"
        )
        for img in data.get("results", []):
            if img.get("os_distro") == "fedora-coreos" and "x64" in img.get("display_name", img.get("name", "")):
                # Prefer the latest version
                return img["id"]

        raise CloudProviderError(
            f"Fedora CoreOS image not found in region {region_id}"
        )

    async def _wait_for_task(self, task_id: str) -> dict:
        """Poll a Gcore task until it completes.

        Returns the task result containing created resource IDs.
        """
        elapsed = 0
        while elapsed < TASK_POLL_TIMEOUT:
            data = await self._request("GET", f"/tasks/{task_id}")
            state = data.get("state")

            if state == "FINISHED":
                return data
            elif state == "ERROR":
                error = data.get("error") or "Task failed"
                raise CloudProviderError(f"Instance creation task failed: {error}")

            await asyncio.sleep(TASK_POLL_INTERVAL)
            elapsed += TASK_POLL_INTERVAL

        raise CloudProviderError(
            f"Task {task_id} timed out after {TASK_POLL_TIMEOUT}s"
        )

    async def ensure_direct_connect_security_group(self, region_id: int) -> str:
        """Ensure the tf2-direct-connect security group exists in the region.

        Creates the security group and inbound rules for game ports (UDP 27015,
        27020) and SSH (TCP 22) if they don't already exist. Returns the
        security group ID.
        """
        if region_id in self._sg_cache:
            return self._sg_cache[region_id]

        # List existing security groups in this region
        data = await self._request(
            "GET", f"/securitygroups/{self.project_id}/{region_id}"
        )

        sg_id = None
        existing_rules = []
        for sg in data.get("results", []):
            if sg.get("name") == DIRECT_CONNECT_SG_NAME:
                sg_id = sg["id"]
                existing_rules = sg.get("security_group_rules", [])
                break

        if sg_id is None:
            create_data = await self._request(
                "POST",
                f"/securitygroups/{self.project_id}/{region_id}",
                json={
                    "security_group": {
                        "name": DIRECT_CONNECT_SG_NAME,
                        "description": "TF2 game server direct connect (UDP 27015, 27020) + SSH",
                    },
                },
            )
            sg_id = create_data["id"]
            existing_rules = []
            logger.info(
                f"Created security group '{DIRECT_CONNECT_SG_NAME}' in region {region_id}: {sg_id}"
            )

        # Check which port rules already exist
        needed_rules = {("udp", 27015), ("udp", 27020), ("tcp", 22)}
        for rule in existing_rules:
            key = (rule.get("protocol"), rule.get("port_range_min"))
            if rule.get("direction") == "ingress" and key in needed_rules:
                needed_rules.discard(key)

        for protocol, port in sorted(needed_rules):
            await self._request(
                "POST",
                f"/securitygroups/{self.project_id}/{region_id}/{sg_id}/rules",
                json={
                    "direction": "ingress",
                    "ethertype": "IPv4",
                    "protocol": protocol,
                    "port_range_min": port,
                    "port_range_max": port,
                    "remote_ip_prefix": "0.0.0.0/0",
                },
            )
            logger.info(
                f"Added {protocol.upper()}/{port} ingress rule to '{DIRECT_CONNECT_SG_NAME}' in region {region_id}"
            )

        self._sg_cache[region_id] = sg_id
        return sg_id

    async def add_security_group_to_instance(
        self, instance_id: str, region_id: int, security_group_name: str
    ) -> None:
        """Attach a security group to a running instance."""
        await self._request(
            "POST",
            f"/instances/{self.project_id}/{region_id}/{instance_id}/addsecuritygroup",
            json={"name": security_group_name},
        )
        logger.info(f"Attached security group '{security_group_name}' to instance {instance_id}")

    async def remove_security_group_from_instance(
        self, instance_id: str, region_id: int, security_group_name: str
    ) -> None:
        """Detach a security group from a running instance."""
        try:
            await self._request(
                "POST",
                f"/instances/{self.project_id}/{region_id}/{instance_id}/delsecuritygroup",
                json={"name": security_group_name},
            )
            logger.info(f"Detached security group '{security_group_name}' from instance {instance_id}")
        except CloudProviderError as e:
            if e.status_code in (404, 409):
                logger.debug(
                    f"Security group '{security_group_name}' not attached to {instance_id}, ignoring"
                )
            else:
                raise

    async def create_instance(
        self,
        region: str,
        label: str,
        user_data: str,
        hostname: Optional[str] = None,
        plan: Optional[str] = None,
        security_groups: Optional[list[str]] = None,
    ) -> CloudInstanceData:
        """Create a new cloud instance on Gcore.

        Args:
            region: Gcore region ID (as string, will be converted to int)
            label: Instance name/label
            user_data: Ignition config (base64 encoded JSON)
            hostname: Optional hostname (used as instance name if provided)
            plan: Optional flavor override

        Returns:
            CloudInstanceData with new instance details
        """
        region_id = int(region)

        # Find FCOS image for this region
        image_id = await self._get_fcos_image_id(region_id)

        payload = {
            "flavor": plan or GCORE_FLAVOR,
            "names": [hostname or label],
            "volumes": [
                {
                    "source": "image",
                    "image_id": image_id,
                    "size": GCORE_BOOT_VOLUME_SIZE,
                    "boot_index": 0,
                    "type_name": "standard",
                }
            ],
            "interfaces": [
                {"type": "external"}
            ],
            "user_data": user_data,
        }

        if security_groups:
            payload["security_groups"] = [{"id": sg} for sg in security_groups]

        # Create instance (async - returns task ID)
        data = await self._request(
            "POST",
            f"/instances/{self.project_id}/{region_id}",
            json=payload,
            api_version="v2",
        )

        task_ids = data.get("tasks", [])
        if not task_ids:
            raise CloudProviderError("No task ID returned from instance creation")

        # Poll task until instance is created
        task_result = await self._wait_for_task(task_ids[0])

        # Extract instance ID from completed task
        created_resources = task_result.get("created_resources", {})
        instance_ids = created_resources.get("instances", [])
        if not instance_ids:
            raise CloudProviderError(
                "No instance ID in completed task result"
            )

        instance_id = instance_ids[0]

        # Fetch the created instance details
        return await self.get_instance(instance_id, region_id=region_id)

    async def get_instance(
        self, instance_id: str, region_id: Optional[int] = None
    ) -> CloudInstanceData:
        """Get instance details.

        Args:
            instance_id: Gcore instance UUID
            region_id: Optional region ID (avoids scanning all regions)
        """
        if region_id is not None:
            regions_to_check = [region_id]
        else:
            # If no region specified, we need to try to find it
            # First try listing all instances across the account
            regions_to_check = await self._get_active_region_ids()

        for rid in regions_to_check:
            try:
                data = await self._request(
                    "GET",
                    f"/instances/{self.project_id}/{rid}/{instance_id}",
                )
                instance = data.get("instance", data)

                # Extract the public IP from addresses
                main_ip = self._extract_public_ip(instance)

                return CloudInstanceData(
                    id=instance["id"],
                    region=str(rid),
                    plan=instance.get("flavor", {}).get("flavor_name", ""),
                    main_ip=main_ip,
                    status=instance.get("status", ""),
                    power_status=instance.get("vm_state", ""),
                    date_created=instance.get("created_at", ""),
                )
            except CloudProviderError as e:
                if e.status_code == 404:
                    continue
                raise

        raise CloudProviderError(
            f"Instance {instance_id} not found", status_code=404
        )

    async def _get_instance_volume_ids(
        self, instance_id: str, region_id: int
    ) -> list[str]:
        """Get volume IDs attached to an instance.

        Returns an empty list if the instance or volumes can't be fetched.
        """
        try:
            data = await self._request(
                "GET",
                f"/instances/{self.project_id}/{region_id}/{instance_id}",
            )
            instance = data.get("instance", data)
            return [
                v["id"] for v in instance.get("volumes", []) if "id" in v
            ]
        except CloudProviderError:
            return []

    async def destroy_instance(self, instance_id: str, region: Optional[str] = None) -> None:
        """Delete/destroy an instance.

        Args:
            instance_id: Gcore instance UUID
            region: Provider region ID (avoids scanning all regions)
        """
        if region:
            volume_ids = await self._get_instance_volume_ids(
                instance_id, int(region)
            )
            query = {"delete_floatings": True}
            if volume_ids:
                query["volumes"] = ",".join(volume_ids)
            data = await self._request(
                "DELETE",
                f"/instances/{self.project_id}/{region}/{instance_id}",
                params=query,
            )
            # If DELETE returns a task, wait for it
            task_ids = data.get("tasks", []) if isinstance(data, dict) else []
            if task_ids:
                await self._wait_for_task(task_ids[0])
            return

        # Fallback: scan regions (shouldn't normally happen)
        for rid in await self._get_active_region_ids():
            try:
                volume_ids = await self._get_instance_volume_ids(
                    instance_id, rid
                )
                query = {"delete_floatings": True}
                if volume_ids:
                    query["volumes"] = ",".join(volume_ids)
                data = await self._request(
                    "DELETE",
                    f"/instances/{self.project_id}/{rid}/{instance_id}",
                    params=query,
                )
                task_ids = data.get("tasks", []) if isinstance(data, dict) else []
                if task_ids:
                    await self._wait_for_task(task_ids[0])
                return
            except CloudProviderError as e:
                if e.status_code == 404:
                    continue
                raise

        raise CloudProviderError(
            f"Instance {instance_id} not found for deletion", status_code=404
        )

    async def list_instances(
        self, label_prefix: Optional[str] = None
    ) -> list[CloudInstanceData]:
        """List all instances, optionally filtered by name prefix."""
        instances = []

        for rid in await self._get_active_region_ids():
            try:
                data = await self._request(
                    "GET", f"/instances/{self.project_id}/{rid}"
                )
            except CloudProviderError:
                continue

            for instance in data.get("results", []):
                name = instance.get("name", "")
                if label_prefix and not name.startswith(label_prefix):
                    continue

                main_ip = self._extract_public_ip(instance)

                instances.append(
                    CloudInstanceData(
                        id=instance["id"],
                        region=str(rid),
                        plan=instance.get("flavor", {}).get(
                            "flavor_name", ""
                        ),
                        main_ip=main_ip,
                        status=instance.get("status", ""),
                        power_status=instance.get("vm_state", ""),
                        date_created=instance.get("created_at", ""),
                    )
                )

        return instances

    async def _get_active_region_ids(self) -> list[int]:
        """Get list of active region IDs for this project."""
        data = await self._request("GET", "/regions")
        return [
            r["id"]
            for r in data.get("results", [])
            if r.get("state") == "ACTIVE" and r.get("has_kvm")
        ]

    @staticmethod
    def _extract_public_ip(instance: dict) -> str:
        """Extract the public/external IP from a Gcore instance's addresses."""
        addresses = instance.get("addresses", {})
        for network_name, addrs in addresses.items():
            for addr in addrs:
                if addr.get("type") == "fixed" and addr.get("addr"):
                    # External network addresses
                    return addr["addr"]
        return ""


def get_gcore_client() -> Optional[GcoreClient]:
    """Get Gcore client if API key and project ID are configured."""
    if not settings.gcore_configured:
        return None
    return GcoreClient(settings.gcore_api_key, settings.gcore_project_id)

"""Vultr API client for cloud instance management."""

from typing import Optional
from urllib.parse import urlencode
import httpx

from app.config import get_settings
from app.services.cloud_provider import (
    CloudProvider,
    CloudInstanceData,
    CloudProviderError,
    CloudCreationUncertain,
)


settings = get_settings()

VULTR_API_BASE = "https://api.vultr.com/v2"

# Instance plan
VULTR_PLAN = "vhf-1c-1gb"  # High Frequency, 1 vCPU, 1GB RAM

# Fedora CoreOS image ID (stable)
# Note: This may need to be updated periodically
VULTR_FCOS_IMAGE = "fedora-coreos"


class VultrClient(CloudProvider):
    """Vultr API client."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json: Optional[dict] = None,
    ) -> dict:
        """Make API request to Vultr."""
        url = f"{VULTR_API_BASE}{endpoint}"

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=self.headers,
                json=json,
                timeout=30.0,
            )

        if response.status_code >= 400:
            error_msg = response.text
            try:
                error_data = response.json()
                error_msg = error_data.get("error", response.text)
            except Exception:
                pass
            raise CloudProviderError(error_msg, response.status_code)

        if response.status_code == 204:  # No content
            return {}

        return response.json()

    async def get_available_os(self) -> list[dict]:
        """List available OS images."""
        data = await self._request("GET", "/os")
        return data.get("os", [])

    async def get_fcos_id(self) -> Optional[int]:
        """Get Fedora CoreOS image ID."""
        os_list = await self.get_available_os()
        for os_item in os_list:
            if "fedora" in os_item.get("name", "").lower() and "coreos" in os_item.get("name", "").lower():
                return os_item["id"]
        # Fallback to first Fedora image
        for os_item in os_list:
            if "fedora" in os_item.get("name", "").lower():
                return os_item["id"]
        return None

    async def create_instance(
        self,
        region: str,
        label: str,
        user_data: str,
        hostname: Optional[str] = None,
        plan: Optional[str] = None,
        creation_key: Optional[str] = None,
    ) -> CloudInstanceData:
        """Create a new VPS instance.

        Args:
            region: Vultr region slug (e.g., 'sto', 'icn', 'scl')
            label: Instance label for identification
            user_data: Ignition config (base64 encoded JSON)
            hostname: Optional hostname

        Returns:
            CloudInstanceData with new instance details
        """
        if not region:
            raise CloudProviderError("No region specified")

        # Get Fedora CoreOS image ID
        os_id = await self.get_fcos_id()
        if not os_id:
            raise CloudProviderError("Fedora CoreOS image not found")

        payload = {
            "region": region,
            "plan": plan or VULTR_PLAN,
            "os_id": os_id,
            "label": label,
            "user_data": user_data,
            "backups": "disabled",
            "ddos_protection": False,
            "enable_ipv6": False,
        }

        if hostname:
            payload["hostname"] = hostname

        if creation_key:
            payload["tags"] = [creation_key]

        # Once POST starts, a lost response (including a server-side error) is
        # not proof that creation failed. The caller must reconcile its intent.
        try:
            data = await self._request("POST", "/instances", json=payload)
            return self._instance_data(data["instance"])
        except CloudProviderError as exc:
            if 400 <= exc.status_code < 500 and exc.status_code != 408:
                raise  # Definitive rejection; retry/failover is safe.
            raise CloudCreationUncertain("Vultr creation outcome unknown") from exc
        except Exception as exc:
            raise CloudCreationUncertain("Vultr creation outcome unknown") from exc

    @staticmethod
    def _instance_data(instance_data: dict) -> CloudInstanceData:
        if not instance_data.get("id"):
            raise ValueError("Vultr returned no instance ID")
        return CloudInstanceData(
            id=instance_data["id"],
            region=instance_data["region"],
            plan=instance_data["plan"],
            main_ip=instance_data.get("main_ip", ""),
            status=instance_data["status"],
            power_status=instance_data.get("power_status", ""),
            date_created=instance_data["date_created"],
            label=instance_data.get("label", ""),
            tags=tuple(instance_data.get("tags") or ()),
        )

    async def get_instance(self, instance_id: str) -> CloudInstanceData:
        """Get instance details."""
        data = await self._request("GET", f"/instances/{instance_id}")
        return self._instance_data(data["instance"])

    async def destroy_instance(self, instance_id: str, region: Optional[str] = None) -> None:
        """Delete/destroy an instance."""
        await self._request("DELETE", f"/instances/{instance_id}")

    async def list_instances(self, label_prefix: Optional[str] = None) -> list[CloudInstanceData]:
        """List all instances, optionally filtered by label prefix."""
        instances = []
        cursor = ""
        seen_cursors = set()
        while True:
            query = {"per_page": 100}
            if cursor:
                query["cursor"] = cursor
            data = await self._request("GET", "/instances?" + urlencode(query))
            for item in data.get("instances", []):
                if label_prefix and not item.get("label", "").startswith(label_prefix):
                    continue
                instances.append(self._instance_data(item))
            cursor = data.get("meta", {}).get("links", {}).get("next", "")
            if not cursor:
                return instances
            if cursor in seen_cursors:
                raise CloudProviderError("Vultr repeated a pagination cursor")
            seen_cursors.add(cursor)


def get_vultr_client() -> Optional[VultrClient]:
    """Get Vultr client if API key is configured."""
    if not settings.vultr_configured:
        return None
    return VultrClient(settings.vultr_api_key)

"""OneQode (OpenStack) API client for cloud instance management."""

import asyncio
import logging
import time
from datetime import datetime
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

# Default flavor: xer.small — 1 vCPU, 2GB RAM
ONEQODE_DEFAULT_FLAVOR = "227e7dec-5556-45df-b69d-c587e26e9e58"

# Security group that allows inbound traffic
ONEQODE_SECURITY_GROUP = "fleio"

# Polling settings for server creation (BUILD -> ACTIVE)
SERVER_POLL_INTERVAL = 3  # seconds
SERVER_POLL_TIMEOUT = 300  # seconds


class OneQodeClient(CloudProvider):
    """OneQode OpenStack API client."""

    def __init__(
        self,
        username: str,
        password: str,
        project_id: str,
        auth_url: str,
        region: str,
        image_id: str,
        network_id: str,
    ):
        self.username = username
        self.password = password
        self.project_id = project_id
        self.auth_url = auth_url.rstrip("/")
        self.region = region
        self.image_id = image_id
        self.network_id = network_id

        # Token cache
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._compute_url: Optional[str] = None

    async def _authenticate(self) -> None:
        """Authenticate with Keystone and cache the token + service catalog."""
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "domain": {"id": "default"},
                            "name": self.username,
                            "password": self.password,
                        }
                    },
                },
                "scope": {
                    "project": {"id": self.project_id}
                },
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.auth_url}/auth/tokens",
                json=payload,
                timeout=30.0,
            )

        if response.status_code >= 400:
            raise CloudProviderError(
                f"OneQode authentication failed: {response.text}",
                response.status_code,
            )

        self._token = response.headers.get("x-subject-token")
        if not self._token:
            raise CloudProviderError("No token in authentication response")

        data = response.json()
        token_data = data.get("token", {})

        # Parse expiry — keep a 5-minute buffer
        expires_str = token_data.get("expires_at", "")
        if expires_str:
            expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            self._token_expires_at = expires_dt.timestamp() - 300
        else:
            # Fallback: assume 55 minutes from now
            self._token_expires_at = time.time() + 3300

        # Discover compute endpoint from service catalog
        self._compute_url = None
        for service in token_data.get("catalog", []):
            if service.get("type") == "compute":
                for endpoint in service.get("endpoints", []):
                    if (
                        endpoint.get("region") == self.region
                        and endpoint.get("interface") == "public"
                    ):
                        self._compute_url = endpoint["url"].rstrip("/")
                        break

        if not self._compute_url:
            raise CloudProviderError(
                f"No compute endpoint found for region {self.region}"
            )

        logger.info("OneQode: authenticated, compute endpoint: %s", self._compute_url)

    async def _ensure_token(self) -> None:
        """Ensure we have a valid token, re-authenticating if needed."""
        if self._token and time.time() < self._token_expires_at:
            return
        await self._authenticate()

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
    ) -> httpx.Response:
        """Make an authenticated request to the Nova compute API.

        Returns the raw httpx.Response so callers can inspect status codes
        (e.g. 204 on delete).
        """
        await self._ensure_token()
        url = f"{self._compute_url}{path}"

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers={
                    "X-Auth-Token": self._token,
                    "Content-Type": "application/json",
                },
                json=json,
                timeout=30.0,
            )

        if response.status_code >= 400:
            error_msg = response.text
            try:
                error_data = response.json()
                # OpenStack errors can be nested under various keys
                for key in ("badRequest", "itemNotFound", "conflictingRequest", "forbidden"):
                    if key in error_data:
                        error_msg = error_data[key].get("message", error_msg)
                        break
            except Exception:
                pass
            raise CloudProviderError(error_msg, response.status_code)

        return response

    @staticmethod
    def _extract_public_ip(server: dict) -> str:
        """Extract the public IP from a server's addresses dict."""
        for network_name, addrs in server.get("addresses", {}).items():
            for addr in addrs:
                if addr.get("version") == 4 and addr.get("addr"):
                    return addr["addr"]
        return ""

    def _server_to_instance_data(self, server: dict) -> CloudInstanceData:
        """Map an OpenStack server dict to CloudInstanceData."""
        flavor = server.get("flavor", {})
        # Nova may return flavor as a dict with 'id' or 'original_name'
        plan = flavor.get("original_name") or flavor.get("id", "")

        status = server.get("status", "").lower()
        # Map OpenStack statuses to the convention used by other providers
        power_status = "running" if status == "active" else "stopped"

        return CloudInstanceData(
            id=server["id"],
            region=self.region,
            plan=plan,
            main_ip=self._extract_public_ip(server),
            status=status,
            power_status=power_status,
            date_created=server.get("created", ""),
        )

    async def _wait_for_active(self, server_id: str) -> CloudInstanceData:
        """Poll a server until it reaches ACTIVE status."""
        elapsed = 0
        while elapsed < SERVER_POLL_TIMEOUT:
            response = await self._request("GET", f"/servers/{server_id}")
            server = response.json().get("server", {})
            status = server.get("status", "")

            if status == "ACTIVE":
                return self._server_to_instance_data(server)
            elif status == "ERROR":
                fault = server.get("fault", {})
                msg = fault.get("message", "Server entered ERROR state")
                raise CloudProviderError(f"Instance creation failed: {msg}")

            await asyncio.sleep(SERVER_POLL_INTERVAL)
            elapsed += SERVER_POLL_INTERVAL

        raise CloudProviderError(
            f"Server {server_id} did not become ACTIVE within {SERVER_POLL_TIMEOUT}s"
        )

    async def create_instance(
        self,
        region: str,
        label: str,
        user_data: str,
        hostname: Optional[str] = None,
        plan: Optional[str] = None,
    ) -> CloudInstanceData:
        """Create a new compute instance on OneQode.

        Args:
            region: OpenStack region name (e.g., 'Guam')
            label: Instance name/label
            user_data: Ignition config (base64 encoded JSON)
            hostname: Optional hostname (used as server name if provided)
            plan: Optional flavor name override
        """
        server_name = hostname or label

        payload = {
            "server": {
                "name": server_name,
                "imageRef": self.image_id,
                "flavorRef": plan or ONEQODE_DEFAULT_FLAVOR,
                "networks": [{"uuid": self.network_id}],
                "user_data": user_data,
                "config_drive": True,
                "security_groups": [{"name": ONEQODE_SECURITY_GROUP}],
            }
        }

        response = await self._request("POST", "/servers", json=payload)
        server_data = response.json().get("server", {})
        server_id = server_data.get("id")
        if not server_id:
            raise CloudProviderError("No server ID in creation response")

        logger.info("OneQode: server %s created (BUILD), polling for ACTIVE", server_id)
        return await self._wait_for_active(server_id)

    async def get_instance(self, instance_id: str) -> CloudInstanceData:
        """Get instance details."""
        response = await self._request("GET", f"/servers/{instance_id}")
        server = response.json().get("server", {})
        return self._server_to_instance_data(server)

    async def destroy_instance(self, instance_id: str, region: Optional[str] = None) -> None:
        """Delete/destroy an instance."""
        await self._request("DELETE", f"/servers/{instance_id}")

    async def list_instances(self, label_prefix: Optional[str] = None) -> list[CloudInstanceData]:
        """List instances, optionally filtered by name prefix."""
        response = await self._request("GET", "/servers/detail")
        instances = []

        for server in response.json().get("servers", []):
            if label_prefix and not server.get("name", "").startswith(label_prefix):
                continue
            instances.append(self._server_to_instance_data(server))

        return instances


def get_oneqode_client() -> Optional[OneQodeClient]:
    """Get OneQode client if configured."""
    if not settings.oneqode_configured:
        return None
    return OneQodeClient(
        username=settings.oneqode_username,
        password=settings.oneqode_password,
        project_id=settings.oneqode_project_id,
        auth_url=settings.oneqode_auth_url,
        region=settings.oneqode_region,
        image_id=settings.oneqode_image_id,
        network_id=settings.oneqode_network_id,
    )

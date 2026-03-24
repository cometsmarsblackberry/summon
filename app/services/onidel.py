"""Onidel Cloud API client for cloud instance management."""

import base64
import json
import logging
import re
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

ONIDEL_API_BASE = "https://api.cloud.onidel.com"


# Environment variables from Ignition that are Fedora CoreOS-specific
_FCOS_ONLY_ENVS = {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}


class OnidelClient(CloudProvider):
    """Onidel Cloud API client."""

    def __init__(self, api_key: str, team_id: str):
        self.api_key = api_key
        self.team_id = team_id
        self.headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict | list:
        """Make API request to Onidel Cloud."""
        url = f"{ONIDEL_API_BASE}{endpoint}"

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
                if isinstance(error_data, dict):
                    error_msg = error_data.get("error", error_data.get("message", response.text))
            except Exception:
                pass
            raise CloudProviderError(error_msg, response.status_code)

        if response.status_code == 204:
            return {}

        return response.json()

    async def _get_os_template_id(self) -> int:
        """Find the first Ubuntu OS template ID."""
        data = await self._request("GET", "/os_templates")
        if not isinstance(data, list):
            raise CloudProviderError("Unexpected OS template response format")

        for tmpl in data:
            if tmpl.get("family") == "ubuntu":
                return tmpl["id"]
        # Fallback to first available
        if data:
            return data[0]["id"]
        raise CloudProviderError("No OS templates available")

    async def _create_startup_script(self, name: str, content: str) -> str:
        """Create a startup script and return its UUID."""
        data = await self._request(
            "POST",
            "/startup_scripts",
            json={
                "name": name,
                "content": content,
                "team_id": self.team_id,
            },
        )
        return data["script"]["id"]

    async def _delete_startup_script(self, script_id: str) -> None:
        """Delete a startup script (best-effort cleanup)."""
        try:
            await self._request(
                "DELETE",
                f"/startup_scripts/{script_id}",
                params={"team_id": self.team_id},
            )
        except CloudProviderError as e:
            logger.warning(f"Failed to delete startup script {script_id}: {e}")

    @staticmethod
    def _ignition_to_startup_script(user_data: str) -> str:
        """Convert base64-encoded Ignition config to a bash startup script.

        Extracts environment variables and agent URL from the Ignition
        systemd unit and generates an equivalent bash script for Ubuntu.
        """
        try:
            ignition = json.loads(base64.b64decode(user_data))
        except Exception:
            raise CloudProviderError("Failed to decode Ignition config from user_data")

        # Find the tf2-agent.service unit
        units = ignition.get("systemd", {}).get("units", [])
        agent_unit = None
        for unit in units:
            if unit.get("name") == "tf2-agent.service":
                agent_unit = unit.get("contents", "")
                break

        if not agent_unit:
            raise CloudProviderError("tf2-agent.service not found in Ignition config")

        # Extract environment variables (skip FCOS-specific ones)
        env_lines = []
        for match in re.finditer(r"Environment=(\w+)=(.+)", agent_unit):
            key, value = match.group(1), match.group(2).strip()
            if key not in _FCOS_ONLY_ENVS:
                env_lines.append(f"Environment={key}={value}")

        # Extract agent download URL from curl command
        agent_url = ""
        curl_match = re.search(r"curl\s+-L\s+-o\s+\S+\s+(\S+)", agent_unit)
        if curl_match:
            agent_url = curl_match.group(1)

        # Extract SSH keys from Ignition passwd section
        ssh_keys = []
        for user in ignition.get("passwd", {}).get("users", []):
            ssh_keys.extend(user.get("sshAuthorizedKeys", []))

        ssh_setup = ""
        if ssh_keys:
            keys_str = "\n".join(ssh_keys)
            ssh_setup = f"""
# Set up SSH keys
mkdir -p /root/.ssh
cat >> /root/.ssh/authorized_keys << 'SSHKEYS'
{keys_str}
SSHKEYS
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
"""

        env_block = "\n".join(env_lines)

        script = f"""#!/bin/bash
set -euo pipefail

# Create swap
if [ ! -f /var/swapfile ]; then
    fallocate -l 2G /var/swapfile
    chmod 600 /var/swapfile
    mkswap /var/swapfile
    swapon /var/swapfile
fi
{ssh_setup}
# Download agent
curl -L -o /usr/local/bin/tf2-agent {agent_url}
chmod +x /usr/local/bin/tf2-agent

# Create systemd service
cat > /etc/systemd/system/tf2-agent.service << 'UNIT'
[Unit]
Description=TF2 Server Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{env_block}
ExecStart=/usr/local/bin/tf2-agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now tf2-agent
"""
        return script

    @staticmethod
    def _parse_plan(plan: str) -> tuple[str, int, int, int]:
        """Parse an Onidel plan string into (instance_type, cpu, ram, disk).

        Format: "instance_type_uuid:cpu:ram_mb:disk_gb"
        Example: "6bcc78c4-07ab-44e6-a5a7-dc64414a1e10:1:2048:20"
        """
        parts = plan.split(":")
        if len(parts) != 4:
            raise CloudProviderError(
                f"Invalid Onidel plan format '{plan}'. "
                f"Expected 'instance_type_uuid:cpu:ram_mb:disk_gb'"
            )
        try:
            return parts[0], int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            raise CloudProviderError(
                f"Invalid Onidel plan format '{plan}'. "
                f"cpu, ram, and disk must be integers"
            )

    async def create_instance(
        self,
        region: str,
        label: str,
        user_data: str,
        hostname: Optional[str] = None,
        plan: Optional[str] = None,
    ) -> CloudInstanceData:
        """Create a new VM on Onidel.

        Args:
            region: Onidel location name (e.g., 'Sydney')
            label: VM name/label
            user_data: Base64-encoded Ignition config (converted to startup script)
            hostname: Optional hostname (used as VM name if provided)
            plan: Onidel plan string "instance_type_uuid:cpu:ram_mb:disk_gb"

        Returns:
            CloudInstanceData with new instance details
        """
        if not plan:
            raise CloudProviderError(
                "Onidel requires a plan in the format 'instance_type_uuid:cpu:ram_mb:disk_gb'"
            )

        instance_type, cpu, ram, disk = self._parse_plan(plan)

        os_id = await self._get_os_template_id()

        # Convert Ignition config to bash startup script
        script_content = self._ignition_to_startup_script(user_data)

        vm_name = hostname or label
        script_id = await self._create_startup_script(
            f"summon-{vm_name}", script_content
        )

        try:
            payload = {
                "name": vm_name,
                "payment_cycle": "hourly",
                "instance_type": instance_type,
                "location": region,
                "cpu": cpu,
                "ram": ram,
                "disk": disk,
                "os": os_id,
                "team_id": self.team_id,
                "startup_script_id": script_id,
            }

            data = await self._request("POST", "/vm", json=payload)

            vm = data if isinstance(data, dict) else {}

            return CloudInstanceData(
                id=vm.get("id", ""),
                region=region,
                plan=instance_type,
                main_ip=vm.get("main_ipv4", ""),
                status=vm.get("status", "building"),
                power_status=vm.get("status", "building"),
                date_created=vm.get("created_at", ""),
            )
        finally:
            # Clean up startup script to stay within the 10-script limit
            await self._delete_startup_script(script_id)

    async def get_instance(self, instance_id: str) -> CloudInstanceData:
        """Get VM details."""
        data = await self._request(
            "GET",
            f"/vm/{instance_id}",
            params={"team_id": self.team_id},
        )

        vm = data if isinstance(data, dict) else {}

        return CloudInstanceData(
            id=vm.get("id", instance_id),
            region="",
            plan="",
            main_ip=vm.get("main_ipv4", ""),
            status=vm.get("status", ""),
            power_status=vm.get("status", ""),
            date_created=vm.get("created_at", ""),
        )

    async def destroy_instance(self, instance_id: str, region: Optional[str] = None) -> None:
        """Delete/destroy a VM."""
        await self._request("DELETE", f"/vm/{instance_id}")

    async def list_instances(self, label_prefix: Optional[str] = None) -> list[CloudInstanceData]:
        """List all VMs, optionally filtered by name prefix."""
        data = await self._request(
            "GET",
            "/vm",
            params={"team_id": self.team_id},
        )

        instances = []
        vms = data if isinstance(data, list) else []

        for vm in vms:
            name = vm.get("name", "")
            if label_prefix and not name.startswith(label_prefix):
                continue
            instances.append(CloudInstanceData(
                id=vm.get("id", ""),
                region="",
                plan="",
                main_ip=vm.get("main_ipv4", ""),
                status=vm.get("status", ""),
                power_status=vm.get("status", ""),
                date_created=vm.get("created_at", ""),
            ))

        return instances


def get_onidel_client() -> Optional[OnidelClient]:
    """Get Onidel client if API key and team ID are configured."""
    if not settings.onidel_configured:
        return None
    return OnidelClient(settings.onidel_api_key, settings.onidel_team_id)

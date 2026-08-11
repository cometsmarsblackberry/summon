import base64
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.cloud_provider import CloudProviderError
from app.services import orchestrator
from app.services.onidel import OnidelClient


class AgentBootstrapTests(unittest.TestCase):
    def _ignition_config(self) -> tuple[str, str]:
        encoded = orchestrator.generate_ignition_config(
            instance_id="instance-id",
            auth_token="auth-token",
            reservation=SimpleNamespace(id=42),
        )
        ignition = json.loads(base64.b64decode(encoded))
        agent_unit = next(
            unit["contents"]
            for unit in ignition["systemd"]["units"]
            if unit["name"] == "tf2-agent.service"
        )
        return encoded, agent_unit

    def test_fcos_download_fails_on_http_errors_and_retries_indefinitely(self):
        _, agent_unit = self._ignition_config()

        digest = hashlib.sha256(Path("static/tf2-agent").read_bytes()).hexdigest()
        self.assertIn("StartLimitIntervalSec=0", agent_unit)
        self.assertIn("Environment=AGENT_MODE=cloud", agent_unit)
        self.assertIn("/usr/bin/curl --fail --location", agent_unit)
        self.assertIn("--retry 5 --retry-connrefused --retry-delay 5", agent_unit)
        self.assertIn("--output /home/core/tf2-agent.download", agent_unit)
        self.assertIn(f"/static/tf2-agent?sha256={digest}", agent_unit)
        self.assertIn("/usr/bin/mv /home/core/tf2-agent.download /home/core/tf2-agent", agent_unit)

    def test_onidel_preserves_hardened_agent_download_in_systemd(self):
        encoded, _ = self._ignition_config()
        script = OnidelClient._ignition_to_startup_script(encoded)

        digest = hashlib.sha256(Path("static/tf2-agent").read_bytes()).hexdigest()
        expected_url = (
            f"{orchestrator.settings.base_url}/static/tf2-agent"
            f"?sha256={digest}"
        )
        self.assertIn("StartLimitIntervalSec=0", script)
        self.assertIn("/usr/bin/curl --fail --location", script)
        self.assertIn("--output /usr/local/bin/tf2-agent.download", script)
        self.assertIn(expected_url, script)
        self.assertNotIn("\n# Download agent\ncurl", script)

    def test_onidel_rejects_ignition_without_an_agent_url(self):
        ignition = {
            "systemd": {
                "units": [
                    {
                        "name": "tf2-agent.service",
                        "contents": "[Service]\nExecStart=/home/core/tf2-agent\n",
                    }
                ]
            }
        }
        encoded = base64.b64encode(json.dumps(ignition).encode()).decode()

        with self.assertRaisesRegex(CloudProviderError, "Agent download URL not found"):
            OnidelClient._ignition_to_startup_script(encoded)

    def test_instant_host_installer_explains_checksum_failure(self):
        script = Path("static/install-instant-host.sh").read_text()

        self.assertIn("if ! printf '%s  %s\\n'", script)
        self.assertIn("Agent download failed integrity verification.", script)
        self.assertIn("Expected SHA-256:", script)
        self.assertIn("Downloaded SHA-256:", script)
        self.assertIn("The agent service was not created.", script)
        self.assertIn("request a new enrollment token", script)

    def test_instant_host_installer_requires_pasta_network_helper(self):
        script = Path("static/install-instant-host.sh").read_text()

        self.assertIn("podman passt uidmap", script)
        self.assertIn("command -v pasta", script)
        self.assertIn("requires the pasta executable", script)

    def test_instant_host_card_resolves_location_display_name(self):
        template = Path("templates/admin/index.html").read_text()

        self.assertIn("locationName(code)", template)
        self.assertIn('x-text="locationName(host.location)"', template)
        self.assertIn(':title="host.location"', template)


if __name__ == "__main__":
    unittest.main()

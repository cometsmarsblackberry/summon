import enum
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "services" / "failure_messages.py"


def load_failure_messages_module():
    app_pkg = sys.modules.setdefault("app", types.ModuleType("app"))
    app_pkg.__path__ = [str(ROOT / "app")]

    models_pkg = sys.modules.setdefault("app.models", types.ModuleType("app.models"))
    models_pkg.__path__ = [str(ROOT / "app" / "models")]

    class ReservationStatus(enum.Enum):
        FAILED = "failed"
        ACTIVE = "active"

    config_mod = types.ModuleType("app.config")
    config_mod.get_settings = lambda: types.SimpleNamespace(max_provision_attempts=3)

    i18n_mod = types.ModuleType("app.i18n")

    def translate(key, **kwargs):
        messages = {
            "errors.provision_failed": "Provisioning failed after {attempts} attempts. Please try creating a new reservation.",
            "status.provision_error": "An error occurred during provisioning. Please try again.",
        }
        return messages[key].format(**kwargs)

    i18n_mod.t = translate

    reservation_mod = types.ModuleType("app.models.reservation")
    reservation_mod.ReservationStatus = ReservationStatus

    sys.modules["app.config"] = config_mod
    sys.modules["app.i18n"] = i18n_mod
    sys.modules["app.models.reservation"] = reservation_mod

    spec = importlib.util.spec_from_file_location(
        "test_failure_messages_target",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, ReservationStatus


class PublicFailureReasonTests(unittest.TestCase):
    def setUp(self):
        self.module, self.status = load_failure_messages_module()

    def test_failed_reservation_hides_raw_provider_details_before_exhaustion(self):
        message = self.module.public_failure_reason(
            self.status.FAILED,
            provision_attempts=1,
            failure_reason="Provisioning failed: Unauthorized IP address: 203.0.113.1",
        )

        self.assertEqual(message, "An error occurred during provisioning. Please try again.")
        self.assertNotIn("203.0.113.1", message)

    def test_failed_reservation_uses_retry_exhausted_message_after_max_attempts(self):
        message = self.module.public_failure_reason(
            self.status.FAILED,
            provision_attempts=3,
            failure_reason="Server failed to boot after 3 attempts: backend at 203.0.113.1",
        )

        self.assertEqual(message, "Provisioning failed after 3 attempts. Please try creating a new reservation.")
        self.assertNotIn("203.0.113.1", message)

    def test_non_failed_reservation_returns_original_reason(self):
        message = self.module.public_failure_reason(
            self.status.ACTIVE,
            provision_attempts=0,
            failure_reason="unchanged",
        )

        self.assertEqual(message, "unchanged")


if __name__ == "__main__":
    unittest.main()

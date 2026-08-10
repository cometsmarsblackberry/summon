import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from app.main import app as production_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PreviewEnvironmentTests(unittest.TestCase):
    def _environment(self, data_dir: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(PROJECT_ROOT),
                "SUMMON_PREVIEW": "1",
                "DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'preview.db'}",
                "SECRET_KEY": "preview-test-secret",
                "BASE_URL": "http://127.0.0.1:8000",
                "BETA_MODE": "false",
                "LOG_DIR": str(data_dir / "logs"),
                "ONIDEL_API_KEY": "should-be-replaced",
                "ONIDEL_TEAM_ID": "99",
                "VULTR_API_KEY": "should-be-cleared",
                "GCORE_API_KEY": "should-be-cleared",
                "GCORE_PROJECT_ID": "99",
                "STEAM_API_KEY": "should-be-cleared",
                "HCAPTCHA_SITE_KEY": "should-be-cleared",
                "HCAPTCHA_SECRET_KEY": "should-be-cleared",
            }
        )
        return env

    def test_production_app_does_not_expose_preview_login(self):
        paths = {getattr(route, "path", None) for route in production_app.routes}
        self.assertNotIn("/__dev/login", paths)

    def test_preview_entrypoint_requires_explicit_flag(self):
        env = os.environ.copy()
        env.pop("SUMMON_PREVIEW", None)
        result = subprocess.run(
            [sys.executable, "-c", "import app.preview"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("SUMMON_PREVIEW=1", result.stderr)

    def test_preview_entrypoint_rejects_a_public_base_url(self):
        with tempfile.TemporaryDirectory(prefix="summon-preview-test-") as tmp:
            env = self._environment(Path(tmp))
            env["BASE_URL"] = "https://preview.example.com"
            result = subprocess.run(
                [sys.executable, "-c", "import app.preview"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("loopback hostname", result.stderr)

    def test_preview_login_seeds_a_usable_creation_form(self):
        script = textwrap.dedent(
            """
            import json
            from fastapi.testclient import TestClient
            from app.preview import app
            from app.config import get_settings

            with TestClient(app) as client:
                settings = get_settings()
                login = client.get('/__dev/login', follow_redirects=False)
                second_login = client.get('/__dev/login', follow_redirects=False)
                session = login.cookies.get('session')
                home = client.get('/', cookies={'session': session})
                configs = client.get('/api/reservations/configs').json()
                print('PREVIEW_RESULT=' + json.dumps({
                    'login_status': login.status_code,
                    'second_login_status': second_login.status_code,
                    'login_location': login.headers.get('location'),
                    'has_session': bool(session),
                    'home_status': home.status_code,
                    'has_user': 'UI Tester' in home.text,
                    'has_location': (
                        'Helsinki' in home.text and 'Finland' in home.text
                    ),
                    'has_uploads': 'Automatic uploads' in home.text,
                    'configs_available': configs.get('available'),
                    'config_leagues': sorted(configs.get('configs', {}).keys()),
                    'real_integrations_disabled': not any((
                        settings.vultr_configured,
                        settings.gcore_configured,
                        settings.steam_configured,
                        settings.hcaptcha_configured,
                    )),
                }, sort_keys=True))
            """
        )

        with tempfile.TemporaryDirectory(prefix="summon-preview-test-") as tmp:
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=PROJECT_ROOT,
                env=self._environment(Path(tmp)),
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        marker = next(
            line.removeprefix("PREVIEW_RESULT=")
            for line in result.stdout.splitlines()
            if line.startswith("PREVIEW_RESULT=")
        )
        payload = json.loads(marker)
        self.assertEqual(302, payload["login_status"])
        self.assertEqual(302, payload["second_login_status"])
        self.assertEqual("/", payload["login_location"])
        self.assertTrue(payload["has_session"])
        self.assertEqual(200, payload["home_status"])
        self.assertTrue(payload["has_user"])
        self.assertTrue(payload["has_location"])
        self.assertTrue(payload["has_uploads"])
        self.assertTrue(payload["configs_available"])
        self.assertTrue(payload["real_integrations_disabled"])
        self.assertIn("ETF2L", payload["config_leagues"])
        self.assertIn("RGL", payload["config_leagues"])


if __name__ == "__main__":
    unittest.main()

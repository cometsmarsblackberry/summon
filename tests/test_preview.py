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

    def test_preview_active_reservation_supports_config_changes(self):
        script = textwrap.dedent(
            """
            import json
            from fastapi.testclient import TestClient
            from app.preview import app, _preview_agent

            with TestClient(app) as client:
                active = client.get(
                    '/__dev/active-reservation', follow_redirects=False
                )
                session = active.cookies.get('session')
                location = active.headers['location']
                reservation_id = int(location.rsplit('/', 1)[-1])
                cookies = {'session': session}
                page = client.get(location, cookies=cookies)
                configs = client.get(
                    f'/api/reservations/{reservation_id}/configs',
                    cookies=cookies,
                ).json()
                changed = client.post(
                    f'/api/reservations/{reservation_id}/config',
                    cookies=cookies,
                    json={'cfg_file': 'rgl_6s_5cp_match_pro'},
                )
                reservation = client.get(
                    f'/api/reservations/{reservation_id}', cookies=cookies
                ).json()
                print('PREVIEW_ACTIVE_RESULT=' + json.dumps({
                    'active_status': active.status_code,
                    'active_location': location,
                    'page_status': page.status_code,
                    'page_is_active': 'Reservation #1' in page.text,
                    'picker_grouped': (
                        'x-for="group in filteredConfigGroups"' in page.text
                    ),
                    'picker_separates_format': (
                        'x-text="config.format"' in page.text
                    ),
                    'flat_picker_removed': (
                        "c.league + ' / ' + c.format" not in page.text
                    ),
                    'configs_available': configs.get('available'),
                    'change_status': changed.status_code,
                    'changed_config': reservation.get('config_file'),
                    'agent_command': _preview_agent.messages[-1],
                }, sort_keys=True))
            """
        )

        with tempfile.TemporaryDirectory(prefix="summon-preview-active-test-") as tmp:
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
            line.removeprefix("PREVIEW_ACTIVE_RESULT=")
            for line in result.stdout.splitlines()
            if line.startswith("PREVIEW_ACTIVE_RESULT=")
        )
        payload = json.loads(marker)
        self.assertEqual(302, payload["active_status"])
        self.assertRegex(payload["active_location"], r"^/reservations/\d+$")
        self.assertEqual(200, payload["page_status"])
        self.assertTrue(payload["page_is_active"])
        self.assertTrue(payload["picker_grouped"])
        self.assertTrue(payload["picker_separates_format"])
        self.assertTrue(payload["flat_picker_removed"])
        self.assertTrue(payload["configs_available"])
        self.assertEqual(200, payload["change_status"])
        self.assertEqual("rgl_6s_5cp_match_pro", payload["changed_config"])
        self.assertEqual(
            {
                "type": "rcon",
                "command": "sm_config rgl_6s_5cp_match_pro",
            },
            payload["agent_command"],
        )

    def test_preview_active_reservation_supports_server_commands(self):
        script = textwrap.dedent(
            """
            import json
            from fastapi.testclient import TestClient
            from app.preview import app, _preview_agent

            with TestClient(app) as client:
                active = client.get(
                    '/__dev/active-reservation', follow_redirects=False
                )
                cookies = {'session': active.cookies.get('session')}
                location = active.headers['location']
                reservation_id = int(location.rsplit('/', 1)[-1])
                page = client.get(location, cookies=cookies)
                commands = client.get(
                    f'/api/reservations/{reservation_id}/commands',
                    cookies=cookies,
                )
                executed = client.post(
                    f'/api/reservations/{reservation_id}/commands',
                    cookies=cookies,
                    json={'command': 'mp_timelimit 30'},
                )
                payload = executed.json()
                message = _preview_agent.messages[-1]
                html = page.text
                print('PREVIEW_COMMAND_RESULT=' + json.dumps({
                    'page_status': page.status_code,
                    'commands_status': commands.status_code,
                    'has_command': 'mp_timelimit' in commands.json().get('commands', []),
                    'execute_status': executed.status_code,
                    'execute_payload': payload,
                    'correlated': bool(message.get('command_id')),
                    'wrapped': message.get('command'),
                    'section_order': (
                        html.index('<!-- Restricted server command console -->')
                        > html.index('<!-- Connection details')
                        and html.index('<!-- Restricted server command console -->')
                        < html.index('<!-- Player list -->')
                    ),
                    'autocomplete_present': (
                        'commandSuggestions' in html
                        and 'handleCommandKeydown' in html
                    ),
                    'safe_result_binding': 'x-text="commandResult?.output' in html,
                }, sort_keys=True))
            """
        )

        with tempfile.TemporaryDirectory(prefix="summon-preview-command-test-") as tmp:
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
            line.removeprefix("PREVIEW_COMMAND_RESULT=")
            for line in result.stdout.splitlines()
            if line.startswith("PREVIEW_COMMAND_RESULT=")
        )
        payload = json.loads(marker)
        self.assertEqual(200, payload["page_status"])
        self.assertEqual(200, payload["commands_status"])
        self.assertTrue(payload["has_command"])
        self.assertEqual(200, payload["execute_status"])
        self.assertEqual({
            "command": "mp_timelimit 30",
            "ok": True,
            "output": "Preview executed: mp_timelimit 30",
            "error_code": None,
        }, payload["execute_payload"])
        self.assertTrue(payload["correlated"])
        self.assertEqual(
            "sm_summon_owner_command 76561198000000000 mp_timelimit 30",
            payload["wrapped"],
        )
        self.assertTrue(payload["section_order"])
        self.assertTrue(payload["autocomplete_present"])
        self.assertTrue(payload["safe_result_binding"])

    def test_preview_active_reservation_clears_warm_pool_state(self):
        script = textwrap.dedent(
            """
            import asyncio
            import json
            from fastapi.testclient import TestClient
            from app.database import async_session_maker
            from app.models.instance import CloudInstance
            from app.preview import app, _PREVIEW_INSTANCE_ID

            async def get_instance_state():
                async with async_session_maker() as db:
                    instance = await db.get(CloudInstance, _PREVIEW_INSTANCE_ID)
                    return {
                        'is_available': instance.is_available,
                        'available_since': (
                            instance.available_since.isoformat()
                            if instance.available_since else None
                        ),
                        'current_reservation_id': instance.current_reservation_id,
                        'status': instance.status,
                    }

            with TestClient(app) as client:
                first = client.get(
                    '/__dev/active-reservation', follow_redirects=False
                )
                cookies = {'session': first.cookies.get('session')}
                first_id = int(first.headers['location'].rsplit('/', 1)[-1])
                ended = client.post(
                    f'/api/reservations/{first_id}/end', cookies=cookies
                )
                second = client.get(
                    '/__dev/active-reservation', follow_redirects=False
                )
                second_id = int(second.headers['location'].rsplit('/', 1)[-1])
                state = asyncio.run(get_instance_state())
                print('PREVIEW_REUSE_RESULT=' + json.dumps({
                    'end_status': ended.status_code,
                    'second_status': second.status_code,
                    'second_id': second_id,
                    'instance': state,
                }, sort_keys=True))
            """
        )

        with tempfile.TemporaryDirectory(prefix="summon-preview-reuse-test-") as tmp:
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
            line.removeprefix("PREVIEW_REUSE_RESULT=")
            for line in result.stdout.splitlines()
            if line.startswith("PREVIEW_REUSE_RESULT=")
        )
        payload = json.loads(marker)
        self.assertEqual(200, payload["end_status"])
        self.assertEqual(302, payload["second_status"])
        self.assertFalse(payload["instance"]["is_available"])
        self.assertIsNone(payload["instance"]["available_since"])
        self.assertEqual(
            payload["second_id"], payload["instance"]["current_reservation_id"]
        )
        self.assertEqual("active", payload["instance"]["status"])


if __name__ == "__main__":
    unittest.main()

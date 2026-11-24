import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from fastapi import HTTPException

from app.models.reservation import ReservationStatus
from app.routers.internal import (
    connected_agents,
    handle_agent_message,
    pending_cloud_rcon,
    send_correlated_rcon_command,
)
from app.routers.reservations import (
    OwnerCommandRequest,
    _owner_command_reservations,
    get_reservation_commands,
    run_reservation_command,
)
from app.services.owner_commands import (
    MAX_COMMAND_BYTES,
    OwnerCommandAllowlistError,
    OwnerCommandValidationError,
    clear_owner_command_allowlist_cache,
    get_owner_commands,
    parse_owner_command_allowlist,
    parse_plugin_command_result,
    redact_owner_command,
    truncate_command_output,
    validate_owner_command_line,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


VALID_CONFIG = '''
"SummonOwnerCommands"
{
    "mp_timelimit" {}
    "sv_gravity"
    {
    }
}
'''


class OwnerCommandPolicyTests(unittest.TestCase):
    def tearDown(self):
        clear_owner_command_allowlist_cache()

    def test_strict_allowlist_parser_normalizes_names(self):
        self.assertEqual(
            ("mp_timelimit", "sv_gravity"),
            parse_owner_command_allowlist(VALID_CONFIG),
        )

    def test_allowlist_parser_fails_closed(self):
        invalid = (
            "",
            '"WrongRoot" {}',
            '"SummonOwnerCommands" {} trailing',
            '"SummonOwnerCommands" { "bad-name" {} }',
            '"SummonOwnerCommands" { "same" {} "SAME" {} }',
            '"SummonOwnerCommands" { "mp_timelimit" { "value" "x" } }',
            '"SummonOwnerCommands" { "unterminated" { }',
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(OwnerCommandAllowlistError):
                    parse_owner_command_allowlist(source)

    def test_mtime_cache_reloads_valid_and_invalid_replacements(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "commands.cfg"
            path.write_text(VALID_CONFIG, encoding="utf-8")
            first = get_owner_commands(path)
            self.assertEqual(("mp_timelimit", "sv_gravity"), first)

            path.write_text(
                '"SummonOwnerCommands" { "mp_winlimit" {} }\n',
                encoding="utf-8",
            )
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(("mp_winlimit",), get_owner_commands(path))

            path.write_text('"SummonOwnerCommands" {}\n', encoding="utf-8")
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            with self.assertRaises(OwnerCommandAllowlistError):
                get_owner_commands(path)
            # Invalid state is cached fail-closed until the signature changes.
            with self.assertRaises(OwnerCommandAllowlistError):
                get_owner_commands(path)

    def test_command_validation_normalizes_and_enforces_byte_limits(self):
        allowed = ("mp_timelimit", "a" * 63)
        self.assertEqual(
            "MP_TIMELIMIT 30",
            validate_owner_command_line("  MP_TIMELIMIT 30  ", allowed),
        )
        boundary = "mp_timelimit " + "x" * (MAX_COMMAND_BYTES - 13)
        self.assertEqual(MAX_COMMAND_BYTES, len(boundary.encode()))
        self.assertEqual(boundary, validate_owner_command_line(boundary, allowed))

        rejected = (
            "",
            "mp_timelimit; quit",
            "mp_timelimit\nquit",
            "mp_timelimit\x7f",
            "unknown 1",
            "a" * 64,
            boundary + "x",
        )
        for command in rejected:
            with self.subTest(command=command[:80]):
                with self.assertRaises(OwnerCommandValidationError):
                    validate_owner_command_line(command, allowed)

    def test_changelevel_requires_one_safe_map_name(self):
        allowed = ("changelevel",)
        for name in ("cp_process_f12", "cp-process-f12", "koth_proot_b6c-alt2"):
            with self.subTest(name=name):
                self.assertEqual(
                    f"changelevel {name}",
                    validate_owner_command_line(f" changelevel {name} ", allowed),
                )

        rejected = (
            "changelevel",
            "changelevel cp_process_f12 extra",
            "changelevel ../cp_process_f12",
            "changelevel workshop/cp_process_f12",
            "changelevel koth_proot_b6c-alt2.bsp",
            "changelevel " + "a" * 65,
        )
        for command in rejected:
            with self.subTest(command=command):
                with self.assertRaises(OwnerCommandValidationError) as invalid:
                    validate_owner_command_line(command, allowed)
                self.assertEqual("invalid_arguments", invalid.exception.code)

    def test_tv_delay_takes_whole_seconds_up_to_the_engine_limit(self):
        allowed = ("tv_delay",)
        for command in ("tv_delay", "tv_delay 0", "tv_delay 90", "TV_DELAY 120"):
            with self.subTest(command=command):
                self.assertEqual(
                    command, validate_owner_command_line(f" {command} ", allowed)
                )

        rejected = (
            "tv_delay 121",
            "tv_delay 1000",
            "tv_delay -1",
            "tv_delay 30.5",
            "tv_delay abc",
            "tv_delay 0x10",
            'tv_delay "30"',
            "tv_delay 30 60",
        )
        for command in rejected:
            with self.subTest(command=command):
                with self.assertRaises(OwnerCommandValidationError) as invalid:
                    validate_owner_command_line(command, allowed)
                self.assertEqual("invalid_tv_delay", invalid.exception.code)

    def test_exec_takes_one_config_name_outside_the_allowlist(self):
        allowed = ("mp_timelimit",)
        accepted = (
            "exec etf2l_6v6_5cp",
            "EXEC rgl_6s_koth_bo5.cfg",
            "exec etf2l_6v6_5cp.CFG",
            "exec summon_reset",
            "exec " + "a" * 63 + ".cfg",
        )
        for command in accepted:
            with self.subTest(command=command):
                self.assertEqual(
                    command, validate_owner_command_line(f" {command} ", allowed)
                )

        rejected = (
            "exec",
            "exec etf2l_6v6_5cp extra",
            "exec ../server",
            "exec sourcemod/sourcemod",
            "exec etf2l-6v6",
            "exec .cfg",
            "exec etf2l_6v6_5cp.txt",
            'exec "etf2l_6v6_5cp"',
            "exec " + "a" * 64,
        )
        for command in rejected:
            with self.subTest(command=command):
                with self.assertRaises(OwnerCommandValidationError) as invalid:
                    validate_owner_command_line(command, allowed)
                self.assertEqual("invalid_config", invalid.exception.code)

        with self.assertRaises(OwnerCommandValidationError) as invalid:
            validate_owner_command_line("exec etf2l_6v6_5cp; quit", allowed)
        self.assertEqual("unsafe_command", invalid.exception.code)

    def test_checked_in_allowlist_excludes_exec(self):
        self.assertNotIn("exec", get_owner_commands())

    def test_checked_in_allowlist_includes_changelevel(self):
        self.assertIn("changelevel", get_owner_commands())

    def test_checked_in_allowlist_limits_sourcetv_to_delay_and_read_only_details(self):
        # The plugin lets tv_port, tv_password, and tv_status only be read.
        sourcetv = {name for name in get_owner_commands() if name.startswith("tv_")}
        self.assertEqual({"tv_delay", "tv_port", "tv_password", "tv_status"}, sourcetv)

    def test_image_and_plugin_copies_of_the_allowlist_match(self):
        image_copy = (
            PROJECT_ROOT.parent / "tf2-summon" / "sourcemod" / "configs"
            / "summon_owner_commands.cfg"
        )
        if not image_copy.exists():
            self.skipTest("tf2-summon checkout not found next to this repository")
        plugin_copy = (
            PROJECT_ROOT / "tf2-reservation-plugin" / "configs" / "summon_owner_commands.cfg"
        )
        self.assertEqual(plugin_copy.read_text(), image_copy.read_text())

    def test_password_values_are_left_out_of_logged_commands(self):
        for command, logged in (
            ("sv_password hunter2", "sv_password <hidden>"),
            ("SV_PASSWORD   \"two words\"", "SV_PASSWORD <hidden>"),
            ('"sv_logsecret" 12345', '"sv_logsecret" <hidden>'),
            ("tv_password spectate", "tv_password <hidden>"),
            ("rcon_password guess", "rcon_password <hidden>"),
            ("sv_password", "sv_password"),
            ("mp_timelimit 30", "mp_timelimit 30"),
            ("say sv_password hunter2", "say sv_password hunter2"),
        ):
            with self.subTest(command=command):
                self.assertEqual(logged, redact_owner_command(command))

    def test_checked_in_allowlist_keeps_map_server_commands_gated(self):
        # The plugin holds this setting while sv_cheats is on. An owner who
        # could change it would give every player the server console.
        commands = get_owner_commands()
        self.assertIn("sv_cheats", commands)
        self.assertNotIn("sv_allow_point_servercommand", commands)

    def test_checked_in_allowlist_excludes_commands_that_run_other_commands(self):
        forwarding = {
            "alias", "ent_create", "ent_fire", "plugin_load", "rcon",
            "script", "script_execute", "sm", "sm_rcon",
        }
        self.assertEqual(set(), forwarding & set(get_owner_commands()))

    def test_plugin_result_parser_masks_addresses_in_status(self):
        # A command run over RCON prints its output after the plugin's marker,
        # so the plugin cannot mask it.
        result = parse_plugin_command_result(
            "SUMMON_OWNER_COMMAND_RESULT OK\n"
            "hostname: Test 1.2.3\n"
            "udp/ip  : ?.?.?.?:?  (public IP from Steam: 198.51.100.5)\n"
            "sourcetv:  ?.?.?.?:?, delay 90.0s  (local: 10.0.0.2:27020)\n"
            "# userid name                uniqueid            connected ping loss state  adr\n"
            '#      3 "player"            [U:1:12345]         01:02       40    0 active 203.0.113.9:27005'
        )
        self.assertTrue(result.ok)
        for leaked in ("198.51.100.5", "10.0.0.2", "203.0.113.9"):
            self.assertNotIn(leaked, result.output)
        self.assertIn("(public IP from Steam: 0.0.0.0)", result.output)
        self.assertIn("active 0.0.0.0:27005", result.output)
        self.assertIn("hostname: Test 1.2.3", result.output)

        receivers = parse_plugin_command_result(
            "SUMMON_OWNER_COMMAND_RESULT OK\nlogaddress_list: 1 entry\n203.0.113.5:27500"
        )
        self.assertIn("203.0.113.5:27500", receivers.output)

    def test_plugin_result_parser_requires_marker_and_caps_utf8_output(self):
        self.assertIsNone(parse_plugin_command_result("Unknown command"))
        success = parse_plugin_command_result(
            "L 08/13/2026 - 19:46:25: [summon.smx] [summon] Owner command "
            'reservation=16 outcome=ok\r\nSUMMON_OWNER_COMMAND_RESULT OK\r\n'
            '"mp_timelimit" = "30"'
        )
        self.assertTrue(success.ok)
        self.assertEqual('"mp_timelimit" = "30"', success.output)
        failure = parse_plugin_command_result(
            "SUMMON_OWNER_COMMAND_RESULT ERROR cooldown\nWait a moment."
        )
        self.assertFalse(failure.ok)
        self.assertEqual("cooldown", failure.error_code)
        self.assertEqual(4096, len(truncate_command_output("x" * 5000).encode()))
        self.assertLessEqual(
            len(truncate_command_output("€" * 5000).encode("utf-8")), 4096
        )


class CorrelatedCloudRconTests(unittest.IsolatedAsyncioTestCase):
    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def asyncTearDown(self):
        connected_agents.clear()
        for future in pending_cloud_rcon.values():
            if not future.done():
                future.cancel()
        pending_cloud_rcon.clear()

    async def test_out_of_order_results_are_correlated(self):
        socket = self.Socket()
        connected_agents["agent"] = socket
        tasks = [
            asyncio.create_task(
                send_correlated_rcon_command("agent", f"command {index}", timeout=1)
            )
            for index in (1, 2)
        ]
        while len(socket.messages) < 2:
            await asyncio.sleep(0)

        first, second = socket.messages
        await handle_agent_message("agent", {
            "type": "rcon_result",
            "command_id": second["command_id"],
            "output": "second",
        })
        await handle_agent_message("agent", {
            "type": "rcon_result",
            "command_id": "stale-result-id",
            "output": "stale",
        })
        self.assertFalse(tasks[0].done())
        await handle_agent_message("agent", {
            "type": "rcon_result",
            "command_id": first["command_id"],
            "output": "first",
        })
        self.assertEqual(
            [{"output": "first", "error": None}, {"output": "second", "error": None}],
            await asyncio.gather(*tasks),
        )
        self.assertFalse(pending_cloud_rcon)

    async def test_timeout_cleans_pending_request(self):
        connected_agents["agent"] = self.Socket()
        with self.assertRaises(TimeoutError):
            await send_correlated_rcon_command("agent", "status", timeout=0.01)
        self.assertFalse(pending_cloud_rcon)

    async def test_send_failure_cleans_pending_request(self):
        class BrokenSocket:
            async def send_json(self, _message):
                raise ConnectionError("disconnected")

        connected_agents["agent"] = BrokenSocket()
        with self.assertRaises(RuntimeError):
            await send_correlated_rcon_command("agent", "status", timeout=1)
        self.assertFalse(pending_cloud_rcon)
        self.assertNotIn("agent", connected_agents)


class OwnerCommandEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _owner_command_reservations.clear()
        self.user = SimpleNamespace(
            id=7, steam_id="76561198000000000", is_admin=False
        )
        self.reservation = SimpleNamespace(
            id=9,
            user_id=7,
            status=ReservationStatus.ACTIVE,
        )

    def tearDown(self):
        _owner_command_reservations.clear()

    async def test_owner_and_admin_can_list_active_commands(self):
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            owner = await get_reservation_commands(9, user=self.user, db=object())
            admin = await get_reservation_commands(
                9,
                user=SimpleNamespace(
                    id=99, steam_id="76561198000000001", is_admin=True
                ),
                db=object(),
            )
        self.assertIn("mp_timelimit", owner["commands"])
        self.assertEqual(1, owner["commands"].count("exec"))
        self.assertEqual(owner, admin)

    async def test_inactive_and_non_owner_requests_are_rejected(self):
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            with self.assertRaises(HTTPException) as forbidden:
                await get_reservation_commands(
                    9,
                    user=SimpleNamespace(
                        id=8, steam_id="76561198000000002", is_admin=False
                    ),
                    db=object(),
                )
            self.assertEqual(404, forbidden.exception.status_code)

            self.reservation.status = ReservationStatus.ENDED
            with self.assertRaises(HTTPException) as inactive:
                await get_reservation_commands(9, user=self.user, db=object())
            self.assertEqual(400, inactive.exception.status_code)

    async def test_completed_plugin_attempt_returns_structured_result(self):
        transport = {
            "output": "SUMMON_OWNER_COMMAND_RESULT OK\nmp_timelimit = 30",
            "error": None,
        }
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch(
                "app.routers.reservations.awaited_rcon_reservation_runtime",
                new=AsyncMock(return_value=transport),
            ) as execute,
        ):
            response = await run_reservation_command(
                9,
                OwnerCommandRequest(command=" mp_timelimit 30 "),
                user=self.user,
                db=object(),
            )

        self.assertEqual({
            "command": "mp_timelimit 30",
            "ok": True,
            "output": "mp_timelimit = 30",
            "error_code": None,
        }, response)
        execute.assert_awaited_once_with(
            self.reservation,
            ANY,
            "sm_summon_owner_command 76561198000000000 mp_timelimit 30",
            timeout=12.0,
        )

    async def test_exec_is_sent_to_the_plugin_and_bad_names_are_rejected(self):
        transport = {
            "output": "SUMMON_OWNER_COMMAND_RESULT OK\nLoaded config: etf2l_6v6_5cp",
            "error": None,
        }
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch(
                "app.routers.reservations.awaited_rcon_reservation_runtime",
                new=AsyncMock(return_value=transport),
            ) as execute,
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            response = await run_reservation_command(
                9,
                OwnerCommandRequest(command="exec etf2l_6v6_5cp.cfg"),
                user=self.user,
                db=object(),
            )
            with self.assertRaises(HTTPException) as rejected:
                await run_reservation_command(
                    9,
                    OwnerCommandRequest(command="exec ../server"),
                    user=self.user,
                    db=object(),
                )

        self.assertTrue(response["ok"])
        self.assertEqual("Loaded config: etf2l_6v6_5cp", response["output"])
        execute.assert_awaited_once_with(
            self.reservation,
            ANY,
            "sm_summon_owner_command 76561198000000000 exec etf2l_6v6_5cp.cfg",
            timeout=12.0,
        )
        self.assertEqual(400, rejected.exception.status_code)
        self.assertEqual({
            "code": "invalid_config",
            "message": "errors.invalid_config",
        }, rejected.exception.detail)

    async def test_bad_tv_delay_is_rejected_before_reaching_the_plugin(self):
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch(
                "app.routers.reservations.awaited_rcon_reservation_runtime",
                new=AsyncMock(),
            ) as execute,
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            with self.assertRaises(HTTPException) as rejected:
                await run_reservation_command(
                    9,
                    OwnerCommandRequest(command="tv_delay 300"),
                    user=self.user,
                    db=object(),
                )

        execute.assert_not_awaited()
        self.assertEqual(400, rejected.exception.status_code)
        self.assertEqual({
            "code": "invalid_tv_delay",
            "message": "errors.command_invalid_tv_delay",
        }, rejected.exception.detail)

    async def test_plugin_policy_rejection_is_a_completed_attempt(self):
        transport = {
            "output": (
                "SUMMON_OWNER_COMMAND_RESULT ERROR cooldown\n"
                "Please wait before running another server command."
            ),
            "error": None,
        }
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch(
                "app.routers.reservations.awaited_rcon_reservation_runtime",
                new=AsyncMock(return_value=transport),
            ),
        ):
            response = await run_reservation_command(
                9,
                OwnerCommandRequest(command="mp_timelimit"),
                user=self.user,
                db=object(),
            )
        self.assertFalse(response["ok"])
        self.assertEqual("cooldown", response["error_code"])

    async def test_missing_plugin_marker_fails_closed(self):
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch(
                "app.routers.reservations.awaited_rcon_reservation_runtime",
                new=AsyncMock(return_value={
                    "output": "Unknown command sm_summon_owner_command",
                    "error": None,
                }),
            ),
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                await run_reservation_command(
                    9,
                    OwnerCommandRequest(command="mp_timelimit"),
                    user=self.user,
                    db=object(),
                )
        self.assertEqual(503, unavailable.exception.status_code)

    async def test_concurrent_attempt_is_rejected(self):
        _owner_command_reservations.add(9)
        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=self.reservation),
            ),
            patch("app.routers.reservations.t", side_effect=lambda key: key),
        ):
            with self.assertRaises(HTTPException) as conflict:
                await run_reservation_command(
                    9,
                    OwnerCommandRequest(command="mp_timelimit"),
                    user=self.user,
                    db=object(),
                )
        self.assertEqual(409, conflict.exception.status_code)


if __name__ == "__main__":
    unittest.main()

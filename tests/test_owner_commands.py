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
    truncate_command_output,
    validate_owner_command_line,
)


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
        self.assertEqual(
            "changelevel cp_process_f12",
            validate_owner_command_line(" changelevel cp_process_f12 ", allowed),
        )

        rejected = (
            "changelevel",
            "changelevel cp_process_f12 extra",
            "changelevel ../cp_process_f12",
            "changelevel workshop/cp_process_f12",
            "changelevel cp-process-f12",
            "changelevel " + "a" * 65,
        )
        for command in rejected:
            with self.subTest(command=command):
                with self.assertRaises(OwnerCommandValidationError) as invalid:
                    validate_owner_command_line(command, allowed)
                self.assertEqual("invalid_arguments", invalid.exception.code)

    def test_checked_in_allowlist_includes_changelevel(self):
        self.assertIn("changelevel", get_owner_commands())

    def test_plugin_result_parser_requires_marker_and_caps_utf8_output(self):
        self.assertIsNone(parse_plugin_command_result("Unknown command"))
        success = parse_plugin_command_result(
            "SUMMON_OWNER_COMMAND_RESULT OK\r\nvalue = 30"
        )
        self.assertTrue(success.ok)
        self.assertEqual("value = 30", success.output)
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

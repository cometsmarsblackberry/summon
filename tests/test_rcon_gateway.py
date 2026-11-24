"""Restricted RCON for the reservation owner's remote tools, such as lobby sites."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy import func, select

import test_instant_runtime as instant_tests
from app.models.instance import CloudInstance, GameMap
from app.models.reservation import Reservation, ReservationStatus
from app.routers.reservations import (
    CreateReservationRequest,
    create_reservation_endpoint,
    reservation_to_response,
)
from app.routers.status import _build_status
from app.services.gcore import DIRECT_CONNECT_SG_NAME, RCON_SG_NAME, GcoreClient
from app.services.orchestrator import build_reservation_config, get_warm_instance
from app.services.reservation import create_reservation
from app.services.runtime import (
    claim_instant_slot,
    count_available_instant_slots,
    get_location_runtime_capacity,
    host_supports_rcon_gateway,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def create(enable_direct_connect: bool) -> Reservation:
    user = SimpleNamespace(id=7, steam_id="76561198000000000", reservation_count=0)
    db = SimpleNamespace(add=Mock(), commit=AsyncMock(), refresh=AsyncMock())
    with (
        patch(
            "app.services.settings.get_reservation_settings",
            new=AsyncMock(return_value={"max_duration_hours": 4}),
        ),
        patch(
            "app.services.reservation.get_next_reservation_number",
            new=AsyncMock(return_value=42),
        ),
    ):
        return await create_reservation(
            user=user,
            location="test",
            duration_hours=4,
            first_map="cp_badlands",
            enable_direct_connect=enable_direct_connect,
            db=db,
        )


class OwnerRconPasswordTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_connect_reservation_gets_its_own_rcon_password(self):
        reservation = await create(enable_direct_connect=True)

        self.assertTrue(reservation.enable_rcon)
        self.assertEqual(12, len(reservation.owner_rcon_password))
        self.assertTrue(reservation.owner_rcon_password.isalnum())
        self.assertNotEqual(reservation.rcon_password, reservation.owner_rcon_password)

    async def test_sdr_reservation_has_no_rcon(self):
        reservation = await create(enable_direct_connect=False)

        self.assertFalse(reservation.enable_rcon)
        self.assertIsNone(reservation.owner_rcon_password)

    def test_agent_config_carries_only_the_owner_password_for_the_gateway(self):
        now = datetime.now(timezone.utc)
        reservation = SimpleNamespace(
            id=42, reservation_number=42, location="test",
            password="server-password", rcon_password="server-rcon",
            tv_password="tv-password", first_map="cp_badlands", config_file=None,
            logsecret="log-secret", ends_at=now + timedelta(hours=4),
            plugin_api_key="plugin-key", enable_direct_connect=True,
            enable_rcon=True, owner_rcon_password="owner-rcon-password",
            enable_logs_tf_upload=True, enable_demos_tf_upload=True,
            motd_token="motd-token",
        )

        config = build_reservation_config(reservation)
        self.assertTrue(config["enable_rcon"])
        self.assertEqual("owner-rcon-password", config["owner_rcon_password"])

        # Without a password the agent must not open the gateway at all.
        reservation.owner_rcon_password = None
        config = build_reservation_config(reservation)
        self.assertFalse(config["enable_rcon"])
        self.assertEqual("", config["owner_rcon_password"])

    def test_only_the_owner_view_shows_the_owner_password(self):
        now = datetime.now(timezone.utc)
        reservation = Reservation(
            id=5, reservation_number=5, location="test", starts_at=now,
            ends_at=now + timedelta(hours=1), password="join", rcon_password="server-rcon",
            tv_password="tv", first_map="cp_badlands", motd_token="motd",
            logsecret="log", plugin_api_key="plugin", status=ReservationStatus.ACTIVE,
            enable_direct_connect=True, enable_rcon=True,
            owner_rcon_password="owner-rcon-password", created_at=now,
            enable_logs_tf_upload=True, enable_demos_tf_upload=True,
        )

        public = reservation_to_response(reservation)
        self.assertTrue(public.enable_rcon)
        self.assertIsNone(public.owner_rcon_password)

        owner = reservation_to_response(reservation, include_secrets=True)
        self.assertEqual("owner-rcon-password", owner.owner_rcon_password)
        self.assertNotIn("server-rcon", owner.model_dump_json())


class RconCapabilityTests(unittest.TestCase):
    def test_capability_report_formats(self):
        self.assertTrue(host_supports_rcon_gateway({"rcon_gateway": True, "rcon": True}))
        self.assertTrue(host_supports_rcon_gateway(["rcon", "rcon_gateway"]))
        self.assertFalse(host_supports_rcon_gateway({"rcon": True}))
        self.assertFalse(host_supports_rcon_gateway({"rcon_gateway": "yes"}))
        self.assertFalse(host_supports_rcon_gateway(None))


class RconInstantTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = instant_tests.InstantRuntimeTests.asyncSetUp
    asyncTearDown = instant_tests.InstantRuntimeTests.asyncTearDown
    reservation = staticmethod(instant_tests.InstantRuntimeTests.reservation)
    add_ready_host = instant_tests.InstantRuntimeTests.add_ready_host

    async def test_direct_connect_only_lands_on_a_host_running_the_gateway(self):
        async with self.sessions() as db:
            old = await self.add_ready_host(db, "203.0.113.81", "old-agent")
            old.capabilities = {"rcon": True}
            capable = await self.add_ready_host(db, "203.0.113.82", "gateway")
            capable.capabilities = {"rcon": True, "rcon_gateway": True}
            hidden = await self.add_ready_host(db, "203.0.113.83", "sdr-only-gateway")
            hidden.capabilities = {"rcon_gateway": True}
            hidden.sdr_only = True
            await db.commit()

            self.assertEqual(3, await count_available_instant_slots("instant-only", db))
            self.assertEqual(1, await count_available_instant_slots(
                "instant-only", db, direct_connect=True
            ))
            self.assertTrue((await get_location_runtime_capacity(
                "instant-only", db
            )).direct_connect_available)
            self.assertTrue((await _build_status(db))["instant-only"]["direct_connect_available"])

            first = self.reservation(381)
            first.enable_direct_connect = True
            first.enable_rcon = True
            first.owner_rcon_password = "owner-rcon-password"
            db.add(first)
            await db.commit()
            assignment = await claim_instant_slot(first, db)
            self.assertEqual(capable.id, assignment.slot.host_id)

            # Instant capacity is left on hosts without the gateway, so direct
            # connect is refused rather than sent to cloud.
            capacity = await get_location_runtime_capacity("instant-only", db)
            self.assertTrue(capacity.instant_available)
            self.assertFalse(capacity.direct_connect_available)
            self.assertFalse((await _build_status(db))["instant-only"]["direct_connect_available"])

            second = self.reservation(382)
            second.enable_direct_connect = True
            sdr = self.reservation(383)
            db.add_all([second, sdr])
            await db.commit()
            self.assertIsNone(await claim_instant_slot(second, db))
            self.assertIn(
                (await claim_instant_slot(sdr, db)).slot.host_id, {old.id, hidden.id},
            )

    async def test_direct_connect_is_refused_without_a_gateway_host(self):
        async with self.sessions() as db:
            host = await self.add_ready_host(db, "203.0.113.84", "old-agent-only")
            host.capabilities = {"rcon": True}
            db.add(GameMap(name="cp_badlands", display_name="cp_badlands", enabled=True))
            await db.commit()

            user = SimpleNamespace(id=1, is_banned=False, is_admin=False)
            background = SimpleNamespace(add_task=lambda *args, **kwargs: None)
            with (
                patch("app.services.steam_trust.check_steam_trust", new=AsyncMock()),
                patch("app.services.captcha.requires_captcha", new=AsyncMock(return_value=False)),
                self.assertRaises(HTTPException) as raised,
            ):
                await create_reservation_endpoint(
                    CreateReservationRequest(location="instant-only", enable_direct_connect=True),
                    background,
                    user=user,
                    db=db,
                )

            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("Direct connections", raised.exception.detail)
            self.assertEqual(0, await db.scalar(select(func.count(Reservation.id))))


class RconWarmPoolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = instant_tests.InstantRuntimeTests.asyncSetUp
    asyncTearDown = instant_tests.InstantRuntimeTests.asyncTearDown

    async def test_warm_server_with_an_old_agent_is_not_reused_for_rcon(self):
        later = datetime.utcnow() + timedelta(minutes=40)
        async with self.sessions() as db:
            db.add_all([
                CloudInstance(
                    id="old", instance_id="agent-old", location="instant-only",
                    auth_token="a", is_available=True, billing_hour_ends_at=later,
                ),
                CloudInstance(
                    id="new", instance_id="agent-new", location="instant-only",
                    auth_token="b", is_available=True,
                    billing_hour_ends_at=later - timedelta(minutes=10),
                ),
            ])
            await db.commit()

            old_socket, new_socket = object(), object()
            with (
                patch.dict("app.routers.internal.connected_agents",
                           {"agent-old": old_socket, "agent-new": new_socket}),
                patch.dict("app.routers.internal.agent_features", {
                    id(old_socket): frozenset({"boot-handover"}),
                    id(new_socket): frozenset({"boot-handover", "rcon-gateway"}),
                }),
            ):
                self.assertEqual("old", (await get_warm_instance("instant-only", db)).id)
                self.assertEqual(
                    "new", (await get_warm_instance("instant-only", db, require_rcon=True)).id
                )


class RconSecurityGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_rcon_group_opens_only_the_tcp_game_port(self):
        client = GcoreClient(api_key="key", project_id=1)
        posts = []

        async def request(method, endpoint, **kwargs):
            if method == "GET":
                return {"results": []}
            posts.append((endpoint, kwargs.get("json")))
            if endpoint.endswith("/rules"):
                return {}
            return {"id": f"sg-{len(posts)}"}

        client._request = AsyncMock(side_effect=request)
        rcon_group = await client.ensure_rcon_security_group(152)
        self.assertEqual(rcon_group, await client.ensure_rcon_security_group(152))

        created = posts[0][1]["security_group"]
        self.assertEqual(RCON_SG_NAME, created["name"])
        rules = [body for endpoint, body in posts if endpoint.endswith("/rules")]
        self.assertEqual(1, len(rules))
        self.assertEqual(("tcp", 27015, 27015), (
            rules[0]["protocol"], rules[0]["port_range_min"], rules[0]["port_range_max"],
        ))

        # The direct-connect group is cached separately.
        direct_group = await client.ensure_direct_connect_security_group(152)
        self.assertNotEqual(rcon_group, direct_group)
        self.assertEqual(DIRECT_CONNECT_SG_NAME, posts[2][1]["security_group"]["name"])


class SharedAllowlistTests(unittest.TestCase):
    """RCON uses the owner allowlist: a lobby site holds the owner's password,
    so it gets exactly what the owner gets on every other route."""

    def test_allowlist_has_what_lobby_sites_use(self):
        from app.services.owner_commands import get_owner_commands

        commands = set(get_owner_commands())
        for needed in (
            "hostname", "maxplayers", "status", "users", "kickid", "kickall",
            "say", "sv_password", "sv_tags",
            "log", "sv_logsecret", "logaddress_add", "logaddress_del",
            "logaddress_delall", "logaddress_list", "tv_port", "tv_password",
            "tv_status", "sm_whitelist_id", "mp_tournament", "changelevel",
        ):
            self.assertIn(needed, commands)

    def test_allowlist_has_no_unrestricted_command(self):
        from app.services.owner_commands import get_owner_commands

        commands = set(get_owner_commands())
        for dangerous in (
            "rcon_password", "exec", "sm", "sm_rcon", "quit", "alias",
            "sv_allow_point_servercommand", "plugin_load", "meta",
            "sv_downloadurl", "logstf_apikey", "sm_demostf_apikey", "kick",
            "banid", "addip", "writeip", "listip",
        ):
            self.assertNotIn(dangerous, commands)

    def test_no_separate_rcon_allowlist_remains(self):
        configs = PROJECT_ROOT / "tf2-reservation-plugin" / "configs"
        self.assertFalse((configs / "summon_rcon_commands.cfg").exists())


if __name__ == "__main__":
    unittest.main()

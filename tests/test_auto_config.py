import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Reservation, ReservationStatus
from app.models.instance import EnabledLocation, GameMap
from app.models.upload_link import UploadLink  # noqa: F401 - registers relationship
from app.models.user import User
from app.routers import internal, reservations
from app.services.competitive_configs import (
    AUTO_LEAGUES,
    auto_config_command,
    auto_config_league,
    classify_config,
    startup_config_file,
)
from app.services.orchestrator import build_reservation_config
from app.services.reservation import create_reservation
from tests import test_instant_runtime as instant_tests

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "tf2-reservation-plugin" / "scripting" / "summon.sp"
)
USER = SimpleNamespace(id=7, is_admin=False)


def plugin_table(name: str) -> list[tuple[str, ...]]:
    source = PLUGIN.read_text(encoding="utf-8")
    body = source[source.index(f"{name}[][][] = {{"):]
    body = body[:body.index("};")]
    return [
        tuple(re.findall(r'"([^"]*)"', row))
        for row in re.findall(r"\{([^{}]*)\}", body)
    ]


class AutoConfigLeagueTests(unittest.TestCase):
    def test_league_follows_the_region(self):
        cases = [
            ("Germany", "Europe", "etf2l"),
            ("United Kingdom", "Europe", "etf2l"),
            # Players on these servers ran only ETF2L configs.
            ("Kazakhstan", "Asia", "etf2l"),
            ("Turkey", "Asia", "etf2l"),
            ("United States", "North America", "rgl"),
            ("Canada", "North America", "rgl"),
            # Honolulu is listed under Oceania.
            ("United States", "Oceania", "rgl"),
            ("Brazil", "South America", "rgl"),
            ("Australia", "Australia", "ozfortress"),
            ("New Zealand", "Oceania", "ozfortress"),
            ("Japan", "Asia", "etf2l"),
            (None, None, "etf2l"),
        ]
        for country, continent, league in cases:
            with self.subTest(country=country, continent=continent):
                self.assertEqual(league, auto_config_league(country, continent))

    def test_command_names_the_league(self):
        self.assertEqual("auto_rgl", auto_config_command("rgl"))
        self.assertEqual("auto_etf2l", auto_config_command(None))
        self.assertEqual("auto_etf2l", auto_config_command("unknown"))

    def test_startup_config_is_the_auto_command_while_auto_is_on(self):
        auto = SimpleNamespace(
            auto_config=True, auto_config_league="ozfortress", config_file="ozfortress_6v6_koth",
        )
        manual = SimpleNamespace(
            auto_config=False, auto_config_league="ozfortress", config_file="ozfortress_6v6_koth",
        )
        none = SimpleNamespace(auto_config=False, auto_config_league="etf2l", config_file=None)

        self.assertEqual("auto_ozfortress", startup_config_file(auto))
        self.assertEqual("ozfortress_6v6_koth", startup_config_file(manual))
        self.assertEqual("", startup_config_file(none))

    def test_agent_config_carries_the_auto_command(self):
        now = datetime.now(timezone.utc)
        reservation = SimpleNamespace(
            id=42, reservation_number=42, location="test",
            password="pw", rcon_password="rcon", tv_password="tv",
            first_map="koth_product_final", config_file=None,
            auto_config=True, auto_config_league="rgl",
            logsecret="log", ends_at=now + timedelta(hours=4), plugin_api_key="key",
            enable_direct_connect=False, enable_logs_tf_upload=True,
            enable_demos_tf_upload=True, motd_token="motd",
        )

        self.assertEqual("auto_rgl", build_reservation_config(reservation)["config_file"])


class PluginAutoConfigTableTests(unittest.TestCase):
    def test_plugin_leagues_match_the_backend(self):
        names = dict(plugin_table("g_sAutoLeagueNames"))
        self.assertEqual(AUTO_LEAGUES, names)

    def test_plugin_configs_belong_to_their_league_and_format(self):
        formats = {"6v6": "6v6", "hl": "Highlander"}
        rows = plugin_table("g_sAutoConfigs")
        self.assertTrue(rows)
        for league, kind, cfg_file in rows:
            with self.subTest(cfg_file=cfg_file):
                self.assertIn(league, AUTO_LEAGUES)
                config_league, config_format = classify_config(cfg_file)
                self.assertEqual(AUTO_LEAGUES[league], config_league)
                prefix = kind.split("_", 1)[0]
                if prefix in formats:
                    self.assertEqual(formats[prefix], config_format)

    def test_every_league_has_6v6_and_highlander_configs(self):
        kinds = {(league, kind) for league, kind, _ in plugin_table("g_sAutoConfigs")}
        for league in AUTO_LEAGUES:
            for kind in ("6v6_cp", "6v6_koth", "hl_cp", "hl_koth", "hl_stopwatch"):
                with self.subTest(league=league, kind=kind):
                    self.assertIn((league, kind), kinds)


class CreateAutoReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_stores_auto_instead_of_a_config(self):
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
            reservation = await create_reservation(
                user=user, location="test", duration_hours=4, first_map="cp_badlands",
                config_file="etf2l_6v6_5cp", auto_config=True, auto_config_league="etf2l",
                db=db,
            )

        self.assertTrue(reservation.auto_config)
        self.assertEqual("etf2l", reservation.auto_config_league)
        self.assertIsNone(reservation.config_file)


class CreateAutoReservationEndpointTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = instant_tests.InstantRuntimeTests.asyncSetUp
    asyncTearDown = instant_tests.InstantRuntimeTests.asyncTearDown

    async def create(self, config_file):
        async with self.sessions() as db:
            location = await db.get(EnabledLocation, "instant-only")
            location.country = "United States"
            location.continent = "North America"
            await instant_tests.InstantRuntimeTests.add_ready_host(
                self, db, "203.0.113.90", f"auto-{config_file}",
            )
            db.add(GameMap(name="cp_badlands", display_name="cp_badlands", enabled=True))
            user = User(id=1, steam_id="76561198000000001", display_name="Owner")
            db.add(user)
            await db.commit()

            background = SimpleNamespace(add_task=lambda *args, **kwargs: None)
            with (
                patch("app.services.steam_trust.check_steam_trust", new=AsyncMock()),
                patch("app.services.captcha.requires_captcha", new=AsyncMock(return_value=False)),
            ):
                response = await reservations.create_reservation_endpoint(
                    reservations.CreateReservationRequest(
                        location="instant-only", config_file=config_file,
                    ),
                    background,
                    user=user,
                    db=db,
                )
            reservation = await db.scalar(select(Reservation))
            return response, reservation

    async def test_auto_is_stored_with_the_region_league(self):
        response, reservation = await self.create("auto")

        self.assertTrue(response.auto_config)
        self.assertTrue(reservation.auto_config)
        self.assertEqual("rgl", reservation.auto_config_league)
        self.assertIsNone(reservation.config_file)
        self.assertEqual("auto_rgl", startup_config_file(reservation))

    async def test_a_chosen_config_keeps_auto_off_but_stores_the_league(self):
        _, reservation = await self.create(None)

        self.assertFalse(reservation.auto_config)
        self.assertEqual("rgl", reservation.auto_config_league)


class ActiveAutoConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "auto-config.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.now(timezone.utc)
        async with self.sessions() as db:
            db.add(EnabledLocation(
                code="vultr-syd", name="Sydney", city="Sydney",
                country="Australia", continent="Australia", enabled=True,
            ))
            # Made before Auto existed, so no league is stored.
            db.add(Reservation(
                id=301, reservation_number=301, user_id=USER.id, location="vultr-syd",
                starts_at=now, ends_at=now + timedelta(hours=2),
                first_map="koth_product_final", current_map="koth_product_final",
                config_file="ozfortress_6v6_5cp",
                password="pw", rcon_password="rcon", tv_password="tv",
                motd_token="motd-301", logsecret="log", plugin_api_key="key",
                status=ReservationStatus.ACTIVE,
            ))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def exec_config(self, cfg_file):
        rcon = AsyncMock(return_value=True)
        async with self.sessions() as db:
            with (
                patch.object(reservations, "rcon_reservation_runtime", rcon),
                patch.object(
                    reservations, "get_runtime_competitive_configs",
                    AsyncMock(return_value={"exec_cfg_files": ["ozfortress_6v6_koth"]}),
                ),
            ):
                response = await reservations.exec_competitive_config(
                    301, reservations.ExecConfigRequest(cfg_file=cfg_file), user=USER, db=db,
                )
        return response, rcon

    async def stored(self):
        async with self.sessions() as db:
            return await db.get(Reservation, 301)

    async def report(self, **fields):
        body = internal.PlayerUpdateRequest(player_count=1, **fields)
        with (
            patch.object(internal, "async_session_maker", self.sessions),
            patch.object(internal, "validate_reservation_api_key", AsyncMock(return_value=True)),
        ):
            return await internal.update_players(301, body, x_api_key="key")

    async def test_turning_auto_on_sends_the_league_from_the_location(self):
        response, rcon = await self.exec_config("auto")

        self.assertEqual("auto", response["cfg_file"])
        self.assertEqual("sm_config auto_ozfortress", rcon.await_args.args[2])
        reservation = await self.stored()
        self.assertTrue(reservation.auto_config)
        self.assertEqual("ozfortress", reservation.auto_config_league)
        # The plugin reports the config Auto picks.
        self.assertEqual("ozfortress_6v6_5cp", reservation.config_file)

    async def test_another_config_turns_auto_off(self):
        await self.exec_config("auto")
        _, rcon = await self.exec_config("ozfortress_6v6_koth")

        self.assertEqual("sm_config ozfortress_6v6_koth", rcon.await_args.args[2])
        reservation = await self.stored()
        self.assertFalse(reservation.auto_config)
        self.assertEqual("ozfortress_6v6_koth", reservation.config_file)

    async def test_plugin_reports_auto_and_is_told_the_league(self):
        response = await self.report(config_file="ozfortress_6v6_koth", auto_config=True)

        self.assertEqual("ozfortress", response.auto_league)
        reservation = await self.stored()
        self.assertTrue(reservation.auto_config)
        self.assertEqual("ozfortress_6v6_koth", reservation.config_file)
        self.assertEqual("ozfortress", reservation.auto_config_league)

        # Loading another config in game turns Auto off.
        await self.report(config_file="ozfortress_hl_koth", auto_config=False)
        self.assertFalse((await self.stored()).auto_config)

    async def test_older_plugins_leave_auto_unchanged(self):
        await self.exec_config("auto")
        await self.report(config_file="ozfortress_6v6_koth")

        self.assertTrue((await self.stored()).auto_config)


class AutoConfigPageTests(unittest.TestCase):
    def test_pages_offer_auto(self):
        root = Path(__file__).resolve().parents[1] / "templates"
        home = (root / "home.html").read_text(encoding="utf-8")
        status = (root / "status.html").read_text(encoding="utf-8")

        self.assertIn("selectHomeConfig({ cfg_file: 'auto' })", home)
        self.assertIn("selectPendingConfig({ cfg_file: 'auto' })", status)
        # Polling follows Auto being turned off in game.
        self.assertIn("this.autoConfig = !!data.auto_config;", status)


if __name__ == "__main__":
    unittest.main()

"""Credential-free local preview entrypoint.

This module is deliberately separate from ``app.main``. Production starts
``app.main:app`` and therefore never registers the local login route or loads
the preview fixtures below.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlparse


if os.environ.get("SUMMON_PREVIEW") != "1":
    raise RuntimeError(
        "The preview entrypoint is disabled. Set SUMMON_PREVIEW=1 and run "
        "app.preview:app only in a local environment."
    )


# These defaults must be applied before importing the application because its
# settings and database engine are initialized at import time. The preview
# Compose file supplies the same values explicitly; the defaults also make the
# entrypoint convenient to run directly inside a development container.
_PREVIEW_DEFAULTS = {
    "DATABASE_URL": "sqlite+aiosqlite:////data/preview.db",
    "SECRET_KEY": "summon-local-preview-only",
    "BASE_URL": "http://127.0.0.1:8000",
    "BETA_MODE": "false",
    "LOG_DIR": "/data/logs",
    "SITE_NAME": "Summon Preview",
}
for _key, _value in _PREVIEW_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

# Never inherit real integration credentials from a developer's shell or
# project .env file. The fake Onidel values only satisfy the UI availability
# gate; preview locations use an unconfigured Vultr client.
os.environ.update(
    {
        "VULTR_API_KEY": "",
        "GCORE_API_KEY": "",
        "GCORE_PROJECT_ID": "0",
        "ONIDEL_API_KEY": "preview-not-a-real-key",
        "ONIDEL_TEAM_ID": "1",
        "STEAM_API_KEY": "",
        "HCAPTCHA_SITE_KEY": "",
        "HCAPTCHA_SECRET_KEY": "",
        "ADMIN_STEAM_IDS": "",
        "BETA_ALLOWLIST_STEAM_IDS": "",
    }
)

_preview_host = urlparse(os.environ["BASE_URL"]).hostname
if _preview_host not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("The preview BASE_URL must use a loopback hostname.")
if not (
    os.environ["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    and os.environ["DATABASE_URL"].endswith("/preview.db")
):
    raise RuntimeError(
        "The preview DATABASE_URL must point to a SQLite preview.db file."
    )


from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.database import async_session_maker
from app.models.instance import EnabledLocation, GameMap, LocationProvider, Provider
from app.models.user import User
from app.routers.auth import _session_serializer
from app.routers.internal import competitive_configs
from app.services.competitive_configs import filter_user_selectable


_PREVIEW_STEAM_ID = "76561198000000000"

_PREVIEW_LOCATIONS = (
    {
        "code": "helsinki",
        "name": "Helsinki",
        "provider_region": "preview-helsinki",
        "city": "Helsinki",
        "country": "Finland",
        "continent": "Europe",
        "recommended": True,
        "display_order": 1,
    },
    {
        "code": "chicago",
        "name": "Chicago",
        "provider_region": "preview-chicago",
        "city": "Chicago",
        "country": "United States",
        "continent": "North America",
        "subdivision": "Illinois",
        "recommended": False,
        "display_order": 2,
    },
    {
        "code": "singapore",
        "name": "Singapore",
        "provider_region": "preview-singapore",
        "city": "Singapore",
        "country": "Singapore",
        "continent": "Asia",
        "recommended": True,
        "display_order": 3,
    },
)

_PREVIEW_MAPS = (
    ("cp_process_f12", "Process"),
    ("cp_snakewater_final1", "Snakewater"),
    ("koth_product_final", "Product"),
    ("pl_upward", "Upward"),
)

_PREVIEW_CONFIGS = (
    "etf2l_6v6_5cp",
    "etf2l_6v6_koth",
    "etf2l_9v9_5cp",
    "fbtf_6v6_5cp",
    "ozfortress_6v6_5cp",
    "rgl_6s_5cp_match_pro",
    "rgl_6s_koth_match_pro",
    "rgl_HL_koth",
)


async def _seed_preview_data(db: AsyncSession) -> User:
    """Create deterministic data used by the local UI preview."""
    provider = await db.get(Provider, "vultr")
    if provider is None:
        provider = Provider(
            code="vultr",
            name="Vultr (preview)",
            billing_model="hourly",
            enabled=True,
            display_order=1,
            instance_limit=10,
        )
        db.add(provider)
    else:
        provider.enabled = True
        provider.instance_limit = 10

    for values in _PREVIEW_LOCATIONS:
        location = await db.get(EnabledLocation, values["code"])
        if location is None:
            location = EnabledLocation(
                code=values["code"],
                name=values["name"],
                provider="vultr",
                provider_region=values["provider_region"],
            )
            db.add(location)

        location.name = values["name"]
        location.provider = "vultr"
        location.provider_region = values["provider_region"]
        location.vultr_region = values["provider_region"]
        location.billing_model = "hourly"
        location.city = values["city"]
        location.country = values["country"]
        location.continent = values["continent"]
        location.subdivision = values.get("subdivision")
        location.recommended = values["recommended"]
        location.enabled = True
        location.display_order = values["display_order"]

        mapping_result = await db.execute(
            select(LocationProvider).where(
                LocationProvider.location_code == values["code"],
                LocationProvider.provider_code == "vultr",
            )
        )
        mapping = mapping_result.scalar_one_or_none()
        if mapping is None:
            mapping = LocationProvider(
                location_code=values["code"],
                provider_code="vultr",
                provider_region=values["provider_region"],
            )
            db.add(mapping)
        mapping.provider_region = values["provider_region"]
        mapping.priority = 0
        mapping.enabled = True

    for display_order, (name, display_name) in enumerate(_PREVIEW_MAPS, start=2):
        map_result = await db.execute(select(GameMap).where(GameMap.name == name))
        game_map = map_result.scalar_one_or_none()
        if game_map is None:
            game_map = GameMap(name=name, display_name=display_name)
            db.add(game_map)
        game_map.display_name = display_name
        game_map.enabled = True
        game_map.is_default = False
        game_map.display_order = display_order

    user_result = await db.execute(
        select(User).where(User.steam_id == _PREVIEW_STEAM_ID)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        user = User(
            steam_id=_PREVIEW_STEAM_ID,
            display_name="UI Tester",
            avatar_url="",
            is_banned=False,
            is_admin=False,
            reservation_count=0,
        )
        db.add(user)
    else:
        user.display_name = "UI Tester"
        user.is_banned = False
        user.ban_reason = None
        user.deleted_at = None

    await db.commit()
    await db.refresh(user)

    cfg_files = sorted(_PREVIEW_CONFIGS)
    competitive_configs["preview-agent"] = {
        "cfg_files": cfg_files,
        "exec_cfg_files": sorted(
            set(filter_user_selectable(cfg_files) + ["summon_reset"])
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "container_image": "summon-preview",
    }
    return user


@app.get("/__dev/login", include_in_schema=False)
async def preview_login():
    """Seed the disposable preview database and sign in the fixture user."""
    async with async_session_maker() as db:
        user = await _seed_preview_data(db)

    token = _session_serializer().dumps(
        {"user_id": user.id, "steam_id": user.steam_id}
    )
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=False,
        max_age=60 * 60 * 24,
        samesite="lax",
    )
    return response

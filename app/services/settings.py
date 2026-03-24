"""Helpers for reading and writing site settings from the database."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SiteSetting
from app.config import get_settings


async def get_setting(key: str, db: AsyncSession, default: str = "") -> str:
    result = await db.execute(select(SiteSetting).where(SiteSetting.key == key))
    s = result.scalar_one_or_none()
    return s.value if s else default


async def set_setting(key: str, value: str, db: AsyncSession) -> None:
    result = await db.execute(select(SiteSetting).where(SiteSetting.key == key))
    s = result.scalar_one_or_none()
    if s:
        s.value = value
    else:
        db.add(SiteSetting(key=key, value=value))
    await db.commit()


async def get_rate_limit_settings(db: AsyncSession) -> dict:
    """Return current rate limit values, falling back to config defaults."""
    cfg = get_settings()
    return {
        "per_user_hour": int(await get_setting(
            "rate_limit_per_user_hour", db, str(cfg.rate_limit_per_user_hour)
        )),
        "admin_per_hour": int(await get_setting(
            "rate_limit_admin_per_hour", db, str(cfg.rate_limit_admin_per_hour)
        )),
        "failed_multiplier": int(await get_setting(
            "rate_limit_failed_multiplier", db, str(cfg.rate_limit_failed_multiplier)
        )),
        "site_provisioning_max": int(await get_setting(
            "rate_limit_site_provisioning_max", db, str(cfg.rate_limit_site_provisioning_max)
        )),
        "per_user_day": int(await get_setting(
            "rate_limit_per_user_day", db, str(cfg.rate_limit_per_user_day)
        )),
        "admin_per_day": int(await get_setting(
            "rate_limit_admin_per_day", db, str(cfg.rate_limit_admin_per_day)
        )),
        "daily_hours_limit": int(await get_setting(
            "daily_hours_limit", db, str(cfg.daily_hours_limit)
        )),
        "sitewide_per_hour": int(await get_setting(
            "rate_limit_sitewide_per_hour", db, str(cfg.rate_limit_sitewide_per_hour)
        )),
        "sitewide_per_day": int(await get_setting(
            "rate_limit_sitewide_per_day", db, str(cfg.rate_limit_sitewide_per_day)
        )),
        "circuit_breaker_window_minutes": int(await get_setting(
            "circuit_breaker_window_minutes", db, str(cfg.circuit_breaker_window_minutes)
        )),
        "circuit_breaker_threshold": int(await get_setting(
            "circuit_breaker_threshold", db, str(cfg.circuit_breaker_threshold)
        )),
        "circuit_breaker_cooldown_minutes": int(await get_setting(
            "circuit_breaker_cooldown_minutes", db, str(cfg.circuit_breaker_cooldown_minutes)
        )),
    }


async def get_steam_trust_settings(db: AsyncSession) -> dict:
    """Return current Steam trust check settings."""
    return {
        "min_account_age_days": int(await get_setting("steam_min_account_age_days", db, "0")),
        "min_tf2_hours": int(await get_setting("steam_min_tf2_hours", db, "0")),
        "require_tf2_ownership": (await get_setting("steam_require_tf2_ownership", db, "false")) == "true",
        "block_vac_banned": (await get_setting("steam_block_vac_banned", db, "false")) == "true",
        "require_public_profile": (await get_setting("steam_require_public_profile", db, "false")) == "true",
    }


async def get_reservation_settings(db: AsyncSession) -> dict:
    """Return current reservation duration/auto-end settings."""
    cfg = get_settings()
    return {
        "max_duration_hours": int(await get_setting(
            "max_duration_hours", db, str(cfg.max_duration_hours)
        )),
        "auto_end_minutes": int(await get_setting(
            "auto_end_minutes", db, str(cfg.auto_end_minutes)
        )),
    }


async def get_fastdl_url(db: AsyncSession) -> str:
    """Return the FastDL URL, falling back to config default."""
    cfg = get_settings()
    return await get_setting("fastdl_url", db, cfg.fastdl_url)

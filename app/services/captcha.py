"""Conditional hCaptcha verification for reservation creation."""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.reservation import Reservation, ReservationStatus
from app.models.user import User

logger = logging.getLogger(__name__)


async def get_captcha_settings(db: AsyncSession) -> dict:
    """Return current captcha settings, falling back to defaults."""
    from app.services.settings import get_setting

    return {
        "enabled": (await get_setting("captcha_enabled", db, "true")) == "true",
        "trust_after_n": int(await get_setting("captcha_trust_after_n", db, "3")),
        "min_tf2_hours": int(await get_setting("captcha_min_tf2_hours", db, "50")),
        "min_account_age_days": int(await get_setting("captcha_min_account_age_days", db, "180")),
    }


async def requires_captcha(user: User, db: AsyncSession) -> bool:
    """Check whether the user must solve a captcha for their next reservation.

    Returns False (skip captcha) when:
    - hCaptcha is not configured (no site key / secret)
    - Captcha is disabled in admin settings
    - User is an admin

    Returns True (require captcha) when any of:
    - User has fewer reservations than the trust threshold
    - User's TF2 playtime is below the minimum
    - User's Steam account is younger than the minimum age
    - User had a no-show in the last 24 hours
    """
    settings = get_settings()
    if not settings.hcaptcha_site_key or not settings.hcaptcha_secret_key:
        return False

    if user.is_admin:
        return False

    captcha_cfg = await get_captcha_settings(db)
    if not captcha_cfg["enabled"]:
        return False

    # New user (few reservations)
    if user.reservation_count < captcha_cfg["trust_after_n"]:
        return True

    # Low TF2 playtime
    min_hours = captcha_cfg["min_tf2_hours"]
    if min_hours > 0 and user.tf2_playtime_hours is not None and user.tf2_playtime_hours < min_hours:
        return True

    # Young Steam account
    min_age = captcha_cfg["min_account_age_days"]
    if min_age > 0 and user.steam_account_created_at is not None:
        created = user.steam_account_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days < min_age:
            return True

    # Recent no-show
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.user_id == user.id)
        .where(Reservation.status == ReservationStatus.NO_SHOW)
        .where(Reservation.created_at >= cutoff)
    )
    if result.scalar_one() > 0:
        return True

    return False


async def verify_captcha(token: str) -> bool:
    """Verify an hCaptcha response token with the hCaptcha API."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.hcaptcha.com/siteverify",
                data={
                    "secret": settings.hcaptcha_secret_key,
                    "response": token,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            success = result.get("success", False)
            if not success:
                logger.warning(f"hCaptcha verification failed: {result.get('error-codes', [])}")
            return success
    except Exception:
        logger.exception("hCaptcha verification request failed")
        return False

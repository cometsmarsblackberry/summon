"""Steam OpenID authentication."""

import asyncio
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.i18n import t
from app.models.user import User
from app.services.steam_http import create_steam_async_client


logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])
settings = get_settings()

# Steam OpenID constants
STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_API_URL = "https://api.steampowered.com"
LOGIN_STATE_COOKIE = "steam_login_state"
LOGIN_STATE_MAX_AGE = 60 * 10  # 10 minutes
OPENID_NONCE_MAX_AGE = timedelta(hours=6)
OPENID_NONCE_MAX_SIZE = 10_000  # Hard cap to prevent memory exhaustion under attack
_used_openid_nonces: dict[str, datetime] = {}
STEAM_OPENID_VERIFY_ATTEMPTS = 3
STEAM_OPENID_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


def _login_state_serializer():
    """Serializer for Steam login state tokens."""
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(settings.secret_key, salt="steam-login-state")


def _session_serializer():
    """Serializer for signed session cookies."""
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(settings.secret_key, salt="session")


def _build_login_state() -> str:
    """Generate a short-lived signed login state token."""
    return _login_state_serializer().dumps({"nonce": secrets.token_urlsafe(32)})


def _site_name_filename_prefix() -> str:
    """Return a filename-safe prefix derived from the configured site name."""
    prefix = re.sub(r"[^a-z0-9]+", "_", settings.site_name.lower()).strip("_")
    return prefix or "site"


def _validate_login_state(cookie_state: str | None, query_state: str | None) -> bool:
    """Validate the signed login state echoed back on the callback."""
    if not cookie_state or not query_state:
        return False
    if not hmac.compare_digest(cookie_state, query_state):
        return False

    try:
        _login_state_serializer().loads(cookie_state, max_age=LOGIN_STATE_MAX_AGE)
    except Exception:
        return False

    return True


def _consume_openid_nonce(params: dict) -> bool:
    """Return True only once for each OpenID response nonce."""
    nonce = params.get("openid.response_nonce")
    if not nonce:
        return False

    now = datetime.now(timezone.utc)
    cutoff = now - OPENID_NONCE_MAX_AGE

    stale = [key for key, seen_at in _used_openid_nonces.items() if seen_at < cutoff]
    for key in stale:
        _used_openid_nonces.pop(key, None)

    if nonce in _used_openid_nonces:
        return False

    # Hard cap: reject if dict has grown too large (e.g. under sustained attack)
    if len(_used_openid_nonces) >= OPENID_NONCE_MAX_SIZE:
        logger.warning("OpenID nonce store at capacity (%d), rejecting", OPENID_NONCE_MAX_SIZE)
        return False

    _used_openid_nonces[nonce] = now
    return True


def get_steam_login_url(return_url: str) -> str:
    """Generate Steam OpenID login URL."""
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_url,
        "openid.realm": settings.base_url,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID_URL}?{urlencode(params)}"


async def verify_steam_login(params: dict) -> Optional[str]:
    """Verify Steam OpenID response and extract Steam ID.
    
    Returns:
        Steam ID (64-bit) as string, or None if verification fails
    """
    # Change mode to check_authentication
    validation_params = {
        key: value
        for key, value in params.items()
        if key.startswith("openid.")
    }
    validation_params["openid.mode"] = "check_authentication"

    response: httpx.Response | None = None
    for attempt in range(1, STEAM_OPENID_VERIFY_ATTEMPTS + 1):
        try:
            async with create_steam_async_client(timeout=STEAM_OPENID_TIMEOUT) as client:
                response = await client.post(STEAM_OPENID_URL, data=validation_params)
            break
        except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            logger.warning(
                "Steam OpenID verification attempt %s/%s timed out: %r",
                attempt,
                STEAM_OPENID_VERIFY_ATTEMPTS,
                exc,
            )
        except httpx.NetworkError as exc:
            logger.warning(
                "Steam OpenID verification attempt %s/%s network error: %r",
                attempt,
                STEAM_OPENID_VERIFY_ATTEMPTS,
                exc,
            )

        if attempt < STEAM_OPENID_VERIFY_ATTEMPTS:
            await asyncio.sleep(0.75 * attempt)

    if response is None:
        logger.error(
            "Steam OpenID verification failed after %s attempts",
            STEAM_OPENID_VERIFY_ATTEMPTS,
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "Steam verify response status=%s body=%s",
            response.status_code,
            response.text.strip(),
        )
        return None

    if "is_valid:true" not in response.text:
        logger.warning("Steam verify response: %s", response.text.strip())
        return None
    
    # Extract Steam ID from claimed_id
    claimed_id = params.get("openid.claimed_id", "")
    match = re.search(r"steamcommunity\.com/openid/id/(\d+)", claimed_id)
    if not match:
        return None
    
    return match.group(1)


async def get_steam_player_info(steam_id: str) -> dict:
    """Fetch player info from Steam API.
    
    Returns:
        Dict with personaname, avatarfull, etc.
    """
    if not settings.steam_configured:
        return {
            "personaname": f"User {steam_id[-4:]}",
            "avatarfull": "",
        }
    
    url = f"{STEAM_API_URL}/ISteamUser/GetPlayerSummaries/v0002/"
    params = {
        "key": settings.steam_api_key,
        "steamids": steam_id,
    }
    
    async with create_steam_async_client(timeout=10.0) as client:
        response = await client.get(url, params=params)
        data = response.json()
    
    players = data.get("response", {}).get("players", [])
    if not players:
        return {
            "personaname": f"User {steam_id[-4:]}",
            "avatarfull": "",
        }
    
    return players[0]


@router.get("/login")
async def login():
    """Redirect to Steam OpenID login."""
    state = _build_login_state()
    return_url = f"{settings.base_url}/auth/callback?{urlencode({'state': state})}"
    steam_url = get_steam_login_url(return_url)
    redirect = RedirectResponse(url=steam_url)
    redirect.set_cookie(
        key=LOGIN_STATE_COOKIE,
        value=state,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        max_age=LOGIN_STATE_MAX_AGE,
        samesite="lax",
    )
    return redirect


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Steam OpenID callback."""
    params = dict(request.query_params)

    cookie_state = request.cookies.get(LOGIN_STATE_COOKIE)
    query_state = params.get("state")
    if not _validate_login_state(cookie_state, query_state):
        logger.warning(
            "Login state validation failed: cookie_present=%s query_present=%s",
            cookie_state is not None,
            query_state is not None,
        )
        raise HTTPException(status_code=400, detail=t("errors.auth_failed"))

    steam_id = await verify_steam_login(params)
    if not steam_id:
        logger.warning("Steam OpenID verification failed")
        raise HTTPException(status_code=400, detail=t("errors.auth_failed"))
    if not _consume_openid_nonce(params):
        logger.warning("OpenID nonce replay detected")
        raise HTTPException(status_code=400, detail=t("errors.auth_failed"))

    logger.info("Steam login verified: steam_id=%s admin=%s", steam_id, steam_id in settings.admin_steam_id_list)
    
    # Get or create user
    result = await db.execute(
        select(User).where(User.steam_id == steam_id)
    )
    user = result.scalar_one_or_none()
    
    if user is None:
        # Fetch player info from Steam
        player_info = await get_steam_player_info(steam_id)

        # Check if this Steam ID should be admin
        is_admin = steam_id in settings.admin_steam_id_list

        user = User(
            steam_id=steam_id,
            display_name=player_info.get("personaname", f"User {steam_id[-4:]}"),
            avatar_url=player_info.get("avatarfull", ""),
            is_admin=is_admin,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif user.deleted_at is not None:
        # Deleted account skeleton — don't restore data or create session
        return RedirectResponse(url="/", status_code=302)
    else:
        # Update last login and potentially refresh profile info
        player_info = await get_steam_player_info(steam_id)
        user.display_name = player_info.get("personaname", user.display_name)
        user.avatar_url = player_info.get("avatarfull", user.avatar_url)
        # Also update admin status in case config changed
        user.is_admin = steam_id in settings.admin_steam_id_list
        await db.commit()
    
    # Update Steam trust data if stale or missing
    try:
        from app.services.settings import get_steam_trust_settings
        trust_settings = await get_steam_trust_settings(db)

        from app.services.steam_trust import steam_trust_needs_refresh
        missing_required = steam_trust_needs_refresh(user, trust_settings)

        should_update = (
            missing_required
            or
            user.steam_data_updated_at is None
            or (datetime.now(timezone.utc) - (
                user.steam_data_updated_at.replace(tzinfo=timezone.utc)
                if user.steam_data_updated_at.tzinfo is None
                else user.steam_data_updated_at
            )) > timedelta(hours=24)
        )
        if should_update:
            from app.services.steam_trust import update_user_steam_trust
            await update_user_steam_trust(user, player_info, db)
    except Exception:
        logger.warning("Failed to update Steam trust data for %s", user.steam_id, exc_info=True)

    # Set session cookie (signed + time-limited cookie approach)
    session_token = _session_serializer().dumps({"user_id": user.id, "steam_id": steam_id})
    
    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        max_age=60 * 60 * 24 * 7,  # 7 days
        samesite="lax",
    )
    redirect.delete_cookie(LOGIN_STATE_COOKIE)
    return redirect


@router.post("/logout")
async def logout():
    """Clear session and redirect to home."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session")
    response.delete_cookie(LOGIN_STATE_COOKIE)
    return response


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Get current user from session cookie.
    
    Returns None if not logged in (doesn't raise exception).
    """
    session_cookie = request.cookies.get("session")
    if not session_cookie:
        return None
    
    try:
        data = _session_serializer().loads(session_cookie, max_age=60 * 60 * 24 * 7)  # 7 days
        user_id = data.get("user_id")
    except Exception:
        return None
    
    if not user_id:
        return None
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        return None

    # Admin status is configured via env (`ADMIN_STEAM_IDS`) and can change while a user
    # still has a valid session cookie. Sync it here so existing sessions/users start
    # working immediately without requiring an explicit logout/login cycle.
    desired_is_admin = user.steam_id in settings.admin_steam_id_list
    if user.is_admin != desired_is_admin:
        user.is_admin = desired_is_admin
        await db.commit()

    return user


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require authenticated user, raise 401 if not logged in."""
    user = await get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail=t("errors.not_authenticated"))
    if user.is_banned:
        raise HTTPException(status_code=403, detail=t("errors.account_banned"))
    if settings.beta_mode and not user.is_admin:
        raise HTTPException(status_code=403, detail=t("home.beta_notice"))
    return user


async def require_user_allow_banned(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require authenticated user, allow banned users through."""
    user = await get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail=t("errors.not_authenticated"))
    if settings.beta_mode and not user.is_admin:
        raise HTTPException(status_code=403, detail=t("home.beta_notice"))
    return user


async def require_admin(
    user: User = Depends(require_user),
) -> User:
    """Require admin user."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail=t("errors.admin_required"))
    return user


@router.get("/api/account/export")
async def export_account_data(
    user: User = Depends(require_user_allow_banned),
    db: AsyncSession = Depends(get_db),
):
    """Export all personal data for the current user (GDPR Art. 20)."""
    from app.models.reservation import Reservation
    from app.models.steam_trust_snapshot import SteamTrustSnapshot
    from app.models.upload_link import UploadLink

    # Reservations with upload links
    result = await db.execute(
        select(Reservation)
        .where(Reservation.user_id == user.id)
        .options(selectinload(Reservation.upload_links))
        .order_by(Reservation.created_at.desc())
    )
    reservations = result.scalars().all()

    # Steam trust snapshots
    result = await db.execute(
        select(SteamTrustSnapshot)
        .where(SteamTrustSnapshot.user_id == user.id)
        .order_by(SteamTrustSnapshot.fetched_at.desc())
    )
    snapshots = result.scalars().all()

    def fmt_dt(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "steam_id": user.steam_id,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "is_banned": user.is_banned,
            "ban_reason": user.ban_reason,
            "reservation_count": user.reservation_count,
            "created_at": fmt_dt(user.created_at),
            "last_login_at": fmt_dt(user.last_login_at),
            "steam_account_created_at": fmt_dt(user.steam_account_created_at),
            "tf2_playtime_hours": user.tf2_playtime_hours,
            "owns_tf2": user.owns_tf2,
            "has_vac_ban": user.has_vac_ban,
            "profile_public": user.profile_public,
            "steam_data_updated_at": fmt_dt(user.steam_data_updated_at),
        },
        "reservations": [
            {
                "reservation_number": r.reservation_number,
                "location": r.location,
                "status": r.status.value,
                "first_map": r.first_map,
                "starts_at": fmt_dt(r.starts_at),
                "ends_at": fmt_dt(r.ends_at),
                "started_at": fmt_dt(r.started_at),
                "created_at": fmt_dt(r.created_at),
                "upload_links": [
                    {
                        "type": link.type.value,
                        "url": link.url,
                        "created_at": fmt_dt(link.created_at),
                    }
                    for link in r.upload_links
                ],
            }
            for r in reservations
        ],
        "steam_trust_snapshots": [
            {
                "fetched_at": fmt_dt(s.fetched_at),
                "source": s.source,
                "steam_account_created_at": fmt_dt(s.steam_account_created_at),
                "tf2_playtime_hours": s.tf2_playtime_hours,
                "owns_tf2": s.owns_tf2,
                "has_vac_ban": s.has_vac_ban,
                "profile_public": s.profile_public,
            }
            for s in snapshots
        ],
    }
    export_filename = f"{_site_name_filename_prefix()}_data_{user.steam_id}.json"

    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f'attachment; filename="{export_filename}"',
        },
    )


@router.delete("/api/account")
async def delete_account(
    user: User = Depends(require_user_allow_banned),
    db: AsyncSession = Depends(get_db),
):
    """Delete the current user's account.

    Banned users keep a skeleton record so the ban persists.
    """
    from app.models.reservation import Reservation, ReservationStatus
    from app.models.steam_trust_snapshot import SteamTrustSnapshot

    # End any active/provisioning reservations first
    active_statuses = (ReservationStatus.ACTIVE, ReservationStatus.PROVISIONING, ReservationStatus.PENDING)
    result = await db.execute(
        select(Reservation).where(
            Reservation.user_id == user.id,
            Reservation.status.in_(active_statuses),
        )
    )
    active_reservations = result.scalars().all()
    for reservation in active_reservations:
        reservation.status = ReservationStatus.ENDED
        reservation.ended_at = datetime.now(timezone.utc)
        if reservation.instance_id:
            try:
                from app.routers.internal import send_container_stop
                from app.models.instance import CloudInstance
                from app.services.orchestrator import release_to_warm_pool, destroy_instance, is_hourly_billing

                ci_result = await db.execute(
                    select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
                )
                cloud_instance = ci_result.scalar_one_or_none()
                if cloud_instance:
                    await send_container_stop(cloud_instance.instance_id)

                if await is_hourly_billing(reservation.location, db):
                    await release_to_warm_pool(reservation.instance_id, db)
                else:
                    await destroy_instance(reservation.instance_id, db)
            except Exception:
                logger.warning("Failed to clean up instance for reservation %s", reservation.id, exc_info=True)

        from app.services.timer import cancel_expiry_timer
        cancel_expiry_timer(reservation.id)

    # Delete steam trust snapshots
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(SteamTrustSnapshot).where(SteamTrustSnapshot.user_id == user.id))

    # Anonymize reservations by severing the user link
    from sqlalchemy import update as sql_update
    await db.execute(
        sql_update(Reservation)
        .where(Reservation.user_id == user.id)
        .values(user_id=None)
    )

    if user.is_banned:
        # Keep skeleton record so ban persists
        user.display_name = f"Deleted User {user.steam_id[-4:]}"
        user.avatar_url = ""
        user.api_key_hash = None
        user.api_key_hint = None
        user.steam_account_created_at = None
        user.tf2_playtime_hours = None
        user.owns_tf2 = None
        user.has_vac_ban = None
        user.profile_public = None
        user.steam_data_updated_at = None
        user.deleted_at = datetime.now(timezone.utc)
    else:
        await db.delete(user)

    await db.commit()

    # Clear session
    response = Response(status_code=200)
    response.delete_cookie("session")
    return response

"""Reservation API endpoints."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.reservation import Reservation, ReservationStatus
from app.models.user import User
from app.routers.auth import require_user
from app.services.reservation import (
    create_reservation,
    end_reservation,
    get_user_active_reservation,
    get_reservation_by_id,
    get_user_reservations,
)
from app.services.orchestrator import (
    provision_instance_for_reservation,
    destroy_instance,
    get_enabled_locations,
    release_to_warm_pool,
    is_hourly_billing,
)
from app.i18n import t
from app.services.failure_messages import public_failure_reason
from app.services.rate_limit import (
    check_user_rate_limit,
    check_site_rate_limit,
    check_circuit_breaker,
    RateLimitExceeded,
    CircuitBreakerOpen,
)
from app.utils.maps import is_valid_map_name


router = APIRouter(prefix="/api/reservations", tags=["reservations"])
captcha_router = APIRouter(prefix="/api/captcha", tags=["captcha"])
logger = logging.getLogger(__name__)
settings = get_settings()

_CFG_FILE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_reservation_creation_lock = asyncio.Lock()


@captcha_router.get("/check")
async def check_captcha_required(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if the current user needs captcha for their next reservation."""
    from app.services.captcha import requires_captcha
    needed = await requires_captcha(user, db)
    return {
        "required": needed,
        "site_key": settings.hcaptcha_site_key if needed else None,
    }


class CreateReservationRequest(BaseModel):
    """Request body for creating a reservation."""
    location: str = Field(..., description="Location code (e.g., 'santiago', 'seoul')")
    first_map: str = Field("cp_badlands", description="Initial map")
    enable_direct_connect: bool = Field(False, description="Allow direct IP connections (opens UDP ports)")
    captcha_token: str | None = Field(None, description="hCaptcha response token")


class ReservationResponse(BaseModel):
    """Reservation details response."""
    id: int
    reservation_number: int
    location: str
    status: str
    starts_at: datetime
    ends_at: datetime
    first_map: str
    created_at: datetime
    # Connection details (only for owner when active)
    password: Optional[str] = None
    # NOTE: Intentionally excluded from API responses to avoid exposing RCON creds.
    # Re-enable by uncommenting this field and the assignment in reservation_to_response().
    # rcon_password: Optional[str] = None
    tv_password: Optional[str] = None
    sdr_ip: Optional[str] = None
    sdr_port: Optional[int] = None
    sdr_tv_port: Optional[int] = None
    # Direct connect
    enable_direct_connect: bool = False
    ip_address: Optional[str] = None
    # Boot progress (during provisioning)
    boot_progress: Optional[dict] = None
    # Failure details
    failure_reason: Optional[str] = None

    class Config:
        from_attributes = True


def reservation_to_response(
    reservation: Reservation,
    include_secrets: bool = False,
    cloud_instance = None,  # Optional CloudInstance for boot progress lookup
) -> ReservationResponse:
    """Convert reservation model to response."""
    response = ReservationResponse(
        id=reservation.id,
        reservation_number=reservation.reservation_number,
        location=reservation.location,
        status=reservation.status.value,
        starts_at=reservation.starts_at,
        ends_at=reservation.ends_at,
        first_map=reservation.first_map,
        created_at=reservation.created_at,
        enable_direct_connect=reservation.enable_direct_connect,
        failure_reason=public_failure_reason(
            reservation.status,
            reservation.provision_attempts,
            reservation.failure_reason,
        ),
    )

    # Include secrets and connection details for owner only
    if include_secrets:
        response.password = reservation.password
        # NOTE: Intentionally excluded from API responses to avoid exposing RCON creds.
        # Re-enable by uncommenting this line and the ReservationResponse field.
        # response.rcon_password = reservation.rcon_password
        response.tv_password = reservation.tv_password
        response.sdr_ip = reservation.sdr_ip
        response.sdr_port = reservation.sdr_port
        response.sdr_tv_port = reservation.sdr_tv_port
        if reservation.enable_direct_connect and cloud_instance:
            ip = cloud_instance.ip_address
            if ip and ip != "0.0.0.0":
                response.ip_address = ip
    
    # Include boot progress during provisioning
    if reservation.status == ReservationStatus.PROVISIONING and cloud_instance:
        from app.routers.internal import get_boot_progress
        progress = get_boot_progress(cloud_instance.instance_id)
        if progress:
            response.boot_progress = progress
    
    return response


def _user_can_access_reservation(user: User, reservation: Reservation) -> bool:
    """Return True when the viewer owns the reservation or is an admin."""
    return reservation.user_id == user.id or user.is_admin


def _require_reservation_access_or_404(user: User, reservation: Reservation) -> None:
    """Hide reservation existence from authenticated non-owners."""
    if not _user_can_access_reservation(user, reservation):
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))


async def provision_reservation_background(
    reservation_id: int,
    db_url: str,
):
    """Background task to provision instance for reservation with retry loop.

    Retries up to max_provision_attempts on transient failures (provider 5xx/429,
    unexpected exceptions). Non-retryable errors (4xx) immediately FAIL.
    If the reservation is cancelled (ENDED) during provisioning, the loop exits.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

    engine = create_async_engine(db_url)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    while True:
        async with async_session() as db:
            reservation = await get_reservation_by_id(reservation_id, db)
            if not reservation:
                return
            # Exit if reservation moved to a terminal or non-provisionable state
            if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.PROVISIONING):
                return

            result = await provision_instance_for_reservation(reservation, db)
            if result:
                return  # Success

            # Re-read status after provisioning attempt
            await db.refresh(reservation)

            if reservation.status == ReservationStatus.FAILED:
                return  # Non-retryable error

            # Check if max attempts exhausted
            if reservation.provision_attempts >= settings.max_provision_attempts:
                reservation.status = ReservationStatus.FAILED
                reservation.failure_reason = public_failure_reason(
                    reservation.status,
                    reservation.provision_attempts,
                )
                await db.commit()
                logger.error(f"Reservation #{reservation.reservation_number} exhausted {reservation.provision_attempts} provision attempts")
                return

        # Wait before retrying (outside the DB session)
        logger.info(f"Retrying provisioning for reservation {reservation_id} in 5s (attempt {reservation.provision_attempts})")
        await asyncio.sleep(5)


@router.post("", response_model=ReservationResponse)
async def create_reservation_endpoint(
    request: CreateReservationRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new server reservation."""
    # Check if user is banned
    if user.is_banned:
        raise HTTPException(
            status_code=403,
            detail=t("errors.banned")
        )
    
    # Steam trust checks
    from app.services.steam_trust import check_steam_trust, SteamTrustBlocked
    try:
        await check_steam_trust(user, db)
    except SteamTrustBlocked as e:
        raise HTTPException(status_code=403, detail=e.message)

    # Captcha check
    from app.services.captcha import requires_captcha, verify_captcha
    if await requires_captcha(user, db):
        if not request.captcha_token:
            raise HTTPException(
                status_code=428,
                detail=t("errors.captcha_required"),
                headers={"X-Captcha-Required": "true"},
            )
        if not await verify_captcha(request.captcha_token):
            raise HTTPException(
                status_code=400,
                detail=t("errors.captcha_failed"),
            )

    # Check if any cloud provider is configured
    if not settings.cloud_configured:
        raise HTTPException(
            status_code=503,
            detail=t("errors.not_configured")
        )
    
    # Validate location
    locations = await get_enabled_locations(db)
    location_codes = [loc.code for loc in locations]
    if request.location not in location_codes:
        raise HTTPException(
            status_code=400,
            detail=t("errors.invalid_location", locations=', '.join(location_codes))
        )
    
    # Validate first_map against enabled maps
    from app.models.instance import GameMap
    map_result = await db.execute(
        select(GameMap.name).where(GameMap.enabled == True)
    )
    valid_maps = {row[0] for row in map_result}
    if request.first_map not in valid_maps:
        raise HTTPException(
            status_code=400,
            detail=t("errors.invalid_map", map=request.first_map)
        )

    # Serialize reservation creation so capacity, rate-limit, and single-active
    # checks stay accurate under concurrent API requests.
    async with _reservation_creation_lock:
        # Check circuit breaker first (admins bypass)
        if not user.is_admin:
            try:
                await check_circuit_breaker(db)
            except CircuitBreakerOpen as e:
                raise HTTPException(
                    status_code=503,
                    detail=e.message,
                    headers={"Retry-After": str(e.retry_after_seconds)},
                )

        # Check rate limits (per-user and site-wide) - admins get higher limits
        try:
            await check_user_rate_limit(user.id, db, is_admin=user.is_admin)
            await check_site_rate_limit(db)
        except RateLimitExceeded as e:
            raise HTTPException(
                status_code=429,
                detail=e.message,
                headers={"Retry-After": str(e.retry_after_seconds)},
            )

        # Check for existing active reservation
        existing = await get_user_active_reservation(user, db)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=t("errors.existing_reservation", number=existing.reservation_number)
            )

        from app.services.settings import get_reservation_settings
        res_settings = await get_reservation_settings(db)

        # Check daily hours limit
        from app.services.rate_limit import check_daily_hours_limit, DailyHoursExceeded
        try:
            await check_daily_hours_limit(
                user.id,
                db,
                requested_hours=res_settings["max_duration_hours"],
            )
        except DailyHoursExceeded as e:
            raise HTTPException(status_code=429, detail=e.message)

        # Create reservation (use configured max duration, auto-end on empty handles early cleanup)
        reservation = await create_reservation(
            user=user,
            location=request.location,
            duration_hours=res_settings["max_duration_hours"],
            first_map=request.first_map,
            enable_direct_connect=request.enable_direct_connect,
            db=db,
        )

    # Start provisioning in background
    background_tasks.add_task(
        provision_reservation_background,
        reservation.id,
        settings.database_url,
    )
    
    return reservation_to_response(reservation, include_secrets=True)


@router.get("/configs")
async def get_competitive_configs():
    """(Deprecated) List competitive configs if any agent has reported them."""
    from app.routers.internal import competitive_configs as cfg_cache
    from app.services.competitive_configs import group_for_ui

    # Pick an arbitrary instance's list for backwards compatibility.
    for item in cfg_cache.values():
        return {
            "available": True,
            "configs": group_for_ui(item.get("cfg_files") or []),
            "updated_at": item.get("updated_at"),
            "container_image": item.get("container_image"),
        }
    return {
        "available": False,
        "configs": {},
        "message": "Config list unavailable. Use !config in-game to see available configs.",
    }


@router.get("/mine", response_model=list[ReservationResponse])
async def get_my_reservations(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's reservations."""
    reservations = await get_user_reservations(user, db)
    return [reservation_to_response(r, include_secrets=True) for r in reservations]


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get reservation details."""
    from sqlalchemy.orm import selectinload
    from app.models.instance import CloudInstance
    
    # Fetch reservation with cloud_instance relationship
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(selectinload(Reservation.cloud_instance))
    )
    reservation = result.scalar_one_or_none()
    
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    if not _user_can_access_reservation(user, reservation):
        # Return 404 to avoid turning reservation IDs into an enumeration oracle.
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    # Include secrets only for the reservation owner or an admin.
    include_secrets = _user_can_access_reservation(user, reservation)
    return reservation_to_response(
        reservation, 
        include_secrets=include_secrets,
        cloud_instance=reservation.cloud_instance,
    )


@router.get("/{reservation_id}/players")
async def get_reservation_players(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current player list for an active reservation."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    from app.routers.internal import get_player_data
    data = get_player_data(reservation.reservation_number)

    return {
        "players": data["players"] if data else [],
        "player_count": data["player_count"] if data else 0,
        "updated_at": data["updated_at"] if data else None,
        "empty_since": reservation.empty_since.isoformat() if reservation.empty_since else None,
    }


@router.get("/{reservation_id}/configs")
async def get_reservation_configs(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get competitive configs available on this reservation's server (reported by the agent)."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    if not reservation.instance_id:
        return {
            "available": False,
            "configs": {},
            "message": "Server not assigned yet. Use !config in-game once it is running.",
        }

    from app.models.instance import CloudInstance
    instance_result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
    )
    cloud_instance = instance_result.scalar_one_or_none()
    if not cloud_instance:
        return {
            "available": False,
            "configs": {},
            "message": "Server not assigned yet. Use !config in-game once it is running.",
        }

    from app.routers.internal import get_competitive_configs as get_instance_configs
    cache = get_instance_configs(cloud_instance.instance_id)
    if not cache or not cache.get("cfg_files"):
        return {
            "available": False,
            "configs": {},
            "message": "Config list unavailable. Use !config in-game to see available configs.",
        }

    from app.services.competitive_configs import group_for_ui
    return {
        "available": True,
        "configs": group_for_ui(cache["cfg_files"]),
        "updated_at": cache.get("updated_at"),
        "container_image": cache.get("container_image"),
    }


@router.get("/{reservation_id}/stats")
async def get_reservation_stats(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get system stats for an active reservation's server."""
    from app.models.instance import CloudInstance, EnabledLocation, Provider

    # Single query to avoid per-poll DB chatter (this endpoint is polled by the UI).
    result = await db.execute(
        select(Reservation, CloudInstance.instance_id, Provider.name)
        .outerjoin(CloudInstance, CloudInstance.id == Reservation.instance_id)
        .outerjoin(EnabledLocation, EnabledLocation.code == Reservation.location)
        .outerjoin(Provider, Provider.code == EnabledLocation.provider)
        .where(Reservation.id == reservation_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    reservation, agent_instance_id, provider_name = row

    _require_reservation_access_or_404(user, reservation)

    if reservation.status not in (ReservationStatus.PROVISIONING, ReservationStatus.ACTIVE):
        return {"stats": None}

    from app.routers.internal import get_agent_stats
    stats = get_agent_stats(agent_instance_id) if agent_instance_id else None

    return {"stats": stats, "provider_name": provider_name}


@router.post("/{reservation_id}/end")
async def end_reservation_endpoint(
    reservation_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """End a reservation early."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))
    
    _require_reservation_access_or_404(user, reservation)
    
    if not reservation.can_be_ended:
        raise HTTPException(
            status_code=400,
            detail=t("errors.cannot_end_status", status=reservation.status.value)
        )
    
    was_active = reservation.status == ReservationStatus.ACTIVE
    had_started = reservation.started_at is not None

    await end_reservation(reservation, db)

    from app.services.timer import cancel_expiry_timer
    cancel_expiry_timer(reservation_id)

    # Clear in-memory player data
    from app.routers.internal import clear_player_data
    clear_player_data(reservation.reservation_number)

    # Handle instance based on billing model
    if reservation.instance_id:
        if was_active or had_started:
            # Tell the agent to end the reservation (RCON kicks, log copy, S3 upload)
            from app.models.instance import CloudInstance
            from app.routers.internal import send_to_agent
            ci_result = await db.execute(
                select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
            )
            cloud_instance = ci_result.scalar_one_or_none()
            if cloud_instance:
                await send_to_agent(cloud_instance.instance_id, {"type": "reservation.end"})

        if await is_hourly_billing(reservation.location, db):
            if was_active or had_started:
                # Instance reached active — release to warm pool
                await release_to_warm_pool(reservation.instance_id, db)
            else:
                # Still provisioning — let it complete and warm pool on server_ready.
                # Don't destroy; the billing hour is already paid for.
                pass
        else:
            # Per-second billing: destroy immediately to save costs
            await destroy_instance(reservation.instance_id, db)
    
    return {"message": "Reservation ended", "status": reservation.status.value}


@router.post("/{reservation_id}/restart")
async def restart_reservation_endpoint(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Restart the game server for an active reservation."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    if reservation.status != ReservationStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=t("errors.cannot_restart_status", status=reservation.status.value)
        )

    if not reservation.instance_id:
        raise HTTPException(status_code=400, detail=t("errors.no_instance"))

    from app.models.instance import CloudInstance
    instance_result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
    )
    cloud_instance = instance_result.scalar_one_or_none()
    if not cloud_instance:
        raise HTTPException(status_code=404, detail=t("errors.instance_not_found"))

    from app.routers.internal import send_container_restart, clear_player_data
    from app.utils.passwords import generate_password

    clear_player_data(reservation.reservation_number)
    reservation.password = generate_password(8)
    reservation.rcon_password = generate_password(12)
    reservation.tv_password = generate_password(8)
    reservation.status = ReservationStatus.PROVISIONING
    reservation.empty_since = None  # Clear during restart; reset when server is ready

    if not await send_container_restart(cloud_instance.instance_id, {
        "password": reservation.password,
        "rcon_password": reservation.rcon_password,
        "tv_password": reservation.tv_password,
    }):
        raise HTTPException(status_code=503, detail=t("errors.agent_not_connected"))
    await db.commit()

    return {"message": "Server restart initiated"}


def _steamid64_to_steamid3(steamid64: str) -> str:
    """Convert SteamID64 to SteamID3 format for SourceMod targeting."""
    try:
        account_id = int(steamid64) - 76561197960265728
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=t("errors.invalid_steam_id"))
    if account_id < 0:
        raise HTTPException(status_code=400, detail=t("errors.invalid_steam_id_2"))
    return f"[U:1:{account_id}]"


class ExecConfigRequest(BaseModel):
    cfg_file: str = Field(..., description="Config identifier (e.g., 'rgl_6s_5cp_match_pro')")


@router.post("/{reservation_id}/config")
async def exec_competitive_config(
    reservation_id: int,
    body: ExecConfigRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a competitive config on the game server."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    if reservation.status != ReservationStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=t("errors.server_not_active"))

    if not reservation.instance_id:
        raise HTTPException(status_code=400, detail=t("errors.no_instance"))

    cfg_file = (body.cfg_file or "").strip()
    if not _CFG_FILE_RE.fullmatch(cfg_file):
        raise HTTPException(status_code=400, detail=t("errors.invalid_config"))
    from app.services.competitive_configs import ALLOWED_PREFIXES
    allowed_prefixes = list(ALLOWED_PREFIXES)
    if settings.custom_config_prefixes:
        allowed_prefixes.extend(
            p.strip() for p in settings.custom_config_prefixes.split(",") if p.strip()
        )
    if cfg_file != "summon_reset" and not cfg_file.startswith(tuple(allowed_prefixes)):
        raise HTTPException(status_code=400, detail=t("errors.unknown_config"))

    from app.models.instance import CloudInstance
    instance_result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
    )
    cloud_instance = instance_result.scalar_one_or_none()
    if not cloud_instance:
        raise HTTPException(status_code=404, detail=t("errors.instance_not_found"))

    # If we have a server-reported list, validate against it to avoid drift.
    from app.routers.internal import get_competitive_configs as get_instance_configs
    cache = get_instance_configs(cloud_instance.instance_id)
    if cache and cache.get("exec_cfg_files") and cfg_file != "summon_reset":
        if cfg_file not in set(cache["exec_cfg_files"]):
            raise HTTPException(
                status_code=400,
                detail=t("errors.config_not_available"),
            )

    from app.routers.internal import send_rcon_command
    if not await send_rcon_command(cloud_instance.instance_id, f"sm_config {cfg_file}"):
        raise HTTPException(status_code=503, detail=t("errors.agent_not_connected"))

    logger.info(
        f"Executed config {cfg_file} on reservation #{reservation.reservation_number}"
    )
    return {"message": f"Loaded {cfg_file}", "cfg_file": cfg_file}


class ChangeLevelRequest(BaseModel):
    map_name: str = Field(..., description="Map to change to (e.g., 'cp_process_f12')")


@router.post("/{reservation_id}/changelevel")
async def change_level(
    reservation_id: int,
    body: ChangeLevelRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the map on an active reservation's server."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    if reservation.status != ReservationStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=t("errors.server_not_active"))

    if not reservation.instance_id:
        raise HTTPException(status_code=400, detail=t("errors.no_instance"))

    map_name = (body.map_name or "").strip()
    if not is_valid_map_name(map_name):
        raise HTTPException(status_code=400, detail=t("errors.invalid_map_name"))

    # Validate against enabled maps
    from app.models.instance import GameMap
    map_result = await db.execute(
        select(GameMap.name).where(GameMap.enabled == True)
    )
    valid_maps = {row[0] for row in map_result}
    if map_name not in valid_maps:
        raise HTTPException(
            status_code=400,
            detail=t("errors.invalid_map", map=map_name)
        )

    from app.models.instance import CloudInstance
    instance_result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
    )
    cloud_instance = instance_result.scalar_one_or_none()
    if not cloud_instance:
        raise HTTPException(status_code=404, detail=t("errors.instance_not_found"))

    from app.routers.internal import send_rcon_command
    if not await send_rcon_command(cloud_instance.instance_id, f"changelevel {map_name}"):
        raise HTTPException(status_code=503, detail=t("errors.agent_not_connected"))

    logger.info(
        f"Changelevel to {map_name} on reservation #{reservation.reservation_number}"
    )
    return {"message": f"Changing map to {map_name}", "map_name": map_name}


@router.get("/{reservation_id}/maps")
async def get_reservation_maps(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get available maps for an active reservation."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    from app.models.instance import GameMap
    maps_result = await db.execute(
        select(GameMap)
        .where(GameMap.enabled == True)
        .order_by(GameMap.display_order)
    )
    maps = [
        {"name": m.name, "display": m.display_name}
        for m in maps_result.scalars().all()
    ]
    return {"maps": maps}


@router.get("/{reservation_id}/uploads")
async def get_reservation_uploads(
    reservation_id: int,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Get logs.tf and demos.tf upload links for a reservation."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    if not _user_can_access_reservation(user, reservation):
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    from app.models.upload_link import UploadLink
    result = await db.execute(
        select(UploadLink)
        .where(UploadLink.reservation_id == reservation_id)
        .order_by(UploadLink.created_at)
    )
    links = result.scalars().all()

    return {
        "uploads": [
            {
                "type": link.type.value,
                "external_id": link.external_id,
                "url": link.url,
                "created_at": link.created_at.isoformat() if link.created_at else None,
            }
            for link in links
        ]
    }


class KickPlayerRequest(BaseModel):
    steam_id: str


@router.post("/{reservation_id}/kick")
async def kick_player(
    reservation_id: int,
    body: KickPlayerRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Kick a player from the server."""
    reservation = await get_reservation_by_id(reservation_id, db)
    if not reservation:
        raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))

    _require_reservation_access_or_404(user, reservation)

    if reservation.status != ReservationStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=t("errors.server_not_active"))

    if not reservation.instance_id:
        raise HTTPException(status_code=400, detail=t("errors.no_instance"))

    # Validate player is actually on the server
    from app.routers.internal import get_player_data
    data = get_player_data(reservation.reservation_number)
    if data:
        player_ids = [p["steam_id"] for p in data.get("players", [])]
        if body.steam_id not in player_ids:
            raise HTTPException(status_code=400, detail=t("errors.player_not_found"))

    steamid3 = _steamid64_to_steamid3(body.steam_id)

    from app.models.instance import CloudInstance
    instance_result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
    )
    cloud_instance = instance_result.scalar_one_or_none()
    if not cloud_instance:
        raise HTTPException(status_code=404, detail=t("errors.instance_not_found"))

    from app.routers.internal import send_rcon_command
    command = f'sm_kick "#{steamid3}" Kicked by reservation owner'
    if not await send_rcon_command(cloud_instance.instance_id, command):
        raise HTTPException(status_code=503, detail=t("errors.agent_not_connected"))

    logger.info(f"Kick sent for {body.steam_id} from reservation #{reservation.reservation_number}")
    return {"message": "Kick command sent"}

"""Status and SSE endpoints."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.models.reservation import Reservation, ReservationStatus
from app.models.instance import EnabledLocation, CloudInstance, Provider, LocationProvider
from app.services.failure_messages import public_failure_reason
from app.services.orchestrator import get_enabled_locations
from app.utils.location_flags import build_location_flag


router = APIRouter(tags=["status"])
logger = logging.getLogger(__name__)

# In-memory cache for /api/status (same for all visitors)
_status_cache: dict | None = None
_status_cache_time: float = 0
_STATUS_CACHE_TTL: float = 3.0  # seconds

_reservation_stream_lock = asyncio.Lock()
_reservation_stream_counts: dict[int, int] = {}
_reservation_stream_ip_counts: dict[tuple[int, str], int] = {}
_MAX_STREAMS_PER_RESERVATION = 12
_MAX_STREAMS_PER_IP_OWNER = 4
_MAX_STREAMS_PER_IP_ANON = 2


async def _build_status(db: AsyncSession) -> dict:
    """Get public status of all locations.

    For locations with multiple providers, availability is the sum of
    available slots across all providers serving that location.
    """
    locations = await get_enabled_locations(db)

    # Build per-location active counts
    active_result = await db.execute(
        select(Reservation.location, func.count(Reservation.id))
        .where(Reservation.status.in_([ReservationStatus.PROVISIONING, ReservationStatus.ACTIVE]))
        .group_by(Reservation.location)
    )
    active_by_location: dict[str, int] = dict(active_result.all())

    def _is_region_scoped_provider(provider_code: str) -> bool:
        return provider_code == "gcore"

    # Load all LocationProvider entries for quota accounting (only from enabled providers)
    lp_result = await db.execute(
        select(LocationProvider)
        .join(Provider, LocationProvider.provider_code == Provider.code)
        .where(LocationProvider.enabled == True)
        .where(Provider.enabled == True)
    )
    all_location_providers = list(lp_result.scalars().all())

    # Build provider→region mappings from LocationProvider entries.
    # Also build per-instance provider info from CloudInstance records.
    # A single location can now map to multiple (provider, region) pairs.
    location_to_providers: dict[str, list[LocationProvider]] = {}
    for lp in all_location_providers:
        location_to_providers.setdefault(lp.location_code, []).append(lp)

    # Fallback: locations without LocationProvider entries use legacy fields
    loc_all_result = await db.execute(select(EnabledLocation))
    all_locations_map = {loc.code: loc for loc in loc_all_result.scalars().all()}

    # Count active instances per (provider, region) from CloudInstance records.
    # Use provider_code stored on instances; fall back to location lookup for legacy.
    instance_result = await db.execute(
        select(CloudInstance)
        .where(CloudInstance.status != "terminated")
    )
    all_instances = list(instance_result.scalars().all())

    # Active (non-warm) counts per provider (global) and per (provider, region)
    active_by_provider: dict[str, int] = {}
    active_by_provider_region: dict[tuple[str, str], int] = {}
    # Warm counts per location, per provider, per (provider, region)
    warm_by_location: dict[str, int] = {}
    warm_by_provider: dict[str, int] = {}
    warm_by_provider_region: dict[tuple[str, str], int] = {}
    # Warm counts per (location, provider)
    warm_by_loc_provider: dict[tuple[str, str], int] = {}

    for inst in all_instances:
        prov = inst.provider_code
        prov_region = inst.provider_region
        # Legacy fallback
        if not prov:
            loc_info = all_locations_map.get(inst.location)
            if loc_info:
                prov = loc_info.provider
                prov_region = loc_info.provider_region

        if not prov:
            continue

        if inst.is_available:
            warm_by_location[inst.location] = warm_by_location.get(inst.location, 0) + 1
            warm_by_provider[prov] = warm_by_provider.get(prov, 0) + 1
            warm_by_loc_provider[(inst.location, prov)] = warm_by_loc_provider.get((inst.location, prov), 0) + 1
            if prov_region:
                key = (prov, prov_region)
                warm_by_provider_region[key] = warm_by_provider_region.get(key, 0) + 1
        else:
            active_by_provider[prov] = active_by_provider.get(prov, 0) + 1
            if prov_region:
                key = (prov, prov_region)
                active_by_provider_region[key] = active_by_provider_region.get(key, 0) + 1

    # Load provider limits (only enabled providers)
    providers_result = await db.execute(select(Provider).where(Provider.enabled == True))
    providers: dict[str, Provider] = {p.code: p for p in providers_result.scalars().all()}

    status = {}
    for location in locations:
        active_count = active_by_location.get(location.code, 0)
        warm_count = warm_by_location.get(location.code, 0)

        # Get all providers for this location
        loc_providers = location_to_providers.get(location.code, [])

        # Fallback to legacy single-provider if no LocationProvider entries
        if not loc_providers and location.provider and location.provider_region:
            loc_providers = [LocationProvider(
                location_code=location.code,
                provider_code=location.provider,
                provider_region=location.provider_region,
                instance_plan=location.instance_plan,
                region_instance_limit=location.region_instance_limit,
            )]

        # Sum available slots across all providers for this location
        total_remaining = 0
        for lp in loc_providers:
            provider_record = providers.get(lp.provider_code)
            default_limit = provider_record.instance_limit if provider_record else 10

            if _is_region_scoped_provider(lp.provider_code):
                region_limit = lp.region_instance_limit or default_limit
                region_key = (lp.provider_code, lp.provider_region)
                region_active = active_by_provider_region.get(region_key, 0)
                region_warm = warm_by_provider_region.get(region_key, 0)
                total_remaining += max(0, region_limit - region_active - region_warm)
            else:
                provider_active = active_by_provider.get(lp.provider_code, 0)
                provider_warm = warm_by_provider.get(lp.provider_code, 0)
                total_remaining += max(0, default_limit - provider_active - provider_warm)

        location_available = warm_count + total_remaining

        status[location.code] = {
            "name": location.name,
            "city": location.city,
            "country": location.country,
            "continent": location.continent,
            "flag": build_location_flag(
                country=location.country,
                city=location.city,
                name=location.name,
                provider_region=location.provider_region,
                subdivision=location.subdivision,
            ),
            "recommended": location.recommended,
            "instant": warm_count > 0,
            "active": active_count,
            "available": location_available,
            "enabled": location.enabled,
        }

    return status


@router.get("/api/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Cached wrapper around _build_status."""
    global _status_cache, _status_cache_time
    now = time.monotonic()
    if _status_cache is not None and (now - _status_cache_time) < _STATUS_CACHE_TTL:
        return _status_cache
    result = await _build_status(db)
    _status_cache = result
    _status_cache_time = now
    return result


@router.get("/api/banned_user.cfg")
async def get_banned_users_cfg(
    db: AsyncSession = Depends(get_db),
):
    """Get banned users in Source engine cfg format.
    
    Returns a plain text file with one 'banid 0 <SteamID2>' per line.
    Used by TF2 servers for ban enforcement.
    """
    from fastapi.responses import PlainTextResponse
    from app.models.user import User
    from app.utils.steam import steamid64_to_steamid2
    
    result = await db.execute(
        select(User.steam_id).where(User.is_banned == True)
    )
    banned_ids = [row[0] for row in result.all()]
    
    # Format: banid 0 STEAM_0:X:Y
    lines = [f"banid 0 {steamid64_to_steamid2(sid)}" for sid in banned_ids]
    content = "\n".join(lines)
    
    return PlainTextResponse(
        content=content,
        media_type="text/plain",
        headers={"Cache-Control": "max-age=60"}  # Cache for 1 minute
    )


async def reservation_event_generator(
    reservation_id: int,
    is_owner: bool = False,
    base_poll_seconds: int = 1,
) -> AsyncGenerator[str, None]:
    """Generate SSE events for reservation status updates."""
    from app.database import async_session_maker

    last_status = None
    start_time = time.monotonic()

    while True:
        # Create fresh session each time to get latest data
        async with async_session_maker() as db:
            result = await db.execute(
                select(Reservation)
                .where(Reservation.id == reservation_id)
                .options(selectinload(Reservation.cloud_instance))  # Eagerly load
            )
            reservation = result.scalar_one_or_none()

            if not reservation:
                yield json.dumps({"type": "error", "message": "Reservation not found"})
                break

            current_status = reservation.status.value

            # Get boot progress from agent if available
            from app.routers.internal import get_boot_progress
            progress_data = None
            if reservation.cloud_instance:
                progress_data = get_boot_progress(reservation.cloud_instance.instance_id)

        # Send update if status changed or progress updated
        if current_status != last_status:
            event_data = {
                "type": "status_change",
                "status": current_status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # Include boot progress if available
            if progress_data:
                event_data["boot_progress"] = progress_data

            # Include connection details when active — only for owner
            if reservation.status == ReservationStatus.ACTIVE and is_owner:
                event_data["connection"] = {
                    "sdr_ip": reservation.sdr_ip,
                    "sdr_port": reservation.sdr_port,
                    "sdr_tv_port": reservation.sdr_tv_port,
                    "password": reservation.password,
                    # NOTE: Intentionally excluded from API responses to avoid exposing RCON creds.
                    # Re-enable by uncommenting this line.
                    # "rcon_password": reservation.rcon_password,
                    "tv_password": reservation.tv_password,
                    "enable_direct_connect": reservation.enable_direct_connect,
                }
                if reservation.enable_direct_connect and reservation.cloud_instance:
                    ip = reservation.cloud_instance.ip_address
                    if ip and ip != "0.0.0.0":
                        event_data["connection"]["ip_address"] = ip

            # Include error message for failed status
            if reservation.status == ReservationStatus.FAILED:
                event_data["error"] = public_failure_reason(
                    reservation.status,
                    reservation.provision_attempts,
                    reservation.failure_reason,
                )

            yield json.dumps(event_data)
            last_status = current_status
        elif progress_data and current_status == "provisioning":
            # Send progress updates even if status hasn't changed
            yield json.dumps({
                "type": "boot_progress",
                "boot_progress": progress_data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # Stop streaming for terminal states
        if reservation.status in (
            ReservationStatus.ACTIVE,
            ReservationStatus.ENDED,
            ReservationStatus.FAILED,
            ReservationStatus.CANCELLED,
        ):
            break

        elapsed = time.monotonic() - start_time
        sleep_seconds = base_poll_seconds if elapsed < 300 else max(base_poll_seconds, 3)
        await asyncio.sleep(sleep_seconds)


async def acquire_reservation_stream_slot(
    reservation_id: int,
    client_ip: str,
    per_ip_limit: int,
) -> None:
    """Limit concurrent SSE streams per reservation and per client IP."""
    async with _reservation_stream_lock:
        current_reservation_count = _reservation_stream_counts.get(reservation_id, 0)
        if current_reservation_count >= _MAX_STREAMS_PER_RESERVATION:
            raise HTTPException(status_code=429, detail="Too many live status viewers for this reservation")

        ip_key = (reservation_id, client_ip)
        current_ip_count = _reservation_stream_ip_counts.get(ip_key, 0)
        if current_ip_count >= per_ip_limit:
            raise HTTPException(status_code=429, detail="Too many live status streams from this IP")

        _reservation_stream_counts[reservation_id] = current_reservation_count + 1
        _reservation_stream_ip_counts[ip_key] = current_ip_count + 1


async def release_reservation_stream_slot(reservation_id: int, client_ip: str) -> None:
    """Release an SSE slot when a stream finishes."""
    async with _reservation_stream_lock:
        reservation_count = _reservation_stream_counts.get(reservation_id, 0)
        if reservation_count <= 1:
            _reservation_stream_counts.pop(reservation_id, None)
        elif reservation_count > 1:
            _reservation_stream_counts[reservation_id] = reservation_count - 1

        ip_key = (reservation_id, client_ip)
        ip_count = _reservation_stream_ip_counts.get(ip_key, 0)
        if ip_count <= 1:
            _reservation_stream_ip_counts.pop(ip_key, None)
        elif ip_count > 1:
            _reservation_stream_ip_counts[ip_key] = ip_count - 1


@router.get("/api/reservations/{reservation_id}/events")
async def reservation_events(
    reservation_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint for reservation status updates.

    Only the reservation owner (or an admin) can subscribe.
    """
    from app.routers.auth import get_current_user

    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    client_ip = request.client.host if request.client and request.client.host else "unknown"

    # Determine ownership
    result = await db.execute(
        select(Reservation.user_id).where(Reservation.id == reservation_id)
    )
    row = result.first()
    if not row or (row[0] != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Reservation not found")

    is_owner = True

    per_ip_limit = _MAX_STREAMS_PER_IP_OWNER if is_owner else _MAX_STREAMS_PER_IP_ANON
    await acquire_reservation_stream_slot(reservation_id, client_ip, per_ip_limit)

    async def event_stream():
        try:
            poll_seconds = 1 if is_owner else 3
            async for event in reservation_event_generator(
                reservation_id,
                is_owner=is_owner,
                base_poll_seconds=poll_seconds,
            ):
                if await request.is_disconnected():
                    break
                yield {"event": "message", "data": event}
        finally:
            await release_reservation_stream_slot(reservation_id, client_ip)

    return EventSourceResponse(event_stream())

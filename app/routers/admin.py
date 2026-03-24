"""Admin panel endpoints."""

from decimal import Decimal
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.reservation import Reservation, ReservationStatus
from app.models.instance import CloudInstance, EnabledLocation, LocationProvider, Provider
from app.models.cost import MonthlyCost
from app.routers.auth import require_admin
from app.services.orchestrator import get_enabled_locations
from app.utils.maps import is_valid_map_name
from app.utils.location_flags import normalize_subdivision


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")


class LocationToggleRequest(BaseModel):
    """Request to toggle a location's enabled status."""
    enabled: bool


class CreateLocationRequest(BaseModel):
    """Request to create a new location."""
    code: str
    name: str
    provider: str = "vultr"
    provider_region: str
    billing_model: str = "hourly"
    city: str | None = None
    country: str | None = None
    continent: str | None = None
    subdivision: str | None = None
    recommended: bool = False
    instance_plan: str | None = None
    region_instance_limit: int | None = None


class UpdateLocationRequest(BaseModel):
    """Request to update a location."""
    name: str | None = None
    provider: str | None = None
    provider_region: str | None = None
    display_order: int | None = None
    city: str | None = None
    country: str | None = None
    continent: str | None = None
    subdivision: str | None = None
    recommended: bool | None = None
    instance_plan: str | None = None
    region_instance_limit: int | None = None


class CreateProviderRequest(BaseModel):
    """Request to create a new provider."""
    code: str
    name: str
    billing_model: str = "hourly"


class UpdateProviderRequest(BaseModel):
    """Request to update a provider."""
    name: str | None = None
    billing_model: str | None = None
    instance_plan: str | None = None
    container_image: str | None = None
    instance_limit: int | None = None
    enabled: bool | None = None
    display_order: int | None = None


@router.get("", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render admin panel."""
    # Get locations
    result = await db.execute(
        select(EnabledLocation).order_by(EnabledLocation.display_order)
    )
    locations = list(result.scalars().all())
    
    # Get active instances
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.status != "terminated")
    )
    instances = list(result.scalars().all())
    
    # Get current month cost
    current_month = datetime.now().strftime("%Y-%m")
    result = await db.execute(
        select(MonthlyCost).where(MonthlyCost.year_month == current_month)
    )
    monthly_cost = result.scalar_one_or_none()
    
    # Get reservation stats
    result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.status.in_([
            ReservationStatus.PROVISIONING,
            ReservationStatus.ACTIVE,
        ]))
    )
    active_count = result.scalar_one()
    
    result = await db.execute(select(func.count(Reservation.id)))
    total_count = result.scalar_one()
    
    # Get recent reservations with user and cloud instance info
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Reservation)
        .options(selectinload(Reservation.user), selectinload(Reservation.cloud_instance))
        .order_by(Reservation.created_at.desc())
        .limit(200)
    )
    reservations = result.scalars().all()
    
    # Build a location lookup for provider info
    location_lookup = {loc.code: loc for loc in locations}
    
    # Convert reservations to serializable dicts
    reservations_data = [
        {
            "id": r.id,
            "reservation_number": r.reservation_number,
            "location": r.location,
            "location_name": location_lookup[r.location].name if r.location in location_lookup else r.location,
            "provider": location_lookup[r.location].provider if r.location in location_lookup else None,
            "instance_id": r.cloud_instance.instance_id if r.cloud_instance else r.instance_id,
            "cloud_instance_db_id": r.cloud_instance.id if r.cloud_instance else r.instance_id,
            "status": r.status.value,
            "user_name": r.user.display_name if r.user else "Unknown",
            "user_steam_id": r.user.steam_id if r.user else None,
            "starts_at": r.starts_at.isoformat() if r.starts_at else None,
            "ends_at": r.ends_at.isoformat() if r.ends_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "first_map": r.first_map,
            "sdr_ip": r.sdr_ip,
            "ip_address": r.cloud_instance.ip_address if r.cloud_instance else None,
        }
        for r in reservations
    ]
    
    # Convert locations to serializable dicts
    locations_data = [
        {
            "code": loc.code,
            "name": loc.name,
            "city": loc.city,
            "country": loc.country,
            "continent": loc.continent,
            "subdivision": loc.subdivision,
            "provider": loc.provider,
            "provider_region": loc.provider_region,
            "billing_model": loc.billing_model,
            "recommended": loc.recommended,
            "enabled": loc.enabled,
            "display_order": loc.display_order,
            "instance_plan": loc.instance_plan,
            "region_instance_limit": loc.region_instance_limit,
        }
        for loc in locations
    ]
    
    # Convert instances to serializable dicts with warm pool info
    from datetime import timezone
    now = datetime.now(timezone.utc)
    
    def get_billing_minutes(billing_end):
        if not billing_end:
            return None
        # Make naive datetime timezone-aware if needed
        if billing_end.tzinfo is None:
            billing_end = billing_end.replace(tzinfo=timezone.utc)
        return int((billing_end - now).total_seconds() / 60)
    
    instances_data = [
        {
            "id": inst.id,
            "instance_id": inst.instance_id,
            "location": inst.location,
            "ip_address": inst.ip_address,
            "status": inst.status,
            "is_available": inst.is_available,
            "billing_hour_ends_at": inst.billing_hour_ends_at.isoformat() if inst.billing_hour_ends_at else None,
            "minutes_until_billing": get_billing_minutes(inst.billing_hour_ends_at),
            "current_reservation_id": inst.current_reservation_id,
        }
        for inst in instances
    ]
    
    # Get maps
    from app.models.instance import GameMap
    result = await db.execute(
        select(GameMap).order_by(GameMap.display_order)
    )
    maps_data = [
        {
            "id": m.id,
            "name": m.name,
            "display_name": m.display_name,
            "enabled": m.enabled,
            "is_default": m.is_default,
        }
        for m in result.scalars().all()
    ]
    
    # Get users list
    result = await db.execute(
        select(User).order_by(User.last_login_at.desc())
    )
    users_data = [
        {
            "steam_id": u.steam_id,
            "display_name": u.display_name,
            "is_banned": u.is_banned,
            "ban_reason": u.ban_reason or "",
            "is_admin": u.is_admin,
            "reservation_count": u.reservation_count,
        }
        for u in result.scalars().all()
    ]
    
    # Get providers list
    result = await db.execute(
        select(Provider).order_by(Provider.display_order)
    )
    providers_data = [
        {
            "code": p.code,
            "name": p.name,
            "billing_model": p.billing_model,
            "instance_plan": p.instance_plan,
            "container_image": p.container_image,
            "instance_limit": p.instance_limit,
            "enabled": p.enabled,
            "display_order": p.display_order,
        }
        for p in result.scalars().all()
    ]

    # Get location-provider mappings
    result = await db.execute(
        select(LocationProvider).order_by(LocationProvider.location_code, LocationProvider.priority)
    )
    from app.services.provider_priority import get_provider_status
    location_providers_data = [
        {
            "id": lp.id,
            "location_code": lp.location_code,
            "provider_code": lp.provider_code,
            "provider_region": lp.provider_region,
            "priority": lp.priority,
            "enabled": lp.enabled,
            "instance_plan": lp.instance_plan,
            "region_instance_limit": lp.region_instance_limit,
            **get_provider_status(lp.location_code, lp.provider_code),
        }
        for lp in result.scalars().all()
    ]
    
    # Get trivia facts
    from app.models.trivia import TriviaFact
    result = await db.execute(
        select(TriviaFact).order_by(TriviaFact.scope, TriviaFact.key, TriviaFact.id)
    )
    trivia_data = [
        {
            "id": t.id,
            "scope": t.scope,
            "key": t.key,
            "fact": t.fact,
        }
        for t in result.scalars().all()
    ]

    # Get rate limit settings, server settings, and steam trust settings
    from app.services.settings import get_rate_limit_settings, get_fastdl_url, get_steam_trust_settings, get_reservation_settings
    from app.services.captcha import get_captcha_settings
    rate_limit_settings = await get_rate_limit_settings(db)
    fastdl_url = await get_fastdl_url(db)
    steam_trust_settings = await get_steam_trust_settings(db)
    reservation_settings = await get_reservation_settings(db)
    captcha_settings = await get_captcha_settings(db)

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "user": user,
            "locations": locations_data,
            "providers": providers_data,
            "location_providers": location_providers_data,
            "instances": instances_data,
            "reservations": reservations_data,
            "maps": maps_data,
            "users": users_data,
            "monthly_cost": monthly_cost,
            "active_reservations": active_count,
            "total_reservations": total_count,
            "rate_limit_settings": rate_limit_settings,
            "fastdl_url": fastdl_url,
            "steam_trust_settings": steam_trust_settings,
            "reservation_settings": reservation_settings,
            "captcha_settings": captcha_settings,
            "trivia": trivia_data,
        }
    )


@router.post("/locations/{location_code}/toggle")
async def toggle_location(
    location_code: str,
    request: LocationToggleRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a location's enabled status."""
    result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == location_code)
    )
    location = result.scalar_one_or_none()
    
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    
    location.enabled = request.enabled
    await db.commit()
    
    return {"code": location.code, "enabled": location.enabled}


@router.post("/locations")
async def create_location(
    request: CreateLocationRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new location."""
    # Check if code already exists
    result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == request.code)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Location code already exists")
    
    # Get max display order
    result = await db.execute(select(func.max(EnabledLocation.display_order)))
    max_order = result.scalar_one() or 0

    if request.region_instance_limit is not None and request.region_instance_limit < 1:
        raise HTTPException(status_code=400, detail="region_instance_limit must be >= 1")

    subdivision = normalize_subdivision(request.subdivision) if request.subdivision else None

    new_location = EnabledLocation(
        code=request.code,
        name=request.name,
        provider=request.provider,
        provider_region=request.provider_region,
        vultr_region=request.provider_region if request.provider == "vultr" else None,  # Legacy field
        billing_model=request.billing_model,
        city=request.city,
        country=request.country,
        continent=request.continent,
        subdivision=subdivision,
        recommended=request.recommended,
        instance_plan=request.instance_plan or None,
        region_instance_limit=request.region_instance_limit,
        enabled=True,
        display_order=max_order + 1,
    )
    db.add(new_location)
    await db.commit()
    await db.refresh(new_location)
    
    return {
        "code": new_location.code,
        "name": new_location.name,
        "provider": new_location.provider,
        "provider_region": new_location.provider_region,
    }


@router.put("/locations/{location_code}")
async def update_location(
    location_code: str,
    request: UpdateLocationRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a location."""
    result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == location_code)
    )
    location = result.scalar_one_or_none()
    
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    
    # Update only provided fields
    if request.name is not None:
        location.name = request.name
    if request.provider is not None:
        location.provider = request.provider
    if request.provider_region is not None:
        location.provider_region = request.provider_region
        if location.provider == "vultr":
            location.vultr_region = request.provider_region
    if request.display_order is not None:
        location.display_order = request.display_order
    if request.city is not None:
        location.city = request.city
    if request.country is not None:
        location.country = request.country
    if request.continent is not None:
        location.continent = request.continent
    if request.subdivision is not None:
        location.subdivision = normalize_subdivision(request.subdivision) if request.subdivision else None
    if request.recommended is not None:
        location.recommended = request.recommended
    if request.instance_plan is not None:
        location.instance_plan = request.instance_plan or None
    if request.region_instance_limit is not None:
        if request.region_instance_limit < 1:
            raise HTTPException(status_code=400, detail="region_instance_limit must be >= 1")
        location.region_instance_limit = request.region_instance_limit

    await db.commit()

    return {
        "code": location.code,
        "name": location.name,
        "city": location.city,
        "country": location.country,
        "continent": location.continent,
        "subdivision": location.subdivision,
        "provider": location.provider,
        "provider_region": location.provider_region,
        "billing_model": location.billing_model,
        "recommended": location.recommended,
    }


@router.delete("/locations/{location_code}")
async def delete_location(
    location_code: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a location."""
    result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == location_code)
    )
    location = result.scalar_one_or_none()
    
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    
    # Check for active reservations in this location
    result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.location == location_code)
        .where(Reservation.status.in_([
            ReservationStatus.PROVISIONING,
            ReservationStatus.ACTIVE,
        ]))
    )
    active_count = result.scalar_one()
    
    if active_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete location with {active_count} active reservation(s)"
        )
    
    # Check for active instances in this location
    result = await db.execute(
        select(func.count(CloudInstance.id))
        .where(CloudInstance.location == location_code)
        .where(CloudInstance.status != "terminated")
    )
    instance_count = result.scalar_one()
    
    if instance_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete location with {instance_count} active instance(s)"
        )
    
    await db.delete(location)
    await db.commit()
    
    return {"deleted": True, "code": location_code}


# ==================== PROVIDER CRUD ====================

@router.post("/providers")
async def create_provider(
    request: CreateProviderRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new provider."""
    # Check if code already exists
    result = await db.execute(
        select(Provider).where(Provider.code == request.code)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Provider code already exists")
    
    # Get max display order
    result = await db.execute(select(func.max(Provider.display_order)))
    max_order = result.scalar_one() or 0
    
    new_provider = Provider(
        code=request.code,
        name=request.name,
        billing_model=request.billing_model,
        enabled=True,
        display_order=max_order + 1,
    )
    db.add(new_provider)
    await db.commit()
    await db.refresh(new_provider)
    
    return {
        "code": new_provider.code,
        "name": new_provider.name,
        "billing_model": new_provider.billing_model,
    }


@router.put("/providers/{provider_code}")
async def update_provider(
    provider_code: str,
    request: UpdateProviderRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a provider."""
    result = await db.execute(
        select(Provider).where(Provider.code == provider_code)
    )
    provider = result.scalar_one_or_none()
    
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    # Update only provided fields
    if request.name is not None:
        provider.name = request.name
    if request.billing_model is not None:
        provider.billing_model = request.billing_model
    if request.instance_plan is not None:
        provider.instance_plan = request.instance_plan
    if request.container_image is not None:
        provider.container_image = request.container_image
    if request.instance_limit is not None:
        provider.instance_limit = request.instance_limit
    if request.enabled is not None:
        provider.enabled = request.enabled
    if request.display_order is not None:
        provider.display_order = request.display_order
    
    await db.commit()
    
    return {
        "code": provider.code,
        "name": provider.name,
        "billing_model": provider.billing_model,
        "instance_plan": provider.instance_plan,
        "container_image": provider.container_image,
        "enabled": provider.enabled,
    }


@router.delete("/providers/{provider_code}")
async def delete_provider(
    provider_code: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a provider."""
    result = await db.execute(
        select(Provider).where(Provider.code == provider_code)
    )
    provider = result.scalar_one_or_none()
    
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    # Check for locations using this provider (legacy + location_providers)
    result = await db.execute(
        select(func.count(EnabledLocation.code))
        .where(EnabledLocation.provider == provider_code)
    )
    location_count = result.scalar_one()

    result = await db.execute(
        select(func.count(LocationProvider.id))
        .where(LocationProvider.provider_code == provider_code)
    )
    lp_count = result.scalar_one()

    total = location_count + lp_count
    if total > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete provider with {total} location mapping(s) using it"
        )
    
    await db.delete(provider)
    await db.commit()
    
    return {"deleted": True, "code": provider_code}


# ==================== LOCATION PROVIDER PRIORITY CRUD ====================


class CreateLocationProviderRequest(BaseModel):
    """Request to add a provider to a location."""
    location_code: str
    provider_code: str
    provider_region: str
    priority: int = 0
    instance_plan: str | None = None
    region_instance_limit: int | None = None


class UpdateLocationProviderRequest(BaseModel):
    """Request to update a location-provider mapping."""
    provider_region: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    instance_plan: str | None = None
    region_instance_limit: int | None = None


@router.get("/location-providers")
async def list_location_providers(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all location-provider mappings."""
    result = await db.execute(
        select(LocationProvider).order_by(LocationProvider.location_code, LocationProvider.priority)
    )
    return [
        {
            "id": lp.id,
            "location_code": lp.location_code,
            "provider_code": lp.provider_code,
            "provider_region": lp.provider_region,
            "priority": lp.priority,
            "enabled": lp.enabled,
            "instance_plan": lp.instance_plan,
            "region_instance_limit": lp.region_instance_limit,
        }
        for lp in result.scalars().all()
    ]


@router.post("/location-providers")
async def create_location_provider(
    request: CreateLocationProviderRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a provider to a location with a given priority."""
    # Validate location exists
    loc = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == request.location_code)
    )
    if not loc.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Location not found")

    # Validate provider exists
    prov = await db.execute(
        select(Provider).where(Provider.code == request.provider_code)
    )
    if not prov.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Provider not found")

    if request.region_instance_limit is not None and request.region_instance_limit < 1:
        raise HTTPException(status_code=400, detail="region_instance_limit must be >= 1")

    lp = LocationProvider(
        location_code=request.location_code,
        provider_code=request.provider_code,
        provider_region=request.provider_region,
        priority=request.priority,
        enabled=True,
        instance_plan=request.instance_plan or None,
        region_instance_limit=request.region_instance_limit,
    )
    db.add(lp)
    await db.commit()
    await db.refresh(lp)

    return {
        "id": lp.id,
        "location_code": lp.location_code,
        "provider_code": lp.provider_code,
        "provider_region": lp.provider_region,
        "priority": lp.priority,
    }


@router.put("/location-providers/{lp_id}")
async def update_location_provider(
    lp_id: int,
    request: UpdateLocationProviderRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a location-provider mapping."""
    result = await db.execute(
        select(LocationProvider).where(LocationProvider.id == lp_id)
    )
    lp = result.scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Location-provider mapping not found")

    if request.provider_region is not None:
        lp.provider_region = request.provider_region
    if request.priority is not None:
        lp.priority = request.priority
    if request.enabled is not None:
        lp.enabled = request.enabled
    if request.instance_plan is not None:
        lp.instance_plan = request.instance_plan or None
    if request.region_instance_limit is not None:
        if request.region_instance_limit < 1:
            raise HTTPException(status_code=400, detail="region_instance_limit must be >= 1")
        lp.region_instance_limit = request.region_instance_limit

    await db.commit()
    return {
        "id": lp.id,
        "location_code": lp.location_code,
        "provider_code": lp.provider_code,
        "provider_region": lp.provider_region,
        "priority": lp.priority,
        "enabled": lp.enabled,
    }


@router.delete("/location-providers/{lp_id}")
async def delete_location_provider(
    lp_id: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a provider from a location."""
    result = await db.execute(
        select(LocationProvider).where(LocationProvider.id == lp_id)
    )
    lp = result.scalar_one_or_none()
    if not lp:
        raise HTTPException(status_code=404, detail="Location-provider mapping not found")

    await db.delete(lp)
    await db.commit()
    return {"deleted": True, "id": lp_id}


# ==================== PROVIDER FAILOVER STATUS ====================


@router.get("/provider-status")
async def get_provider_failover_status(
    user: User = Depends(require_admin),
):
    """Get current provider failure tracking status (in-memory)."""
    from app.services.provider_priority import get_all_provider_status
    return get_all_provider_status()


@router.post("/provider-status/{location_code}/{provider_code}/reset")
async def reset_provider_status(
    location_code: str,
    provider_code: str,
    user: User = Depends(require_admin),
):
    """Manually reset suspension for a provider at a location."""
    from app.services.provider_priority import reset_provider_suspension
    reset_provider_suspension(location_code, provider_code)
    return {"reset": True, "location": location_code, "provider": provider_code}


@router.post("/instances/{instance_id}/destroy")
async def force_destroy_instance(
    instance_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Force destroy an instance."""
    from app.services.orchestrator import destroy_instance
    
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == instance_id)
    )
    instance = result.scalar_one_or_none()
    
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    
    success = await destroy_instance(instance_id, db)
    return {"destroyed": success, "instance_id": instance_id}


class MapToggleRequest(BaseModel):
    """Request to toggle a map's enabled status."""
    enabled: bool


class AddMapRequest(BaseModel):
    """Request to add a new map."""
    name: str
    display_name: str


class BulkImportMapsRequest(BaseModel):
    """Request to bulk-import maps (one map name per line)."""
    maps_text: str


@router.post("/maps/{map_id}/toggle")
async def toggle_map(
    map_id: int,
    request: MapToggleRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a map's enabled status."""
    from app.models.instance import GameMap
    
    result = await db.execute(
        select(GameMap).where(GameMap.id == map_id)
    )
    game_map = result.scalar_one_or_none()
    
    if not game_map:
        raise HTTPException(status_code=404, detail="Map not found")
    
    game_map.enabled = request.enabled
    await db.commit()
    
    return {"id": game_map.id, "name": game_map.name, "enabled": game_map.enabled}


@router.post("/maps")
async def add_map(
    request: AddMapRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new map."""
    from app.models.instance import GameMap

    name = request.name.strip()
    if not is_valid_map_name(name):
        raise HTTPException(status_code=400, detail="Invalid map name")

    display_name = request.display_name.strip() or name
    
    # Get max display order
    result = await db.execute(select(func.max(GameMap.display_order)))
    max_order = result.scalar_one() or 0
    
    new_map = GameMap(
        name=name,
        display_name=display_name,
        enabled=True,
        display_order=max_order + 1,
    )
    db.add(new_map)
    await db.commit()
    await db.refresh(new_map)
    
    return {"id": new_map.id, "name": new_map.name, "display_name": new_map.display_name}


@router.post("/maps/bulk")
async def bulk_import_maps(
    request: BulkImportMapsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-import maps from a newline-separated list of map names."""
    from app.models.instance import GameMap

    lines = [line.strip() for line in request.maps_text.splitlines()]
    names = [n for n in lines if n]
    if not names:
        raise HTTPException(status_code=400, detail="No map names provided")

    # Fetch existing map names to skip duplicates
    result = await db.execute(select(GameMap.name))
    existing = {row[0] for row in result}

    # Get current max display order
    result = await db.execute(select(func.max(GameMap.display_order)))
    max_order = result.scalar_one() or 0

    added = []
    skipped = []
    for name in names:
        if not is_valid_map_name(name):
            raise HTTPException(status_code=400, detail=f"Invalid map name: {name}")
        if name in existing:
            skipped.append(name)
            continue
        max_order += 1
        new_map = GameMap(
            name=name,
            display_name=name,
            enabled=True,
            display_order=max_order,
        )
        db.add(new_map)
        existing.add(name)
        added.append(name)

    await db.commit()
    return {"added": added, "skipped": skipped}


@router.delete("/maps/{map_id}")
async def delete_map(
    map_id: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a map."""
    from app.models.instance import GameMap
    
    result = await db.execute(
        select(GameMap).where(GameMap.id == map_id)
    )
    game_map = result.scalar_one_or_none()
    
    if not game_map:
        raise HTTPException(status_code=404, detail="Map not found")
    
    await db.delete(game_map)
    await db.commit()
    
    return {"deleted": True, "id": map_id}


class BanToggleRequest(BaseModel):
    """Request to ban/unban a user."""
    banned: bool
    reason: str = ""


@router.post("/users/{steam_id}/ban")
async def toggle_user_ban(
    steam_id: str,
    request: BanToggleRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ban or unban a user."""
    result = await db.execute(
        select(User).where(User.steam_id == steam_id)
    )
    target_user = result.scalar_one_or_none()

    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    target_user.is_banned = request.banned
    target_user.ban_reason = request.reason.strip() if request.banned else None
    await db.commit()

    return {
        "steam_id": steam_id,
        "display_name": target_user.display_name,
        "banned": target_user.is_banned,
        "ban_reason": target_user.ban_reason,
        "is_admin": target_user.is_admin,
        "reservation_count": target_user.reservation_count,
    }


class PreBanRequest(BaseModel):
    """Request to pre-ban a Steam ID that hasn't registered yet."""
    steam_id: str
    reason: str = ""


@router.post("/users/pre-ban")
async def pre_ban_user(
    request: PreBanRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ban a Steam ID, creating a placeholder user if they haven't registered."""
    steam_id = request.steam_id.strip()
    if not steam_id.isdigit() or len(steam_id) != 17:
        raise HTTPException(status_code=400, detail="Invalid Steam ID (must be 17-digit SteamID64)")

    result = await db.execute(select(User).where(User.steam_id == steam_id))
    target_user = result.scalar_one_or_none()

    if target_user:
        target_user.is_banned = True
        target_user.ban_reason = request.reason.strip() or None
    else:
        # Fetch display name from Steam if possible
        from app.routers.auth import get_steam_player_info
        player_info = await get_steam_player_info(steam_id)
        target_user = User(
            steam_id=steam_id,
            display_name=player_info.get("personaname", f"User {steam_id[-4:]}"),
            avatar_url=player_info.get("avatarfull", ""),
            is_banned=True,
            ban_reason=request.reason.strip() or None,
        )
        db.add(target_user)

    await db.commit()
    await db.refresh(target_user)

    return {
        "steam_id": steam_id,
        "display_name": target_user.display_name,
        "banned": True,
        "ban_reason": target_user.ban_reason,
        "is_admin": target_user.is_admin,
        "reservation_count": target_user.reservation_count,
    }


@router.get("/api/stats")
async def get_admin_stats(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get admin statistics."""
    # Active reservations by location
    locations = await get_enabled_locations(db)
    location_stats = {}
    
    for location in locations:
        result = await db.execute(
            select(func.count(Reservation.id))
            .where(Reservation.location == location.code)
            .where(Reservation.status.in_([
                ReservationStatus.PROVISIONING,
                ReservationStatus.ACTIVE,
            ]))
        )
        active = result.scalar_one()
        
        result = await db.execute(
            select(func.count(Reservation.id))
            .where(Reservation.location == location.code)
        )
        total = result.scalar_one()
        
        location_stats[location.code] = {
            "name": location.name,
            "active": active,
            "total": total,
            "enabled": location.enabled,
        }
    
    # Monthly costs (last 6 months)
    result = await db.execute(
        select(MonthlyCost)
        .order_by(MonthlyCost.year_month.desc())
        .limit(6)
    )
    monthly_costs = [
        {
            "month": mc.year_month,
            "hours": mc.total_hours,
            "cost_usd": float(mc.total_cost_usd),
            "reservations": mc.reservation_count,
        }
        for mc in result.scalars().all()
    ]
    
    return {
        "locations": location_stats,
        "monthly_costs": monthly_costs,
    }


class UpdateSettingsRequest(BaseModel):
    """Request to update site settings."""
    # Reservation defaults
    max_duration_hours: int | None = None
    auto_end_minutes: int | None = None
    # Rate limits
    per_user_hour: int | None = None
    admin_per_hour: int | None = None
    failed_multiplier: int | None = None
    site_provisioning_max: int | None = None
    fastdl_url: str | None = None
    # New rate limits
    per_user_day: int | None = None
    admin_per_day: int | None = None
    daily_hours_limit: int | None = None
    sitewide_per_hour: int | None = None
    sitewide_per_day: int | None = None
    # Circuit breaker
    circuit_breaker_window_minutes: int | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_minutes: int | None = None
    # Steam trust settings
    steam_min_account_age_days: int | None = None
    steam_min_tf2_hours: int | None = None
    steam_require_tf2_ownership: bool | None = None
    steam_block_vac_banned: bool | None = None
    steam_require_public_profile: bool | None = None
    # Captcha settings
    captcha_enabled: bool | None = None
    captcha_trust_after_n: int | None = None
    captcha_min_tf2_hours: int | None = None
    captcha_min_account_age_days: int | None = None


@router.get("/settings")
async def get_settings_endpoint(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get current rate limit settings."""
    from app.services.settings import get_rate_limit_settings
    return await get_rate_limit_settings(db)


@router.put("/settings")
async def update_settings_endpoint(
    request: UpdateSettingsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update rate limit settings."""
    from app.services.settings import set_setting

    # Reservation defaults (must be >= 1)
    reservation_mapping = {
        "max_duration_hours": "max_duration_hours",
        "auto_end_minutes": "auto_end_minutes",
    }
    for field, key in reservation_mapping.items():
        value = getattr(request, field)
        if value is not None:
            if value < 1:
                raise HTTPException(status_code=400, detail=f"{field} must be at least 1")
            await set_setting(key, str(value), db)

    # Integer settings that must be >= 1
    mapping_min1 = {
        "per_user_hour": "rate_limit_per_user_hour",
        "admin_per_hour": "rate_limit_admin_per_hour",
        "failed_multiplier": "rate_limit_failed_multiplier",
        "site_provisioning_max": "rate_limit_site_provisioning_max",
    }
    for field, key in mapping_min1.items():
        value = getattr(request, field)
        if value is not None:
            if value < 1:
                raise HTTPException(status_code=400, detail=f"{field} must be at least 1")
            await set_setting(key, str(value), db)

    # Circuit breaker: window and cooldown must be >= 1, threshold allows 0 (disabled)
    cb_min1 = {
        "circuit_breaker_window_minutes": "circuit_breaker_window_minutes",
        "circuit_breaker_cooldown_minutes": "circuit_breaker_cooldown_minutes",
    }
    for field, key in cb_min1.items():
        value = getattr(request, field)
        if value is not None:
            if value < 1:
                raise HTTPException(status_code=400, detail=f"{field} must be at least 1")
            await set_setting(key, str(value), db)

    # Integer settings that allow 0 (0 = disabled)
    mapping_min0 = {
        "per_user_day": "rate_limit_per_user_day",
        "admin_per_day": "rate_limit_admin_per_day",
        "daily_hours_limit": "daily_hours_limit",
        "sitewide_per_hour": "rate_limit_sitewide_per_hour",
        "sitewide_per_day": "rate_limit_sitewide_per_day",
        "circuit_breaker_threshold": "circuit_breaker_threshold",
        "steam_min_account_age_days": "steam_min_account_age_days",
        "steam_min_tf2_hours": "steam_min_tf2_hours",
    }
    for field, key in mapping_min0.items():
        value = getattr(request, field)
        if value is not None:
            if value < 0:
                raise HTTPException(status_code=400, detail=f"{field} must be at least 0")
            await set_setting(key, str(value), db)

    # Boolean settings
    bool_mapping = {
        "steam_require_tf2_ownership": "steam_require_tf2_ownership",
        "steam_block_vac_banned": "steam_block_vac_banned",
        "steam_require_public_profile": "steam_require_public_profile",
    }
    for field, key in bool_mapping.items():
        value = getattr(request, field)
        if value is not None:
            await set_setting(key, "true" if value else "false", db)

    # Captcha boolean
    if request.captcha_enabled is not None:
        await set_setting("captcha_enabled", "true" if request.captcha_enabled else "false", db)

    # Captcha integer settings (allow 0)
    captcha_min0 = {
        "captcha_trust_after_n": "captcha_trust_after_n",
        "captcha_min_tf2_hours": "captcha_min_tf2_hours",
        "captcha_min_account_age_days": "captcha_min_account_age_days",
    }
    for field, key in captcha_min0.items():
        value = getattr(request, field)
        if value is not None:
            if value < 0:
                raise HTTPException(status_code=400, detail=f"{field} must be at least 0")
            await set_setting(key, str(value), db)

    if request.fastdl_url is not None:
        url = request.fastdl_url.strip()
        if url:
            await set_setting("fastdl_url", url, db)
    return {"message": "Settings updated"}


# ---------------------------------------------------------------------------
# Trivia CRUD
# ---------------------------------------------------------------------------

class AddTriviaRequest(BaseModel):
    """Request to add a trivia fact."""
    scope: str  # city, subdivision, country, generic
    key: str = ""
    fact: str


class UpdateTriviaRequest(BaseModel):
    """Request to update a trivia fact."""
    scope: str | None = None
    key: str | None = None
    fact: str | None = None


@router.post("/trivia")
async def add_trivia(
    request: AddTriviaRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new trivia fact."""
    from app.models.trivia import TriviaFact

    valid_scopes = ("city", "subdivision", "country", "generic")
    if request.scope not in valid_scopes:
        raise HTTPException(status_code=400, detail=f"scope must be one of: {', '.join(valid_scopes)}")
    if not request.fact.strip():
        raise HTTPException(status_code=400, detail="fact cannot be empty")

    key = request.key.lower().strip() if request.scope != "generic" else ""
    fact = TriviaFact(scope=request.scope, key=key, fact=request.fact.strip())
    db.add(fact)
    await db.commit()
    await db.refresh(fact)
    return {"id": fact.id, "scope": fact.scope, "key": fact.key, "fact": fact.fact}


@router.put("/trivia/{trivia_id}")
async def update_trivia(
    trivia_id: int,
    request: UpdateTriviaRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a trivia fact."""
    from app.models.trivia import TriviaFact

    result = await db.execute(select(TriviaFact).where(TriviaFact.id == trivia_id))
    fact = result.scalar_one_or_none()
    if not fact:
        raise HTTPException(status_code=404, detail="Trivia fact not found")

    if request.scope is not None:
        valid_scopes = ("city", "subdivision", "country", "generic")
        if request.scope not in valid_scopes:
            raise HTTPException(status_code=400, detail=f"scope must be one of: {', '.join(valid_scopes)}")
        fact.scope = request.scope
    if request.key is not None:
        fact.key = request.key.lower().strip()
    if request.fact is not None:
        if not request.fact.strip():
            raise HTTPException(status_code=400, detail="fact cannot be empty")
        fact.fact = request.fact.strip()

    await db.commit()
    return {"id": fact.id, "scope": fact.scope, "key": fact.key, "fact": fact.fact}


@router.delete("/trivia/{trivia_id}")
async def delete_trivia(
    trivia_id: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a trivia fact."""
    from app.models.trivia import TriviaFact

    result = await db.execute(select(TriviaFact).where(TriviaFact.id == trivia_id))
    fact = result.scalar_one_or_none()
    if not fact:
        raise HTTPException(status_code=404, detail="Trivia fact not found")

    await db.delete(fact)
    await db.commit()
    return {"deleted": True, "id": trivia_id}

"""Page rendering endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.i18n import t
from app.models.user import User
from app.routers.auth import get_current_user, require_user, require_user_allow_banned
from app.services.orchestrator import get_enabled_locations
from app.services.reservation import (
    get_user_reservations,
    get_user_active_reservation,
    get_reservation_by_id,
)
from app.routers.status import _build_status


# Static mapping: Vultr provider region → Looking Glass hostname slug
VULTR_PING_SLUGS = {
    "scl": "scl-cl",     "icn": "sel-kor",    "jnb": "jnb-za",
    "blr": "blr-in",     "del": "del-in",     "bom": "bom-in",
    "tlv": "tlv-il",     "itm": "osk-jp",     "nrt": "hnd-jp",
    "sgp": "sgp",        "mel": "mel-au",     "syd": "syd-au",
    "ams": "ams-nl",     "fra": "fra-de",     "lhr": "lon-gb",
    "man": "man-uk",     "mad": "mad-es",     "cdg": "par-fr",
    "sto": "sto-se",     "waw": "waw-pl",     "atl": "ga-us",
    "ord": "il-us",      "dfw": "tx-us",      "lax": "lax-ca-us",
    "mia": "fl-us",      "ewr": "nj-us",      "sea": "wa-us",
    "sjc": "sjo-ca-us",  "yto": "tor-ca",     "mex": "mex-mx",
    "hnl": "hon-hi-us",
    "sao": "sao-br",
}

# Static mapping: Gcore region ID → speedtest hostname prefix
# URLs: https://{prefix}-speedtest.tools.gcore.com/speedtest-backend/empty.php
GCORE_PING_SLUGS = {
    "26": "am3",        # Amsterdam
    "180": "fr5",       # Frankfurt
    "104": "thn2",      # London
    "196": "thn2",      # London-2
    "100": "pa5",       # Paris
    "80": "wa2",        # Warsaw
    "18": "sg1",        # Singapore
    "30": "cc1",        # Tokyo
    "46": "kal",        # Almaty
    "50": "tii",        # Istanbul
    "64": "hk2",        # Hong Kong
    "68": "min4",       # Chicago → Minneapolis (nearest)
    "84": "jp1",        # Johannesburg
    "88": "sy4",        # Sydney
    "92": "sp3",        # São Paulo
    "108": "ww",        # Mumbai
    "116": "eti",       # Dubai
    "124": "sp3",       # São Paulo-2
    "128": "speedtest-dta",  # Baku (unique hostname format)
    "140": "kx",        # Incheon / Seoul
    "14": "ny2",        # Manassas → New York (nearest)
    "34": "la2",        # Santa Clara → Los Angeles (nearest)
}


# Gcore regions whose speedtest server is in a nearby city, not the same city.
GCORE_APPROXIMATE_REGIONS = {"68", "14", "34"}  # Chicago, Manassas, Santa Clara


def _ping_url(provider: str, provider_region: str) -> str | None:
    """Build the full ping base URL for a location, or None if unavailable."""
    if provider == "vultr":
        slug = VULTR_PING_SLUGS.get(provider_region)
        if slug:
            return f"https://{slug}-ping.vultr.com/p"
    elif provider == "gcore":
        prefix = GCORE_PING_SLUGS.get(provider_region)
        if prefix:
            if prefix == "speedtest-dta":
                return "https://speedtest-dta.gcore.com/speedtest-backend/empty.php"
            return f"https://{prefix}-speedtest.tools.gcore.com/speedtest-backend/empty.php"
    return None


def _ping_approximate(provider: str, provider_region: str) -> bool:
    """Return True if the ping server is in a nearby city, not co-located."""
    return provider == "gcore" and provider_region in GCORE_APPROXIMATE_REGIONS

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="templates")
settings = get_settings()


@router.get("/robots.txt", response_class=HTMLResponse)
async def robots_txt():
    """Serve robots.txt for search engine crawlers."""
    from fastapi.responses import PlainTextResponse

    content = (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /admin\n"
        f"Disallow: /api/\n"
        f"Disallow: /my-reservations\n"
        f"Disallow: /profile\n"
        f"Disallow: /reservations/\n"
        f"\n"
        f"Sitemap: {settings.base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/sitemap.xml", response_class=HTMLResponse)
async def sitemap_xml():
    """Serve sitemap.xml for search engine crawlers."""
    from datetime import date
    from fastapi.responses import Response

    today = date.today().isoformat()
    pages = [
        ("/", "daily", "1.0"),
        ("/about", "monthly", "0.8"),
        ("/stats", "daily", "0.7"),
        ("/ping", "monthly", "0.6"),
        ("/maps", "monthly", "0.5"),
        ("/bans", "weekly", "0.4"),
    ]

    # Only include /privacy if the template exists
    from pathlib import Path
    if Path("templates/privacy.html").exists():
        pages.append(("/privacy", "monthly", "0.3"))

    base = settings.base_url
    urls = "\n".join(
        f"  <url>\n"
        f"    <loc>{base}{path}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        f"  </url>"
        for path, freq, prio in pages
    )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Home page."""
    user = await get_current_user(request, db)
    locations = await get_enabled_locations(db)

    # Beta mode check
    beta_locked = settings.beta_mode and (not user or not user.is_admin)

    # Check for existing active reservation (to show notice instead of form)
    active_reservation = None
    if user and not beta_locked:
        active_reservation = await get_user_active_reservation(user, db)

    # Fetch enabled maps for the reservation form
    from app.models.instance import GameMap
    maps_result = await db.execute(
        select(GameMap)
        .where(GameMap.enabled == True)
        .order_by(GameMap.name)
    )
    maps = [
        {"name": m.name, "display": m.display_name}
        for m in maps_result.scalars().all()
    ]

    location_cities = {loc.code: loc.city or loc.code for loc in locations}

    from app.services.settings import get_reservation_settings
    res_settings = await get_reservation_settings(db)

    # Pre-fetch status so the client doesn't need a separate /api/status call
    initial_status = await _build_status(db)

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "locations": locations,
            "maps": maps,
            "active_reservation": active_reservation,
            "cloud_configured": settings.cloud_configured,
            "beta_mode": settings.beta_mode,
            "beta_locked": beta_locked,
            "ping_urls": {loc.code: url for loc in locations
                         if (url := _ping_url(loc.provider, loc.provider_region))},
            "approximate_pings": [loc.code for loc in locations
                                  if _ping_approximate(loc.provider, loc.provider_region)],
            "location_cities": location_cities,
            "reservation_settings": res_settings,
            "hcaptcha_site_key": settings.hcaptcha_site_key if settings.hcaptcha_configured else "",
            "initial_status": initial_status,
        }
    )


@router.get("/reserve", response_class=HTMLResponse)
async def reserve_page(request: Request):
    """Redirect legacy reserve page to home."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=301)


@router.get("/reservations/{reservation_id}", response_class=HTMLResponse)
async def reservation_status_page(
    request: Request,
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Reservation status page."""
    user = await get_current_user(request, db)
    reservation = await get_reservation_by_id(reservation_id, db)
    
    if not reservation:
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "user": user,
                "locations": [],
                "maps": [],
                "ping_urls": {},
                "beta_locked": False,
                "active_reservation": None,
                "error": t("errors.reservation_does_not_exist"),
                "cloud_configured": settings.cloud_configured,
            },
            status_code=404,
        )
    
    # Check if user is owner (or admin)
    is_owner = user and (user.id == reservation.user_id or user.is_admin)
    if not is_owner:
        return templates.TemplateResponse(
            request,
            "404.html",
            {"user": user},
            status_code=404,
        )

    # For admins viewing another user's reservation, load the owner info
    owner_name = None
    if user.is_admin and reservation.user_id != user.id:
        from sqlalchemy.orm import selectinload
        await db.refresh(reservation, attribute_names=["user"])
        if reservation.user:
            owner_name = reservation.user.display_name

    # Look up location city name for display
    from app.models.instance import EnabledLocation
    loc_result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == reservation.location)
    )
    loc_record = loc_result.scalar_one_or_none()
    location_display = loc_record.city if loc_record and loc_record.city else reservation.location

    # Build download URL for custom maps
    map_download_url = None
    from app.models.instance import GameMap
    map_result = await db.execute(
        select(GameMap).where(GameMap.name == reservation.first_map)
    )
    game_map = map_result.scalar_one_or_none()
    if not game_map or not game_map.is_default:
        from app.services.settings import get_fastdl_url
        fastdl = await get_fastdl_url(db)
        if not fastdl.endswith("/"):
            fastdl += "/"
        if fastdl.endswith("maps/"):
            fastdl = fastdl[:-5]  # strip trailing maps/ to avoid duplication
        map_download_url = f"{fastdl}maps/{reservation.first_map}.bsp"

    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "user": user,
            "reservation": reservation,
            "is_owner": is_owner,
            "owner_name": owner_name,
            "location_display": location_display,
            "map_download_url": map_download_url,
        }
    )


@router.get("/my-reservations", response_class=HTMLResponse)
async def my_reservations_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """User's reservation history page."""
    user = await get_current_user(request, db)
    if not user:
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "user": None,
                "locations": [],
                "error": t("errors.login_required"),
                "cloud_configured": settings.cloud_configured,
            }
        )
    
    reservations = await get_user_reservations(user, db)

    # Build location code → city name map for display
    from app.models.instance import EnabledLocation
    loc_result = await db.execute(select(EnabledLocation))
    location_cities = {
        loc.code: loc.city or loc.code
        for loc in loc_result.scalars().all()
    }

    return templates.TemplateResponse(
        request,
        "my_reservations.html",
        {
            "user": user,
            "reservations": reservations,
            "location_cities": location_cities,
        }
    )


@router.get("/about", response_class=HTMLResponse)
async def about_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """About page."""
    from app.services.settings import get_reservation_settings
    user = await get_current_user(request, db)
    res_settings = await get_reservation_settings(db)
    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "user": user,
            "reservation_settings": res_settings,
        }
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Stats page."""
    user = await get_current_user(request, db)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "user": user,
        }
    )


@router.get("/maps", response_class=HTMLResponse)
async def maps_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public page listing all custom maps with download links."""
    user = await get_current_user(request, db)

    from app.models.instance import GameMap
    from app.services.settings import get_fastdl_url

    result = await db.execute(
        select(GameMap)
        .where(GameMap.enabled == True)
        .where(GameMap.is_default == False)
        .order_by(GameMap.display_order)
    )
    custom_maps = result.scalars().all()

    fastdl = await get_fastdl_url(db)
    if not fastdl.endswith("/"):
        fastdl += "/"
    if fastdl.endswith("maps/"):
        fastdl = fastdl[:-5]  # strip trailing maps/ to avoid duplication

    maps_data = [
        {"name": m.name, "download_url": f"{fastdl}maps/{m.name}.bsp"}
        for m in custom_maps
    ]

    return templates.TemplateResponse(
        request,
        "maps.html",
        {
            "user": user,
            "maps": maps_data,
        }
    )


@router.get("/bans", response_class=HTMLResponse)
async def bans_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public bans page."""
    user = await get_current_user(request, db)
    from app.models.user import User as UserModel
    result = await db.execute(
        select(UserModel).where(UserModel.is_banned == True)
    )
    banned_users = result.scalars().all()
    bans = [
        {
            "steam_id": u.steam_id,
            "display_name": u.display_name,
            "reason": u.ban_reason,
        }
        for u in banned_users
    ]
    return templates.TemplateResponse(
        request,
        "bans.html",
        {"user": user, "bans": bans},
    )


from pathlib import Path as _Path

if _Path("templates/privacy.html").exists():
    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy_page(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ):
        """Privacy policy page."""
        user = await get_current_user(request, db)
        return templates.TemplateResponse(
            request,
            "privacy.html",
            {
                "user": user,
            }
        )


@router.get("/ping", response_class=HTMLResponse)
async def ping_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Ping test page — measure latency to each server location."""
    user = await get_current_user(request, db)
    locations = await get_enabled_locations(db)

    ping_locations = []
    for loc in locations:
        url = _ping_url(loc.provider, loc.provider_region)
        if url and not _ping_approximate(loc.provider, loc.provider_region):
            ping_locations.append({
                "code": loc.code,
                "name": loc.name,
                "city": loc.city or "",
                "country": loc.country or "",
                "continent": loc.continent or "",
                "ping_url": url,
            })

    return templates.TemplateResponse(
        request,
        "ping.html",
        {
            "user": user,
            "locations": ping_locations,
        }
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: User = Depends(require_user_allow_banned),
    db: AsyncSession = Depends(get_db),
):
    """User profile page."""
    from datetime import datetime, timezone
    from app.services.rate_limit import get_user_reservation_counts
    from app.services.settings import get_rate_limit_settings, get_steam_trust_settings
    from app.services.reservation import get_user_active_reservation

    from app.services.rate_limit import get_user_daily_hours

    active_reservation = await get_user_active_reservation(user, db)
    hourly_total, _ = await get_user_reservation_counts(user.id, db, hours=1)
    daily_total, _ = await get_user_reservation_counts(user.id, db, hours=24)
    daily_hours_used = await get_user_daily_hours(user.id, db)
    rate_limits = await get_rate_limit_settings(db)
    steam_trust = await get_steam_trust_settings(db)

    # Compute account age in days
    age_days = None
    if user.steam_account_created_at is not None:
        created = user.steam_account_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days

    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "active_reservation": active_reservation,
            "hourly_count": hourly_total,
            "daily_count": daily_total,
            "daily_hours_used": round(daily_hours_used, 1),
            "rate_limits": rate_limits,
            "steam_trust": steam_trust,
            "age_days": age_days,
        }
    )

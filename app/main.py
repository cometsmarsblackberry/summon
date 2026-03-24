"""FastAPI application entry point."""

import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import StarletteHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import create_tables, async_session_maker
from app.models.instance import LocationProvider  # noqa: F401 — registers table with Base
from app.models.setting import SiteSetting  # noqa: F401 — registers table with Base
from app.models.steam_trust_snapshot import SteamTrustSnapshot  # noqa: F401 — registers table with Base
from app.models.trivia import TriviaFact  # noqa: F401 — registers table with Base
from app.models.upload_link import UploadLink  # noqa: F401 — registers table with Base
from app.services.orchestrator import seed_default_locations
from app.routers import auth, reservations, status, internal, admin, pages, ping, motd
from app.i18n import (
    get_locale,
    set_current_locale,
    make_translate_func,
    SUPPORTED_LOCALES,
    LOCALE_NAMES,
    LOCALE_COOKIE,
    DEFAULT_LOCALE,
)


settings = get_settings()

# Configure logging
_log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

logging.basicConfig(
    level=_log_level,
    format=_log_format,
)

# Add rotating file handler
os.makedirs(settings.log_dir, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(settings.log_dir, "app.log"),
    maxBytes=settings.log_max_bytes,
    backupCount=settings.log_backup_count,
)
_file_handler.setLevel(_log_level)
_file_handler.setFormatter(logging.Formatter(_log_format))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    import asyncio
    
    # Startup
    logger.info("Starting %s...", settings.site_name)
    
    # Create database tables
    await create_tables()
    logger.info("Database tables created")
    
    # Seed default providers (must run before locations due to FK)
    async with async_session_maker() as db:
        from app.services.orchestrator import seed_default_providers
        await seed_default_providers(db)
    
    # Seed default locations and maps
    async with async_session_maker() as db:
        await seed_default_locations(db)
        from app.services.orchestrator import seed_default_maps
        await seed_default_maps(db)

    # Seed location_providers from existing locations (one-time migration)
    async with async_session_maker() as db:
        from app.services.provider_priority import seed_location_providers
        await seed_location_providers(db)
    
    # Restore per-reservation expiry timers
    from app.services.timer import restore_expiry_timers
    await restore_expiry_timers()

    logger.info(f"Cloud provider configured: {settings.cloud_configured}")
    logger.info(f"Steam API configured: {settings.steam_configured}")
    logger.info("Beta mode: %s", settings.beta_mode)
    if settings.allow_legacy_internal_api_key:
        logger.warning(
            "Legacy INTERNAL_API_KEY fallback is enabled. "
            "Any leaked global plugin key can affect every reservation."
        )
    if settings.allow_legacy_agent_query_token:
        logger.warning(
            "Legacy agent query-string auth is enabled. "
            "Agent tokens may appear in URLs and intermediary logs."
        )
    if settings.beta_mode and not settings.admin_steam_id_list:
        logger.warning(
            "BETA_MODE=true but ADMIN_STEAM_IDS is empty. "
            "No one will be able to reserve or access /admin until an admin SteamID is configured."
        )
    
    # Start background task for instance cleanup and sync
    async def cleanup_loop():
        from app.services.orchestrator import cleanup_expired_instances, sync_cloud_instances
        from app.services.orchestrator import release_to_warm_pool, destroy_instance, is_hourly_billing
        from app.models.reservation import Reservation, ReservationStatus
        from app.models.instance import CloudInstance
        from app.routers.internal import clear_player_data, send_to_agent
        from sqlalchemy import select
        from datetime import datetime, timedelta, timezone

        from app.services.settings import get_reservation_settings

        sync_counter = 0
        while True:
            try:
                destroyed = await cleanup_expired_instances()
                if destroyed > 0:
                    logger.info(f"Cleanup: destroyed {destroyed} expired instances")

                # Auto-end empty reservations
                try:
                    async with async_session_maker() as settings_db:
                        res_settings = await get_reservation_settings(settings_db)
                    auto_end_minutes = res_settings["auto_end_minutes"]
                    cutoff = datetime.now(timezone.utc) - timedelta(minutes=auto_end_minutes)
                    async with async_session_maker() as db:
                        result = await db.execute(
                            select(Reservation).where(
                                Reservation.status == ReservationStatus.ACTIVE,
                                Reservation.empty_since != None,
                                Reservation.empty_since <= cutoff,
                            )
                        )
                        empty_reservations = list(result.scalars().all())

                        for reservation in empty_reservations:
                            logger.info(
                                f"Auto-ending reservation #{reservation.reservation_number} "
                                f"(empty for {auto_end_minutes}+ minutes)"
                            )
                            reservation.status = ReservationStatus.ENDED
                            reservation.ended_at = datetime.now(timezone.utc)
                            clear_player_data(reservation.reservation_number)
                            from app.services.timer import cancel_expiry_timer
                            cancel_expiry_timer(reservation.id)

                            if reservation.instance_id:
                                # Notify agent to stop container
                                ci_result = await db.execute(
                                    select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
                                )
                                cloud_instance = ci_result.scalar_one_or_none()
                                if cloud_instance:
                                    await send_to_agent(cloud_instance.instance_id, {"type": "reservation.end"})

                                # Release or destroy based on billing model
                                if await is_hourly_billing(reservation.location, db):
                                    await release_to_warm_pool(reservation.instance_id, db)
                                else:
                                    await destroy_instance(reservation.instance_id, db)

                        if empty_reservations:
                            await db.commit()
                except Exception as e:
                    logger.error(f"Auto-end error: {e}")

                # Sync with cloud providers every 5 minutes (every 5 iterations)
                sync_counter += 1
                if sync_counter >= 5:
                    sync_counter = 0
                    removed = await sync_cloud_instances()
                    if removed > 0:
                        logger.info(f"Sync: removed {removed} orphaned instance records")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)  # Run every minute
    
    cleanup_task = asyncio.create_task(cleanup_loop())
    
    yield
    
    # Shutdown
    from app.services.timer import cancel_all_expiry_timers
    cancel_all_expiry_timers()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down %s...", settings.site_name)


app = FastAPI(
    title=settings.site_name,
    description="On-demand TF2 server reservation system",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(reservations.router)
app.include_router(reservations.captcha_router)
app.include_router(status.router)
app.include_router(internal.router)
app.include_router(admin.router)
app.include_router(ping.router)
app.include_router(motd.router)

_templates = Jinja2Templates(directory="templates")


# --- i18n middleware ---

class LocaleMiddleware(BaseHTTPMiddleware):
    """Set request.state.locale from cookie / Accept-Language header."""

    async def dispatch(self, request: Request, call_next):
        locale = get_locale(request)
        request.state.locale = locale
        set_current_locale(locale)
        response = await call_next(request)
        return response


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Default to no-store for dynamic responses so CDN does not cache them."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if "cache-control" not in response.headers and not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply a baseline set of browser hardening headers."""

    _CSP = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://d1f8nxls7qx69o.cloudfront.net https://js.hcaptcha.com https://hcaptcha.com https://*.hcaptcha.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.bunny.net https://hcaptcha.com https://*.hcaptcha.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https://fonts.bunny.net; "
        "connect-src 'self' https://api.hcaptcha.com https://hcaptcha.com https://*.hcaptcha.com; "
        "frame-src https://hcaptcha.com https://*.hcaptcha.com; "
        "form-action 'self' https://steamcommunity.com"
    )
    _CSP_SKIP_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/static", "/motd")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=()",
        )

        if settings.base_url.startswith("https://"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        if not request.url.path.startswith(self._CSP_SKIP_PREFIXES):
            response.headers.setdefault("Content-Security-Policy", self._CSP)

        return response


class TrustedProxyHeadersMiddleware(BaseHTTPMiddleware):
    """Apply forwarded client IP headers only when the immediate peer is trusted."""

    def __init__(self, app: FastAPI, trusted_cidrs: list[str]):
        super().__init__(app)
        self._trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for cidr in trusted_cidrs:
            try:
                self._trusted_networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                logger.warning("Ignoring invalid trusted proxy CIDR: %s", cidr)

    @staticmethod
    def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        if not value:
            return None
        try:
            return ipaddress.ip_address(value.strip())
        except ValueError:
            return None

    def _peer_is_trusted(self, host: str | None) -> bool:
        peer = self._parse_ip(host)
        return peer is not None and any(peer in network for network in self._trusted_networks)

    def _forwarded_client_ip(self, request: Request) -> str | None:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            candidate = forwarded_for.split(",")[0].strip()
            if self._parse_ip(candidate):
                return candidate

        real_ip = request.headers.get("x-real-ip")
        if real_ip and self._parse_ip(real_ip):
            return real_ip.strip()

        return None

    async def dispatch(self, request: Request, call_next):
        client = request.client
        if client and self._peer_is_trusted(client.host):
            forwarded_ip = self._forwarded_client_ip(request)
            if forwarded_ip:
                request.scope["client"] = (forwarded_ip, client.port)

        return await call_next(request)


app.add_middleware(LocaleMiddleware)
app.add_middleware(CacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    TrustedProxyHeadersMiddleware,
    trusted_cidrs=settings.trusted_proxy_cidr_list,
)


# Register i18n helpers as Jinja2 globals on every Templates instance used in the app
def _install_i18n_globals(templates: Jinja2Templates) -> None:
    """Add _(), get_locale, SUPPORTED_LOCALES, LOCALE_NAMES to a Jinja2 env."""
    env = templates.env

    # We use a Jinja2 context function so `_()` is request-aware
    from jinja2 import pass_context

    @pass_context
    def _translate(context, key: str, **kwargs):
        request = context.get("request")
        if request:
            locale = getattr(request.state, "locale", DEFAULT_LOCALE)
        else:
            locale = DEFAULT_LOCALE
        from app.i18n import translate
        return translate(key, locale, **kwargs)

    # Newer Jinja2 includes env.globals in the template cache key, which fails
    # when any global value is unhashable (e.g. LOCALE_NAMES dict).
    env.cache = None  # type: ignore[assignment]

    env.globals["_"] = _translate
    env.globals["SUPPORTED_LOCALES"] = SUPPORTED_LOCALES
    env.globals["LOCALE_NAMES"] = LOCALE_NAMES
    env.globals["DEFAULT_LOCALE"] = DEFAULT_LOCALE

    # Branding globals
    env.globals["site_name"] = settings.site_name
    env.globals["base_url"] = settings.base_url
    env.globals["logo_url"] = settings.logo_url
    env.globals["favicon_url"] = settings.favicon_url
    env.globals["og_image_url"] = settings.og_image_url
    env.globals["discord_url"] = settings.discord_url
    env.globals["steam_group_url"] = settings.steam_group_url
    env.globals["contact_email"] = settings.contact_email
    env.globals["rules_url"] = settings.rules_url
    env.globals["login_image_url"] = settings.login_image_url
    env.globals["login_image_wide_url"] = settings.login_image_wide_url
    env.globals["privacy_available"] = Path("templates/privacy.html").exists()


# Install on all Jinja2Templates instances used across the app
_install_i18n_globals(_templates)
_install_i18n_globals(pages.templates)
_install_i18n_globals(admin.templates)
_install_i18n_globals(motd.templates)


# --- Language switcher endpoint ---

@app.post("/set-language")
async def set_language(request: Request):
    """Set the language preference cookie."""
    from urllib.parse import urlparse

    form = await request.form()
    lang = str(form.get("lang", DEFAULT_LOCALE))
    if lang not in SUPPORTED_LOCALES:
        lang = DEFAULT_LOCALE

    # Only follow the Referer path when it belongs to the same origin;
    # otherwise an attacker-controlled Referer could redirect externally.
    redirect_to = "/"
    referer = request.headers.get("referer")
    if referer:
        parsed = urlparse(referer)
        base_parsed = urlparse(settings.base_url)
        if parsed.netloc == "" or parsed.netloc == base_parsed.netloc:
            redirect_to = parsed.path or "/"
            if parsed.query:
                redirect_to += f"?{parsed.query}"

    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        key=LOCALE_COOKIE,
        value=lang,
        max_age=60 * 60 * 24 * 365,  # 1 year
        httponly=False,
        samesite="lax",
    )
    return response


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: StarletteHTTPException):
    """Custom 404 page."""
    from app.routers.auth import get_current_user
    from app.database import async_session_maker

    async with async_session_maker() as db:
        user = await get_current_user(request, db)

    return _templates.TemplateResponse(
        request,
        "404.html",
        {"user": user},
        status_code=404,
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "cloud_configured": settings.cloud_configured,
        "steam_configured": settings.steam_configured,
    }

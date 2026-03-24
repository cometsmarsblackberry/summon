"""Ping results collection and statistics API."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from statistics import median

import httpx
import pycountry
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, func, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.instance import EnabledLocation
from app.models.ping import PingSubmission
from app.models.reservation import Reservation, ReservationStatus

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

MIN_PING_MS = 1
MAX_PING_MS = 5000

# ---------------------------------------------------------------------------
# In-memory rate limiting: {ip: [timestamp, ...]}
# ---------------------------------------------------------------------------
_submit_timestamps: dict[str, list[float]] = {}
SUBMIT_LIMIT = 3
SUBMIT_WINDOW = 3600  # 1 hour

# ---------------------------------------------------------------------------
# In-memory stats cache
# ---------------------------------------------------------------------------
_stats_cache: dict | None = None
_stats_cache_time: float = 0
STATS_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    """Return the normalized client IP after trusted-proxy handling."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_rate_limit(ip: str) -> bool:
    """Return True if allowed, False if rate-limited."""
    now = time.time()
    timestamps = _submit_timestamps.get(ip, [])
    # Prune old entries
    timestamps = [t for t in timestamps if now - t < SUBMIT_WINDOW]
    _submit_timestamps[ip] = timestamps
    return len(timestamps) < SUBMIT_LIMIT


def _record_submission(ip: str):
    now = time.time()
    _submit_timestamps.setdefault(ip, []).append(now)


async def _resolve_location(ip: str) -> dict:
    """Call IPinfo to get approximate city/country. Returns dict with nullable fields."""
    token = settings.ipinfo_token
    if not token:
        return {"city": None, "region": None, "country": None, "country_code": None}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"https://ipinfo.io/{ip}?token={token}")
            if resp.status_code != 200:
                logger.warning(f"IPinfo returned {resp.status_code} for {ip}")
                return {"city": None, "region": None, "country": None, "country_code": None}
            data = resp.json()
            cc = data.get("country")  # 2-letter code from IPinfo
            country_name = cc
            if cc:
                entry = pycountry.countries.get(alpha_2=cc)
                if entry:
                    country_name = entry.name
            return {
                "city": data.get("city"),
                "region": data.get("region"),
                "country": country_name,
                "country_code": cc,
            }
    except Exception as e:
        logger.warning(f"IPinfo lookup failed: {e}")
        return {"city": None, "region": None, "country": None, "country_code": None}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PingResultsRequest(BaseModel):
    results: dict[str, int]  # {location_code: ms}


# ---------------------------------------------------------------------------
# POST /api/ping-results
# ---------------------------------------------------------------------------

@router.post("/api/ping-results")
async def submit_ping_results(
    body: PingResultsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = get_client_ip(request)

    # Rate limit
    if not _check_rate_limit(ip):
        return JSONResponse(
            {"error": "Rate limit exceeded. Max 3 submissions per hour."},
            status_code=429,
        )

    results = body.results
    if not results:
        return JSONResponse({"error": "No results provided."}, status_code=400)

    # Validate location codes against DB
    loc_result = await db.execute(select(EnabledLocation.code))
    valid_codes = {row[0] for row in loc_result}

    filtered = {
        code: ms
        for code, ms in results.items()
        if code in valid_codes and MIN_PING_MS <= ms <= MAX_PING_MS
    }
    if not filtered:
        return JSONResponse(
            {
                "error": (
                    f"No valid location codes with ping values between "
                    f"{MIN_PING_MS} and {MAX_PING_MS} ms."
                )
            },
            status_code=400,
        )

    # Find best
    best_code = min(filtered, key=filtered.get)
    best_ms = filtered[best_code]

    # Resolve user location (IP used only for this call, then discarded)
    geo = await _resolve_location(ip)

    submission = PingSubmission(
        user_city=geo["city"],
        user_region=geo["region"],
        user_country=geo["country"],
        user_country_code=geo["country_code"],
        best_location=best_code,
        best_ping_ms=best_ms,
        ping_results=json.dumps(filtered),
        created_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    await db.commit()

    _record_submission(ip)

    # Invalidate stats cache
    global _stats_cache, _stats_cache_time
    _stats_cache = None
    _stats_cache_time = 0

    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /api/ping-stats
# ---------------------------------------------------------------------------

@router.get("/api/ping-stats")
async def get_ping_stats(db: AsyncSession = Depends(get_db)):
    global _stats_cache, _stats_cache_time

    now = time.time()
    if _stats_cache is not None and now - _stats_cache_time < STATS_CACHE_TTL:
        return _stats_cache

    # Fetch all submissions
    result = await db.execute(select(PingSubmission))
    submissions = result.scalars().all()

    total = len(submissions)
    if total == 0:
        stats = {"total_submissions": 0, "locations": [], "countries": []}
        _stats_cache = stats
        _stats_cache_time = now
        return stats

    # Load location names for display
    loc_result = await db.execute(select(EnabledLocation.code, EnabledLocation.name))
    loc_names = {row[0]: row[1] for row in loc_result}

    # Aggregate per-location
    loc_pings: dict[str, list[int]] = {}
    loc_best_count: dict[str, int] = {}

    # Aggregate per-country
    country_data: dict[str, dict] = {}

    for sub in submissions:
        # Per-location pings
        try:
            results_dict = json.loads(sub.ping_results)
        except (json.JSONDecodeError, TypeError):
            continue

        for code, ms in results_dict.items():
            loc_pings.setdefault(code, []).append(ms)

        # Best location tally
        loc_best_count[sub.best_location] = loc_best_count.get(sub.best_location, 0) + 1

        # Per-country
        cc = sub.user_country_code or "Unknown"
        country_name = sub.user_country or "Unknown"
        if cc != "Unknown" and len(cc) == 2:
            entry = pycountry.countries.get(alpha_2=cc)
            if entry:
                country_name = entry.name
        if cc not in country_data:
            country_data[cc] = {
                "country_code": cc,
                "country": country_name,
                "count": 0,
                "best_locations": {},
            }
        country_data[cc]["count"] += 1
        bl = sub.best_location
        country_data[cc]["best_locations"][bl] = country_data[cc]["best_locations"].get(bl, 0) + 1

    # Build location stats
    location_stats = []
    for code, pings in loc_pings.items():
        pings_sorted = sorted(pings)
        location_stats.append({
            "code": code,
            "name": loc_names.get(code, code),
            "avg_ms": round(sum(pings) / len(pings)),
            "median_ms": round(median(pings)),
            "min_ms": pings_sorted[0],
            "submissions": len(pings),
            "times_best": loc_best_count.get(code, 0),
        })
    location_stats.sort(key=lambda x: x["avg_ms"])

    # Build country stats
    country_stats = []
    for cc, data in country_data.items():
        best_loc_code = max(data["best_locations"], key=data["best_locations"].get)
        country_stats.append({
            "country_code": data["country_code"],
            "country": data["country"],
            "submissions": data["count"],
            "most_common_best": loc_names.get(best_loc_code, best_loc_code),
        })
    country_stats.sort(key=lambda x: x["submissions"], reverse=True)

    stats = {
        "total_submissions": total,
        "locations": location_stats,
        "countries": country_stats,
    }

    _stats_cache = stats
    _stats_cache_time = now
    return stats


# ---------------------------------------------------------------------------
# In-memory reservation stats cache
# ---------------------------------------------------------------------------
_res_stats_cache: dict | None = None
_res_stats_cache_time: float = 0


# ---------------------------------------------------------------------------
# GET /api/reservation-stats
# ---------------------------------------------------------------------------

@router.get("/api/reservation-stats")
async def get_reservation_stats(db: AsyncSession = Depends(get_db)):
    global _res_stats_cache, _res_stats_cache_time

    now = time.time()
    if _res_stats_cache is not None and now - _res_stats_cache_time < STATS_CACHE_TTL:
        return _res_stats_cache

    utc_now = datetime.now(timezone.utc)
    today_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=utc_now.weekday())
    month_start = today_start.replace(day=1)

    # Overview counts
    total = (await db.execute(select(func.count(Reservation.id)))).scalar_one()

    active = (await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.status.in_([ReservationStatus.ACTIVE, ReservationStatus.PROVISIONING]))
    )).scalar_one()

    today_count = (await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.created_at >= today_start)
    )).scalar_one()

    week_count = (await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.created_at >= week_start)
    )).scalar_one()

    month_count = (await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.created_at >= month_start)
    )).scalar_one()

    # Per-location stats
    loc_names_result = await db.execute(select(EnabledLocation.code, EnabledLocation.name))
    loc_names = {row[0]: row[1] for row in loc_names_result}

    loc_rows = (await db.execute(
        select(
            Reservation.location,
            func.count(Reservation.id).label("total"),
            func.avg(case(
                (
                    and_(Reservation.started_at.isnot(None), Reservation.ended_at.isnot(None)),
                    func.extract("epoch", Reservation.ended_at) - func.extract("epoch", Reservation.started_at),
                ),
                else_=None,
            )).label("avg_seconds"),
        )
        .group_by(Reservation.location)
    )).all()

    loc_month_rows = (await db.execute(
        select(
            Reservation.location,
            func.count(Reservation.id).label("month_count"),
        )
        .where(Reservation.created_at >= month_start)
        .group_by(Reservation.location)
    )).all()
    loc_month_map = {row.location: row.month_count for row in loc_month_rows}

    locations = []
    for row in loc_rows:
        avg_minutes = round(row.avg_seconds / 60) if row.avg_seconds else 0
        locations.append({
            "location": row.location,
            "name": loc_names.get(row.location, row.location),
            "total": row.total,
            "avg_duration": avg_minutes,
            "this_month": loc_month_map.get(row.location, 0),
        })
    locations.sort(key=lambda x: x["total"], reverse=True)

    # Activity: reservations per day for last 30 days
    thirty_days_ago = today_start - timedelta(days=29)
    day_expr = func.date(Reservation.created_at)
    activity_rows = (await db.execute(
        select(
            day_expr.label("day"),
            func.count(Reservation.id).label("count"),
        )
        .where(Reservation.created_at >= thirty_days_ago)
        .group_by(day_expr)
        .order_by(day_expr)
    )).all()
    activity_map = {str(row.day): row.count for row in activity_rows}

    activity = []
    for i in range(30):
        d = thirty_days_ago + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        activity.append({"date": ds, "count": activity_map.get(ds, 0)})

    stats = {
        "overview": {
            "total": total,
            "active": active,
            "today": today_count,
            "this_week": week_count,
            "this_month": month_count,
        },
        "locations": locations,
        "activity": activity,
    }

    _res_stats_cache = stats
    _res_stats_cache_time = now
    return stats

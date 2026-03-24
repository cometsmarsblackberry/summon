"""Provider priority and failure tracking for multi-provider failover.

Tracks consecutive failures per (location, provider) pair in memory.
After a configurable number of consecutive failures, the provider is
temporarily suspended for that location. The suspension lifts automatically.
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import LocationProvider, EnabledLocation, Provider


logger = logging.getLogger(__name__)

# --- Configuration ---
# Suspend a provider after this many consecutive failures at a location
FAILURE_THRESHOLD = 3
# How long (seconds) to suspend a failing provider before retrying
SUSPEND_DURATION_SECONDS = 10 * 60  # 10 minutes


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    suspended_until: float = 0.0


# In-memory failure state keyed by (location_code, provider_code)
_failure_state: dict[tuple[str, str], _ProviderState] = {}


def record_provider_failure(location_code: str, provider_code: str) -> None:
    """Record a create_instance() failure for a provider at a location."""
    key = (location_code, provider_code)
    state = _failure_state.setdefault(key, _ProviderState())
    state.consecutive_failures += 1
    state.last_failure_time = time.monotonic()

    if state.consecutive_failures >= FAILURE_THRESHOLD:
        state.suspended_until = time.monotonic() + SUSPEND_DURATION_SECONDS
        logger.warning(
            f"Provider {provider_code} suspended for {location_code} "
            f"after {state.consecutive_failures} consecutive failures "
            f"(suspended for {SUSPEND_DURATION_SECONDS}s)"
        )


def record_provider_success(location_code: str, provider_code: str) -> None:
    """Reset failure count on successful provisioning."""
    key = (location_code, provider_code)
    state = _failure_state.get(key)
    if state and state.consecutive_failures > 0:
        logger.info(
            f"Provider {provider_code} recovered for {location_code} "
            f"(was at {state.consecutive_failures} consecutive failures)"
        )
        state.consecutive_failures = 0
        state.suspended_until = 0.0


def is_provider_suspended(location_code: str, provider_code: str) -> bool:
    """Check if a provider is currently suspended for a location."""
    key = (location_code, provider_code)
    state = _failure_state.get(key)
    if not state:
        return False
    if state.suspended_until <= time.monotonic():
        return False
    return True


def get_provider_status(location_code: str, provider_code: str) -> dict:
    """Get failure tracking status for a provider at a location (for admin/debug)."""
    key = (location_code, provider_code)
    state = _failure_state.get(key)
    if not state:
        return {"failures": 0, "suspended": False}
    now = time.monotonic()
    suspended = state.suspended_until > now
    return {
        "failures": state.consecutive_failures,
        "suspended": suspended,
        "suspended_remaining_seconds": int(state.suspended_until - now) if suspended else 0,
    }


def get_all_provider_status() -> dict[str, dict]:
    """Get failure tracking status for all tracked providers (for admin/debug)."""
    now = time.monotonic()
    result = {}
    for (loc, prov), state in _failure_state.items():
        if state.consecutive_failures == 0:
            continue
        suspended = state.suspended_until > now
        result[f"{loc}:{prov}"] = {
            "failures": state.consecutive_failures,
            "suspended": suspended,
            "suspended_remaining_seconds": int(state.suspended_until - now) if suspended else 0,
        }
    return result


def reset_provider_suspension(location_code: str, provider_code: str) -> None:
    """Manually reset suspension for a provider at a location (admin action)."""
    key = (location_code, provider_code)
    state = _failure_state.get(key)
    if state:
        state.consecutive_failures = 0
        state.suspended_until = 0.0
        logger.info(f"Admin reset suspension for {provider_code} at {location_code}")


async def get_providers_for_location(
    location_code: str, db: AsyncSession
) -> list[LocationProvider]:
    """Get enabled providers for a location, ordered by priority.

    Excludes disabled entries but does NOT exclude suspended providers
    (the caller decides whether to skip them).
    """
    result = await db.execute(
        select(LocationProvider)
        .join(Provider, LocationProvider.provider_code == Provider.code)
        .where(LocationProvider.location_code == location_code)
        .where(LocationProvider.enabled == True)
        .where(Provider.enabled == True)
        .order_by(LocationProvider.priority)
    )
    return list(result.scalars().all())


async def seed_location_providers(db: AsyncSession) -> None:
    """Seed LocationProvider entries from existing EnabledLocation records.

    Only runs if the location_providers table is empty — preserves manually
    configured entries on subsequent restarts.
    """
    from sqlalchemy import func

    count_result = await db.execute(select(func.count(LocationProvider.id)))
    if count_result.scalar_one() > 0:
        return  # Already seeded

    loc_result = await db.execute(select(EnabledLocation))
    locations = list(loc_result.scalars().all())

    if not locations:
        return

    added = []
    for loc in locations:
        if not loc.provider or not loc.provider_region:
            continue
        lp = LocationProvider(
            location_code=loc.code,
            provider_code=loc.provider,
            provider_region=loc.provider_region,
            priority=0,
            enabled=True,
            instance_plan=loc.instance_plan,
            region_instance_limit=loc.region_instance_limit,
        )
        db.add(lp)
        added.append(f"{loc.code}:{loc.provider}")

    if added:
        await db.commit()
        logger.info(f"Seeded location_providers from existing locations: {', '.join(added)}")

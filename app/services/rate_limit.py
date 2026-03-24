"""Rate limiting for reservation creation."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.i18n import t
from app.models.reservation import Reservation, ReservationStatus


logger = logging.getLogger(__name__)
settings = get_settings()


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after_seconds: int = 3600):
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class DailyHoursExceeded(Exception):
    """Raised when user exceeds daily reservation hours limit."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def get_user_reservation_counts(
    user_id: int,
    db: AsyncSession,
    hours: int = 1,
) -> tuple[int, int]:
    """Get user's reservation counts in the last N hours.
    
    Returns:
        Tuple of (total_count, failed_count)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    # Count all reservations in time window
    total_result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.user_id == user_id)
        .where(Reservation.created_at >= cutoff)
    )
    total_count = total_result.scalar_one()
    
    # Count failed reservations in time window
    failed_result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.user_id == user_id)
        .where(Reservation.created_at >= cutoff)
        .where(Reservation.status == ReservationStatus.FAILED)
    )
    failed_count = failed_result.scalar_one()
    
    return total_count, failed_count


async def check_user_rate_limit(user_id: int, db: AsyncSession, is_admin: bool = False) -> None:
    """Check if user is within rate limits.

    Args:
        user_id: The user's ID
        db: Database session
        is_admin: If True, use more lax admin rate limits

    Raises:
        RateLimitExceeded: If user has exceeded their reservation limit
    """
    from app.services.settings import get_rate_limit_settings
    limits = await get_rate_limit_settings(db)

    total_count, failed_count = await get_user_reservation_counts(user_id, db, hours=1)

    successful_count = total_count - failed_count
    effective_count = successful_count + (failed_count * limits["failed_multiplier"])

    limit = limits["admin_per_hour"] if is_admin else limits["per_user_hour"]
    if effective_count >= limit:
        logger.warning(
            f"User {user_id} rate limited: {effective_count} effective reservations "
            f"(total={total_count}, failed={failed_count}, limit={limit}, is_admin={is_admin})"
        )
        raise RateLimitExceeded(
            t("errors.rate_limit_exceeded", limit=limit, multiplier=limits['failed_multiplier']),
            retry_after_seconds=3600,
        )

    # Daily limit
    daily_limit = limits["admin_per_day"] if is_admin else limits["per_user_day"]
    if daily_limit > 0:
        daily_total, daily_failed = await get_user_reservation_counts(user_id, db, hours=24)
        daily_successful = daily_total - daily_failed
        daily_effective = daily_successful + (daily_failed * limits["failed_multiplier"])
        if daily_effective >= daily_limit:
            logger.warning(
                f"User {user_id} daily rate limited: {daily_effective} effective "
                f"(limit={daily_limit})"
            )
            raise RateLimitExceeded(
                t("errors.daily_limit_exceeded", limit=daily_limit),
                retry_after_seconds=3600,
            )


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker has tripped due to too many failures."""

    def __init__(self, message: str, retry_after_seconds: int = 600):
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


async def check_circuit_breaker(db: AsyncSession) -> None:
    """Check if the site-wide circuit breaker has tripped.

    Counts FAILED reservations in a recent window. If the count exceeds the
    threshold, provisioning is blocked until ``cooldown_minutes`` have elapsed
    since the most recent failure.

    Raises:
        CircuitBreakerOpen: If provisioning should be halted.
    """
    from app.services.settings import get_rate_limit_settings

    limits = await get_rate_limit_settings(db)
    window = limits["circuit_breaker_window_minutes"]
    threshold = limits["circuit_breaker_threshold"]
    cooldown = limits["circuit_breaker_cooldown_minutes"]

    if threshold <= 0:
        return  # Disabled

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)

    # Count failures in window (using created_at as proxy — reservations fail
    # shortly after creation, so this is accurate within the window)
    fail_count_result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.status == ReservationStatus.FAILED)
        .where(Reservation.created_at >= cutoff)
    )
    fail_count = fail_count_result.scalar_one()

    if fail_count < threshold:
        return  # Below threshold

    # Circuit breaker tripped — check if cooldown has elapsed since last failure
    last_failure_result = await db.execute(
        select(func.max(Reservation.created_at))
        .where(Reservation.status == ReservationStatus.FAILED)
        .where(Reservation.created_at >= cutoff)
    )
    last_failure_at = last_failure_result.scalar_one()

    if last_failure_at is None:
        return  # Shouldn't happen, but be safe

    # Make timezone-aware if naive (SQLite stores naive datetimes)
    if last_failure_at.tzinfo is None:
        last_failure_at = last_failure_at.replace(tzinfo=timezone.utc)

    cooldown_ends = last_failure_at + timedelta(minutes=cooldown)
    now = datetime.now(timezone.utc)

    if now < cooldown_ends:
        remaining_seconds = int((cooldown_ends - now).total_seconds())
        remaining_minutes = remaining_seconds // 60 + 1
        logger.warning(
            f"Circuit breaker OPEN: {fail_count} failures in last {window}min "
            f"(threshold={threshold}), cooldown ends in {remaining_minutes}min"
        )
        raise CircuitBreakerOpen(
            t("errors.circuit_breaker", count=fail_count, window=window, minutes=remaining_minutes),
            retry_after_seconds=remaining_seconds,
        )


async def get_site_creation_count(db: AsyncSession, hours: int) -> int:
    """Get count of all reservations created site-wide in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.created_at >= cutoff)
    )
    return result.scalar_one()


async def get_site_provisioning_count(db: AsyncSession) -> int:
    """Get count of currently provisioning reservations site-wide."""
    result = await db.execute(
        select(func.count(Reservation.id))
        .where(Reservation.status.in_([
            ReservationStatus.PENDING,
            ReservationStatus.PROVISIONING,
        ]))
    )
    return result.scalar_one()


async def check_site_rate_limit(db: AsyncSession) -> None:
    """Check if site is within provisioning capacity.

    Raises:
        RateLimitExceeded: If too many reservations are currently provisioning
    """
    from app.services.settings import get_rate_limit_settings
    limits = await get_rate_limit_settings(db)

    current_count = await get_site_provisioning_count(db)
    limit = limits["site_provisioning_max"]

    if current_count >= limit:
        logger.warning(
            f"Site-wide rate limit hit: {current_count} provisioning (limit={limit})"
        )
        raise RateLimitExceeded(
            t("errors.site_capacity", current=current_count, max=limit),
            retry_after_seconds=300,
        )

    # Sitewide hourly creation limit
    sitewide_hourly = limits["sitewide_per_hour"]
    if sitewide_hourly > 0:
        hourly_count = await get_site_creation_count(db, hours=1)
        if hourly_count >= sitewide_hourly:
            logger.warning(f"Sitewide hourly limit hit: {hourly_count} (limit={sitewide_hourly})")
            raise RateLimitExceeded(
                t("errors.sitewide_hourly", current=hourly_count, max=sitewide_hourly),
                retry_after_seconds=300,
            )

    # Sitewide daily creation limit
    sitewide_daily = limits["sitewide_per_day"]
    if sitewide_daily > 0:
        daily_count = await get_site_creation_count(db, hours=24)
        if daily_count >= sitewide_daily:
            logger.warning(f"Sitewide daily limit hit: {daily_count} (limit={sitewide_daily})")
            raise RateLimitExceeded(
                t("errors.sitewide_daily", current=daily_count, max=sitewide_daily),
                retry_after_seconds=3600,
            )


async def get_user_daily_hours(user_id: int, db: AsyncSession) -> float:
    """Get total reservation hours for a user today (last 24 hours).

    For ended reservations with an ended_at timestamp, counts actual consumed
    time (ended_at - started_at, or ended_at - starts_at if never activated).
    For active/provisioning/pending reservations, counts the full scheduled
    duration (ends_at - starts_at).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Ended statuses where we should use actual consumed time
    ended_statuses = [
        ReservationStatus.ENDED,
        ReservationStatus.CANCELLED,
        ReservationStatus.NO_SHOW,
    ]

    # For ended reservations with ended_at: use actual time consumed
    # Use started_at if available (more accurate), fall back to starts_at
    ended_result = await db.execute(
        select(
            func.sum(
                (func.julianday(Reservation.ended_at)
                 - func.julianday(func.coalesce(Reservation.started_at, Reservation.starts_at)))
                * 24
            )
        )
        .where(Reservation.user_id == user_id)
        .where(Reservation.created_at >= cutoff)
        .where(Reservation.status.in_(ended_statuses))
        .where(Reservation.ended_at != None)
    )
    ended_hours = ended_result.scalar_one_or_none() or 0.0

    # For active/provisioning/pending or old ended reservations without ended_at:
    # use scheduled duration (ends_at - starts_at)
    other_result = await db.execute(
        select(
            func.sum(
                (func.julianday(Reservation.ends_at) - func.julianday(Reservation.starts_at)) * 24
            )
        )
        .where(Reservation.user_id == user_id)
        .where(Reservation.created_at >= cutoff)
        .where(Reservation.status != ReservationStatus.FAILED)
        .where(
            ~and_(
                Reservation.status.in_(ended_statuses),
                Reservation.ended_at != None,
            )
        )
    )
    other_hours = other_result.scalar_one_or_none() or 0.0

    return float(ended_hours + other_hours)


async def check_daily_hours_limit(
    user_id: int,
    db: AsyncSession,
    requested_hours: int = 4,
) -> None:
    """Check if creating a reservation would exceed the daily hours limit.

    Raises:
        DailyHoursExceeded: If user would exceed their daily hours budget
    """
    from app.services.settings import get_setting

    limit_hours = int(await get_setting("daily_hours_limit", db, "12"))
    if limit_hours <= 0:
        return  # Disabled

    current_hours = await get_user_daily_hours(user_id, db)
    if current_hours + requested_hours > limit_hours:
        remaining = max(0, limit_hours - current_hours)
        logger.warning(
            f"User {user_id} daily hours limit: {current_hours:.1f}h used "
            f"(limit={limit_hours}h, remaining={remaining:.1f}h)"
        )
        raise DailyHoursExceeded(
            t("errors.daily_hours_exceeded", used=f'{current_hours:.1f}', limit=limit_hours)
        )

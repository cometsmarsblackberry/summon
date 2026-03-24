"""Reservation business logic."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reservation import Reservation, ReservationStatus
from app.models.user import User
from app.utils.passwords import generate_motd_token, generate_password, generate_logsecret


logger = logging.getLogger(__name__)


async def get_next_reservation_number(db: AsyncSession) -> int:
    """Get the next sequential reservation number."""
    result = await db.execute(
        select(func.max(Reservation.reservation_number))
    )
    max_number = result.scalar_one_or_none()
    return (max_number or 0) + 1


async def create_reservation(
    user: User,
    location: str,
    duration_hours: int,
    first_map: str,
    db: AsyncSession,
    starts_at: Optional[datetime] = None,
    enable_direct_connect: bool = False,
) -> Reservation:
    """Create a new reservation.

    Args:
        user: Reservation owner
        location: Location code (e.g., 'santiago', 'seoul')
        duration_hours: Duration in hours (1-4)
        first_map: Initial map to load
        db: Database session
        starts_at: Optional scheduled start time (default: now)

    Returns:
        Created reservation
    """
    # Validate duration
    from app.services.settings import get_reservation_settings
    res_settings = await get_reservation_settings(db)
    max_hours = res_settings["max_duration_hours"]
    if duration_hours < 1 or duration_hours > max_hours:
        raise ValueError(f"Duration must be 1-{max_hours} hours")
    
    # Set times
    if starts_at is None:
        starts_at = datetime.now(timezone.utc)
    ends_at = starts_at + timedelta(hours=duration_hours, seconds=-15)
    
    # Get next reservation number
    reservation_number = await get_next_reservation_number(db)
    
    # Generate credentials
    reservation = Reservation(
        reservation_number=reservation_number,
        user_id=user.id,
        location=location,
        starts_at=starts_at,
        ends_at=ends_at,
        password=generate_password(8),
        rcon_password=generate_password(12),
        tv_password=generate_password(8),
        first_map=first_map,
        auto_end=True,
        enable_direct_connect=enable_direct_connect,
        motd_token=generate_motd_token(),
        logsecret=generate_logsecret(32),
        plugin_api_key=generate_logsecret(32),
        status=ReservationStatus.PENDING,
    )
    
    db.add(reservation)
    
    # Increment user reservation count
    user.reservation_count += 1
    
    await db.commit()
    await db.refresh(reservation)
    
    logger.info(
        f"Created reservation #{reservation_number} for user {user.steam_id} "
        f"at {location}"
    )
    
    return reservation


async def end_reservation(
    reservation: Reservation,
    db: AsyncSession,
    status: ReservationStatus = ReservationStatus.ENDED,
) -> None:
    """End a reservation.
    
    Args:
        reservation: Reservation to end
        db: Database session
        status: Final status (ENDED, CANCELLED, NO_SHOW)
    """
    if not reservation.can_be_ended:
        raise ValueError(f"Cannot end reservation in status {reservation.status}")
    
    reservation.status = status
    reservation.ended_at = datetime.now(timezone.utc)
    await db.commit()
    
    logger.info(
        f"Ended reservation #{reservation.reservation_number} with status {status.value}"
    )


async def get_user_active_reservation(
    user: User,
    db: AsyncSession,
) -> Optional[Reservation]:
    """Get user's current active reservation, if any."""
    result = await db.execute(
        select(Reservation)
        .where(Reservation.user_id == user.id)
        .where(Reservation.status.in_([
            ReservationStatus.PENDING,
            ReservationStatus.PROVISIONING,
            ReservationStatus.ACTIVE,
        ]))
    )
    return result.scalar_one_or_none()


async def get_reservation_by_id(
    reservation_id: int,
    db: AsyncSession,
) -> Optional[Reservation]:
    """Get reservation by ID."""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    return result.scalar_one_or_none()


async def get_reservation_by_number(
    reservation_number: int,
    db: AsyncSession,
) -> Optional[Reservation]:
    """Get reservation by number."""
    result = await db.execute(
        select(Reservation).where(Reservation.reservation_number == reservation_number)
    )
    return result.scalar_one_or_none()


async def get_user_reservations(
    user: User,
    db: AsyncSession,
    limit: int = 20,
) -> list[Reservation]:
    """Get user's recent reservations."""
    result = await db.execute(
        select(Reservation)
        .where(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())

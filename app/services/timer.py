"""Per-reservation expiry timers.

Schedules an asyncio task per active reservation that fires at ends_at,
immediately ending the reservation and destroying the instance.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import exists, select

from app.database import async_session_maker
from app.models import InstantAssignment, Reservation, ReservationStatus, RuntimeKind


logger = logging.getLogger(__name__)

# reservation_id -> asyncio.Task
_expiry_tasks: dict[int, asyncio.Task] = {}


def schedule_expiry_timer(
    reservation_id: int,
    reservation_number: int,
    ends_at: datetime,
    instance_id: str | None,
) -> None:
    """Schedule (or reschedule) an expiry timer for a reservation.

    Args:
        reservation_id: Reservation PK
        reservation_number: Human-readable reservation number
        ends_at: When the reservation expires
        instance_id: Legacy CloudInstance.id used for logging only. Instant
            reservations leave it unset and are resolved from the database.
    """
    cancel_expiry_timer(reservation_id)

    now = datetime.now(timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    delay = max((ends_at - now).total_seconds(), 0)

    task = asyncio.create_task(
        _expiry_worker(reservation_id, reservation_number, delay, instance_id)
    )
    _expiry_tasks[reservation_id] = task
    logger.info(
        f"Scheduled expiry timer for reservation #{reservation_number} "
        f"in {delay:.0f}s (instance_id={instance_id})"
    )


def cancel_expiry_timer(reservation_id: int) -> None:
    """Cancel the expiry timer for a reservation, if any."""
    task = _expiry_tasks.pop(reservation_id, None)
    if task is not None:
        task.cancel()


def cancel_all_expiry_timers() -> None:
    """Cancel every active expiry timer (used during shutdown)."""
    for task in _expiry_tasks.values():
        task.cancel()
    _expiry_tasks.clear()


async def restore_expiry_timers() -> None:
    """Re-schedule expiry timers for active and leased Instant reservations.

    Called once at application startup so that timers survive restarts.
    """
    async with async_session_maker() as db:
        instant_history = exists().where(
            InstantAssignment.reservation_id == Reservation.id
        )
        result = await db.execute(select(Reservation).where(
            (Reservation.status == ReservationStatus.ACTIVE)
            | (
                Reservation.status.in_((
                    ReservationStatus.PENDING,
                    ReservationStatus.PROVISIONING,
                ))
                & (
                    (Reservation.runtime_kind == RuntimeKind.INSTANT)
                    | instant_history
                )
            )
        ))
        reservations = list(result.scalars().all())

    for r in reservations:
        if r.ends_at:
            schedule_expiry_timer(r.id, r.reservation_number, r.ends_at, r.instance_id)

    if reservations:
        logger.info(f"Restored expiry timers for {len(reservations)} active reservations")


async def _expiry_worker(
    reservation_id: int,
    reservation_number: int,
    delay: float,
    instance_id: str | None,
) -> None:
    """Sleep until expiry, then end the reservation and destroy the instance.

    Player-facing warnings and kicks are handled by the SourceMod plugin
    autonomously (it reads ends_at from its ConVar). This worker only
    handles the backend side: marking ENDED and destroying the VM.
    """
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return

    try:
        async with async_session_maker() as db:
            result = await db.execute(
                select(Reservation).where(Reservation.id == reservation_id)
            )
            reservation = result.scalar_one_or_none()

            instant_history_result = await db.execute(select(
                exists().where(
                    InstantAssignment.reservation_id == reservation_id
                )
            )) if reservation else None
            has_instant_history = bool(
                instant_history_result.scalar()
                if instant_history_result is not None else False
            )
            instant_provisioning = bool(
                reservation
                and reservation.status in {
                    ReservationStatus.PENDING,
                    ReservationStatus.PROVISIONING,
                }
                and (
                    reservation.runtime_kind == RuntimeKind.INSTANT
                    or has_instant_history
                )
            )
            if not reservation or (
                reservation.status != ReservationStatus.ACTIVE
                and not instant_provisioning
            ):
                return  # Race guard: already ended

            # End the reservation
            reservation.status = ReservationStatus.ENDED
            reservation.ended_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(
                f"Expiry timer ended reservation #{reservation_number}"
            )

            # Clear player data
            from app.routers.internal import clear_player_data
            clear_player_data(reservation_number)

            # Runtime-specific expiry. Instant keeps the slot leased until a
            # stop acknowledgement/reconciliation; cloud is destroyed because
            # no paid reservation time remains.
            from app.services.runtime import end_reservation_runtime
            await end_reservation_runtime(
                reservation,
                db,
                was_active=True,
                had_started=reservation.started_at is not None,
                at_expiry=True,
            )

    except Exception:
        logger.exception(f"Expiry worker error for reservation #{reservation_number}")
    finally:
        _expiry_tasks.pop(reservation_id, None)

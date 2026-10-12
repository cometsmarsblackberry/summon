"""Recover Vultr creates without ever repeating an uncertain POST."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import CloudCreationAttempt, CloudInstance
from app.models.reservation import Reservation, ReservationStatus
from app.services.failure_messages import public_failure_reason
from app.services.cloud_provider import CloudInstanceData, CloudProvider

logger = logging.getLogger(__name__)


async def record_created_instance(
    attempt: CloudCreationAttempt, cloud: CloudInstanceData, db: AsyncSession,
) -> CloudInstance | None:
    """Atomically exchange the durable intent for the provider's instance ID."""
    # Claim in the same transaction as the insert. Both background provisioning
    # and periodic sync can discover the instance; only one may register it.
    claimed = await db.execute(
        delete(CloudCreationAttempt)
        .where(CloudCreationAttempt.id == attempt.id)
        .returning(CloudCreationAttempt.id)
    )
    if claimed.scalar_one_or_none() is None:
        return await db.get(CloudInstance, cloud.id)

    reservation = await db.get(Reservation, attempt.reservation_id)
    await db.refresh(reservation)
    now = datetime.utcnow()
    provisionable = reservation.status in (
        ReservationStatus.PENDING, ReservationStatus.PROVISIONING,
    ) and reservation.ends_at > now
    if not provisionable and reservation.status in (
        ReservationStatus.PENDING, ReservationStatus.PROVISIONING,
    ):
        reservation.status = ReservationStatus.ENDED
        reservation.ended_at = now

    try:
        created = datetime.fromisoformat(cloud.date_created.replace("Z", "+00:00"))
        if created.tzinfo:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        created = attempt.created_at
    instance = CloudInstance(
        id=cloud.id,
        instance_id=attempt.instance_id,
        auth_token=attempt.auth_token,
        location=reservation.location,
        provider_code="vultr",
        provider_region=attempt.region,
        shape=cloud.plan,
        ip_address=cloud.main_ip if cloud.main_ip != "0.0.0.0" else None,
        status=cloud.status,
        created_at=created,
        current_reservation_id=reservation.id if provisionable else None,
        # Late results for cancelled/failed/expired reservations enter cleanup
        # immediately; their agent must never be given a live reservation.
        is_available=not provisionable,
        billing_hour_ends_at=reservation.ends_at if provisionable else now,
    )
    db.add(instance)
    reservation.instance_id = cloud.id
    await db.commit()
    logger.info("Recorded Vultr instance %s for creation attempt %s (reservation #%s)",
                cloud.id, attempt.id, reservation.reservation_number)
    return instance


async def reconcile_creation_attempt(
    attempt: CloudCreationAttempt, client: CloudProvider, db: AsyncSession,
    cloud_instances: list[CloudInstanceData] | None = None,
) -> CloudInstance | None:
    """Preserve unknown outcomes, even across restarts and empty list responses."""
    if cloud_instances is None:
        try:
            cloud_instances = await client.list_instances()
        except Exception:
            logger.warning("Cannot reconcile Vultr creation attempt %s; retaining intent",
                           attempt.id, exc_info=True)
            cloud_instances = []
    matches = [i for i in cloud_instances
               if attempt.id in i.tags and i.region == attempt.region]
    if len(matches) == 1:
        return await record_created_instance(attempt, matches[0], db)
    if len(matches) > 1:
        logger.error("Multiple Vultr instances match creation attempt %s; manual reconciliation required",
                     attempt.id)

    # Stop making the player wait indefinitely, but keep the intent so a VM
    # appearing later is still recovered and cleaned up. Absence is not proof
    # that it is safe to submit another create request.
    if attempt.created_at < datetime.utcnow() - timedelta(minutes=5):
        reservation = await db.get(Reservation, attempt.reservation_id)
        await db.refresh(reservation)
        if reservation.status in (ReservationStatus.PENDING, ReservationStatus.PROVISIONING):
            reservation.status = ReservationStatus.FAILED
            reservation.failure_reason = public_failure_reason(
                reservation.status, reservation.provision_attempts,
            )
            await db.commit()
        logger.warning("Unresolved Vultr creation attempt %s for reservation %s; retaining for reconciliation",
                       attempt.id, attempt.reservation_id)
    return None

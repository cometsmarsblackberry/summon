"""Infrastructure-neutral reservation runtime service.

Every reservation lifecycle entry point calls this module. The cloud adapter
is deliberately a thin wrapper around the existing orchestrator; Instant
reservations never enter provider, billing, or warm-pool code.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models.instance import CloudInstance, EnabledLocation, LocationProvider, Provider
from app.models.instant import InstantAssignment, InstantHost, InstantSlot
from app.models.reservation import Reservation, ReservationStatus, RuntimeKind
from app.services.cloud_provider import get_cloud_client
from app.services.instant_hosts import protocol_is_compatible
from app.services.provider_priority import get_providers_for_location
from app.services.settings import get_instant_settings


logger = logging.getLogger(__name__)
settings = get_settings()

OPEN_ASSIGNMENT_STATES = frozenset({
    "claimed", "starting", "ready", "restarting", "stopping", "degraded",
})
TERMINAL_FAILURE_CLASSES = frozenset({
    "configuration", "reservation_data", "invalid_command", "unsupported_config",
})
HOST_FAILURE_CLASSES = frozenset({
    "host", "podman", "podman_unavailable", "disk", "memory", "preflight", "image",
})


class LocationCapacityError(RuntimeError):
    """Raised when neither Instant nor cloud has capacity in a location."""

    def __init__(self, location: str):
        super().__init__(f"No server capacity is currently available in {location}")
        self.location = location
        self.code = "location_capacity"


@dataclass(frozen=True)
class RuntimeCapacity:
    instant_slots_available: int
    cloud_slots_available: int
    warm_cloud_available: bool = False

    @property
    def instant_available(self) -> bool:
        return self.instant_slots_available > 0

    @property
    def cloud_available(self) -> bool:
        return self.cloud_slots_available > 0

    @property
    def reservable(self) -> bool:
        return self.instant_available or self.cloud_available


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_value(reservation: Reservation) -> str:
    kind = getattr(reservation, "runtime_kind", RuntimeKind.CLOUD)
    return kind.value if isinstance(kind, RuntimeKind) else str(kind or "cloud").lower()


def is_instant_reservation(reservation: Reservation) -> bool:
    return _runtime_value(reservation) == RuntimeKind.INSTANT.value


async def _get_cloud_instance(
    reservation: Reservation, db: AsyncSession
) -> CloudInstance | None:
    instance_id = getattr(reservation, "instance_id", None)
    if not instance_id:
        return None
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == instance_id)
    )
    return result.scalar_one_or_none()


async def get_open_assignment(
    reservation_id: int,
    db: AsyncSession,
    *,
    load_slot: bool = True,
) -> InstantAssignment | None:
    query = select(InstantAssignment).where(
        InstantAssignment.reservation_id == reservation_id,
        InstantAssignment.closed_at.is_(None),
    )
    if load_slot:
        query = query.options(
            selectinload(InstantAssignment.slot).selectinload(InstantSlot.host)
        )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _instant_enabled(db: AsyncSession) -> bool:
    try:
        return bool((await get_instant_settings(db))["enabled"])
    except Exception:
        logger.warning("Could not read Instant rollout setting", exc_info=True)
        return False


def _host_cutoff() -> datetime:
    return _now() - timedelta(seconds=settings.instant_host_offline_seconds)


async def count_available_instant_slots(location: str, db: AsyncSession) -> int:
    if not await _instant_enabled(db):
        return 0
    occupied = exists().where(
        InstantAssignment.slot_id == InstantSlot.id,
        InstantAssignment.closed_at.is_(None),
    )
    result = await db.execute(
        select(func.count(InstantSlot.id))
        .join(InstantHost, InstantSlot.host_id == InstantHost.id)
        .where(
            InstantHost.location == location,
            InstantHost.enabled.is_(True),
            InstantHost.draining.is_(False),
            InstantHost.deleted_at.is_(None),
            InstantHost.credential_hash.is_not(None),
            InstantHost.last_heartbeat_at.is_not(None),
            InstantHost.last_heartbeat_at >= _host_cutoff(),
            InstantHost.health_status.in_(("ready", "healthy", "online", "degraded")),
            InstantHost.health_error.is_(None),
            InstantHost.ready_image_digest.is_not(None),
            InstantHost.image_status.in_(("ready", "failed")),
            InstantHost.protocol_min <= settings.instant_protocol_max,
            InstantHost.protocol_max >= settings.instant_protocol_min,
            or_(
                InstantHost.version_pin.is_(None),
                InstantHost.agent_version == InstantHost.version_pin,
            ),
            InstantSlot.enabled.is_(True),
            InstantSlot.quarantined_at.is_(None),
            InstantSlot.error_code.is_(None),
            ~occupied,
        )
    )
    return int(result.scalar_one() or 0)


async def _cloud_capacity(
    location: str,
    db: AsyncSession,
    *,
    exclude_reservation_id: int | None = None,
) -> tuple[int, bool]:
    """Return conservative local cloud capacity and warm-pool availability."""
    mappings = await get_providers_for_location(location, db)
    loc = await db.get(EnabledLocation, location)
    if not mappings and loc and loc.provider and loc.provider_region:
        mappings = [LocationProvider(
            location_code=location,
            provider_code=loc.provider,
            provider_region=loc.provider_region,
            priority=0,
            enabled=True,
            instance_plan=loc.instance_plan,
            region_instance_limit=loc.region_instance_limit,
        )]
    if not mappings:
        return 0, False

    valid_mappings: list[tuple[LocationProvider, Provider]] = []
    for mapping in mappings:
        try:
            client_configured = get_cloud_client(mapping.provider_code) is not None
        except Exception:
            logger.warning(
                "Cloud provider %s is not usable while checking capacity",
                mapping.provider_code,
                exc_info=True,
            )
            client_configured = False
        if not client_configured:
            continue
        provider = await db.get(Provider, mapping.provider_code)
        if provider is not None and provider.enabled:
            valid_mappings.append((mapping, provider))
    if not valid_mappings:
        return 0, False

    configured_codes = {mapping.provider_code for mapping, _ in valid_mappings}
    warm_result = await db.execute(
        select(func.count(CloudInstance.id)).where(
            CloudInstance.location == location,
            CloudInstance.provider_code.in_(configured_codes),
            CloudInstance.status != "terminated",
            CloudInstance.is_available.is_(True),
        )
    )
    warm_count = int(warm_result.scalar_one() or 0)
    total = warm_count
    counted_global: set[str] = set()
    for mapping, provider in valid_mappings:
        limit = mapping.region_instance_limit or provider.instance_limit
        if mapping.provider_code == "gcore":
            used_result = await db.execute(
                select(func.count(CloudInstance.id)).where(
                    CloudInstance.provider_code == mapping.provider_code,
                    CloudInstance.provider_region == mapping.provider_region,
                    CloudInstance.status != "terminated",
                )
            )
        else:
            if mapping.provider_code in counted_global:
                continue
            counted_global.add(mapping.provider_code)
            used_result = await db.execute(
                select(func.count(CloudInstance.id)).where(
                    CloudInstance.provider_code == mapping.provider_code,
                    CloudInstance.status != "terminated",
                )
            )
        used = int(used_result.scalar_one() or 0)
        total += max(0, int(limit) - used)
    # A just-created cloud reservation has not produced a CloudInstance yet,
    # but it already consumes one unit of schedulable capacity. Counting these
    # leases makes the reservation-creation lock meaningful for cloud-only and
    # mixed locations instead of overcommitting the same final slot.
    pending_query = select(func.count(Reservation.id)).where(
            Reservation.location == location,
            Reservation.runtime_kind == RuntimeKind.CLOUD,
            Reservation.instance_id.is_(None),
            Reservation.status.in_((
                ReservationStatus.PENDING,
                ReservationStatus.PROVISIONING,
            )),
        )
    if exclude_reservation_id is not None:
        pending_query = pending_query.where(Reservation.id != exclude_reservation_id)
    pending_result = await db.execute(pending_query)
    pending = int(pending_result.scalar_one() or 0)
    return max(0, total - pending), warm_count > 0


async def get_location_runtime_capacity(
    location: str, db: AsyncSession
) -> RuntimeCapacity:
    instant = await count_available_instant_slots(location, db)
    cloud, warm = await _cloud_capacity(location, db)
    return RuntimeCapacity(instant, cloud, warm)


async def ensure_location_capacity(location: str, db: AsyncSession) -> RuntimeCapacity:
    capacity = await get_location_runtime_capacity(location, db)
    if not capacity.reservable:
        raise LocationCapacityError(location)
    return capacity


async def claim_instant_slot(
    reservation: Reservation,
    db: AsyncSession,
    *,
    excluded_host_ids: set[int] | None = None,
) -> InstantAssignment | None:
    """Atomically claim the best available slot.

    The scheduler orders by host load, host recency, and slot index. Partial
    unique indexes are the final concurrency guard; a losing transaction tries
    the next candidate without leaking a duplicate lease.
    """
    if not await _instant_enabled(db):
        return None
    existing = await get_open_assignment(reservation.id, db)
    if existing is not None:
        return existing

    excluded = excluded_host_ids or set()
    occupied = exists().where(
        InstantAssignment.slot_id == InstantSlot.id,
        InstantAssignment.closed_at.is_(None),
    )
    host_load = (
        select(func.count(InstantAssignment.id))
        .join(InstantSlot, InstantAssignment.slot_id == InstantSlot.id)
        .where(
            InstantSlot.host_id == InstantHost.id,
            InstantAssignment.closed_at.is_(None),
        )
        .correlate(InstantHost)
        .scalar_subquery()
    )
    query = (
        select(InstantSlot, InstantHost)
        .join(InstantHost, InstantSlot.host_id == InstantHost.id)
        .where(
            InstantHost.location == reservation.location,
            InstantHost.enabled.is_(True),
            InstantHost.draining.is_(False),
            InstantHost.deleted_at.is_(None),
            InstantHost.credential_hash.is_not(None),
            InstantHost.last_heartbeat_at >= _host_cutoff(),
            InstantHost.health_status.in_(("ready", "healthy", "online", "degraded")),
            InstantHost.health_error.is_(None),
            InstantHost.ready_image_digest.is_not(None),
            InstantHost.image_status.in_(("ready", "failed")),
            InstantHost.protocol_min <= settings.instant_protocol_max,
            InstantHost.protocol_max >= settings.instant_protocol_min,
            or_(
                InstantHost.version_pin.is_(None),
                InstantHost.agent_version == InstantHost.version_pin,
            ),
            InstantSlot.enabled.is_(True),
            InstantSlot.quarantined_at.is_(None),
            InstantSlot.error_code.is_(None),
            ~occupied,
        )
        .order_by(
            host_load.asc(),
            InstantHost.last_used_at.asc().nullsfirst(),
            InstantSlot.slot_index.asc(),
        )
    )
    if excluded:
        query = query.where(InstantHost.id.not_in(excluded))
    candidates = (await db.execute(query)).all()
    if not candidates:
        return None

    generation_result = await db.execute(
        select(func.max(InstantAssignment.generation)).where(
            InstantAssignment.reservation_id == reservation.id
        )
    )
    generation = int(generation_result.scalar_one_or_none() or 0) + 1

    for slot, host in candidates:
        assignment = InstantAssignment(
            reservation_id=reservation.id,
            slot_id=slot.id,
            generation=generation,
            command_id=str(uuid.uuid4()),
            state="claimed",
            image_digest=host.ready_image_digest,
        )
        try:
            async with db.begin_nested():
                db.add(assignment)
                await db.flush()
        except IntegrityError:
            continue

        reservation.runtime_kind = RuntimeKind.INSTANT
        reservation.instance_id = None
        reservation.status = ReservationStatus.PROVISIONING
        reservation.direct_ip = host.public_ipv4
        reservation.direct_port = slot.game_port
        reservation.direct_tv_port = slot.tv_port
        slot.last_used_at = _now()
        host.last_used_at = _now()
        await db.commit()
        # Ensure relationships are usable after the commit without lazy IO.
        assignment.slot = slot
        slot.host = host
        logger.info(
            "instant_event=slot_claim host_id=%s slot_index=%s reservation=%s generation=%s",
            host.id, slot.slot_index, reservation.reservation_number, generation,
        )
        return assignment
    return None


def _ready_image_reference(host: InstantHost, desired: str) -> str:
    digest = host.ready_image_digest or ""
    if "@" in digest:
        return digest
    if digest.startswith("sha256:"):
        return f"{desired.split('@', 1)[0]}@{digest}"
    return desired


async def _instant_config(
    reservation: Reservation,
    assignment: InstantAssignment,
    db: AsyncSession,
) -> dict:
    from app.models.user import User
    from app.services.orchestrator import build_reservation_config
    from app.services.settings import get_fastdl_url
    from app.utils.steam import steamid64_to_steamid2

    slot = assignment.slot
    host = slot.host
    owner = await db.get(User, reservation.user_id) if reservation.user_id else None
    loc = await db.get(EnabledLocation, reservation.location)
    admin_result = await db.execute(select(User.steam_id).where(User.is_admin.is_(True)))
    desired_image = host.desired_image or (await get_instant_settings(db))["container_image"]
    config = build_reservation_config(
        reservation=reservation,
        owner_steam_id=owner.steam_id if owner else "",
        owner_name=owner.display_name if owner else "",
        location_city=loc.city if loc and loc.city else reservation.location,
        container_image=_ready_image_reference(host, desired_image),
        fastdl_url=await get_fastdl_url(db),
        admin_steam_ids=[steamid64_to_steamid2(row[0]) for row in admin_result.all()],
    )
    config.update({
        "runtime_kind": "instant",
        "host_id": host.id,
        "slot_id": slot.id,
        "slot_index": slot.slot_index,
        "assignment_id": assignment.id,
        "generation": assignment.generation,
        "external_game_port": slot.game_port,
        "external_tv_port": slot.tv_port,
        "state_dir": f"slots/{slot.slot_index}",
        "container_name": f"summon-h{host.id}-s{slot.slot_index}",
        "labels": {
            "summon.runtime": "instant",
            "summon.host_id": str(host.id),
            "summon.slot_id": str(slot.id),
            "summon.slot_index": str(slot.slot_index),
            "summon.assignment_id": str(assignment.id),
            "summon.reservation_id": str(reservation.id),
            "summon.generation": str(assignment.generation),
            "summon.lease_expires_at": str(int(reservation.ends_at.timestamp())),
        },
    })
    return config


def _command_envelope(
    command_type: str,
    reservation: Reservation,
    assignment: InstantAssignment,
    *,
    command_id: str | None = None,
    **payload: Any,
) -> dict:
    slot = assignment.slot
    return {
        "type": command_type,
        "protocol": 1,
        "command_id": command_id or str(uuid.uuid4()),
        "reservation_id": reservation.id,
        "assignment_id": assignment.id,
        "slot_id": slot.id,
        "slot_index": slot.slot_index,
        "generation": assignment.generation,
        **payload,
    }


async def dispatch_instant_start(
    reservation: Reservation,
    assignment: InstantAssignment,
    db: AsyncSession,
) -> bool:
    from app.routers.internal import send_instant_command

    # Lifecycle actions can race the detached provisioning worker. Never emit
    # a late start after an end/cancel/failure has committed. ACTIVE is allowed
    # because reconciliation may need to recreate a disappeared desired
    # container without changing runtimes.
    await db.refresh(reservation, attribute_names=["status"])
    if reservation.status not in {
        ReservationStatus.PENDING,
        ReservationStatus.PROVISIONING,
        ReservationStatus.ACTIVE,
    }:
        logger.info(
            "Skipped stale Instant start for reservation #%s in status %s",
            reservation.reservation_number,
            reservation.status.value,
        )
        return True

    command = _command_envelope(
        "server.start",
        reservation,
        assignment,
        command_id=assignment.command_id,
        lease_expires_at=int(reservation.ends_at.timestamp()),
        game_port=assignment.slot.game_port,
        tv_port=assignment.slot.tv_port,
        image_digest=assignment.image_digest,
        config=await _instant_config(reservation, assignment, db),
    )
    assignment.state = "starting"
    assignment.start_sent_at = _now()
    assignment.last_command_id = assignment.command_id
    await db.commit()
    # Cover provisioning as well as ready servers. The host enforces this lease
    # locally, while the backend timer prevents a disconnected/provisioning
    # reservation from remaining open forever and is restored after restarts.
    from app.services.timer import schedule_expiry_timer
    schedule_expiry_timer(
        reservation.id,
        reservation.reservation_number,
        reservation.ends_at,
        None,
    )
    sent = await send_instant_command(assignment.slot.host_id, command)
    if not sent:
        logger.warning(
            "Instant host %s disconnected while starting reservation #%s",
            assignment.slot.host_id, reservation.reservation_number,
        )
    return sent


async def _close_failed_assignment(
    assignment: InstantAssignment,
    db: AsyncSession,
    *,
    failure_class: str,
    failure_code: str | None,
    failure_message: str | None,
) -> None:
    now = _now()
    assignment.state = "failed"
    assignment.failure_class = failure_class
    assignment.failure_code = failure_code
    assignment.failure_message = (failure_message or "")[:4000] or None
    assignment.failed_at = now
    assignment.closed_at = now
    slot = assignment.slot
    host = slot.host
    if failure_class in HOST_FAILURE_CLASSES:
        host.health_status = "quarantined"
        host.health_error = assignment.failure_message or failure_code
    elif failure_class not in TERMINAL_FAILURE_CLASSES:
        # Reservation/configuration failures belong to the reservation, not
        # the infrastructure. The agent rejects these before accepting a game
        # container, so quarantining a healthy slot would only leak capacity.
        slot.error_code = failure_code or failure_class
        slot.error_message = assignment.failure_message
        slot.quarantined_at = now
    await db.commit()
    logger.warning(
        "instant_event=start_failure host_id=%s slot_index=%s reservation_id=%s "
        "assignment_id=%s failure_class=%s failure_code=%s",
        host.id, slot.slot_index, assignment.reservation_id, assignment.id,
        failure_class, failure_code or "unknown",
    )


async def _prepare_cloud_fallback(
    reservation: Reservation, db: AsyncSession
) -> bool:
    """Persist a cloud fallback decision without doing provider network I/O."""
    cloud_capacity, _ = await _cloud_capacity(
        reservation.location,
        db,
        exclude_reservation_id=reservation.id,
    )
    if cloud_capacity <= 0:
        reservation.status = ReservationStatus.FAILED
        reservation.failure_reason = "No server capacity is currently available in this location."
        await db.commit()
        from app.services.timer import cancel_expiry_timer
        cancel_expiry_timer(reservation.id)
        return False
    reservation.runtime_kind = RuntimeKind.CLOUD
    reservation.instance_id = None
    reservation.status = ReservationStatus.PROVISIONING
    reservation.direct_ip = None
    reservation.direct_port = None
    reservation.direct_tv_port = None
    await db.commit()
    logger.info(
        "instant_event=cloud_fallback reservation=%s location=%s",
        reservation.reservation_number, reservation.location,
    )
    return True


async def _fallback_to_cloud(reservation: Reservation, db: AsyncSession):
    if not await _prepare_cloud_fallback(reservation, db):
        return None
    from app.services.orchestrator import provision_instance_for_reservation
    return await provision_instance_for_reservation(reservation, db)


async def provision_reservation_runtime(reservation: Reservation, db: AsyncSession):
    """Prefer Instant, retry a different host once, then use cloud."""
    # Once cloud provisioning has begun, retries stay on the existing cloud
    # path; a transient provider retry must not jump back to an Instant host.
    prior_instant_result = await db.execute(
        select(InstantAssignment.id)
        .where(InstantAssignment.reservation_id == reservation.id)
        .limit(1)
    )
    has_instant_history = prior_instant_result.scalar_one_or_none() is not None
    if not is_instant_reservation(reservation) and (
        int(getattr(reservation, "provision_attempts", 0) or 0) > 0
        or has_instant_history
    ):
        from app.services.orchestrator import provision_instance_for_reservation
        return await provision_instance_for_reservation(reservation, db)

    existing = await get_open_assignment(reservation.id, db)
    if existing:
        if await dispatch_instant_start(reservation, existing, db):
            return existing
        await _close_failed_assignment(
            existing, db,
            failure_class="host",
            failure_code="disconnected",
            failure_message="Host disconnected before accepting start command",
        )

    previous = await db.execute(
        select(InstantAssignment)
        .options(selectinload(InstantAssignment.slot).selectinload(InstantSlot.host))
        .where(InstantAssignment.reservation_id == reservation.id)
        .order_by(InstantAssignment.generation)
    )
    prior_assignments = list(previous.scalars().all())
    excluded_hosts = {assignment.slot.host_id for assignment in prior_assignments}
    attempts_remaining = max(0, 2 - len(excluded_hosts))

    for _ in range(attempts_remaining):
        assignment = await claim_instant_slot(
            reservation, db, excluded_host_ids=excluded_hosts
        )
        if assignment is None:
            break
        if await dispatch_instant_start(reservation, assignment, db):
            return assignment
        excluded_hosts.add(assignment.slot.host_id)
        await _close_failed_assignment(
            assignment, db,
            failure_class="host",
            failure_code="disconnected",
            failure_message="Host disconnected before accepting start command",
        )

    return await _fallback_to_cloud(reservation, db)


async def handle_instant_start_failure(
    assignment: InstantAssignment,
    reservation: Reservation,
    db: AsyncSession,
    *,
    failure_class: str,
    failure_code: str | None = None,
    failure_message: str | None = None,
    defer_cloud: bool = False,
) -> bool:
    """Apply failure policy and continue the bounded fallback sequence."""
    if assignment.closed_at is not None:
        return False
    await _close_failed_assignment(
        assignment,
        db,
        failure_class=failure_class,
        failure_code=failure_code,
        failure_message=failure_message,
    )
    if failure_class in TERMINAL_FAILURE_CLASSES:
        reservation.status = ReservationStatus.FAILED
        reservation.failure_reason = "The server configuration could not be started."
        await db.commit()
        from app.services.timer import cancel_expiry_timer
        cancel_expiry_timer(reservation.id)
        return False

    previous = await db.execute(
        select(InstantAssignment)
        .options(selectinload(InstantAssignment.slot).selectinload(InstantSlot.host))
        .where(InstantAssignment.reservation_id == reservation.id)
    )
    attempted = list(previous.scalars().all())
    excluded = {item.slot.host_id for item in attempted}
    if len(excluded) < 2:
        retry_assignment = await claim_instant_slot(
            reservation, db, excluded_host_ids=excluded
        )
        if retry_assignment is not None:
            if await dispatch_instant_start(reservation, retry_assignment, db):
                return False
            await _close_failed_assignment(
                retry_assignment, db,
                failure_class="host",
                failure_code="disconnected",
                failure_message="Retry host disconnected before accepting start command",
            )
    if defer_cloud:
        # Host events share one WebSocket reader. Persist the routing decision
        # here, then let a detached reservation worker perform provider I/O so
        # heartbeats and events for the host's other slots keep flowing.
        return await _prepare_cloud_fallback(reservation, db)
    await _fallback_to_cloud(reservation, db)
    return False


async def end_reservation_runtime(
    reservation: Reservation,
    db: AsyncSession,
    *,
    was_active: bool = True,
    had_started: bool = True,
    at_expiry: bool = False,
) -> bool:
    """Stop/release the selected runtime without crossing adapter boundaries."""
    if is_instant_reservation(reservation):
        assignment = await get_open_assignment(reservation.id, db)
        if assignment is None:
            return True
        from app.routers.internal import send_instant_command
        command_id = str(uuid.uuid4())
        assignment.state = "stopping"
        assignment.stop_requested_at = _now()
        assignment.last_command_id = command_id
        await db.commit()
        return await send_instant_command(
            assignment.slot.host_id,
            _command_envelope(
                "server.stop", reservation, assignment,
                command_id=command_id,
                reason="expiry" if at_expiry else "reservation_ended",
            ),
        )

    if not reservation.instance_id:
        return True
    # Cloud behavior remains exactly where it was: hourly servers may return to
    # the warm pool, while per-second/expired servers are destroyed.
    from app.routers.internal import send_to_agent
    from app.services.orchestrator import destroy_instance, is_hourly_billing, release_to_warm_pool
    cloud = await _get_cloud_instance(reservation, db)
    if cloud and (was_active or had_started):
        await send_to_agent(cloud.instance_id, {"type": "reservation.end"})
    if at_expiry:
        return await destroy_instance(reservation.instance_id, db)
    if await is_hourly_billing(reservation.location, db):
        if was_active or had_started:
            return await release_to_warm_pool(reservation.instance_id, db)
        return True
    return await destroy_instance(reservation.instance_id, db)


async def _send_instant_control(
    command_type: str,
    reservation: Reservation,
    db: AsyncSession,
    **payload: Any,
) -> bool:
    assignment = await get_open_assignment(reservation.id, db)
    if assignment is None:
        return False
    from app.routers.internal import send_instant_command
    command_id = str(uuid.uuid4())
    assignment.last_command_id = command_id
    if command_type == "server.restart":
        assignment.state = "restarting"
    await db.commit()
    return await send_instant_command(
        assignment.slot.host_id,
        _command_envelope(
            command_type, reservation, assignment,
            command_id=command_id, **payload,
        ),
    )


async def restart_reservation_runtime(
    reservation: Reservation, db: AsyncSession, config: dict
) -> bool:
    if is_instant_reservation(reservation):
        return await _send_instant_control(
            "server.restart", reservation, db, config=config,
            lease_expires_at=int(reservation.ends_at.timestamp()),
        )
    cloud = await _get_cloud_instance(reservation, db)
    if cloud is None:
        return False
    from app.routers.internal import send_container_restart
    return await send_container_restart(cloud.instance_id, config)


async def rcon_reservation_runtime(
    reservation: Reservation, db: AsyncSession, command: str
) -> bool:
    if is_instant_reservation(reservation):
        return await _send_instant_control(
            "server.rcon", reservation, db, command=command
        )
    cloud = await _get_cloud_instance(reservation, db)
    if cloud is None:
        return False
    from app.routers.internal import send_rcon_command
    return await send_rcon_command(cloud.instance_id, command)


async def awaited_rcon_reservation_runtime(
    reservation: Reservation,
    db: AsyncSession,
    command: str,
    *,
    timeout: float = 12.0,
) -> dict:
    """Run RCON and return only the correlated agent result."""
    if is_instant_reservation(reservation):
        assignment = await get_open_assignment(reservation.id, db)
        if assignment is None:
            from app.routers.internal import RconRequestUnavailable
            raise RconRequestUnavailable("Instant reservation has no open assignment")
        from app.routers.internal import send_correlated_instant_rcon
        command_id = str(uuid.uuid4())
        assignment.last_command_id = command_id
        await db.commit()
        envelope = _command_envelope(
            "server.rcon",
            reservation,
            assignment,
            command_id=command_id,
            command=command,
        )
        return await send_correlated_instant_rcon(
            assignment.slot.host_id, envelope, timeout=timeout
        )

    cloud = await _get_cloud_instance(reservation, db)
    if cloud is None:
        from app.routers.internal import RconRequestUnavailable
        raise RconRequestUnavailable("Cloud reservation has no instance")
    from app.routers.internal import send_correlated_rcon_command
    return await send_correlated_rcon_command(
        cloud.instance_id, command, timeout=timeout
    )


async def configure_uploads_runtime(
    reservation: Reservation,
    db: AsyncSession,
    *,
    logs_tf: bool,
    demos_tf: bool,
) -> bool:
    if is_instant_reservation(reservation):
        return await _send_instant_control(
            "server.uploads.configure", reservation, db,
            logs_tf=logs_tf, demos_tf=demos_tf,
        )
    cloud = await _get_cloud_instance(reservation, db)
    if cloud is None:
        return False
    from app.routers.internal import send_upload_settings
    return await send_upload_settings(
        cloud.instance_id, logs_tf=logs_tf, demos_tf=demos_tf
    )


async def runtime_agent_key(
    reservation: Reservation, db: AsyncSession
) -> tuple[str, str | int] | None:
    if is_instant_reservation(reservation):
        assignment = await get_open_assignment(reservation.id, db)
        return ("instant", assignment.slot.host_id) if assignment else None
    cloud = await _get_cloud_instance(reservation, db)
    return ("cloud", cloud.instance_id) if cloud else None


async def get_runtime_stats(reservation: Reservation, db: AsyncSession) -> dict | None:
    key = await runtime_agent_key(reservation, db)
    if key is None:
        return None
    if key[0] == "cloud":
        from app.routers.internal import get_agent_stats
        return get_agent_stats(str(key[1]))

    assignment = await get_open_assignment(reservation.id, db)
    if assignment is None:
        return None
    from app.routers.internal import get_instant_container_stats
    return {
        "runtime_kind": "instant",
        "host": assignment.slot.host.system_stats,
        "container": get_instant_container_stats(assignment.id),
        "host_shared": True,
    }


async def get_runtime_competitive_configs(
    reservation: Reservation, db: AsyncSession
) -> dict | None:
    key = await runtime_agent_key(reservation, db)
    if key is None:
        return None
    from app.routers.internal import get_competitive_configs
    if key[0] == "cloud":
        return get_competitive_configs(str(key[1]))
    return get_competitive_configs(f"instant:{key[1]}")

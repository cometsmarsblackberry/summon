"""Enrollment and lifecycle helpers for operator-owned Instant hosts."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.instance import EnabledLocation
from app.models.instant import InstantAssignment, InstantHost, InstantSlot


ENROLLMENT_TTL = timedelta(minutes=15)
DEFAULT_BASE_PORT = 27015
DEFAULT_PORT_STRIDE = 10
DEFAULT_TV_OFFSET = 5
MAX_SLOTS = 64


class InstantHostError(ValueError):
    """Safe validation error for Instant host operations."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hash_host_secret(secret: str) -> str:
    """Hash a high-entropy agent secret with a deployment-specific key."""
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_host_secret(secret: str, expected_hash: str | None) -> bool:
    if not secret or not expected_hash:
        return False
    return hmac.compare_digest(hash_host_secret(secret), expected_hash)


def generate_slot_ports(
    slot_count: int,
    base_port: int = DEFAULT_BASE_PORT,
    *,
    stride: int = DEFAULT_PORT_STRIDE,
    tv_offset: int = DEFAULT_TV_OFFSET,
) -> list[tuple[int, int, int]]:
    """Generate and validate stable ``(index, game, SourceTV)`` tuples."""
    if not 1 <= slot_count <= MAX_SLOTS:
        raise InstantHostError(f"slot_count must be between 1 and {MAX_SLOTS}")
    if not 1024 <= base_port <= 65535:
        raise InstantHostError("base_port must be between 1024 and 65535")
    if stride <= 0 or tv_offset <= 0:
        raise InstantHostError("port stride and SourceTV offset must be positive")

    ports: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for slot_index in range(slot_count):
        game_port = base_port + slot_index * stride
        tv_port = game_port + tv_offset
        if game_port > 65535 or tv_port > 65535:
            raise InstantHostError("generated port range exceeds 65535")
        if game_port in seen or tv_port in seen:
            raise InstantHostError("generated game and SourceTV ports overlap")
        seen.update((game_port, tv_port))
        ports.append((slot_index, game_port, tv_port))
    return ports


def validate_public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise InstantHostError("public_ipv4 must be a valid IPv4 address") from exc
    if address.version != 4:
        raise InstantHostError("Instant hosts require IPv4")
    if not address.is_global:
        raise InstantHostError("public_ipv4 must be a globally routable static address")
    return str(address)


def validate_image_reference(value: str) -> str:
    """Return a safe, bounded container-image reference."""
    image = value.strip()
    if not image or len(image) > 255 or any(character.isspace() for character in image):
        raise InstantHostError("container image must be a non-empty reference without whitespace")
    return image


def new_enrollment_token(host: InstantHost) -> str:
    token = secrets.token_urlsafe(32)
    host.enrollment_token_hash = hash_host_secret(token)
    host.enrollment_expires_at = utcnow() + ENROLLMENT_TTL
    host.enrollment_used_at = None
    return token


def new_stable_credential(host: InstantHost) -> str:
    credential = secrets.token_urlsafe(48)
    host.credential_hash = hash_host_secret(credential)
    host.credential_rotated_at = utcnow()
    return credential


async def create_host(
    db: AsyncSession,
    *,
    name: str,
    location: str,
    public_ipv4: str,
    slot_count: int,
    base_port: int = DEFAULT_BASE_PORT,
    desired_image: str | None = None,
) -> tuple[InstantHost, str]:
    """Create a disabled host, its stable slots, and a copy-once token."""
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 64:
        raise InstantHostError("name must be between 1 and 64 characters")
    address = validate_public_ipv4(public_ipv4)
    ports = generate_slot_ports(slot_count, base_port)

    location_record = await db.get(EnabledLocation, location)
    if location_record is None:
        raise InstantHostError("location does not exist")

    duplicate = await db.execute(
        select(InstantHost.id).where(
            InstantHost.public_ipv4 == address,
            InstantHost.deleted_at.is_(None),
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise InstantHostError("an Instant host already uses this IPv4 address")

    host = InstantHost(
        name=clean_name,
        location=location,
        public_ipv4=address,
        enabled=False,
        draining=False,
        health_status="unenrolled",
        desired_image=(
            validate_image_reference(desired_image) if desired_image else None
        ),
    )
    token = new_enrollment_token(host)
    db.add(host)
    await db.flush()
    for slot_index, game_port, tv_port in ports:
        db.add(InstantSlot(
            host_id=host.id,
            slot_index=slot_index,
            game_port=game_port,
            tv_port=tv_port,
            enabled=True,
        ))
    await db.commit()
    await db.refresh(host)
    return host, token


async def renew_enrollment(db: AsyncSession, host: InstantHost) -> str:
    if host.deleted_at is not None:
        raise InstantHostError("deleted hosts cannot be enrolled")
    host.enabled = False
    host.health_status = "unenrolled"
    host.credential_hash = None
    token = new_enrollment_token(host)
    await db.commit()
    return token


async def exchange_enrollment_token(
    db: AsyncSession, token: str
) -> tuple[InstantHost, str]:
    token_hash = hash_host_secret(token)
    now = utcnow()
    credential = secrets.token_urlsafe(48)
    credential_hash = hash_host_secret(credential)
    # Consume and clear the one-use token in the same conditional UPDATE. This
    # prevents two simultaneous installers from both receiving credentials;
    # only the transaction that changes one row wins.
    result = await db.execute(
        update(InstantHost)
        .where(
            InstantHost.enrollment_token_hash == token_hash,
            InstantHost.deleted_at.is_(None),
            InstantHost.enrollment_used_at.is_(None),
            InstantHost.enrollment_expires_at.is_not(None),
            InstantHost.enrollment_expires_at > now,
        )
        .values(
            credential_hash=credential_hash,
            credential_rotated_at=now,
            enrollment_used_at=now,
            enrollment_token_hash=None,
            enrollment_expires_at=None,
            health_status="offline",
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise InstantHostError("enrollment token is invalid, expired, or already used")
    await db.commit()
    db.expire_all()
    host_result = await db.execute(
        select(InstantHost).where(InstantHost.credential_hash == credential_hash)
    )
    host = host_result.scalar_one()
    return host, credential


async def host_has_open_assignments(db: AsyncSession, host_id: int) -> bool:
    result = await db.execute(
        select(func.count(InstantAssignment.id))
        .join(InstantSlot, InstantAssignment.slot_id == InstantSlot.id)
        .where(InstantSlot.host_id == host_id, InstantAssignment.closed_at.is_(None))
    )
    return bool(result.scalar_one())


async def configure_slots(
    db: AsyncSession,
    host: InstantHost,
    *,
    slot_count: int,
    base_port: int,
) -> None:
    """Change capacity only while the host is drained and idle."""
    if not host.draining:
        raise InstantHostError("host must be drained before changing capacity")
    if await host_has_open_assignments(db, host.id):
        raise InstantHostError("host must be idle before changing capacity")
    ports = generate_slot_ports(slot_count, base_port)

    result = await db.execute(
        select(InstantSlot)
        .where(InstantSlot.host_id == host.id)
        .order_by(InstantSlot.slot_index)
    )
    existing = {slot.slot_index: slot for slot in result.scalars().all()}

    # A valid base-port shift can temporarily collide with another row's old
    # unique port (for example 27015/27025 -> 27025/27035). Park every existing
    # row on unused ports and flush inside the same transaction before applying
    # the final range. Retired rows stay parked and disabled, preserving their
    # identities and assignment relationships without reserving live ports.
    reserved_ports = {
        port
        for slot in existing.values()
        for port in (slot.game_port, slot.tv_port)
    }
    reserved_ports.update(
        port for _, game_port, tv_port in ports for port in (game_port, tv_port)
    )
    parking_ports: list[int] = []
    candidate = 65535
    while len(parking_ports) < len(existing) * 2 and candidate >= 1024:
        if candidate not in reserved_ports:
            parking_ports.append(candidate)
        candidate -= 1
    if len(parking_ports) < len(existing) * 2:
        raise InstantHostError("could not reserve temporary ports for capacity change")
    for position, slot in enumerate(existing.values()):
        slot.game_port = parking_ports[position * 2]
        slot.tv_port = parking_ports[position * 2 + 1]
    if existing:
        await db.flush()

    for slot_index, game_port, tv_port in ports:
        slot = existing.get(slot_index)
        if slot is None:
            slot = InstantSlot(host_id=host.id, slot_index=slot_index)
            db.add(slot)
        slot.game_port = game_port
        slot.tv_port = tv_port
        slot.enabled = True
        slot.error_code = None
        slot.error_message = None
        slot.quarantined_at = None

    for slot_index, slot in existing.items():
        if slot_index >= slot_count:
            # Preserve the row and assignment history; retired capacity is only
            # disabled, never deleted.
            slot.enabled = False
    await db.commit()


async def soft_delete_host(db: AsyncSession, host: InstantHost) -> None:
    if await host_has_open_assignments(db, host.id):
        raise InstantHostError("host still has active assignments")
    host.enabled = False
    host.draining = True
    host.deleted_at = utcnow()
    host.credential_hash = None
    host.enrollment_token_hash = None
    host.enrollment_expires_at = None
    host.health_status = "deleted"
    await db.commit()


def host_is_online(host: InstantHost, *, now: datetime | None = None) -> bool:
    heartbeat = _as_utc(host.last_heartbeat_at)
    if heartbeat is None:
        return False
    current = now or utcnow()
    return heartbeat >= current - timedelta(
        seconds=get_settings().instant_host_offline_seconds
    )


def protocol_is_compatible(host: InstantHost) -> bool:
    lower = host.protocol_min if host.protocol_min is not None else host.protocol_version
    upper = host.protocol_max if host.protocol_max is not None else host.protocol_version
    if lower is None or upper is None:
        return False
    configured = get_settings()
    return (
        lower <= configured.instant_protocol_max
        and upper >= configured.instant_protocol_min
    )


def serialize_host(
    host: InstantHost,
    *,
    slots: Iterable[InstantSlot] | None = None,
    open_by_slot: dict[int, InstantAssignment] | None = None,
) -> dict:
    """Serialize safe host metadata; credentials are intentionally impossible to export."""
    slot_rows = list(slots if slots is not None else host.slots)
    assignments = open_by_slot or {}
    return {
        "id": host.id,
        "name": host.name,
        "location": host.location,
        "public_ipv4": host.public_ipv4,
        "enabled": host.enabled,
        "draining": host.draining,
        "deleted": host.deleted_at is not None,
        "enrolled": host.enrolled,
        "connected": host_is_online(host),
        "health_status": host.health_status,
        "health_error": host.health_error,
        "preflight_report": host.preflight_report,
        "reconciliation_error": host.reconciliation_error,
        "agent_version": host.agent_version,
        "protocol_version": host.protocol_version,
        "protocol_min": host.protocol_min,
        "protocol_max": host.protocol_max,
        "protocol_compatible": protocol_is_compatible(host),
        "capabilities": host.capabilities,
        "platform": host.platform,
        "architecture": host.architecture,
        "last_heartbeat_at": host.last_heartbeat_at,
        "desired_image": host.desired_image,
        "ready_image_digest": host.ready_image_digest,
        "image_status": host.image_status,
        "image_error": host.image_error,
        "update_status": host.update_status,
        "update_error": host.update_error,
        "version_pin": host.version_pin,
        "system_stats": host.system_stats,
        "slots": [
            {
                "id": slot.id,
                "slot_index": slot.slot_index,
                "game_port": slot.game_port,
                "tv_port": slot.tv_port,
                "enabled": slot.enabled,
                "error_code": slot.error_code,
                "error_message": slot.error_message,
                "quarantined_at": slot.quarantined_at,
                "last_used_at": slot.last_used_at,
                **({
                    "assignment": {
                        "id": assignments[slot.id].id,
                        "reservation_id": assignments[slot.id].reservation_id,
                        "generation": assignments[slot.id].generation,
                        "state": assignments[slot.id].state,
                    }
                } if slot.id in assignments else {}),
            }
            for slot in slot_rows
        ],
    }


async def get_admin_counters(db: AsyncSession) -> dict:
    """Return durable/current Instant operations counters for administrators."""
    from app.models.reservation import Reservation, RuntimeKind

    configured = get_settings()
    cutoff = utcnow() - timedelta(seconds=configured.instant_host_offline_seconds)
    occupied = exists().where(
        InstantAssignment.slot_id == InstantSlot.id,
        InstantAssignment.closed_at.is_(None),
    )
    available_slots = int((await db.execute(
        select(func.count(InstantSlot.id))
        .join(InstantHost, InstantSlot.host_id == InstantHost.id)
        .where(
            InstantHost.enabled.is_(True),
            InstantHost.draining.is_(False),
            InstantHost.deleted_at.is_(None),
            InstantHost.credential_hash.is_not(None),
            InstantHost.last_heartbeat_at >= cutoff,
            InstantHost.protocol_min <= configured.instant_protocol_max,
            InstantHost.protocol_max >= configured.instant_protocol_min,
            or_(
                InstantHost.version_pin.is_(None),
                InstantHost.agent_version == InstantHost.version_pin,
            ),
            InstantHost.health_status.in_(("ready", "healthy", "online", "degraded")),
            InstantHost.ready_image_digest.is_not(None),
            InstantSlot.enabled.is_(True),
            InstantSlot.quarantined_at.is_(None),
            InstantSlot.error_code.is_(None),
            ~occupied,
        )
    )).scalar_one() or 0)
    from app.services.settings import get_instant_settings
    if not (await get_instant_settings(db))["enabled"]:
        available_slots = 0
    active_assignments = int((await db.execute(
        select(func.count(InstantAssignment.id)).where(
            InstantAssignment.closed_at.is_(None)
        )
    )).scalar_one() or 0)
    start_failures = int((await db.execute(
        select(func.count(InstantAssignment.id)).where(
            InstantAssignment.failed_at.is_not(None)
        )
    )).scalar_one() or 0)
    fallback_reservations = int((await db.execute(
        select(func.count(func.distinct(InstantAssignment.reservation_id)))
        .join(Reservation, Reservation.id == InstantAssignment.reservation_id)
        .where(
            InstantAssignment.failed_at.is_not(None),
            Reservation.runtime_kind == RuntimeKind.CLOUD,
        )
    )).scalar_one() or 0)
    offline_hosts = int((await db.execute(
        select(func.count(InstantHost.id)).where(
            InstantHost.deleted_at.is_(None),
            InstantHost.credential_hash.is_not(None),
            or_(
                InstantHost.last_heartbeat_at.is_(None),
                InstantHost.last_heartbeat_at < cutoff,
            ),
        )
    )).scalar_one() or 0)
    reconciliation_conflicts = int((await db.execute(
        select(func.count(InstantHost.id)).where(
            InstantHost.deleted_at.is_(None),
            InstantHost.reconciliation_error.is_not(None),
        )
    )).scalar_one() or 0)

    ready_rows = (await db.execute(
        select(InstantAssignment.claimed_at, InstantAssignment.ready_at).where(
            InstantAssignment.ready_at.is_not(None)
        )
    )).all()
    start_latencies = [
        max(0.0, (ready - claimed).total_seconds())
        for claimed, ready in ready_rows
        if claimed is not None and ready is not None
    ]
    version_rows = (await db.execute(
        select(InstantHost.agent_version, func.count(InstantHost.id))
        .where(InstantHost.deleted_at.is_(None))
        .group_by(InstantHost.agent_version)
    )).all()
    image_rows = (await db.execute(
        select(InstantHost.ready_image_digest, func.count(InstantHost.id))
        .where(
            InstantHost.deleted_at.is_(None),
            InstantHost.ready_image_digest.is_not(None),
        )
        .group_by(InstantHost.ready_image_digest)
    )).all()
    return {
        "available_slots": available_slots,
        "active_assignments": active_assignments,
        "starts_succeeded": len(start_latencies),
        "start_failures": start_failures,
        "fallback_reservations": fallback_reservations,
        "average_start_latency_seconds": (
            round(sum(start_latencies) / len(start_latencies), 1)
            if start_latencies else None
        ),
        "offline_hosts": offline_hosts,
        "reconciliation_conflicts": reconciliation_conflicts,
        "agent_versions": {
            version or "unknown": int(count) for version, count in version_rows
        },
        "ready_images": {digest: int(count) for digest, count in image_rows},
    }

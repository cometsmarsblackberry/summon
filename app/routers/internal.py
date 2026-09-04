"""Internal endpoints for agent communication."""

import asyncio
import hmac
import logging
import uuid
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import async_session_maker
from app.models.reservation import Reservation, ReservationStatus
from app.models.instance import CloudInstance
from app.models.instant import InstantAssignment, InstantHost, InstantSlot
from app.utils.upload_links import is_allowed_upload_url


router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)
settings = get_settings()

# Track connected agents (instance_id -> WebSocket)
connected_agents: dict[str, WebSocket] = {}

# Track the current effective instance_id for each WebSocket
# This allows us to reassign an agent to a new instance_id during warm pool reuse
# Key is the WebSocket object id, value is the current instance_id
agent_instance_ids: dict[int, str] = {}

# Track boot progress for SSE broadcasting (instance_id -> progress data)
boot_progress: dict[str, dict] = {}

# Track player data from SourceMod plugin (reservation_number -> player data)
player_data: dict[int, dict] = {}

# Track agent system stats (instance_id -> sysinfo data)
agent_stats: dict[str, dict] = {}

# Track competitive config lists reported by agents (instance_id -> data)
competitive_configs: dict[str, dict] = {}

# Persistent-host connections and per-assignment observations. Host-wide and
# container stats stay separate so shared load is never presented as a single
# reservation's resource usage.
connected_instant_hosts: dict[int, WebSocket] = {}
instant_host_send_locks: dict[int, asyncio.Lock] = {}
instant_boot_progress: dict[int, dict] = {}
instant_container_stats: dict[int, dict] = {}
instant_fallback_tasks: set[asyncio.Task] = set()

# Correlated RCON requests are used only by the reservation command console.
# Existing operational RCON controls remain fire-and-forget.  Keys include the
# authenticated route and lease identity so stale or mismatched results cannot
# resolve a newer request.
pending_cloud_rcon: dict[tuple[str, str], asyncio.Future] = {}
pending_instant_rcon: dict[
    tuple[int, str, int, int, int, int], asyncio.Future
] = {}


class RconRequestUnavailable(RuntimeError):
    """The selected agent cannot accept a correlated RCON request."""


class RconRequestTimeout(TimeoutError):
    """A correlated RCON request did not complete before its deadline."""


def _sdr_ports(
    reported_game_port: int | str | None, fallback_game_port: int
) -> tuple[int, int]:
    """Return the real SDR game and SourceTV ports.

    TF2 repeats the game FakeIP port for SourceTV in status output, but SDR
    exposes SourceTV on the following port. Ignore the reported SourceTV port
    so this also corrects readiness messages from older agents.
    """
    game_port = int(reported_game_port or fallback_game_port)
    return game_port, game_port + 1


def _fail_cloud_rcon_for_instance(instance_id: str, message: str) -> None:
    for key, future in list(pending_cloud_rcon.items()):
        if key[0] == instance_id:
            pending_cloud_rcon.pop(key, None)
            if not future.done():
                future.set_exception(RconRequestUnavailable(message))


def _fail_instant_rcon_for_host(host_id: int, message: str) -> None:
    for key, future in list(pending_instant_rcon.items()):
        if key[0] == host_id:
            pending_instant_rcon.pop(key, None)
            if not future.done():
                future.set_exception(RconRequestUnavailable(message))


def _extract_agent_token(websocket: WebSocket) -> str | None:
    """Extract an agent auth token from the WebSocket handshake.

    Prefer Authorization headers so tokens never appear in URLs or routine
    proxy logs. A query-string token remains available only as an explicit
    compatibility fallback during rolling upgrades.
    """
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    if settings.allow_legacy_agent_query_token:
        token = websocket.query_params.get("token")
        if token:
            logger.warning(
                "Agent %s authenticated with deprecated query-string token transport",
                websocket.path_params.get("instance_id", "unknown"),
            )
            return token

    return None


def get_boot_progress(instance_id: str) -> Optional[dict]:
    """Get current boot progress for an instance."""
    return boot_progress.get(instance_id)


def get_agent_stats(instance_id: str) -> Optional[dict]:
    """Get current system stats for an instance."""
    return agent_stats.get(instance_id)

def get_competitive_configs(instance_id: str) -> Optional[dict]:
    """Get the last reported competitive configs for an instance."""
    return competitive_configs.get(instance_id)


def get_instant_boot_progress(assignment_id: int) -> Optional[dict]:
    return instant_boot_progress.get(assignment_id)


def get_instant_container_stats(assignment_id: int) -> Optional[dict]:
    return instant_container_stats.get(assignment_id)


async def send_instant_command(host_id: int, message: dict) -> bool:
    """Send a versioned command to a connected persistent host agent."""
    websocket = connected_instant_hosts.get(host_id)
    if websocket is None:
        return False
    return await _send_instant_message(host_id, websocket, message)


async def _send_instant_message(
    host_id: int, websocket: WebSocket, message: dict
) -> bool:
    """Serialize writes to one host while allowing different hosts in parallel."""
    lock = instant_host_send_locks.setdefault(host_id, asyncio.Lock())
    try:
        async with lock:
            if connected_instant_hosts.get(host_id) is not websocket:
                return False
            await websocket.send_json(message)
        return True
    except Exception as exc:
        logger.warning("Failed to send command to Instant host %s: %s", host_id, exc)
        if connected_instant_hosts.get(host_id) is websocket:
            connected_instant_hosts.pop(host_id, None)
            _fail_instant_rcon_for_host(
                host_id, "Instant host disconnected during RCON request"
            )
        return False


def get_player_data(reservation_number: int) -> Optional[dict]:
    """Get current player data for a reservation."""
    return player_data.get(reservation_number)


def clear_player_data(reservation_number: int) -> None:
    """Clear player data when a reservation ends."""
    player_data.pop(reservation_number, None)


def reassign_agent_instance_id(old_instance_id: str, new_instance_id: str) -> bool:
    """Reassign an agent's effective instance_id for warm pool reuse.
    
    This updates the tracking so that boot progress messages from the agent
    are stored under the new instance_id.
    
    Args:
        old_instance_id: The current instance_id the agent is registered under
        new_instance_id: The new instance_id to assign
        
    Returns:
        True if successful, False if agent not found
    """
    websocket = connected_agents.get(old_instance_id)
    if not websocket:
        return False

    _fail_cloud_rcon_for_instance(
        old_instance_id, "Cloud agent was reassigned during RCON request"
    )
    
    # Move the WebSocket to the new instance_id in connected_agents
    del connected_agents[old_instance_id]
    connected_agents[new_instance_id] = websocket
    
    # Update the effective instance_id mapping
    ws_id = id(websocket)
    agent_instance_ids[ws_id] = new_instance_id
    
    # Migrate any existing boot_progress (shouldn't have any for new reservation, but just in case)
    if old_instance_id in boot_progress:
        boot_progress[new_instance_id] = boot_progress.pop(old_instance_id)

    # Migrate competitive configs so they're available under the new instance_id
    if old_instance_id in competitive_configs:
        competitive_configs[new_instance_id] = competitive_configs.pop(old_instance_id)

    logger.info(f"Reassigned agent: {old_instance_id} -> {new_instance_id}")
    return True


@router.websocket("/ws/agent/{instance_id}")
async def agent_websocket(
    websocket: WebSocket,
    instance_id: str,
):
    """WebSocket endpoint for instance agents."""
    token = _extract_agent_token(websocket)

    # Validate token
    if not token:
        await websocket.close(code=4001, reason="Token required")
        return
    
    # Verify token matches instance
    async with async_session_maker() as db:
        result = await db.execute(
            select(CloudInstance).where(CloudInstance.instance_id == instance_id)
        )
        instance = result.scalar_one_or_none()
        
        if not instance or not hmac.compare_digest(instance.auth_token, token):
            await websocket.close(code=4003, reason="Invalid token")
            return
    
    await websocket.accept()
    connected_agents[instance_id] = websocket
    ws_id = id(websocket)
    agent_instance_ids[ws_id] = instance_id  # Track effective instance_id
    logger.info(f"Agent connected for instance {instance_id}")

    # Send reservation config to agent on connect (secrets delivered via
    # authenticated WebSocket instead of being baked into cloud user_data)
    try:
        await _send_initial_config(instance_id, instance, websocket)
    except Exception as e:
        logger.error(f"Failed to send initial config to agent {instance_id}: {e}")

    try:
        while True:
            data = await websocket.receive_json()
            # Use the current effective instance_id (may have been reassigned)
            effective_id = agent_instance_ids.get(ws_id, instance_id)
            await handle_agent_message(effective_id, data)
                
    except WebSocketDisconnect:
        effective_id = agent_instance_ids.get(ws_id, instance_id)
        logger.info(f"Agent disconnected for instance {effective_id}")
    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")
    finally:
        # Clean up using effective instance_id
        effective_id = agent_instance_ids.pop(ws_id, instance_id)
        if connected_agents.get(effective_id) is websocket:
            connected_agents.pop(effective_id, None)
            _fail_cloud_rcon_for_instance(
                effective_id, "Cloud agent disconnected during RCON request"
            )
        boot_progress.pop(effective_id, None)
        agent_stats.pop(effective_id, None)


def _host_token(websocket: WebSocket) -> str | None:
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip() or None
    return None


async def _send_host_configuration(host: InstantHost, websocket: WebSocket) -> None:
    from app.services.settings import get_instant_settings

    async with async_session_maker() as db:
        instant_settings = await get_instant_settings(db)
        slots = list((await db.execute(
            select(InstantSlot)
            .where(InstantSlot.host_id == host.id, InstantSlot.enabled.is_(True))
            .order_by(InstantSlot.slot_index)
        )).scalars().all())
    desired_image = host.desired_image or instant_settings["container_image"]
    sent = await _send_instant_message(host.id, websocket, {
        "type": "host.configure",
        "protocol": 1,
        "host_id": host.id,
        "heartbeat_interval_seconds": 10,
        "desired_image": desired_image,
        "force_image_prepare": host.image_status == "preparing",
        "version_pin": host.version_pin,
        "slots": [
            {
                "slot_id": slot.id,
                "slot_index": slot.slot_index,
                "game_port": slot.game_port,
                "tv_port": slot.tv_port,
            }
            for slot in slots
        ],
        "agent_manifest_url": (
            f"{settings.base_url}/internal/instant-hosts/{host.id}/agent-manifest"
        ),
        "update_check_interval_seconds": 900,
    })
    if not sent:
        raise RuntimeError("Instant host disconnected before configuration was sent")


async def refresh_instant_host_configuration(host: InstantHost) -> bool:
    """Push current slots/image/version policy to a connected host."""
    websocket = connected_instant_hosts.get(host.id)
    if websocket is None:
        return False
    try:
        await _send_host_configuration(host, websocket)
        return True
    except Exception:
        logger.exception("Failed to refresh configuration for Instant host %s", host.id)
        if connected_instant_hosts.get(host.id) is websocket:
            connected_instant_hosts.pop(host.id, None)
        return False


@router.websocket("/ws/instant-host/{host_id}")
async def instant_host_websocket(websocket: WebSocket, host_id: int):
    """Authenticated multi-slot protocol endpoint for persistent hosts."""
    token = _host_token(websocket)
    if not token:
        await websocket.close(code=4001, reason="Credential required")
        return

    from app.services.instant_hosts import verify_host_secret
    async with async_session_maker() as db:
        host = await db.get(InstantHost, host_id)
        if (
            host is None
            or host.deleted_at is not None
            or not verify_host_secret(token, host.credential_hash)
        ):
            await websocket.close(code=4003, reason="Invalid credential")
            return
        host.last_heartbeat_at = datetime.now(timezone.utc)
        # A reconnect must not inherit a previously compatible protocol or
        # preflight result. Keep it out of scheduling until this connection's
        # host.hello inventory has been validated below.
        host.health_status = "connecting"
        await db.commit()

    await websocket.accept()
    previous = connected_instant_hosts.get(host_id)
    connected_instant_hosts[host_id] = websocket
    if previous is not None and previous is not websocket:
        # Replacing an old in-memory route does not issue any host command.
        try:
            await previous.close(code=4000, reason="Superseded connection")
        except Exception:
            pass
    logger.info("Instant host %s connected", host_id)
    await _send_host_configuration(host, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            await handle_instant_host_message(host_id, data, websocket)
    except WebSocketDisconnect:
        logger.info("Instant host %s disconnected", host_id)
    except Exception:
        logger.exception("Instant host %s WebSocket error", host_id)
    finally:
        if connected_instant_hosts.get(host_id) is websocket:
            connected_instant_hosts.pop(host_id, None)
            _fail_instant_rcon_for_host(
                host_id, "Instant host disconnected during RCON request"
            )


def _parse_protocol_range(data: dict) -> tuple[int | None, int | None, int | None]:
    value = data.get("protocol_version")
    lower = data.get("protocol_min", value)
    upper = data.get("protocol_max", value)
    try:
        version = int(value) if value is not None else None
        minimum = int(lower) if lower is not None else None
        maximum = int(upper) if upper is not None else None
    except (TypeError, ValueError):
        return None, None, None
    return version, minimum, maximum


async def handle_instant_host_message(
    host_id: int, data: dict, websocket: WebSocket | None = None
) -> None:
    """Validate and apply one host protocol event."""
    message_type = str(data.get("type") or "")
    if message_type in {"host.hello", "host.status"}:
        await _handle_host_status(host_id, data, hello=message_type == "host.hello")
        return
    if message_type in {
        "host.image", "host.update", "host.competitive_configs",
        "server.progress", "server.ready", "server.stopped", "server.failed",
        "server.rcon.result",
    }:
        try:
            event_protocol = int(data.get("protocol"))
        except (TypeError, ValueError):
            event_protocol = -1
        if not settings.instant_protocol_min <= event_protocol <= settings.instant_protocol_max:
            logger.warning(
                "Rejected incompatible %s event from Instant host %s",
                message_type, host_id,
            )
            return
    if message_type == "host.image":
        await _handle_host_image(host_id, data)
        return
    if message_type == "host.update":
        await _handle_host_update(host_id, data)
        return
    if message_type == "host.competitive_configs":
        configs = data.get("configs")
        if isinstance(configs, list):
            from app.services.competitive_configs import filter_user_selectable
            cfg_files = [str(item)[:128] for item in configs[:500]]
            exec_cfg_files = sorted(set(
                filter_user_selectable(cfg_files) + ["summon_reset"]
            ))
            competitive_configs[f"instant:{host_id}"] = {
                "cfg_files": sorted(set(cfg_files)),
                "exec_cfg_files": exec_cfg_files,
                "container_image": str(data.get("container_image") or "")[:255],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        return
    if message_type not in {
        "server.progress", "server.ready", "server.stopped", "server.failed",
        "server.rcon.result",
    }:
        logger.warning("Unknown event %r from Instant host %s", message_type, host_id)
        return

    required = ("command_id", "reservation_id", "assignment_id", "slot_id", "generation")
    if any(data.get(field) is None for field in required):
        logger.warning("Incomplete %s event from Instant host %s", message_type, host_id)
        return

    try:
        assignment_id = int(data["assignment_id"])
        reservation_id = int(data["reservation_id"])
        slot_id = int(data["slot_id"])
        generation = int(data["generation"])
    except (TypeError, ValueError):
        logger.warning("Invalid identifiers in %s from Instant host %s", message_type, host_id)
        return

    async with async_session_maker() as db:
        result = await db.execute(
            select(InstantAssignment)
            .options(
                selectinload(InstantAssignment.slot).selectinload(InstantSlot.host),
                selectinload(InstantAssignment.reservation),
            )
            .where(InstantAssignment.id == assignment_id)
        )
        assignment = result.scalar_one_or_none()
        if (
            assignment is None
            or assignment.reservation_id != reservation_id
            or assignment.slot_id != slot_id
            or assignment.generation != generation
            or assignment.slot.host_id != host_id
        ):
            logger.warning(
                "Rejected stale/conflicting %s event from host %s assignment %s generation %s",
                message_type, host_id, assignment_id, generation,
            )
            return

        reservation = assignment.reservation
        if message_type == "server.progress":
            instant_boot_progress[assignment.id] = {
                "stage": data.get("stage"),
                "progress": int(data.get("progress") or 0),
                "message": str(data.get("message") or "")[:512],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            return
        if message_type == "server.rcon.result":
            key = (
                host_id,
                str(data.get("command_id")),
                reservation_id,
                assignment_id,
                slot_id,
                generation,
            )
            future = pending_instant_rcon.pop(key, None)
            if future is not None and not future.done():
                future.set_result({
                    "output": data.get("output"),
                    "error": data.get("error"),
                })
            logger.debug(
                "Instant assignment %s RCON result for command %s: %s",
                assignment.id, data.get("command_id"), data.get("output"),
            )
            return
        if message_type == "server.ready":
            await _handle_instant_ready(assignment, reservation, data, db)
            return
        if message_type == "server.stopped":
            await _handle_instant_stopped(assignment, data, db)
            return
        if message_type == "server.failed":
            failure_class = str(data.get("failure_class") or "infrastructure")
            failure_code = str(data.get("failure_code") or "start_failed")
            failure_message = str(data.get("message") or "Server start failed")
            # Only an initial start failure participates in bounded
            # Instant-to-Instant retry and cloud fallback. Control-operation or
            # restart failures must never duplicate an existing reservation on
            # another runtime.
            if assignment.state not in {"claimed", "starting"}:
                assignment.state = "degraded"
                assignment.failure_class = failure_class[:64]
                assignment.failure_code = failure_code[:64]
                assignment.failure_message = failure_message[:4000]
                assignment.slot.error_code = failure_code[:64]
                assignment.slot.error_message = failure_message[:4000]
                assignment.slot.quarantined_at = datetime.now(timezone.utc)
                if reservation.status == ReservationStatus.PROVISIONING:
                    reservation.status = ReservationStatus.FAILED
                    reservation.failure_reason = "The assigned server could not be restarted."
                await db.commit()
                return
            # Release this session before any cloud provider fallback can wait
            # on network I/O.
            from app.services.runtime import handle_instant_start_failure
            provision_cloud = await handle_instant_start_failure(
                assignment, reservation, db,
                failure_class=failure_class,
                failure_code=failure_code,
                failure_message=failure_message,
                defer_cloud=True,
            )
            if provision_cloud:
                from app.routers.reservations import provision_reservation_background
                task = asyncio.create_task(provision_reservation_background(
                    reservation.id, settings.database_url
                ))
                instant_fallback_tasks.add(task)
                task.add_done_callback(instant_fallback_tasks.discard)


async def _handle_host_status(host_id: int, data: dict, *, hello: bool) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        host = await db.get(InstantHost, host_id)
        if host is None or host.deleted_at is not None:
            return
        version, minimum, maximum = _parse_protocol_range(data)
        if data.get("agent_version") is not None:
            host.agent_version = str(data.get("agent_version") or "unknown")[:64]
        if version is not None:
            host.protocol_version = version
        if minimum is not None:
            host.protocol_min = minimum
        if maximum is not None:
            host.protocol_max = maximum
        if hello:
            capabilities = data.get("capabilities")
            host.capabilities = capabilities if isinstance(capabilities, (dict, list)) else []
            host.platform = str(data.get("platform") or "")[:64] or None
            host.architecture = str(data.get("architecture") or "")[:32] or None
        host.last_heartbeat_at = now
        if isinstance(data.get("sysinfo"), dict):
            host.system_stats = {**data["sysinfo"], "updated_at": now.isoformat()}
        if isinstance(data.get("preflight"), dict):
            host.preflight_report = data["preflight"]
        if data.get("health_error") is not None:
            host.health_error = str(data.get("health_error") or "")[:4000] or None

        image = data.get("image") if isinstance(data.get("image"), dict) else {}
        if image:
            if image.get("ready_digest"):
                host.ready_image_digest = str(image["ready_digest"])[:255]
            host.image_status = str(image.get("status") or host.image_status)[:32]
            host.image_error = str(image.get("error") or "")[:4000] or None

        effective_minimum = minimum if minimum is not None else host.protocol_min
        effective_maximum = maximum if maximum is not None else host.protocol_max
        compatible = (
            effective_minimum is not None and effective_maximum is not None
            and effective_minimum <= settings.instant_protocol_max
            and effective_maximum >= settings.instant_protocol_min
        )
        preflight_ok = data.get("preflight_ok") is True
        # A periodic status frame cannot substitute for the connection's
        # identity/capability handshake. This also prevents stale persisted
        # compatibility data from reopening capacity during reconnect.
        if host.health_status == "connecting" and not hello:
            pass
        elif not compatible:
            host.health_status = "incompatible"
            host.health_error = "Agent protocol is not compatible with this Summon release"
        elif not preflight_ok:
            host.health_status = "preflight_failed"
        elif host.image_status == "failed" and host.ready_image_digest:
            host.health_status = "degraded"
        elif host.image_status == "failed":
            host.health_status = "unavailable"
        elif host.ready_image_digest:
            host.health_status = "ready"
        else:
            host.health_status = "preparing"
        await db.commit()

        inventory = data.get("slots")
        if isinstance(inventory, list):
            await _reconcile_host_inventory(host, inventory, db)

    # A hello may report no image yet; configuration tells the agent what to
    # prepare, while the last-known-good digest remains usable after failures.
    socket = connected_instant_hosts.get(host_id)
    if hello and socket is not None:
        await _send_host_configuration(host, socket)


async def _handle_host_image(host_id: int, data: dict) -> None:
    async with async_session_maker() as db:
        host = await db.get(InstantHost, host_id)
        if host is None:
            return
        status_value = str(data.get("status") or "unknown")[:32]
        host.image_status = status_value
        if data.get("ready_digest"):
            host.ready_image_digest = str(data["ready_digest"])[:255]
        host.image_error = str(data.get("error") or "")[:4000] or None
        if status_value == "ready" and host.health_status != "preflight_failed":
            host.health_status = "ready"
        elif status_value == "failed":
            # Keep ready_image_digest as last-known-good.
            host.health_status = "degraded" if host.ready_image_digest else "unavailable"
        await db.commit()
        logger.info(
            "instant_event=image_status host_id=%s status=%s ready_digest=%s error=%s",
            host_id, status_value, host.ready_image_digest or "none",
            host.image_error or "none",
        )


async def _handle_host_update(host_id: int, data: dict) -> None:
    async with async_session_maker() as db:
        host = await db.get(InstantHost, host_id)
        if host is None:
            return
        host.update_status = str(data.get("status") or "unknown")[:32]
        host.update_error = str(data.get("error") or "")[:4000] or None
        if data.get("draining") is True:
            if not host.draining:
                host.update_auto_drained = True
            host.draining = True
        elif (
            data.get("draining") is False
            and host.update_auto_drained
            and host.update_status in {
                "ready", "current", "rolled_back", "failed", "retry_required",
            }
        ):
            host.draining = False
            host.update_auto_drained = False
        if data.get("agent_version"):
            host.agent_version = str(data["agent_version"])[:64]
        await db.commit()


async def _reconcile_host_inventory(
    host: InstantHost, inventory: list, db
) -> None:
    """Reconcile complete labeled Podman inventory with persisted leases."""
    assignment_result = await db.execute(
        select(InstantAssignment)
        .options(
            selectinload(InstantAssignment.slot).selectinload(InstantSlot.host),
            selectinload(InstantAssignment.reservation),
        )
        .join(InstantSlot, InstantAssignment.slot_id == InstantSlot.id)
        .where(InstantSlot.host_id == host.id, InstantAssignment.closed_at.is_(None))
    )
    open_assignments = {item.id: item for item in assignment_result.scalars().all()}
    seen: set[int] = set()
    conflicts: list[str] = []
    now = datetime.now(timezone.utc)

    for item in inventory:
        if not isinstance(item, dict):
            continue
        try:
            assignment_id = int(item.get("assignment_id"))
            slot_id = int(item.get("slot_id"))
            generation = int(item.get("generation"))
        except (TypeError, ValueError):
            conflicts.append("unlabeled managed container")
            continue
        assignment = open_assignments.get(assignment_id)
        if (
            assignment is None
            or assignment.slot_id != slot_id
            or assignment.generation != generation
        ):
            conflicts.append(f"container references stale assignment {assignment_id}")
            slot = await db.get(InstantSlot, slot_id)
            if slot and slot.host_id == host.id:
                slot.error_code = "reconciliation_conflict"
                slot.error_message = conflicts[-1]
                slot.quarantined_at = now
            continue
        if assignment_id in seen:
            conflicts.append(f"duplicate containers for assignment {assignment_id}")
            assignment.slot.error_code = "duplicate_container"
            assignment.slot.quarantined_at = now
            continue
        seen.add(assignment_id)
        if item.get("container_id"):
            assignment.container_id = str(item["container_id"])[:128]
        if isinstance(item.get("stats"), dict):
            instant_container_stats[assignment.id] = {
                **item["stats"], "updated_at": now.isoformat(),
            }
        inventory_ends_at = assignment.reservation.ends_at
        if inventory_ends_at.tzinfo is None:
            inventory_ends_at = inventory_ends_at.replace(tzinfo=timezone.utc)
        reservation_terminal = assignment.reservation.status in {
            ReservationStatus.ENDED,
            ReservationStatus.CANCELLED,
            ReservationStatus.NO_SHOW,
            ReservationStatus.FAILED,
        }
        if (
            inventory_ends_at <= now
            or reservation_terminal
            or assignment.state == "stopping"
        ):
            await send_instant_command(
                host.id,
                _instant_reconcile_command(
                    "server.stop",
                    assignment,
                    reason="expired" if inventory_ends_at <= now else "not_desired",
                ),
            )

    # A desired assignment with no labeled container is safe to reissue because
    # command IDs and generations are idempotency keys on the agent.
    for assignment_id, assignment in open_assignments.items():
        if assignment_id in seen:
            continue
        reservation = assignment.reservation
        ends_at = reservation.ends_at
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at <= now or reservation.status in {
            ReservationStatus.ENDED, ReservationStatus.CANCELLED,
            ReservationStatus.NO_SHOW, ReservationStatus.FAILED,
        }:
            await send_instant_command(
                host.id,
                _instant_reconcile_command("server.stop", assignment, reason="not_desired"),
            )
        elif assignment.state in {
            "claimed", "starting", "ready", "restarting", "degraded",
        }:
            from app.services.runtime import dispatch_instant_start
            await dispatch_instant_start(reservation, assignment, db)

    host.reconciliation_error = "; ".join(conflicts)[:4000] if conflicts else None
    if conflicts:
        # Complete inventory conflicts make slot ownership ambiguous. Keep the
        # host out of scheduling until a later clean inventory or admin action
        # resolves them; never silently allocate around an unknown container.
        host.health_status = "quarantined"
    await db.commit()
    if conflicts:
        logger.warning(
            "instant_event=reconciliation_conflict host_id=%s conflicts=%s",
            host.id, len(conflicts),
        )


def _instant_reconcile_command(
    command_type: str, assignment: InstantAssignment, **extra
) -> dict:
    return {
        "type": command_type,
        "protocol": 1,
        "command_id": str(uuid.uuid4()),
        "reservation_id": assignment.reservation_id,
        "assignment_id": assignment.id,
        "slot_id": assignment.slot_id,
        "slot_index": assignment.slot.slot_index,
        "generation": assignment.generation,
        **extra,
    }


async def _handle_instant_ready(
    assignment: InstantAssignment, reservation: Reservation, data: dict, db
) -> None:
    if assignment.closed_at is not None:
        # A start and a terminal lifecycle action may cross on the wire. Even
        # though the historical lease is already closed, a late readiness
        # frame proves that a container exists and must be stopped explicitly.
        await send_instant_command(
            assignment.slot.host_id,
            _instant_reconcile_command(
                "server.stop", assignment, reason="closed_assignment"
            ),
        )
        return
    now = datetime.now(timezone.utc)
    terminal = reservation.status in {
        ReservationStatus.ENDED,
        ReservationStatus.CANCELLED,
        ReservationStatus.NO_SHOW,
        ReservationStatus.FAILED,
    }
    if assignment.state == "stopping" or terminal:
        # Start and stop commands can cross on the wire. A late ready event must
        # never resurrect a reservation that the user/backend already ended.
        assignment.container_id = (
            str(data.get("container_id") or "")[:128] or assignment.container_id
        )
        assignment.state = "stopping"
        assignment.stop_requested_at = assignment.stop_requested_at or now
        command = _instant_reconcile_command(
            "server.stop", assignment, reason="not_desired"
        )
        assignment.last_command_id = command["command_id"]
        await db.commit()
        await send_instant_command(assignment.slot.host_id, command)
        return
    assignment.state = "ready"
    assignment.ready_at = assignment.ready_at or now
    assignment.container_id = str(data.get("container_id") or "")[:128] or assignment.container_id
    reservation.status = ReservationStatus.ACTIVE
    reservation.started_at = reservation.started_at or now
    reservation.empty_since = now
    reservation.direct_ip = assignment.slot.host.public_ipv4
    reservation.direct_port = assignment.slot.game_port
    reservation.direct_tv_port = assignment.slot.tv_port

    sdr_ip = data.get("sdr_ip")
    if isinstance(sdr_ip, str) and sdr_ip.startswith("169.254."):
        reservation.sdr_ip = sdr_ip
        reservation.sdr_port, reservation.sdr_tv_port = _sdr_ports(
            data.get("sdr_port"), assignment.slot.game_port
        )
    else:
        reservation.sdr_ip = assignment.slot.host.public_ipv4
        reservation.sdr_port = assignment.slot.game_port
        reservation.sdr_tv_port = assignment.slot.tv_port
    if data.get("map"):
        reservation.current_map = str(data["map"])[:64]
    await db.commit()
    from app.services.timer import schedule_expiry_timer
    schedule_expiry_timer(
        reservation.id, reservation.reservation_number, reservation.ends_at, None
    )
    claimed_at = assignment.claimed_at or now
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    logger.info(
        "instant_event=server_ready reservation=%s host_id=%s slot_index=%s start_latency_seconds=%.3f",
        reservation.reservation_number, assignment.slot.host_id, assignment.slot.slot_index,
        max(0.0, (now - claimed_at).total_seconds()),
    )


async def _handle_instant_stopped(
    assignment: InstantAssignment, data: dict, db
) -> None:
    if assignment.closed_at is not None:
        return
    now = datetime.now(timezone.utc)
    assignment.state = "stopped"
    assignment.stopped_at = now
    assignment.closed_at = now
    assignment.container_id = None
    assignment.slot.last_used_at = now
    instant_container_stats.pop(assignment.id, None)
    instant_boot_progress.pop(assignment.id, None)
    await db.commit()


class InstantEnrollmentRequest(BaseModel):
    token: str
    hostname: str | None = None


@router.post("/instant-hosts/enroll")
async def enroll_instant_host(payload: InstantEnrollmentRequest, request: Request):
    """Exchange one copy-once enrollment token for a stable agent credential."""
    from app.services.instant_hosts import InstantHostError, exchange_enrollment_token
    client_ipv4 = request.client.host if request.client else None
    async with async_session_maker() as db:
        try:
            host, credential = await exchange_enrollment_token(
                db,
                payload.token,
                public_ipv4=client_ipv4,
                hostname=payload.hostname,
            )
        except InstantHostError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from app.services.orchestrator import _agent_binary_sha256
        agent_digest = _agent_binary_sha256()
        return {
            "host_id": host.id,
            "credential": credential,
            "websocket_url": (
                settings.base_url.replace("https://", "wss://").replace("http://", "ws://")
                + f"/internal/ws/instant-host/{host.id}"
            ),
            "agent_url": (
                f"{settings.base_url}/static/tf2-agent?sha256={agent_digest}"
            ),
            "agent_sha256": agent_digest,
            "credential_file": "/var/lib/summon-agent/credential",
            "slots": [
                {
                    "slot_index": slot.slot_index,
                    "game_port": slot.game_port,
                    "tv_port": slot.tv_port,
                }
                for slot in (
                    await db.execute(
                        select(InstantSlot)
                        .where(InstantSlot.host_id == host.id, InstantSlot.enabled.is_(True))
                        .order_by(InstantSlot.slot_index)
                    )
                ).scalars().all()
            ],
        }


def _bearer_value(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[7:].strip()


@router.get("/instant-hosts/{host_id}/agent-manifest")
async def instant_agent_manifest(
    host_id: int,
    authorization: str | None = Header(None),
):
    """Return the authenticated update manifest for the deployed agent binary."""
    from app.services.instant_hosts import verify_host_secret
    from app.services.orchestrator import _agent_binary_sha256
    async with async_session_maker() as db:
        host = await db.get(InstantHost, host_id)
        if (
            host is None
            or host.deleted_at is not None
            or not verify_host_secret(_bearer_value(authorization), host.credential_hash)
        ):
            raise HTTPException(status_code=403, detail="Invalid host credential")
    digest = _agent_binary_sha256()
    return {
        "version": settings.agent_version,
        "protocol_min": settings.instant_protocol_min,
        "protocol_max": settings.instant_protocol_max,
        "download_url": f"{settings.base_url}/static/tf2-agent?sha256={digest}",
        "sha256": digest,
        "rollback_timeout_seconds": 90,
    }


async def _send_initial_config(instance_id: str, cloud_instance: CloudInstance, websocket):
    """Send reservation config to a newly connected agent.

    This delivers all sensitive credentials (passwords, API keys) via the
    authenticated WebSocket rather than embedding them in cloud user_data.
    """
    if not cloud_instance.current_reservation_id:
        logger.info(f"Agent {instance_id}: no current reservation, skipping config push")
        return

    async with async_session_maker() as db:
        result = await db.execute(
            select(Reservation).where(Reservation.id == cloud_instance.current_reservation_id)
        )
        reservation = result.scalar_one_or_none()
        if not reservation:
            logger.warning(f"Agent {instance_id}: reservation {cloud_instance.current_reservation_id} not found")
            return

        # Build the full config
        from app.models.user import User
        from app.models.instance import EnabledLocation, Provider
        from app.services.orchestrator import build_reservation_config
        from app.services.settings import get_fastdl_url

        user_result = await db.execute(select(User).where(User.id == reservation.user_id))
        owner = user_result.scalar_one_or_none()

        loc_result = await db.execute(
            select(EnabledLocation).where(EnabledLocation.code == reservation.location)
        )
        loc = loc_result.scalar_one_or_none()

        container_image = ""
        if loc:
            prov_result = await db.execute(select(Provider).where(Provider.code == loc.provider))
            prov = prov_result.scalar_one_or_none()
            if prov:
                container_image = prov.container_image

        fastdl_url = await get_fastdl_url(db)

        # Fetch admin Steam IDs (Steam2 format) for SM_ADMINS
        from app.utils.steam import steamid64_to_steamid2
        admin_result = await db.execute(
            select(User.steam_id).where(User.is_admin == True)
        )
        admin_steam_ids = [
            steamid64_to_steamid2(row[0]) for row in admin_result.all()
        ]

        config = build_reservation_config(
            reservation=reservation,
            owner_steam_id=owner.steam_id if owner else "",
            owner_name=owner.display_name if owner else "",
            location_city=loc.city if loc and loc.city else reservation.location,
            container_image=container_image,
            fastdl_url=fastdl_url,
            auth_token=cloud_instance.auth_token,
            instance_id=instance_id,
            admin_steam_ids=admin_steam_ids,
        )

    await websocket.send_json({
        "type": "container.initial_config",
        "config": config,
    })
    logger.info(f"Sent initial config to agent {instance_id} for reservation #{reservation.reservation_number}")


async def handle_agent_message(instance_id: str, data: dict):
    """Process a message from an agent."""
    message_type = data.get("type")
    
    if message_type == "status":
        logger.debug(f"Agent {instance_id} status: {data}")
        if "sysinfo" in data:
            agent_stats[instance_id] = {
                **data["sysinfo"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        
    elif message_type == "boot_progress":
        stage = data.get("stage")
        progress = data.get("progress", 0)
        message = data.get("message", "")
        
        logger.info(f"Agent {instance_id} boot: {stage} ({progress}%)")
        
        # Store for SSE broadcasting
        boot_progress[instance_id] = {
            "stage": stage,
            "progress": progress,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Handle special stages
        if stage == "server_ready":
            await handle_server_ready(instance_id, data)
        elif stage == "boot_failed":
            await handle_boot_failed(instance_id, data.get("message", "Unknown error"))
    
    elif message_type == "competitive_configs":
        raw = data.get("configs") or []
        if not isinstance(raw, list):
            logger.warning("Agent %s sent invalid competitive_configs payload", instance_id)
            return
        cfg_files = [c for c in raw if isinstance(c, str)]
        from app.services.competitive_configs import filter_user_selectable

        exec_cfg_files = filter_user_selectable(cfg_files)
        # Always allow reset regardless of filtering.
        exec_cfg_files = sorted(set(exec_cfg_files + ["summon_reset"]))

        competitive_configs[instance_id] = {
            "cfg_files": sorted(set(cfg_files)),
            "exec_cfg_files": exec_cfg_files,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "container_image": data.get("container_image"),
        }
        logger.info("Agent %s reported %d competitive configs", instance_id, len(cfg_files))
            
    elif message_type == "rcon_result":
        command_id = data.get("command_id")
        if isinstance(command_id, str) and command_id:
            future = pending_cloud_rcon.pop((instance_id, command_id), None)
            if future is not None and not future.done():
                future.set_result({
                    "output": data.get("output"),
                    "error": data.get("error"),
                })
        logger.debug(f"Agent {instance_id} RCON result: {data}")
        
    else:
        logger.warning(f"Unknown message type from agent: {message_type}")


async def handle_server_ready(instance_id: str, data: dict):
    """Handle server ready notification from agent.
    
    The data dict may contain:
    - Legacy format: ip, port, tv_port (backwards compatible)
    - New SDR format: real_ip, real_port, real_tv_port, sdr_ip, sdr_port, sdr_tv_port, map
    
    If SDR FakeIP is detected (169.254.x.x), we display that to users as the connect address.
    Otherwise, we fall back to the real IP.
    """
    # Extract addresses from data - support both legacy and new format
    real_ip = data.get("real_ip") or data.get("ip")
    real_port = data.get("real_port") or data.get("port", 27015)
    real_tv_port = data.get("real_tv_port") or data.get("tv_port", 27020)

    # Agent's getLocalIP() often returns a private/NAT'd IP or 0.0.0.0.
    # Treat those as unusable — we'll fetch the public IP from the cloud provider below.
    if not real_ip or real_ip == "0.0.0.0" or real_ip.startswith(("10.", "172.", "192.168.")):
        real_ip = None
    
    sdr_ip = data.get("sdr_ip")
    sdr_port = data.get("sdr_port")
    current_map = data.get("map")
    
    # Determine the connect address - prefer SDR FakeIP if available
    has_sdr = sdr_ip and sdr_ip.startswith("169.254.")
    
    if has_sdr:
        connect_ip = sdr_ip
        connect_port, connect_tv_port = _sdr_ports(sdr_port, 27015)
        logger.info(f"Server ready: {instance_id} with SDR FakeIP {connect_ip}:{connect_port} (real: {real_ip}:{real_port})")
    else:
        connect_ip = real_ip
        connect_port = real_port
        connect_tv_port = real_tv_port
        logger.info(f"Server ready: {instance_id} at {connect_ip}:{connect_port} (no SDR)")
    
    async with async_session_maker() as db:
        # Find reservation for this instance (match PROVISIONING or ENDED)
        result = await db.execute(
            select(Reservation)
            .join(CloudInstance)
            .where(CloudInstance.instance_id == instance_id)
            .where(Reservation.status.in_([ReservationStatus.PROVISIONING, ReservationStatus.ENDED]))
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            return

        if reservation.status == ReservationStatus.ENDED:
            # User cancelled during provisioning — warm pool or destroy based on billing model
            from app.services.orchestrator import release_to_warm_pool, destroy_instance, is_hourly_billing
            if await is_hourly_billing(reservation.location, db):
                logger.info(f"Cancelled reservation #{reservation.reservation_number} server ready — releasing to warm pool")
                await release_to_warm_pool(reservation.instance_id, db)
            else:
                logger.info(f"Cancelled reservation #{reservation.reservation_number} server ready — destroying (per-second billing)")
                await destroy_instance(reservation.instance_id, db)
            return

        # Normal PROVISIONING → ACTIVE transition
        reservation.status = ReservationStatus.ACTIVE
        if not reservation.started_at:
            reservation.started_at = datetime.now(timezone.utc)
        reservation.empty_since = datetime.now(timezone.utc)  # Auto-end timer starts when server is ready

        # Also update the CloudInstance status
        instance_result = await db.execute(
            select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
        )
        cloud_instance = instance_result.scalar_one_or_none()
        if cloud_instance:
            cloud_instance.status = "active"

            # Fetch public IP from cloud provider if agent didn't provide a usable one
            if not real_ip:
                try:
                    from app.models.instance import EnabledLocation
                    from app.services.cloud_provider import get_cloud_client
                    loc_result = await db.execute(
                        select(EnabledLocation).where(EnabledLocation.code == reservation.location)
                    )
                    loc = loc_result.scalar_one_or_none()
                    if loc:
                        client = get_cloud_client(loc.provider)
                        if client:
                            # Gcore benefits from a region hint; Vultr does not accept region_id.
                            if loc.provider == "gcore":
                                instance_data = await client.get_instance(
                                    cloud_instance.id, region_id=int(loc.provider_region)
                                )
                            else:
                                instance_data = await client.get_instance(cloud_instance.id)
                            if instance_data.main_ip and instance_data.main_ip != "0.0.0.0":
                                real_ip = instance_data.main_ip
                                logger.info(f"Fetched public IP from provider: {real_ip}")
                except Exception as e:
                    logger.warning(f"Failed to fetch public IP from provider: {e}")

            if real_ip:
                cloud_instance.ip_address = real_ip

        # Store the connect address (SDR FakeIP if available, otherwise real IP)
        reservation.sdr_ip = connect_ip
        reservation.sdr_port = connect_port
        reservation.sdr_tv_port = connect_tv_port
        if reservation.enable_direct_connect and real_ip:
            reservation.direct_ip = real_ip
            reservation.direct_port = real_port
            reservation.direct_tv_port = real_tv_port

        if current_map:
            reservation.current_map = current_map

        await db.commit()

        from app.services.timer import schedule_expiry_timer
        schedule_expiry_timer(reservation.id, reservation.reservation_number, reservation.ends_at, reservation.instance_id)

        logger.info(f"Reservation #{reservation.reservation_number} is now active at {connect_ip}:{connect_port}")


async def handle_boot_failed(instance_id: str, error_message: str):
    """Handle boot failure from agent — destroy instance and retry or fail."""
    logger.error(f"Boot failed for {instance_id}: {error_message}")

    async with async_session_maker() as db:
        # Find the reservation and instance (match PROVISIONING or ENDED)
        result = await db.execute(
            select(Reservation, CloudInstance)
            .join(CloudInstance, Reservation.instance_id == CloudInstance.id)
            .where(CloudInstance.instance_id == instance_id)
            .where(Reservation.status.in_([ReservationStatus.PROVISIONING, ReservationStatus.ENDED]))
        )
        row = result.first()

        if not row:
            return

        reservation, cloud_instance = row
        cloud_id = cloud_instance.id

        if reservation.status == ReservationStatus.ENDED:
            # Cancelled reservation — just destroy, no retry, no status change
            logger.info(f"Boot failed on cancelled reservation #{reservation.reservation_number}, destroying instance {cloud_id}")
            from app.services.orchestrator import destroy_instance
            await destroy_instance(cloud_id, db)
            return

        # PROVISIONING — check if we can retry
        if reservation.provision_attempts < settings.max_provision_attempts:
            # Destroy the failed instance and clear instance_id for retry
            logger.info(f"Boot failed for reservation #{reservation.reservation_number} "
                        f"(attempt {reservation.provision_attempts}/{settings.max_provision_attempts}), scheduling retry")
            try:
                from app.services.cloud_provider import get_cloud_client
                from app.models.instance import EnabledLocation
                loc_result = await db.execute(
                    select(EnabledLocation).where(EnabledLocation.code == reservation.location)
                )
                loc = loc_result.scalar_one_or_none()
                provider_code = loc.provider if loc else "vultr"
                client = get_cloud_client(provider_code)
                if client:
                    await client.destroy_instance(cloud_id, region=loc.provider_region if loc else None)
                    await db.delete(cloud_instance)
            except Exception as e:
                logger.error(f"Failed to destroy instance {cloud_id}: {e}")
                # Still try to clean up DB record
                await db.delete(cloud_instance)

            reservation.instance_id = None
            await db.commit()

            # Schedule retry after delay
            asyncio.create_task(retry_provision_after_boot_failure(reservation.id))
        else:
            # Max attempts exhausted
            from app.services.failure_messages import public_failure_reason

            logger.error(
                "Reservation #%s boot failed after %s attempts: %s",
                reservation.reservation_number,
                reservation.provision_attempts,
                error_message or "Unknown error",
            )
            reservation.status = ReservationStatus.FAILED
            reservation.failure_reason = public_failure_reason(
                reservation.status,
                reservation.provision_attempts,
            )
            await db.commit()
            logger.info(f"Reservation #{reservation.reservation_number} marked as failed (max attempts exhausted)")

            # Destroy the instance
            try:
                from app.services.cloud_provider import get_cloud_client
                from app.models.instance import EnabledLocation
                loc_result = await db.execute(
                    select(EnabledLocation).where(EnabledLocation.code == reservation.location)
                )
                loc = loc_result.scalar_one_or_none()
                provider_code = loc.provider if loc else "vultr"
                client = get_cloud_client(provider_code)
                if client:
                    await client.destroy_instance(cloud_id, region=loc.provider_region if loc else None)
                    await db.delete(cloud_instance)
                    await db.commit()
            except Exception as e:
                logger.error(f"Failed to destroy instance {cloud_id}: {e}")


async def retry_provision_after_boot_failure(reservation_id: int):
    """Retry provisioning after a boot failure, with a short delay."""
    await asyncio.sleep(5)
    from app.routers.reservations import provision_reservation_background
    await provision_reservation_background(reservation_id, settings.database_url)


async def send_to_agent(instance_id: str, message: dict) -> bool:
    """Send a message to a connected agent."""
    websocket = connected_agents.get(instance_id)
    if not websocket:
        return False
    
    try:
        await websocket.send_json(message)
        return True
    except Exception as e:
        logger.error(f"Failed to send to agent {instance_id}: {e}")
        if connected_agents.get(instance_id) is websocket:
            connected_agents.pop(instance_id, None)
            _fail_cloud_rcon_for_instance(
                instance_id, "Cloud agent disconnected during RCON request"
            )
        return False


async def send_container_stop(instance_id: str) -> bool:
    """Send container.stop command to agent."""
    return await send_to_agent(instance_id, {
        "type": "container.stop",
    })


async def send_container_restart(instance_id: str, updated_config: dict | None = None) -> bool:
    """Send container.restart command to agent."""
    msg = {"type": "container.restart"}
    if updated_config:
        msg["config"] = updated_config
    return await send_to_agent(instance_id, msg)


async def send_rcon_command(instance_id: str, command: str) -> bool:
    """Send RCON command to agent."""
    return await send_to_agent(instance_id, {
        "type": "rcon",
        "command": command,
    })


async def send_correlated_rcon_command(
    instance_id: str,
    command: str,
    *,
    timeout: float = 12.0,
) -> dict:
    """Send cloud RCON and await the matching result event."""
    command_id = str(uuid.uuid4())
    key = (instance_id, command_id)
    future = asyncio.get_running_loop().create_future()
    pending_cloud_rcon[key] = future
    try:
        sent = await send_to_agent(instance_id, {
            "type": "rcon",
            "command_id": command_id,
            "command": command,
        })
        if not sent:
            if future.done() and not future.cancelled():
                future.exception()
            raise RconRequestUnavailable("Cloud agent is not connected")
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RconRequestTimeout("Cloud RCON request timed out") from exc
    finally:
        pending_cloud_rcon.pop(key, None)
        if not future.done():
            future.cancel()


async def send_correlated_instant_rcon(
    host_id: int,
    message: dict,
    *,
    timeout: float = 12.0,
) -> dict:
    """Send assignment-scoped RCON and await its validated result event."""
    key = (
        host_id,
        str(message["command_id"]),
        int(message["reservation_id"]),
        int(message["assignment_id"]),
        int(message["slot_id"]),
        int(message["generation"]),
    )
    future = asyncio.get_running_loop().create_future()
    pending_instant_rcon[key] = future
    try:
        if not await send_instant_command(host_id, message):
            if future.done() and not future.cancelled():
                future.exception()
            raise RconRequestUnavailable("Instant host agent is not connected")
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RconRequestTimeout("Instant RCON request timed out") from exc
    finally:
        pending_instant_rcon.pop(key, None)
        if not future.done():
            future.cancel()


async def send_upload_settings(instance_id: str, *, logs_tf: bool, demos_tf: bool) -> bool:
    """Tell an agent to update external match upload settings."""
    return await send_to_agent(instance_id, {
        "type": "uploads.configure",
        "logs_tf": logs_tf,
        "demos_tf": demos_tf,
    })


async def send_reconfigure_command(instance_id: str, config: dict) -> bool:
    """Send reconfigure command to agent for warm pool reuse.
    
    This tells the agent to start a new container with the new reservation config.
    """
    return await send_to_agent(instance_id, {
        "type": "container.reconfigure",
        "config": config,
    })


async def get_connected_agent_by_cloud_id(cloud_id: str, db) -> Optional[str]:
    """Find the connected agent instance_id for a cloud provider instance UUID.

    This is used when reusing a warm pool instance - we need to find which
    agent is connected so we can send it a reconfigure command.

    Args:
        cloud_id: The cloud provider instance UUID (CloudInstance.id)
        db: Database session

    Returns:
        The instance_id of the connected agent, or None if not found
    """
    # Look up the CloudInstance to get its current instance_id
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == cloud_id)
    )
    instance = result.scalar_one_or_none()

    if not instance:
        return None

    # Check if this instance_id has a connected agent
    # Note: After an instance is released to warm pool, the agent may still
    # be connected with its original instance_id
    if instance.instance_id in connected_agents:
        return instance.instance_id

    return None


# ============================================================================
# Plugin HTTP Endpoints (called by SourceMod plugin)
# ============================================================================

def validate_internal_api_key(api_key: str) -> bool:
    """Validate the global internal API key from plugin (legacy fallback)."""
    if not settings.allow_legacy_internal_api_key or not settings.internal_api_key:
        return False
    return hmac.compare_digest(api_key, settings.internal_api_key)


async def validate_reservation_api_key(reservation_number: int, api_key: str) -> bool:
    """Validate the per-reservation plugin API key.

    A site-wide fallback is intentionally disabled by default because it turns a
    single leaked legacy key into fleet-wide access.
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(Reservation.plugin_api_key).where(
                Reservation.reservation_number == reservation_number
            )
        )
        row = result.first()
        if not row:
            return False

        if row[0]:
            return hmac.compare_digest(api_key, row[0])

    if settings.allow_legacy_internal_api_key:
        logger.warning(
            "Reservation #%s used deprecated global INTERNAL_API_KEY fallback",
            reservation_number,
        )
        return validate_internal_api_key(api_key)

    return False


from fastapi import Header, HTTPException
from pydantic import BaseModel


class EndResponse(BaseModel):
    """Response for end endpoint."""
    success: bool
    message: str


@router.post("/reservations/{reservation_number}/end", response_model=EndResponse)
async def end_reservation_from_plugin(
    reservation_number: int,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """
    End a reservation.
    Called by SourceMod plugin when owner uses !end.
    """
    if not await validate_reservation_api_key(reservation_number, x_api_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    async with async_session_maker() as db:
        result = await db.execute(
            select(Reservation).where(Reservation.reservation_number == reservation_number)
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")

        if reservation.status not in (ReservationStatus.ACTIVE, ReservationStatus.PROVISIONING):
            raise HTTPException(status_code=400, detail="Reservation cannot be ended")

        was_active = reservation.status == ReservationStatus.ACTIVE
        had_started = reservation.started_at is not None

        # Mark as ended
        reservation.status = ReservationStatus.ENDED
        reservation.ended_at = datetime.now(timezone.utc)
        await db.commit()

        from app.services.timer import cancel_expiry_timer
        cancel_expiry_timer(reservation.id)

        logger.info(f"Reservation #{reservation_number} marked as ENDED (from plugin)")

        # Clear in-memory player data
        clear_player_data(reservation_number)

        from app.services.runtime import end_reservation_runtime
        await end_reservation_runtime(
            reservation,
            db,
            was_active=was_active,
            had_started=had_started,
        )

        return EndResponse(
            success=True,
            message="Reservation ended"
        )


class UploadLinkRequest(BaseModel):
    """Request body for upload link from plugin."""
    type: str  # "log" or "demo"
    external_id: str
    url: str


class UploadLinkResponse(BaseModel):
    """Response for upload link endpoint."""
    success: bool


@router.post("/reservations/{reservation_number}/uploads", response_model=UploadLinkResponse)
async def report_upload_link(
    reservation_number: int,
    body: UploadLinkRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """
    Report a logs.tf or demos.tf upload for a reservation.
    Called by SourceMod plugin when a log or demo is uploaded.
    """
    if not await validate_reservation_api_key(reservation_number, x_api_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    if body.type not in ("log", "demo"):
        raise HTTPException(status_code=400, detail="Type must be 'log' or 'demo'")

    external_id = body.external_id.strip()
    url = body.url.strip()

    if not external_id or not url:
        raise HTTPException(status_code=400, detail="external_id and url are required")

    # Accept only canonical HTTPS upload links so attacker-controlled schemes
    # cannot be smuggled through the hostname check.
    if body.type == "log" and not is_allowed_upload_url(url, body.type):
        raise HTTPException(status_code=400, detail="Invalid logs.tf URL")
    if body.type == "demo" and not is_allowed_upload_url(url, body.type):
        raise HTTPException(status_code=400, detail="Invalid demos.tf URL")

    from app.models.upload_link import UploadLink, UploadType

    async with async_session_maker() as db:
        result = await db.execute(
            select(Reservation).where(Reservation.reservation_number == reservation_number)
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")

        upload_enabled = (
            reservation.enable_logs_tf_upload
            if body.type == "log"
            else reservation.enable_demos_tf_upload
        )
        if not upload_enabled:
            logger.info(
                "Ignoring disabled %s upload callback for reservation #%s",
                body.type,
                reservation_number,
            )
            return UploadLinkResponse(success=True)

        # Deduplicate by external_id and type
        existing = await db.execute(
            select(UploadLink).where(
                UploadLink.reservation_id == reservation.id,
                UploadLink.type == UploadType(body.type),
                UploadLink.external_id == external_id,
            )
        )
        if existing.scalar_one_or_none():
            return UploadLinkResponse(success=True)

        upload_link = UploadLink(
            reservation_id=reservation.id,
            type=UploadType(body.type),
            external_id=external_id,
            url=url,
        )
        db.add(upload_link)
        await db.commit()

        logger.info(
            f"Upload link reported for reservation #{reservation_number}: "
            f"{body.type} {external_id} -> {url}"
        )

    return UploadLinkResponse(success=True)


class PlayerInfo(BaseModel):
    """Individual player info from plugin."""
    name: str
    steam_id: str
    connect_time: int = 0
    ping: int = 0


class PlayerUpdateRequest(BaseModel):
    """Request body for player update from plugin."""
    player_count: int
    players: list[PlayerInfo] = []


class PlayerUpdateResponse(BaseModel):
    """Response for player update endpoint."""
    success: bool


def _update_persisted_player_state(
    reservation: Reservation,
    player_count: int,
    now: datetime,
) -> bool:
    """Apply player-derived fields and report whether a database write is needed."""
    changed = False

    if player_count > 0 and not reservation.player_joined:
        reservation.player_joined = True
        changed = True

    if player_count > reservation.peak_player_count:
        reservation.peak_player_count = player_count
        changed = True

    if player_count == 0 and reservation.empty_since is None:
        reservation.empty_since = now
        changed = True
    elif player_count > 0 and reservation.empty_since is not None:
        reservation.empty_since = None
        changed = True

    return changed


@router.post("/reservations/{reservation_number}/players", response_model=PlayerUpdateResponse)
async def update_players(
    reservation_number: int,
    body: PlayerUpdateRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """
    Update player list for a reservation.
    Called by SourceMod plugin on player join/leave.
    """
    if not await validate_reservation_api_key(reservation_number, x_api_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    # Store player data in memory
    player_data[reservation_number] = {
        "players": [p.model_dump() for p in body.players],
        "player_count": body.player_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Update reservation DB fields
    async with async_session_maker() as db:
        result = await db.execute(
            select(Reservation).where(Reservation.reservation_number == reservation_number)
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")

        if reservation.status != ReservationStatus.ACTIVE:
            return PlayerUpdateResponse(success=True)

        changed = _update_persisted_player_state(
            reservation,
            body.player_count,
            datetime.now(timezone.utc),
        )

        # Periodic updates arrive every ten seconds. Avoid an unnecessary
        # transaction commit when none of the persisted state changed.
        if changed:
            await db.commit()

    return PlayerUpdateResponse(success=True)

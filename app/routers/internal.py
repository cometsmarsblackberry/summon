"""Internal endpoints for agent communication."""

import asyncio
import hmac
import logging
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_maker
from app.models.reservation import Reservation, ReservationStatus
from app.models.instance import CloudInstance
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
        connected_agents.pop(effective_id, None)
        boot_progress.pop(effective_id, None)
        agent_stats.pop(effective_id, None)


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
    sdr_tv_port = data.get("sdr_tv_port")
    
    current_map = data.get("map")
    
    # Determine the connect address - prefer SDR FakeIP if available
    has_sdr = sdr_ip and sdr_ip.startswith("169.254.")
    
    if has_sdr:
        connect_ip = sdr_ip
        connect_port = sdr_port or 27015
        connect_tv_port = sdr_tv_port or 27020
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

        # Notify agent to stop the container
        instance_id_for_agent = None
        if reservation.instance_id:
            instance_result = await db.execute(
                select(CloudInstance).where(CloudInstance.id == reservation.instance_id)
            )
            cloud_instance = instance_result.scalar_one_or_none()

            if cloud_instance:
                instance_id_for_agent = cloud_instance.instance_id
                await send_to_agent(cloud_instance.instance_id, {
                    "type": "reservation.end",
                })

            # Handle instance cleanup based on billing model
            from app.services.orchestrator import release_to_warm_pool, destroy_instance, is_hourly_billing

            if await is_hourly_billing(reservation.location, db):
                if was_active or had_started:
                    await release_to_warm_pool(reservation.instance_id, db)
                # If still provisioning, let it complete and warm pool on server_ready
            else:
                await destroy_instance(reservation.instance_id, db)

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

        # Track if any player has ever joined
        if body.player_count > 0:
            reservation.player_joined = True

        # Track peak player count
        if body.player_count > reservation.peak_player_count:
            reservation.peak_player_count = body.player_count

        # Track empty_since for auto-end
        if body.player_count == 0 and reservation.empty_since is None:
            reservation.empty_since = datetime.now(timezone.utc)
        elif body.player_count > 0:
            reservation.empty_since = None

        await db.commit()

    return PlayerUpdateResponse(success=True)

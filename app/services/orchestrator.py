"""Instance orchestration - manages cloud instance lifecycle."""

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.instance import CloudInstance, EnabledLocation, LocationProvider, Provider
from app.models.reservation import Reservation, ReservationStatus
from app.services.cloud_provider import get_cloud_client, CloudProviderError
from app.services.failure_messages import public_failure_reason
from app.services.provider_priority import (
    get_providers_for_location,
    is_provider_suspended,
    record_provider_failure,
    record_provider_success,
)


logger = logging.getLogger(__name__)
settings = get_settings()


async def is_hourly_billing(location_code: str, db: AsyncSession) -> bool:
    """Check if a location uses hourly billing by looking up its provider."""
    loc_result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == location_code)
    )
    loc = loc_result.scalar_one_or_none()
    if not loc:
        return False
    provider_result = await db.execute(
        select(Provider).where(Provider.code == loc.provider)
    )
    provider = provider_result.scalar_one_or_none()
    return provider is not None and provider.billing_model == "hourly"


def generate_ignition_config(
    instance_id: str,
    auth_token: str,
    reservation: Reservation,
    owner_steam_id: str = "",
    owner_name: str = "",
    location_city: str = "",
    container_image: str = "",
    fastdl_url: str = "",
) -> str:
    """Generate Ignition config for Fedora CoreOS instance.

    This config only contains bootstrap credentials (backend URL, auth token,
    instance ID). All sensitive reservation config (passwords, API keys) is
    sent via the authenticated WebSocket after the agent connects.

    Returns:
        Base64-encoded Ignition JSON config
    """
    # Construct WebSocket URL
    base_host = settings.base_url.replace('http://', '').replace('https://', '')
    ws_protocol = "wss" if settings.base_url.startswith("https") else "ws"
    ws_url = f"{ws_protocol}://{base_host}/internal/ws/agent/{instance_id}"

    # Agent download URL
    agent_url = f"{settings.base_url}/static/tf2-agent"

    ignition_config = {
        "ignition": {
            "version": "3.4.0"
        },
        **({"passwd": {
            "users": [
                {
                    "name": "core",
                    "sshAuthorizedKeys": [settings.ssh_pubkey]
                }
            ]
        }} if settings.ssh_pubkey else {}),
        "systemd": {
            "units": [
                {
                    "name": "tf2-agent.service",
                    "enabled": True,
                    "contents": f"""[Unit]
Description=TF2 Server Agent
After=network-online.target user@1000.service
Wants=network-online.target
Requires=user@1000.service

[Service]
Type=simple
User=core
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
	Environment=BACKEND_URL={ws_url}
	Environment=AUTH_TOKEN={auth_token}
	Environment=INSTANCE_ID={instance_id}
	Environment=RESERVATION_ID={reservation.id}
	Environment=HEARTBEAT_INTERVAL_SEC={settings.agent_heartbeat_interval_sec}
	ExecStartPre=/usr/bin/loginctl enable-linger core
	ExecStartPre=/usr/bin/curl -L -o /home/core/tf2-agent {agent_url}
	ExecStartPre=/usr/bin/chmod +x /home/core/tf2-agent
	ExecStartPre=/usr/bin/chcon -t bin_t /home/core/tf2-agent
	ExecStart=/home/core/tf2-agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
                },
                {
                    "name": "zincati.service",
                    "mask": True,
                },
                {
                    "name": "create-swap.service",
                    "enabled": True,
                    "contents": """[Unit]
Description=Create and enable swap file
After=local-fs.target
Before=tf2-agent.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'if [ ! -f /var/swapfile ]; then fallocate -l 2G /var/swapfile && chmod 600 /var/swapfile && mkswap /var/swapfile && swapon /var/swapfile; else swapon /var/swapfile 2>/dev/null || true; fi'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
                }
            ]
        },
        "storage": {
            "files": [
                {
                    "path": "/etc/sysctl.d/99-swappiness.conf",
                    "contents": {
                        "source": "data:,vm.swappiness%3D10%0Avm.dirty_ratio%3D10%0Avm.dirty_background_ratio%3D5%0Anet.core.rmem_max%3D4194304%0Anet.core.wmem_max%3D4194304%0Anet.core.rmem_default%3D262144%0Anet.core.wmem_default%3D262144%0Anet.core.netdev_max_backlog%3D2000%0A"
                    },
                    "mode": 0o644,
                }
            ]
        },
    }

    # Return base64 encoded JSON
    return base64.b64encode(json.dumps(ignition_config).encode()).decode()


def build_reservation_config(
    reservation: Reservation,
    owner_steam_id: str = "",
    owner_name: str = "",
    location_city: str = "",
    container_image: str = "",
    fastdl_url: str = "",
    auth_token: str = "",
    instance_id: str = "",
    admin_steam_ids: list[str] | None = None,
) -> dict:
    """Build the full reservation config dict sent to agents via WebSocket.

    This contains all sensitive credentials that were previously baked into
    the Ignition user_data.
    """
    return {
        "reservation_id": reservation.id,
        "reservation_number": reservation.reservation_number,
        "location": reservation.location,
        "location_city": location_city,
        "password": reservation.password,
        "rcon_password": reservation.rcon_password,
        "tv_password": reservation.tv_password,
        "first_map": reservation.first_map,
        "logsecret": reservation.logsecret,
        "owner_steam_id": owner_steam_id,
        "owner_name": owner_name,
        "ends_at": int(reservation.ends_at.timestamp()),
        "backend_url": settings.base_url,
        "internal_api_key": reservation.plugin_api_key,
        "enable_direct_connect": reservation.enable_direct_connect,
        "container_image": container_image,
        "demos_tf_apikey": settings.demos_tf_apikey,
        "logs_tf_apikey": settings.logs_tf_apikey,
        "server_settings": {
            "fastdl_url": fastdl_url or settings.fastdl_url,
            "hostname_format": settings.tf2_hostname_format.replace("{site_name}", settings.site_name),
        },
        "auth_token": auth_token,
        "instance_id": instance_id,
        "motd_url": f"{settings.base_url}/motd/{reservation.motd_token}",
        "admin_steam_ids": admin_steam_ids or [],
        "s3_config": {
            "endpoint": settings.s3_endpoint,
            "access_key": settings.s3_access_key,
            "secret_key": settings.s3_secret_key,
            "bucket": settings.s3_bucket,
            "region": settings.s3_region,
        },
    }


async def provision_instance_for_reservation(
    reservation: Reservation,
    db: AsyncSession,
) -> Optional[CloudInstance]:
    """Create a cloud instance for a reservation.

    Tries providers in priority order (from LocationProvider table).
    For each provider, first checks the warm pool, then tries creating new.
    On API failure, records the failure and falls back to the next provider.

    Args:
        reservation: The reservation needing an instance
        db: Database session

    Returns:
        CloudInstance if successful, None if all providers failed
    """
    # Look up location metadata
    loc_result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == reservation.location)
    )
    loc_record = loc_result.scalar_one_or_none()
    if not loc_record:
        logger.error(
            "Location '%s' is not configured correctly for reservation #%s",
            reservation.location,
            reservation.reservation_number,
        )
        reservation.status = ReservationStatus.FAILED
        reservation.failure_reason = public_failure_reason(
            reservation.status,
            reservation.provision_attempts,
        )
        await db.commit()
        return None

    # Get ordered providers for this location
    location_providers = await get_providers_for_location(reservation.location, db)

    # Fallback: if no LocationProvider entries exist, use legacy EnabledLocation fields
    if not location_providers and loc_record.provider and loc_record.provider_region:
        logger.warning(f"No location_providers for '{reservation.location}', using legacy EnabledLocation fields")
        location_providers = [LocationProvider(
            location_code=reservation.location,
            provider_code=loc_record.provider,
            provider_region=loc_record.provider_region,
            priority=0,
            enabled=True,
            instance_plan=loc_record.instance_plan,
            region_instance_limit=loc_record.region_instance_limit,
        )]

    if not location_providers:
        logger.error(
            "No providers configured for reservation #%s at location '%s'",
            reservation.reservation_number,
            reservation.location,
        )
        reservation.status = ReservationStatus.FAILED
        reservation.failure_reason = public_failure_reason(
            reservation.status,
            reservation.provision_attempts,
        )
        await db.commit()
        return None

    # Load shared data needed across all provider attempts
    from app.models.user import User
    user_result = await db.execute(
        select(User).where(User.id == reservation.user_id)
    )
    owner_user = user_result.scalar_one_or_none()
    owner_steam_id = owner_user.steam_id if owner_user else ""
    owner_name = owner_user.display_name if owner_user else ""
    location_city = loc_record.city if loc_record.city else reservation.location

    from app.services.settings import get_fastdl_url
    fastdl_url = await get_fastdl_url(db)

    # Update reservation status and increment attempt counter
    reservation.status = ReservationStatus.PROVISIONING
    reservation.provision_attempts += 1
    await db.commit()

    # Generate auth token and instance ID for this attempt
    from app.utils.passwords import generate_logsecret
    auth_token = generate_logsecret(32)

    import time
    unique_suffix = hex(int(time.time()))[2:]
    instance_id = f"tf2-{reservation.reservation_number}-{unique_suffix}"

    # Clean up any stale CloudInstance records for this reservation
    stale_instances = await db.execute(
        select(CloudInstance).where(CloudInstance.current_reservation_id == reservation.id)
    )
    for stale in stale_instances.scalars().all():
        logger.info(f"Cleaning up stale instance record: {stale.id}")
        await db.delete(stale)
    await db.commit()

    # Try each provider in priority order
    last_error: Optional[Exception] = None
    all_suspended = True

    for loc_provider in location_providers:
        provider_code = loc_provider.provider_code
        provider_region = loc_provider.provider_region

        # Skip suspended providers (but track if ALL are suspended)
        if is_provider_suspended(reservation.location, provider_code):
            logger.info(f"Skipping suspended provider {provider_code} for {reservation.location}")
            continue
        all_suspended = False

        client = get_cloud_client(provider_code)
        if not client:
            logger.warning(f"Provider '{provider_code}' not configured (no API key?), skipping")
            continue

        # Look up provider record for container_image
        from app.models.instance import Provider
        provider_result = await db.execute(
            select(Provider).where(Provider.code == provider_code)
        )
        provider_record = provider_result.scalar_one_or_none()
        container_image = provider_record.container_image if provider_record else "ghcr.io/cometsmarsblackberry/tf2-summon/i386:nightly"

        # Check warm pool for this specific provider
        warm_instance = await get_warm_instance(reservation.location, db, provider_code=provider_code)
        if warm_instance:
            result = await _reuse_warm_instance(
                warm_instance, reservation, client, loc_provider,
                auth_token, instance_id, location_city, container_image,
                owner_steam_id, owner_name, fastdl_url, db,
            )
            if result:
                record_provider_success(reservation.location, provider_code)
                return result

        # No warm instance — create a new one with this provider
        try:
            result = await _create_new_instance(
                reservation, client, loc_provider, provider_record,
                auth_token, instance_id, location_city, container_image,
                owner_steam_id, owner_name, fastdl_url, db,
            )
            if result:
                record_provider_success(reservation.location, provider_code)
                return result
        except CloudProviderError as e:
            logger.error(f"Provider {provider_code} failed for {reservation.location}: {e}")
            record_provider_failure(reservation.location, provider_code)
            last_error = e
            # Continue to next provider regardless of error type —
            # a 4xx from Vultr might succeed on Gcore
            continue
        except Exception as e:
            logger.exception(f"Unexpected error with provider {provider_code}: {e}")
            record_provider_failure(reservation.location, provider_code)
            last_error = e
            continue

    # All providers exhausted
    if all_suspended:
        # All providers are suspended — leave as PROVISIONING so caller retries
        # (suspensions expire, so a retry after some time may succeed)
        logger.warning(f"All providers suspended for {reservation.location}, will retry")
        return None

    if last_error and isinstance(last_error, CloudProviderError):
        is_retryable = last_error.status_code >= 500 or last_error.status_code == 429
        if not is_retryable and len(location_providers) == 1:
            # Single provider with non-retryable error — fail immediately
            reservation.status = ReservationStatus.FAILED
            reservation.failure_reason = public_failure_reason(
                reservation.status,
                reservation.provision_attempts,
            )
            await db.commit()
            return None

    # Leave as PROVISIONING for caller to retry (transient failure or multi-provider)
    return None


async def _reuse_warm_instance(
    warm_instance: CloudInstance,
    reservation: Reservation,
    client,
    loc_provider: LocationProvider,
    auth_token: str,
    instance_id: str,
    location_city: str,
    container_image: str,
    owner_steam_id: str,
    owner_name: str,
    fastdl_url: str,
    db: AsyncSession,
) -> Optional[CloudInstance]:
    """Reuse a warm pool instance for a new reservation."""
    logger.info(f"Reusing warm pool instance {warm_instance.id} ({loc_provider.provider_code}) for {reservation.location}")

    old_agent_id = warm_instance.instance_id

    warm_instance.is_available = False
    warm_instance.available_since = None
    warm_instance.current_reservation_id = reservation.id
    warm_instance.auth_token = auth_token
    warm_instance.instance_id = instance_id

    new_billing_end = reservation.ends_at + timedelta(minutes=5)
    if new_billing_end.tzinfo is not None:
        new_billing_end = new_billing_end.replace(tzinfo=None)
    warm_instance.billing_hour_ends_at = new_billing_end

    reservation.instance_id = warm_instance.id
    await db.commit()

    # Manage Gcore security group for direct connect
    from app.services.gcore import GcoreClient, DIRECT_CONNECT_SG_NAME
    if isinstance(client, GcoreClient):
        region_id = int(loc_provider.provider_region)
        if reservation.enable_direct_connect:
            await client.ensure_direct_connect_security_group(region_id)
            await client.add_security_group_to_instance(
                warm_instance.id, region_id, DIRECT_CONNECT_SG_NAME
            )
        else:
            await client.remove_security_group_from_instance(
                warm_instance.id, region_id, DIRECT_CONNECT_SG_NAME
            )

    reconfigure_data = {
        "reservation_id": reservation.id,
        "reservation_number": reservation.reservation_number,
        "location": reservation.location,
        "location_city": location_city,
        "password": reservation.password,
        "rcon_password": reservation.rcon_password,
        "tv_password": reservation.tv_password,
        "first_map": reservation.first_map,
        "logsecret": reservation.logsecret,
        "owner_steam_id": owner_steam_id,
        "owner_name": owner_name,
        "enable_direct_connect": reservation.enable_direct_connect,
        "ends_at": int(reservation.ends_at.timestamp()),
        "backend_url": settings.base_url,
        "internal_api_key": reservation.plugin_api_key,
        "container_image": container_image,
        "demos_tf_apikey": settings.demos_tf_apikey,
        "logs_tf_apikey": settings.logs_tf_apikey,
        "server_settings": {
            "fastdl_url": fastdl_url,
            "hostname_format": settings.tf2_hostname_format.replace("{site_name}", settings.site_name),
        },
        "auth_token": auth_token,
        "instance_id": instance_id,
        "s3_config": {
            "endpoint": settings.s3_endpoint,
            "access_key": settings.s3_access_key,
            "secret_key": settings.s3_secret_key,
            "bucket": settings.s3_bucket,
            "region": settings.s3_region,
        },
    }

    from app.routers.internal import send_reconfigure_command, connected_agents, reassign_agent_instance_id
    if old_agent_id and old_agent_id in connected_agents:
        await send_reconfigure_command(old_agent_id, reconfigure_data)
        logger.info(f"Sent reconfigure command to agent {old_agent_id}")
        if reassign_agent_instance_id(old_agent_id, instance_id):
            logger.info(f"Reassigned agent: {old_agent_id} -> {instance_id}")
        else:
            logger.warning(f"Failed to reassign agent {old_agent_id} -> {instance_id}")
    else:
        logger.warning(f"No connected agent found for warm instance {warm_instance.id}, agent may reconnect")

    logger.info(f"Reused warm pool instance {warm_instance.id} for reservation #{reservation.reservation_number}")
    return warm_instance


async def _create_new_instance(
    reservation: Reservation,
    client,
    loc_provider: LocationProvider,
    provider_record,
    auth_token: str,
    instance_id: str,
    location_city: str,
    container_image: str,
    owner_steam_id: str,
    owner_name: str,
    fastdl_url: str,
    db: AsyncSession,
) -> Optional[CloudInstance]:
    """Create a brand new cloud instance with the given provider.

    Raises CloudProviderError or Exception on failure (caller handles fallback).
    """
    label = f"{settings.site_name} #{reservation.reservation_number}"

    create_kwargs: dict = dict(
        region=loc_provider.provider_region,
        label=label,
        user_data=generate_ignition_config(
            instance_id=instance_id,
            auth_token=auth_token,
            reservation=reservation,
            owner_steam_id=owner_steam_id,
            owner_name=owner_name,
            location_city=location_city,
            container_image=container_image,
            fastdl_url=fastdl_url,
        ),
        hostname=f"tf2-{reservation.reservation_number}",
        plan=loc_provider.instance_plan or None,
    )

    from app.services.gcore import GcoreClient
    if isinstance(client, GcoreClient) and reservation.enable_direct_connect:
        sg_id = await client.ensure_direct_connect_security_group(int(loc_provider.provider_region))
        create_kwargs["security_groups"] = [sg_id]

    cloud_instance_data = await client.create_instance(**create_kwargs)

    billing_hour_ends = datetime.now(timezone.utc) + timedelta(hours=1)

    cloud_instance = CloudInstance(
        id=cloud_instance_data.id,
        instance_id=instance_id,
        location=reservation.location,
        provider_code=loc_provider.provider_code,
        provider_region=loc_provider.provider_region,
        ip_address=cloud_instance_data.main_ip if cloud_instance_data.main_ip and cloud_instance_data.main_ip != "0.0.0.0" else None,
        status=cloud_instance_data.status,
        auth_token=auth_token,
        current_reservation_id=reservation.id,
        billing_hour_ends_at=billing_hour_ends,
    )
    db.add(cloud_instance)
    reservation.instance_id = cloud_instance_data.id
    await db.commit()

    logger.info(
        f"Provisioned new instance {cloud_instance_data.id} via {loc_provider.provider_code} "
        f"for reservation #{reservation.reservation_number}"
    )
    return cloud_instance


async def destroy_instance(instance_id: str, db: AsyncSession) -> bool:
    """Destroy a cloud instance.

    Args:
        instance_id: Cloud provider instance ID (CloudInstance.id)
        db: Database session

    Returns:
        True if destroyed successfully
    """
    # Look up instance to determine its provider
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == instance_id)
    )
    instance = result.scalar_one_or_none()

    # Determine provider from the instance record, falling back to location lookup
    provider_code = "vultr"  # fallback default
    provider_region = None
    if instance:
        if instance.provider_code:
            provider_code = instance.provider_code
            provider_region = instance.provider_region
        else:
            # Legacy instance without provider_code — look up from location
            loc_result = await db.execute(
                select(EnabledLocation).where(EnabledLocation.code == instance.location)
            )
            loc = loc_result.scalar_one_or_none()
            if loc:
                provider_code = loc.provider
                provider_region = loc.provider_region

    client = get_cloud_client(provider_code)
    if not client:
        logger.error(f"Cloud provider '{provider_code}' not configured, cannot destroy instance")
        return False

    try:
        await client.destroy_instance(instance_id, region=provider_region)
        logger.info(f"Destroyed instance {instance_id}")

    except CloudProviderError as e:
        if e.status_code == 404:
            # Instance already deleted on provider - proceed with DB cleanup
            logger.info(f"Instance {instance_id} already deleted (404), cleaning up database")
        else:
            logger.error(f"Failed to destroy instance {instance_id}: {e}")
            return False

    # Remove from database and update any associated reservation
    if instance:
        # Find and update any active reservations associated with this instance
        # (There may be multiple due to warm pool reuse, but only one should be ACTIVE)
        reservation_result = await db.execute(
            select(Reservation)
            .where(Reservation.instance_id == instance_id)
            .where(Reservation.status == ReservationStatus.ACTIVE)
        )
        for reservation in reservation_result.scalars().all():
            reservation.status = ReservationStatus.ENDED
            reservation.ended_at = datetime.now(timezone.utc)
            from app.services.timer import cancel_expiry_timer
            cancel_expiry_timer(reservation.id)
            logger.info(f"Marked reservation #{reservation.reservation_number} as ended (instance destroyed)")

        await db.delete(instance)
        await db.commit()

    return True


async def get_enabled_locations(db: AsyncSession) -> list[EnabledLocation]:
    """Get all enabled locations for reservations."""
    result = await db.execute(
        select(EnabledLocation)
        .join(Provider, EnabledLocation.provider == Provider.code)
        .where(EnabledLocation.enabled == True)
        .where(Provider.enabled == True)
        .order_by(EnabledLocation.display_order)
    )
    return list(result.scalars().all())


async def seed_default_locations(db: AsyncSession) -> None:
    """Seed default locations, adding any missing ones."""
    default_locations = []

    existing = await db.execute(select(EnabledLocation.code))
    existing_codes = {row[0] for row in existing.all()}

    added = []
    for location in default_locations:
        if location.code not in existing_codes:
            db.add(location)
            added.append(location.code)

    if added:
        await db.commit()
        logger.info(f"Seeded locations: {', '.join(added)}")


async def seed_default_providers(db: AsyncSession) -> None:
    """Seed default providers, adding any missing ones."""
    from app.models.instance import Provider

    default_providers = [
        Provider(code="vultr", name="Vultr", billing_model="hourly", enabled=True, display_order=1),
        Provider(code="gcore", name="Gcore", billing_model="hourly", enabled=False, display_order=2),
        Provider(code="onidel", name="Onidel", billing_model="hourly", enabled=False, display_order=3),
    ]

    existing = await db.execute(select(Provider.code))
    existing_codes = {row[0] for row in existing.all()}

    added = []
    for provider in default_providers:
        if provider.code not in existing_codes:
            db.add(provider)
            added.append(provider.code)

    if added:
        await db.commit()
        logger.info(f"Seeded providers: {', '.join(added)}")

async def seed_default_maps(db: AsyncSession):
    """Seed the default map pool if none exist."""
    from app.models.instance import GameMap
    from sqlalchemy import func

    result = await db.execute(select(func.count(GameMap.id)))
    if result.scalar_one() > 0:
        return  # Maps already exist

    default_maps = [
        GameMap(name="cp_badlands", display_name="cp_badlands", enabled=True, is_default=True, display_order=1),
    ]

    for map_entry in default_maps:
        db.add(map_entry)

    await db.commit()
    logger.info("Seeded default maps")


async def release_to_warm_pool(instance_id: str, db: AsyncSession) -> bool:
    """Release an instance to the warm pool instead of destroying it.

    Args:
        instance_id: Cloud provider instance UUID
        db: Database session

    Returns:
        True if released successfully
    """
    result = await db.execute(
        select(CloudInstance).where(CloudInstance.id == instance_id)
    )
    instance = result.scalar_one_or_none()

    if not instance:
        logger.warning(f"Instance {instance_id} not found, cannot release to warm pool")
        return False

    # Recalculate the real billing hour boundary from creation time.
    # During reservations, billing_hour_ends_at gets extended to protect
    # the active session — but on release we need the actual billing boundary.
    now = datetime.now(timezone.utc)
    created = instance.created_at
    if created:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        # Hourly providers bill in 1-hour increments from creation time.
        # Find the next billing boundary after now.
        import math
        elapsed_hours = (now - created).total_seconds() / 3600
        next_boundary_hours = math.ceil(elapsed_hours)
        billing_end = created + timedelta(hours=next_boundary_hours)
        instance.billing_hour_ends_at = billing_end.replace(tzinfo=None)  # naive for DB

        if billing_end <= now + timedelta(minutes=2):
            logger.info(f"Instance {instance_id} billing hour expires in <2min, destroying instead of warming")
            await db.commit()
            return await destroy_instance(instance_id, db)

        logger.info(f"Instance {instance_id} next billing boundary: {billing_end} ({int((billing_end - now).total_seconds() / 60)}min left)")
    else:
        # No creation time — can't calculate boundary, destroy to be safe
        logger.warning(f"Instance {instance_id} has no created_at, destroying")
        return await destroy_instance(instance_id, db)

    # Send stop command to agent to clean up container
    from app.routers.internal import send_container_stop
    agent_instance_id = instance.instance_id  # The ID the agent uses
    stop_sent = await send_container_stop(agent_instance_id)
    if stop_sent:
        logger.info(f"Sent stop command to agent {agent_instance_id}")
    else:
        logger.warning(f"Could not send stop to agent {agent_instance_id} (not connected?)")

    # Mark as available
    instance.is_available = True
    instance.available_since = datetime.now(timezone.utc)
    instance.current_reservation_id = None
    await db.commit()

    logger.info(f"Released instance {instance_id} to warm pool (expires {instance.billing_hour_ends_at})")
    return True


async def get_warm_instance(
    location: str,
    db: AsyncSession,
    provider_code: Optional[str] = None,
) -> Optional[CloudInstance]:
    """Get an available warm instance in the specified location.

    Args:
        location: Location code
        db: Database session
        provider_code: If set, only return instances from this provider

    Returns:
        Available CloudInstance or None
    """
    # Use naive datetime for SQLite compatibility
    now = datetime.utcnow()

    query = (
        select(CloudInstance)
        .where(CloudInstance.location == location)
        .where(CloudInstance.is_available == True)
        .where(CloudInstance.billing_hour_ends_at > now)
    )
    if provider_code:
        query = query.where(CloudInstance.provider_code == provider_code)

    query = query.order_by(CloudInstance.billing_hour_ends_at.desc()).limit(1)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def cleanup_expired_instances() -> int:
    """Destroy all instances whose billing hour is about to expire.

    Should be called periodically (e.g., every minute).

    Returns:
        Number of instances destroyed
    """
    from app.database import async_session_maker
    from datetime import timedelta

    # Destroy warm pool instances expiring in the next 2 minutes (use naive datetime for SQLite)
    # Only destroy instances that are in the warm pool (is_available=True),
    # NOT instances actively serving a reservation
    cutoff = datetime.utcnow() + timedelta(minutes=2)
    destroyed = 0

    async with async_session_maker() as db:
        result = await db.execute(
            select(CloudInstance)
            .where(CloudInstance.billing_hour_ends_at < cutoff)
            .where(CloudInstance.billing_hour_ends_at != None)
            .where(CloudInstance.is_available == True)
        )

        for instance in result.scalars().all():
            logger.info(f"Billing expiring for warm pool instance {instance.id}, destroying...")
            if await destroy_instance(instance.id, db):
                destroyed += 1

        # Safety net: destroy non-available instances linked to ENDED reservations
        # whose billing hour is expiring. Catches instances where the agent never
        # connected after a cancellation during provisioning.
        safety_result = await db.execute(
            select(CloudInstance)
            .join(Reservation, Reservation.instance_id == CloudInstance.id)
            .where(CloudInstance.is_available == False)
            .where(Reservation.status == ReservationStatus.ENDED)
            .where(CloudInstance.billing_hour_ends_at < cutoff)
            .where(CloudInstance.billing_hour_ends_at != None)
        )
        for instance in safety_result.scalars().all():
            logger.info(f"Safety-net: destroying stuck instance {instance.id} (reservation ENDED, billing expiring)")
            if await destroy_instance(instance.id, db):
                destroyed += 1

    return destroyed


async def sync_cloud_instances() -> int:
    """Sync database instances with cloud providers - remove records for deleted instances.

    Should be called periodically (e.g., every 5 minutes).

    Returns:
        Number of orphaned records removed
    """
    from app.database import async_session_maker

    removed = 0
    updated = 0

    async with async_session_maker() as db:
        # Get all instances from database
        result = await db.execute(select(CloudInstance))
        db_instances = list(result.scalars().all())

        if not db_instances:
            return 0

        # Group DB instances by provider.
        # Prefer instance.provider_code; fall back to location lookup for legacy records.
        loc_result = await db.execute(select(EnabledLocation))
        location_provider_map = {loc.code: loc.provider for loc in loc_result.scalars().all()}

        by_provider: dict[str, list[CloudInstance]] = {}
        for inst in db_instances:
            prov = inst.provider_code or location_provider_map.get(inst.location, "vultr")
            by_provider.setdefault(prov, []).append(inst)

        # Sync each provider
        for provider_code, instances in by_provider.items():
            client = get_cloud_client(provider_code)
            if not client:
                logger.warning(f"Cloud provider '{provider_code}' not configured, skipping sync")
                continue

            try:
                cloud_instances = await client.list_instances()
                cloud_by_id = {inst.id: inst for inst in cloud_instances}
            except Exception as e:
                logger.error(f"Failed to list instances from {provider_code}: {e}")
                continue

            # Find orphaned records (in DB but not in provider)
            for instance in instances:
                cloud_inst = cloud_by_id.get(instance.id)
                if not cloud_inst:
                    logger.warning(f"Instance {instance.id} not found in {provider_code}, removing from database")
                    await db.delete(instance)
                    removed += 1
                    continue

                # Refresh IP/status in DB (some providers return main_ip empty during create)
                provider_ip = (cloud_inst.main_ip or "").strip()
                if provider_ip and provider_ip != "0.0.0.0":
                    if not instance.ip_address or instance.ip_address == "0.0.0.0":
                        instance.ip_address = provider_ip
                        updated += 1

                if cloud_inst.status and instance.status != cloud_inst.status:
                    instance.status = cloud_inst.status

        if removed > 0 or updated > 0:
            await db.commit()
            if removed > 0:
                logger.info(f"Removed {removed} orphaned instance records")
            if updated > 0:
                logger.info(f"Updated {updated} instance IP/status fields")

    return removed

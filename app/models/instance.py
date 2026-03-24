"""Cloud instance models for provider-agnostic VPS tracking."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CloudInstance(Base):
    """Tracks cloud provider VPS instances."""

    __tablename__ = "cloud_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Cloud provider instance UUID
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)  # Instance ID for agent
    location: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    shape: Mapped[str] = mapped_column(String(32), nullable=False, default="vhf-1c-1gb")
    ip_address: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # Which provider created this instance (for destroy/sync without location lookup)
    provider_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_region: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    
    # Auth token for agent WebSocket connection
    auth_token: Mapped[str] = mapped_column(String(64), nullable=False)
    
    # Status
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    
    # Current reservation using this instance
    current_reservation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reservations.id"), nullable=True
    )
    
    # Warm pool tracking
    is_available: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    available_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Billing tracking - when the current billing hour expires
    billing_hour_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    billed_hours: Mapped[int] = mapped_column(Integer, default=0)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    def __repr__(self) -> str:
        return f"<CloudInstance {self.id[:8]}... @ {self.location}>"


class Provider(Base):
    """Cloud providers with their billing configuration."""
    
    __tablename__ = "providers"
    
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # Display name
    billing_model: Mapped[str] = mapped_column(String(32), nullable=False, default="hourly")  # hourly, per_second
    
    # Configurable settings
    instance_plan: Mapped[str] = mapped_column(String(32), nullable=False, default="vhf-1c-1gb")  # Instance type/plan
    container_image: Mapped[str] = mapped_column(String(128), nullable=False, default="ghcr.io/cometsmarsblackberry/tf2-summon/i386:nightly")
    
    # Max concurrent instances.
    # For providers with global quotas (e.g., Vultr): applies across all regions.
    # For providers with per-region quotas (e.g., Gcore): treated as the default per-region limit.
    instance_limit: Mapped[int] = mapped_column(Integer, default=10)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def uses_warm_pool(self) -> bool:
        """Whether this provider uses warm pool (hourly billing)."""
        return self.billing_model == "hourly"
    
    def __repr__(self) -> str:
        return f"<Provider {self.code}: {self.name}>"


class EnabledLocation(Base):
    """Tracks which locations are available for reservations."""
    
    __tablename__ = "enabled_locations"
    
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    
    # Provider configuration
    provider: Mapped[str] = mapped_column(String(32), ForeignKey("providers.code"), nullable=False, default="vultr")
    provider_region: Mapped[str] = mapped_column(String(32), nullable=False)  # Provider-specific region ID
    
    # Legacy field for backward compatibility (same as provider_region for Vultr)
    vultr_region: Mapped[str] = mapped_column(String(32), nullable=True)
    
    # Keep billing_model for backward compat but it's deprecated - use provider.billing_model
    billing_model: Mapped[str] = mapped_column(String(32), nullable=True, default="hourly")

    # Structured location metadata
    city: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    continent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    subdivision: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    # Optional per-location instance plan override (uses provider default if NULL)
    instance_plan: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Optional per-provider-region instance limit override (used by per-region quota providers like Gcore).
    region_instance_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<Location {self.code}: {self.name} ({self.provider})>"


class LocationProvider(Base):
    """Maps providers to locations with priority for failover.

    A location (e.g., "chicago") can have multiple providers (Vultr priority 1,
    Gcore priority 2). The orchestrator tries them in priority order and falls
    back to the next provider if one fails.
    """

    __tablename__ = "location_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("enabled_locations.code"), nullable=False, index=True
    )
    provider_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("providers.code"), nullable=False
    )
    provider_region: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # Lower = higher priority
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optional overrides (fall back to Provider defaults if NULL)
    instance_plan: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    region_instance_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<LocationProvider {self.location_code}:{self.provider_code} p={self.priority}>"


class GameMap(Base):
    """Available maps for reservations."""
    
    __tablename__ = "game_maps"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # e.g., cp_process_f12
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g., cp_process
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # True = ships with TF2, no download link
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<GameMap {self.name}>"

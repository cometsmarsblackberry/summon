"""Reservation model for server bookings."""

import enum
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ReservationStatus(enum.Enum):
    """Reservation lifecycle states."""
    PENDING = "pending"           # Just created, waiting to provision
    PROVISIONING = "provisioning" # Instance being created
    ACTIVE = "active"             # Server running and ready
    ENDING = "ending"             # Graceful shutdown in progress
    ENDED = "ended"               # Successfully completed
    FAILED = "failed"             # Provisioning failed
    CANCELLED = "cancelled"       # User cancelled before start
    NO_SHOW = "no_show"           # Ended due to no players joining


class Reservation(Base):
    """Server reservation record."""
    
    __tablename__ = "reservations"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_number: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False, index=True
    )
    
    # Owner (nullable: set to NULL on account deletion to anonymize)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    
    # Location
    location: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    
    # Cloud instance (nullable until provisioned)
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    
    # Timing
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    
    # Credentials (auto-generated)
    password: Mapped[str] = mapped_column(String(16), nullable=False)
    rcon_password: Mapped[str] = mapped_column(String(16), nullable=False)
    tv_password: Mapped[str] = mapped_column(String(16), nullable=False)
    
    # Server config
    first_map: Mapped[str] = mapped_column(String(64), nullable=False, default="cp_badlands")
    server_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    whitelist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_end: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_direct_connect: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-reservation API key for plugin→backend communication
    plugin_api_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # MOTD page access token (unguessable URL)
    motd_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # For logs.tf integration
    logsecret: Mapped[str] = mapped_column(String(32), nullable=False)
    
    # Status
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.PENDING, nullable=False, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provision_attempts: Mapped[int] = mapped_column(Integer, default=0)
    
    # SDR (Steam Datagram Relay) connection info - what players connect to
    sdr_ip: Mapped[str | None] = mapped_column(String(15), nullable=True)
    sdr_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sdr_tv_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # Current map (updated during heartbeat)
    current_map: Mapped[str | None] = mapped_column(String(64), nullable=True)
    
    # Server actually started (when status changed to ACTIVE)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # When the reservation actually ended (early end, expiry, auto-end, etc.)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Player tracking
    player_joined: Mapped[bool] = mapped_column(Boolean, default=False)
    peak_player_count: Mapped[int] = mapped_column(Integer, default=0)
    empty_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="reservations")
    cloud_instance: Mapped["CloudInstance"] = relationship(
        "CloudInstance",
        primaryjoin="Reservation.instance_id == foreign(CloudInstance.id)",
        uselist=False,
        viewonly=True,
    )
    upload_links: Mapped[list["UploadLink"]] = relationship(
        "UploadLink", back_populates="reservation", order_by="UploadLink.created_at"
    )
    
    def __repr__(self) -> str:
        return f"<Reservation #{self.reservation_number} @ {self.location}>"
    
    @property
    def is_active(self) -> bool:
        """Check if reservation is currently active."""
        return self.status == ReservationStatus.ACTIVE
    
    @property
    def can_be_ended(self) -> bool:
        """Check if reservation can be ended by user."""
        return self.status in (ReservationStatus.ACTIVE, ReservationStatus.PROVISIONING)

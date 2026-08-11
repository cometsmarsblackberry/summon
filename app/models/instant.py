"""Operator-owned persistent Instant host models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InstantHost(Base):
    """A VPS owned and operated outside Summon."""

    __tablename__ = "instant_hosts"
    __table_args__ = (
        Index(
            "uq_instant_hosts_active_public_ipv4",
            "public_ipv4",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_instant_hosts_location_schedulable", "location", "enabled", "draining", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(
        String(32), ForeignKey("enabled_locations.code"), nullable=False, index=True
    )
    public_ipv4: Mapped[str] = mapped_column(String(15), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # High-entropy credentials and enrollment tokens are stored only as keyed
    # hashes. Enrollment tokens are one-use and short-lived.
    credential_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enrollment_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enrollment_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protocol_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protocol_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    architecture: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unenrolled")
    health_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    preflight_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reconciliation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    desired_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ready_image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unprepared")
    image_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    update_status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    update_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_pin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Tracks whether the updater, rather than an administrator, initiated the
    # drain so a successful update does not undo a manual drain.
    update_auto_drained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    slots: Mapped[list["InstantSlot"]] = relationship(
        "InstantSlot", back_populates="host", order_by="InstantSlot.slot_index"
    )

    @property
    def enrolled(self) -> bool:
        return bool(self.credential_hash)


class InstantSlot(Base):
    """A stable externally-addressable server slot on an Instant host."""

    __tablename__ = "instant_slots"
    __table_args__ = (
        UniqueConstraint("host_id", "slot_index", name="uq_instant_slots_host_index"),
        UniqueConstraint("host_id", "game_port", name="uq_instant_slots_host_game_port"),
        UniqueConstraint("host_id", "tv_port", name="uq_instant_slots_host_tv_port"),
        CheckConstraint("slot_index >= 0", name="ck_instant_slots_index_nonnegative"),
        CheckConstraint("game_port BETWEEN 1 AND 65535", name="ck_instant_slots_game_port"),
        CheckConstraint("tv_port BETWEEN 1 AND 65535", name="ck_instant_slots_tv_port"),
        CheckConstraint("game_port != tv_port", name="ck_instant_slots_distinct_ports"),
        Index("ix_instant_slots_host_enabled", "host_id", "enabled", "quarantined_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instant_hosts.id"), nullable=False, index=True
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    game_port: Mapped[int] = mapped_column(Integer, nullable=False)
    tv_port: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    host: Mapped[InstantHost] = relationship("InstantHost", back_populates="slots")
    assignments: Mapped[list["InstantAssignment"]] = relationship(
        "InstantAssignment", back_populates="slot", order_by="InstantAssignment.generation"
    )


class InstantAssignment(Base):
    """Historical reservation-to-slot lease and command generation."""

    __tablename__ = "instant_assignments"
    __table_args__ = (
        UniqueConstraint("reservation_id", "generation", name="uq_instant_assignment_generation"),
        UniqueConstraint("command_id", name="uq_instant_assignment_command"),
        Index(
            "uq_instant_assignments_open_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
        Index(
            "uq_instant_assignments_open_reservation",
            "reservation_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
        Index("ix_instant_assignments_state", "state", "closed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reservations.id"), nullable=False, index=True
    )
    slot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instant_slots.id"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    claimed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    start_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    reservation: Mapped["Reservation"] = relationship(
        "Reservation", back_populates="instant_assignments"
    )
    slot: Mapped[InstantSlot] = relationship("InstantSlot", back_populates="assignments")

"""User model for Steam-authenticated users."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """User account linked to Steam ID."""
    
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    steam_id: Mapped[str] = mapped_column(String(17), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    
    # API key for serveme.tf compatibility (stored as hash)
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)
    
    # Status flags
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Account deletion
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Stats
    reservation_count: Mapped[int] = mapped_column(Integer, default=0)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Steam trust data
    steam_account_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    tf2_playtime_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    owns_tf2: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    has_vac_ban: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    profile_public: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    steam_data_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    reservations: Mapped[list["Reservation"]] = relationship(
        "Reservation", back_populates="user"
    )
    
    def __repr__(self) -> str:
        return f"<User {self.steam_id}: {self.display_name}>"

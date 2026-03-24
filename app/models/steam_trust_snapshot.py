"""Historical snapshots of Steam trust-check data.

We keep snapshots so that when values change (e.g., TF2 hours increase),
we don't lose the previous results.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SteamTrustSnapshot(Base):
    __tablename__ = "steam_trust_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # When these values were fetched/considered valid.
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Where this snapshot came from (useful when bootstrapping from cached values).
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="steam_api")

    # Steam trust data (nullable when unavailable)
    steam_account_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    tf2_playtime_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    owns_tf2: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    has_vac_ban: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    profile_public: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("ix_steam_trust_snapshots_user_fetched_at", "user_id", "fetched_at"),
    )


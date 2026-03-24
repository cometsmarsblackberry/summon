"""Ping submission model for anonymous latency data collection."""

from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PingSubmission(Base):
    """Anonymous ping test result submitted by a user."""

    __tablename__ = "ping_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Approximate location from IPinfo (nullable if lookup fails)
    user_city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_region: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_country: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_country_code: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)

    # Best result
    best_location: Mapped[str] = mapped_column(String(32), nullable=False)
    best_ping_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # All results as JSON string: {"location_code": ms, ...}
    ping_results: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<PingSubmission {self.id} best={self.best_location}@{self.best_ping_ms}ms>"

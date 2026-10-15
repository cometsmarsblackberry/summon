"""User feedback about reservations and the site."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Feedback(Base):
    """A problem report or suggestion submitted by a signed-in user."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    reservation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reservations.id"), nullable=True, index=True
    )
    # Plain strings validated by app.services.feedback, so categories can
    # change without a schema migration.
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Technical details of the reservation captured at submission time.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User")
    reservation: Mapped["Reservation | None"] = relationship("Reservation")

    def __repr__(self) -> str:
        return f"<Feedback #{self.id} {self.category}>"

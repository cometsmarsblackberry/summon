"""Upload link model for logs.tf and demos.tf uploads."""

import enum
from datetime import datetime
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UploadType(enum.Enum):
    """Type of upload."""
    LOG = "log"
    DEMO = "demo"


class UploadLink(Base):
    """Link to a logs.tf or demos.tf upload for a reservation."""

    __tablename__ = "upload_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reservations.id"), nullable=False, index=True
    )
    type: Mapped[UploadType] = mapped_column(
        Enum(UploadType), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    reservation: Mapped["Reservation"] = relationship("Reservation", back_populates="upload_links")

    def __repr__(self) -> str:
        return f"<UploadLink {self.type.value}:{self.external_id}>"

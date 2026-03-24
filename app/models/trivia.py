"""Trivia facts for MOTD pages."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TriviaFact(Base):
    """Location-based trivia facts shown on MOTD pages."""

    __tablename__ = "trivia_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # city, subdivision, country, generic
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="")  # e.g. "dallas", "texas", "" for generic
    fact: Mapped[str] = mapped_column(String(512), nullable=False)

    def __repr__(self) -> str:
        return f"<TriviaFact {self.scope}:{self.key} #{self.id}>"

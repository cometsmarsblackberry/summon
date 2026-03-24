"""Cost tracking models for billing management."""

from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CostRecord(Base):
    """Individual cost record for instance usage."""
    
    __tablename__ = "cost_records"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reservation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reservations.id"), nullable=True
    )
    
    hours_billed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    def __repr__(self) -> str:
        return f"<CostRecord ${self.cost_usd} for {self.hours_billed}h>"


class MonthlyCost(Base):
    """Aggregated monthly cost summary."""
    
    __tablename__ = "monthly_costs"
    
    year_month: Mapped[str] = mapped_column(String(7), primary_key=True)  # e.g. "2024-01"
    total_hours: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    total_cost_eur: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    reservation_count: Mapped[int] = mapped_column(Integer, default=0)
    
    def __repr__(self) -> str:
        return f"<MonthlyCost {self.year_month}: ${self.total_cost_usd}>"

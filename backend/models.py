from sqlalchemy import Column, Integer, String, Boolean, Date, Numeric, DateTime, UniqueConstraint, Index
from sqlalchemy.sql import func
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    account_type = Column(String(10), nullable=False)  # 'asset' or 'liability'
    override = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Balance(Base):
    __tablename__ = "balances"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, nullable=False)
    period = Column(Date, nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "period", name="uq_balance_account_period"),
        Index("idx_balances_period", "period"),
        Index("idx_balances_account_id", "account_id"),
    )

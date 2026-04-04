from sqlalchemy import Column, Integer, String, Boolean, Date, Numeric, DateTime, UniqueConstraint, Index, ForeignKey
from sqlalchemy.sql import func
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    account_type = Column(String(10), nullable=False)  # 'asset' or 'liability'
    override = Column(Boolean, nullable=False, default=False)
    ignored = Column(Boolean, nullable=False, default=False)
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


class RegisterAccount(Base):
    __tablename__ = "register_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    opening_balance = Column(Numeric(18, 2), nullable=False, default=0)
    notes = Column(String, nullable=True)
    cutoff_date = Column(Date, nullable=True)       # ignore transactions before this date
    cutoff_balance = Column(Numeric(18, 2), nullable=True)  # effective balance as of cutoff_date
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    register_account_id = Column(Integer, ForeignKey("register_accounts.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)  # positive=credit, negative=debit
    source = Column(String(10), nullable=False, default="manual")  # 'manual' | 'bank'
    bank_description = Column(String, nullable=True)   # raw string from bank CSV
    matched_to_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    is_reconciled = Column(Boolean, nullable=False, default=False)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_transactions_account_date", "register_account_id", "date"),
        Index("idx_transactions_matched_to", "matched_to_id"),
    )

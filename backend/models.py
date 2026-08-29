"""
SQLAlchemy models for RecoverAI.

Transaction        -> a merchant payment event that may represent revenue at risk
RecoveryAttempt     -> one bounded action taken against a transaction (retry, reminder, escalate...)
AuditLogEntry       -> immutable, timestamped log of every decision & event, for the judge/demo
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Enum, ForeignKey, Text, Boolean
)
from sqlalchemy.orm import relationship

from database import Base


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class FailureType(str, enum.Enum):
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED = "CARD_EXPIRED"
    BANK_DECLINE = "BANK_DECLINE"
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"
    SUBSCRIPTION_FAILED = "SUBSCRIPTION_FAILED"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"


class TransactionStatus(str, enum.Enum):
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    OVERDUE = "OVERDUE"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"          # workflow stopped, not recovered (limit reached / low probability)
    PENDING = "PENDING"          # not yet processed by the recovery engine


class ActionType(str, enum.Enum):
    RETRY = "RETRY"
    REMINDER = "REMINDER"
    ESCALATE = "ESCALATE"
    DONT_RETRY = "DONT_RETRY"
    STOP = "STOP"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=lambda: gen_id("txn"))
    merchant_id = Column(String, default="demo_merchant")
    customer_id = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR")
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING)
    failure_type = Column(Enum(FailureType), nullable=False)

    # Signals used by the AI diagnosis step
    customer_is_existing = Column(Boolean, default=True)
    previous_successful_payments = Column(Integer, default=0)
    previous_failed_attempts = Column(Integer, default=0)
    days_overdue = Column(Integer, default=0)

    # Bounded-workflow bookkeeping
    retry_count = Column(Integer, default=0)
    last_action = Column(Enum(ActionType), nullable=True)
    last_recovery_probability = Column(Float, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    attempts = relationship("RecoveryAttempt", back_populates="transaction", cascade="all, delete-orphan")


class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    id = Column(String, primary_key=True, default=lambda: gen_id("att"))
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False)
    attempt_number = Column(Integer, nullable=False)

    recovery_probability = Column(Float, nullable=False)
    action_taken = Column(Enum(ActionType), nullable=False)
    reasoning = Column(Text, nullable=True)  # human-readable AI diagnosis explanation

    razorpay_order_id = Column(String, nullable=True)
    razorpay_payment_id = Column(String, nullable=True)
    outcome = Column(String, nullable=True)  # SUCCESS / FAILED / SKIPPED
    amount_recovered = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="attempts")


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id = Column(String, primary_key=True, default=lambda: gen_id("log"))
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=True)
    event = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

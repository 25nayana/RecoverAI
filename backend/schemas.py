from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


class TransactionOut(BaseModel):
    id: str
    customer_id: str
    amount: float
    currency: str
    status: str
    failure_type: str
    customer_is_existing: bool
    previous_successful_payments: int
    previous_failed_attempts: int
    days_overdue: int
    retry_count: int
    last_action: Optional[str] = None
    last_recovery_probability: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RecoveryAttemptOut(BaseModel):
    id: str
    transaction_id: str
    attempt_number: int
    recovery_probability: float
    action_taken: str
    reasoning: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    outcome: Optional[str] = None
    amount_recovered: float
    created_at: datetime

    class Config:
        from_attributes = True


class AuditLogOut(BaseModel):
    id: str
    transaction_id: Optional[str] = None
    event: str
    detail: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MetricsOut(BaseModel):
    revenue_at_risk: float
    eligible_for_recovery: float
    eligible_transactions: int
    revenue_recovered: float
    attempt_success_rate: float       # successful retries / retries attempted
    transaction_recovery_rate: float  # transactions recovered / eligible transactions
    revenue_recovery_rate: float      # ₹ recovered / ₹ eligible
    failed_payments: int
    recovery_attempts: int
    successful_recoveries: int
    by_intervention: dict
    failed_vs_recovered: dict
    not_recovered_reasons: dict


class PolicyOut(BaseModel):
    max_retries: int
    min_retry_interval_minutes: int
    max_transaction_amount: float
    min_recovery_probability: float
    hard_limits: dict


class PolicyUpdate(BaseModel):
    max_retries: Optional[int] = None
    min_retry_interval_minutes: Optional[int] = None
    max_transaction_amount: Optional[float] = None
    min_recovery_probability: Optional[float] = None


class GenerateDataRequest(BaseModel):
    count: int = 1000
    reset: bool = True
    seed: Optional[int] = 42


class ProcessBatchRequest(BaseModel):
    limit: Optional[int] = None


class CheckoutOrderOut(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: Optional[str] = None


class CheckoutVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class WhyOut(BaseModel):
    transaction: TransactionOut
    reasoning: Optional[str] = None
    ai_source: Optional[str] = None
    ai_recommended_action: Optional[str] = None
    safety_checks: dict
    final_action: Optional[str] = None

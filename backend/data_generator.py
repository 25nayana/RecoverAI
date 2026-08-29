"""
Generates a realistic synthetic transaction dataset for Feature 9 (Batch
Measurement). Correlations are intentional, not random noise dressed up:
existing customers with prior successes fail for TEMPORARY reasons more
often (and recover more often); new customers skew toward CARD_EXPIRED /
BANK_DECLINE (recover less often); abandoned checkouts and overdue invoices
are generated with their own realistic shapes.
"""
import random
from models import Transaction, FailureType, TransactionStatus

_FAILURE_WEIGHTS_EXISTING = [
    (FailureType.TEMPORARY_FAILURE, 0.35),
    (FailureType.INSUFFICIENT_FUNDS, 0.18),
    (FailureType.SUBSCRIPTION_FAILED, 0.15),
    (FailureType.BANK_DECLINE, 0.12),
    (FailureType.INVOICE_OVERDUE, 0.10),
    (FailureType.CHECKOUT_ABANDONED, 0.07),
    (FailureType.CARD_EXPIRED, 0.03),
]

_FAILURE_WEIGHTS_NEW = [
    (FailureType.CARD_EXPIRED, 0.20),
    (FailureType.BANK_DECLINE, 0.25),
    (FailureType.CHECKOUT_ABANDONED, 0.30),
    (FailureType.INSUFFICIENT_FUNDS, 0.15),
    (FailureType.TEMPORARY_FAILURE, 0.10),
]


def _weighted_choice(weights):
    items, probs = zip(*weights)
    return random.choices(items, weights=probs, k=1)[0]


def generate_transactions(n: int = 1000, seed: int = None):
    if seed is not None:
        random.seed(seed)

    transactions = []
    for i in range(n):
        is_existing = random.random() < 0.65

        failure_type = _weighted_choice(
            _FAILURE_WEIGHTS_EXISTING if is_existing else _FAILURE_WEIGHTS_NEW
        )

        prev_success = 0
        if is_existing:
            prev_success = max(0, int(random.gauss(6, 4)))
        prev_failed = 0
        # Some fraction already have a failed attempt in this workflow (mid-workflow state)
        if random.random() < 0.15:
            prev_failed = random.choice([1, 1, 2])

        # Amount distribution: mostly small-to-mid tickets, a long tail of larger ones
        amount = round(random.choice([
            random.uniform(199, 999),
            random.uniform(999, 2999),
            random.uniform(2999, 9999),
            random.uniform(9999, 25000),
        ]), 2)

        days_overdue = 0
        if failure_type == FailureType.INVOICE_OVERDUE:
            days_overdue = random.randint(1, 45)

        status = (
            TransactionStatus.ABANDONED if failure_type == FailureType.CHECKOUT_ABANDONED
            else TransactionStatus.OVERDUE if failure_type == FailureType.INVOICE_OVERDUE
            else TransactionStatus.FAILED
        )

        txn = Transaction(
            customer_id=f"cust_{i:05d}",
            amount=amount,
            currency="INR",
            status=status,
            failure_type=failure_type,
            customer_is_existing=is_existing,
            previous_successful_payments=prev_success,
            previous_failed_attempts=prev_failed,
            days_overdue=days_overdue,
            retry_count=prev_failed,  # already spent that many retries in-workflow
        )
        transactions.append(txn)

    return transactions

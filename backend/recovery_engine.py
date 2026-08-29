"""
RecoverAI bounded recovery engine.

Architecture:
    diagnose() -> AI/rule-based diagnosis
    decide()   -> deterministic financial safety gate

The AI may recommend an action, but this module alone authorizes financial
actions. Hard limits are non-negotiable.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Tuple

from models import FailureType, ActionType


@dataclass
class BoundedRecoveryConfig:
    """Merchant-tunable policy inside non-removable hard limits."""
    max_retries: int = 2
    min_retry_interval_minutes: int = 30
    max_transaction_amount: float = 10_000.0
    min_recovery_probability: float = 0.35


# Absolute safety envelope. These limits cannot be removed by merchant policy.
HARD_LIMITS = {
    "max_retries": (1, 5),
    "min_retry_interval_minutes": (5, 24 * 60),
    "max_transaction_amount": (500.0, 100_000.0),
    "min_recovery_probability": (0.10, 0.90),
}


def validate_policy_update(updates: dict) -> list:
    """Validate merchant policy against the immutable safety envelope."""
    errors = []

    for field, value in updates.items():
        if field not in HARD_LIMITS:
            errors.append(f"unknown policy field: {field}")
            continue

        lo, hi = HARD_LIMITS[field]

        if not (lo <= value <= hi):
            errors.append(
                f"{field}={value} is outside the hard-enforced "
                f"range [{lo}, {hi}]"
            )

    return errors


def apply_policy_update(
    config: "BoundedRecoveryConfig",
    updates: dict,
) -> None:
    """Apply already-validated merchant policy changes."""
    for field, value in updates.items():
        setattr(config, field, value)


CONFIG = BoundedRecoveryConfig()


_BASE_PROBABILITY = {
    FailureType.TEMPORARY_FAILURE: 0.72,
    FailureType.INSUFFICIENT_FUNDS: 0.48,
    FailureType.CARD_EXPIRED: 0.18,
    FailureType.BANK_DECLINE: 0.40,
    FailureType.CHECKOUT_ABANDONED: 0.38,
    FailureType.SUBSCRIPTION_FAILED: 0.58,
    FailureType.INVOICE_OVERDUE: 0.55,
}


def diagnose(txn) -> Tuple[float, str, str, str]:
    """
    Get an advisory diagnosis.

    Returns:
        probability,
        reasoning,
        source,
        AI recommended action

    The recommendation is NEVER authorization for a payment.
    """
    from ai_diagnosis import llm_diagnose

    llm_result = llm_diagnose(txn)

    if llm_result is not None:
        probability = round(
            max(0.02, min(0.97, llm_result["probability"])),
            3,
        )

        reasoning = (
            f"[LLM] {llm_result['reason']} "
            f"-> recovery probability {probability:.0%}"
        )

        return (
            probability,
            reasoning,
            "llm",
            llm_result["recommended_action"],
        )

    probability, reasoning = rule_based_diagnose(txn)

    return (
        probability,
        f"[rule-based fallback] {reasoning}",
        "rule_based",
        "",
    )


def rule_based_diagnose(txn) -> Tuple[float, str]:
    """
    Explainable fallback model.

    This is intentionally deterministic so RecoverAI still works when:
    - there is no LLM API key,
    - the network is unavailable,
    - the LLM returns invalid output.
    """
    base = _BASE_PROBABILITY[txn.failure_type]
    score = base

    reasons = [
        f"base rate for {txn.failure_type.value} is {base:.0%}"
    ]

    if txn.previous_successful_payments > 0:
        bonus = min(
            0.20,
            0.03 * txn.previous_successful_payments,
        )
        score += bonus

        reasons.append(
            f"+{bonus:.0%} for "
            f"{txn.previous_successful_payments} prior successful payment(s)"
        )

    if not txn.customer_is_existing:
        score -= 0.10
        reasons.append("-10% new/unverified customer")

    if txn.previous_failed_attempts > 0:
        penalty = min(
            0.30,
            0.12 * txn.previous_failed_attempts,
        )
        score -= penalty

        reasons.append(
            f"-{penalty:.0%} for "
            f"{txn.previous_failed_attempts} prior failed attempt(s)"
        )

    if txn.amount > 5000:
        score -= 0.08
        reasons.append("-8% high transaction amount")

    if (
        txn.failure_type == FailureType.INVOICE_OVERDUE
        and txn.days_overdue
    ):
        aging_penalty = min(
            0.35,
            0.02 * txn.days_overdue,
        )
        score -= aging_penalty

        reasons.append(
            f"-{aging_penalty:.0%} for "
            f"{txn.days_overdue} days overdue"
        )

    score = max(0.02, min(0.97, score))

    reasoning = (
        "; ".join(reasons)
        + f" -> recovery probability {score:.0%}"
    )

    return round(score, 3), reasoning


def decide(
    txn,
    recovery_probability: float,
    now: datetime = None,
    ai_recommended_action: str = "",
) -> Tuple[ActionType, str]:
    """
    Deterministic financial authorization layer.

    IMPORTANT:
    ai_recommended_action is evidence for the audit trail only.
    It can NEVER bypass a safety rule.
    """
    action, reason = _decide_core(
        txn,
        recovery_probability,
        now,
    )

    ai_action = (ai_recommended_action or "").upper().strip()

    if ai_action:
        if ai_action not in {
            "RETRY",
            "REMINDER",
            "ESCALATE",
            "DONT_RETRY",
        }:
            reason += (
                f"; invalid AI recommendation ignored ({ai_action})"
            )
        elif ai_action == action.value:
            reason += (
                f"; safety gate agreed with AI recommendation "
                f"({ai_action})"
            )
        else:
            reason += (
                f"; safety gate overrode AI recommendation "
                f"({ai_action} -> {action.value})"
            )

    return action, reason


def _decide_core(
    txn,
    recovery_probability: float,
    now: datetime = None,
) -> Tuple[ActionType, str]:
    """
    Apply safety checks in a deliberate order.

    The order matters:
      1. already recovered
      2. retry limit
      3. amount cap
      4. retry interval
      5. abandonment handling
      6. probability threshold
      7. repeated-failure escalation
      8. retry
    """
    now = now or datetime.utcnow()

    # 1. Never touch an already recovered transaction.
    if txn.status == "RECOVERED" or getattr(txn.status, "value", None) == "RECOVERED":
        return (
            ActionType.STOP,
            "already recovered; workflow stopped",
        )

    # 2. Immutable retry boundary.
    if txn.retry_count >= CONFIG.max_retries:
        return (
            ActionType.STOP,
            f"maximum retries reached "
            f"({txn.retry_count}/{CONFIG.max_retries}); "
            "no further automatic attempts will be made",
        )

    # 3. Amount safety boundary.
    if txn.amount > CONFIG.max_transaction_amount:
        return (
            ActionType.ESCALATE,
            f"amount ₹{txn.amount:,.0f} exceeds max "
            f"auto-recovery threshold "
            f"(₹{CONFIG.max_transaction_amount:,.0f}); "
            "escalated for manual review",
        )

    # 4. Rate limit.
    if txn.last_attempt_at is not None:
        elapsed = now - txn.last_attempt_at

        if elapsed < timedelta(
            minutes=CONFIG.min_retry_interval_minutes
        ):
            wait_left = (
                timedelta(
                    minutes=CONFIG.min_retry_interval_minutes
                )
                - elapsed
            )

            return (
                ActionType.DONT_RETRY,
                f"minimum retry interval not yet elapsed "
                f"(wait {wait_left})",
            )

    # 5. Abandoned checkout gets a reminder, not a charge retry.
    if txn.failure_type == FailureType.CHECKOUT_ABANDONED:
        return (
            ActionType.REMINDER,
            "checkout abandoned; sending a reminder instead of "
            "a charge retry",
        )

    # 6. Low-confidence cases do not consume a retry.
    if recovery_probability < CONFIG.min_recovery_probability:
        return (
            ActionType.DONT_RETRY,
            f"recovery probability "
            f"{recovery_probability:.0%} below minimum threshold "
            f"({CONFIG.min_recovery_probability:.0%})",
        )

    # 7. Repeated failure + moderate confidence -> human review.
    if (
        txn.previous_failed_attempts >= 1
        and recovery_probability < 0.55
    ):
        return (
            ActionType.ESCALATE,
            f"repeated failure with moderate confidence "
            f"({recovery_probability:.0%}); escalating",
        )

    # 8. Only now is an automatic retry authorized.
    return (
        ActionType.RETRY,
        f"recovery probability "
        f"{recovery_probability:.0%} clears threshold; "
        "retrying payment",
    )

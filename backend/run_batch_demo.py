"""
Standalone CLI demo: generates a synthetic batch, runs the full recovery
loop, and prints the same numbers the dashboard shows. Useful for judges
who just want to see the batch measurement story in a terminal, or for
generating pitch-deck numbers without starting the web server.

Usage:
    cd backend
    python run_batch_demo.py --count 1000
"""
import argparse
from database import Base, engine, SessionLocal
import models
from models import Transaction, RecoveryAttempt, AuditLogEntry, TransactionStatus, ActionType
from data_generator import generate_transactions
from recovery_engine import diagnose, decide, CONFIG
from razorpay_client import razorpay_client
from datetime import datetime


def main(count: int, seed: int):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # reset
    db.query(RecoveryAttempt).delete()
    db.query(AuditLogEntry).delete()
    db.query(Transaction).delete()
    db.commit()

    txns = generate_transactions(count, seed=seed)
    db.add_all(txns)
    db.commit()
    print(f"Generated {len(txns)} synthetic transactions.\n")

    at_risk_statuses = (TransactionStatus.FAILED, TransactionStatus.ABANDONED, TransactionStatus.OVERDUE)
    revenue_at_risk = sum(t.amount for t in txns if t.status in at_risk_statuses)
    print(f"Revenue at risk: ₹{revenue_at_risk:,.2f}\n")

    recovered_amount = 0.0
    attempts_made = 0
    successes = 0
    eligible_amount = 0.0
    eligible_count = 0
    intervention_counts = {}
    not_recovered_reasons = {
        "low_probability": 0, "retry_limit_reached": 0,
        "escalated_manual_review": 0, "payment_attempt_failed": 0,
    }

    for txn in txns:
        prob, reasoning, source, ai_action = diagnose(txn)
        txn.last_recovery_probability = prob
        action, reason = decide(txn, prob, ai_recommended_action=ai_action)
        txn.last_action = action
        intervention_counts[action.value] = intervention_counts.get(action.value, 0) + 1

        if prob >= CONFIG.min_recovery_probability:
            eligible_amount += txn.amount
            eligible_count += 1

        if action == ActionType.DONT_RETRY:
            not_recovered_reasons["low_probability"] += 1
        elif action == ActionType.ESCALATE:
            not_recovered_reasons["escalated_manual_review"] += 1

        if action == ActionType.RETRY:
            attempts_made += 1
            result = razorpay_client.attempt_payment(txn.amount, txn.currency, prob, txn.customer_id)
            success = result["status"] == "captured"
            txn.retry_count += 1
            txn.last_attempt_at = datetime.utcnow()
            if success:
                successes += 1
                recovered_amount += txn.amount
                txn.status = TransactionStatus.RECOVERED
            else:
                not_recovered_reasons["payment_attempt_failed"] += 1
                if txn.retry_count >= CONFIG.max_retries:
                    txn.status = TransactionStatus.STOPPED
                    not_recovered_reasons["retry_limit_reached"] += 1

    db.commit()

    attempt_success_rate = (successes / attempts_made * 100) if attempts_made else 0.0
    transaction_recovery_rate = (successes / eligible_count * 100) if eligible_count else 0.0
    revenue_recovery_rate = (recovered_amount / eligible_amount * 100) if eligible_amount else 0.0

    print("=" * 55)
    print("BATCH RECOVERY REPORT")
    print("=" * 55)
    print(f"Transactions analyzed:       {len(txns):,}")
    print(f"Revenue at risk:          ₹{revenue_at_risk:,.2f}")
    print(f"Eligible for recovery:    ₹{eligible_amount:,.2f}  ({eligible_count:,} txns)")
    print()
    print(f"Attempts made:                {attempts_made:,}")
    print(f"Successful recoveries:        {successes:,}")
    print()
    print(f"Revenue recovered:        ₹{recovered_amount:,.2f}")
    print(f"Attempt success rate:         {attempt_success_rate:.1f}%   (successes / attempts)")
    print(f"Transaction recovery rate:    {transaction_recovery_rate:.1f}%   (recovered / eligible txns)")
    print(f"Revenue recovery rate:        {revenue_recovery_rate:.1f}%   (₹ recovered / ₹ eligible)")
    print()
    print("Actions taken breakdown:")
    for k, v in sorted(intervention_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<12} {v:,}")
    print()
    print("Reasons not recovered:")
    for k, v in not_recovered_reasons.items():
        print(f"  {k:<25} {v:,}")
    print("=" * 55)

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    main(args.count, args.seed)

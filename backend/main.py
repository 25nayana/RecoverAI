"""
RecoverAI backend -- FastAPI app.

Endpoints:
  POST /api/data/generate     generate a synthetic transaction batch
  POST /api/recovery/process  run the recovery engine over pending transactions
  GET  /api/transactions      list transactions
  GET  /api/attempts          list recovery attempts
  GET  /api/audit-trail       list audit log entries (newest first)
  GET  /api/metrics           dashboard metrics
  GET  /                      static dashboard (frontend/index.html)

Run:
  cd backend && uvicorn main:app --reload --port 8000
"""
import os
import re
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import Base, engine, get_db
import models
from models import Transaction, RecoveryAttempt, AuditLogEntry, TransactionStatus, ActionType
import schemas
from data_generator import generate_transactions
from recovery_engine import (
    diagnose, decide, CONFIG, HARD_LIMITS,
    validate_policy_update, apply_policy_update,
)
from razorpay_client import razorpay_client

Base.metadata.create_all(bind=engine)

app = FastAPI(title="RecoverAI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def log_event(db: Session, event: str, detail: str = None, transaction_id: str = None):
    entry = AuditLogEntry(event=event, detail=detail, transaction_id=transaction_id)
    db.add(entry)
    db.commit()


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------
@app.post("/api/data/generate")
def generate_data(req: schemas.GenerateDataRequest, db: Session = Depends(get_db)):
    if req.reset:
        db.query(RecoveryAttempt).delete()
        db.query(AuditLogEntry).delete()
        db.query(Transaction).delete()
        db.commit()

    txns = generate_transactions(req.count, seed=req.seed)
    db.add_all(txns)
    db.commit()

    seed_note = f" (seed={req.seed}, reproducible)" if req.seed is not None else " (unseeded, random)"
    log_event(db, "BATCH_GENERATED", f"{req.count} synthetic transactions generated{seed_note}")

    return {"generated": len(txns), "seed": req.seed}


# ---------------------------------------------------------------------------
# Recovery processing (the whole loop: diagnose -> decide -> act -> log)
# ---------------------------------------------------------------------------
@app.post("/api/recovery/process")
def process_batch(req: schemas.ProcessBatchRequest, db: Session = Depends(get_db)):
    q = db.query(Transaction).filter(
        Transaction.status.in_([
            TransactionStatus.FAILED, TransactionStatus.ABANDONED, TransactionStatus.OVERDUE
        ])
    )
    if req.limit:
        q = q.limit(req.limit)
    pending = q.all()

    processed = 0
    for txn in pending:
        _process_one(db, txn)
        processed += 1

    log_event(db, "BATCH_PROCESSED", f"{processed} transactions run through the recovery engine")
    db.commit()
    return {"processed": processed}


def _notification_copy(txn: Transaction, action: ActionType) -> str:
    """Simulated customer-facing message text. Not actually sent anywhere."""
    amount = f"₹{txn.amount:,.0f}"
    if action == ActionType.RETRY:
        return (
            f"[simulated SMS/WhatsApp to {txn.customer_id}] "
            f"\"Your payment of {amount} didn't go through. We'll retry it "
            f"automatically -- no action needed. Reply STOP to cancel.\""
        )
    return (
        f"[simulated SMS/WhatsApp to {txn.customer_id}] "
        f"\"Looks like your {amount} checkout wasn't completed. "
        f"Would you like to finish your order? [Resume checkout]\""
    )


def _process_one(db: Session, txn: Transaction):
    now = datetime.utcnow()

    probability, reasoning, diagnosis_source, ai_recommended_action = diagnose(txn)
    txn.last_recovery_probability = probability
    diagnosis_detail = reasoning
    if ai_recommended_action:
        diagnosis_detail += f"; recommendation={ai_recommended_action}"
    log_event(db, "AI_DIAGNOSIS", diagnosis_detail, txn.id)

    action, decision_reason = decide(txn, probability, now=now, ai_recommended_action=ai_recommended_action)
    txn.last_action = action
    log_event(db, "RECOVERY_DECISION", f"{action.value}: {decision_reason}", txn.id)

    attempt_number = txn.retry_count + 1

    # Simulated customer notification layer. RecoverAI doesn't actually send
    # SMS/WhatsApp/email -- but every customer-facing action is logged as if
    # it had, so the workflow reads as complete in the audit trail.
    if action in (ActionType.RETRY, ActionType.REMINDER):
        message = _notification_copy(txn, action)
        log_event(db, "NOTIFICATION_SENT", message, txn.id)

    if action == ActionType.RETRY:
        result = razorpay_client.attempt_payment(
            amount_rupees=txn.amount,
            currency=txn.currency,
            success_probability=probability,
            customer_id=txn.customer_id,
        )
        success = result["status"] == "captured"

        attempt = RecoveryAttempt(
            transaction_id=txn.id,
            attempt_number=attempt_number,
            recovery_probability=probability,
            action_taken=action,
            reasoning=reasoning,
            razorpay_order_id=result["order_id"],
            razorpay_payment_id=result.get("payment_id"),
            outcome="SUCCESS" if success else "FAILED",
            amount_recovered=txn.amount if success else 0.0,
        )
        db.add(attempt)

        txn.retry_count = attempt_number
        txn.last_attempt_at = now

        log_event(
            db,
            "RECOVERY_ATTEMPT",
            f"attempt #{attempt_number} via Razorpay ({result['mode']} mode) -> "
            f"{result['status']} (order {result['order_id']})",
            txn.id,
        )

        if success:
            txn.status = TransactionStatus.RECOVERED
            log_event(db, "PAYMENT_RECOVERED", f"₹{txn.amount:,.2f} recovered", txn.id)
            log_event(db, "WORKFLOW_STOPPED", "payment successful; workflow stopped", txn.id)
        else:
            log_event(db, "PAYMENT_FAILED", f"attempt #{attempt_number} failed", txn.id)
            # If this was the last allowed retry, mark as stopped now.
            if txn.retry_count >= CONFIG.max_retries:
                txn.status = TransactionStatus.STOPPED
                log_event(
                    db, "WORKFLOW_STOPPED",
                    "recovery limit reached; no further automatic attempts will be made",
                    txn.id,
                )

    elif action == ActionType.REMINDER:
        attempt = RecoveryAttempt(
            transaction_id=txn.id,
            attempt_number=attempt_number,
            recovery_probability=probability,
            action_taken=action,
            reasoning=reasoning,
            outcome="SKIPPED",
            amount_recovered=0.0,
        )
        db.add(attempt)
        txn.retry_count = attempt_number
        txn.last_attempt_at = now
        log_event(db, "REMINDER_SENT", "checkout abandonment reminder simulated", txn.id)

    elif action == ActionType.ESCALATE:
        attempt = RecoveryAttempt(
            transaction_id=txn.id,
            attempt_number=attempt_number,
            recovery_probability=probability,
            action_taken=action,
            reasoning=reasoning,
            outcome="SKIPPED",
            amount_recovered=0.0,
        )
        db.add(attempt)
        log_event(db, "ESCALATED", "handed off for manual review; no automatic charge attempted", txn.id)

    elif action == ActionType.DONT_RETRY:
        log_event(db, "NO_ACTION", decision_reason, txn.id)

    elif action == ActionType.STOP:
        if txn.status not in (TransactionStatus.RECOVERED,):
            txn.status = TransactionStatus.STOPPED
        log_event(db, "WORKFLOW_STOPPED", decision_reason, txn.id)

    db.commit()


# ---------------------------------------------------------------------------
# AI diagnosis (advisory only)
# ---------------------------------------------------------------------------
@app.post("/api/transactions/{txn_id}/diagnose")
def diagnose_transaction(txn_id: str, db: Session = Depends(get_db)):
    """
    Run AI/rule-based diagnosis for one transaction without performing
    any payment action. The recovery engine remains the authority
    for financial actions.
    """
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="transaction not found")

    probability, reasoning, diagnosis_source, ai_recommended_action = diagnose(txn)

    txn.last_recovery_probability = probability

    log_event(
        db,
        "AI_DIAGNOSIS",
        f"{reasoning}; recommendation={ai_recommended_action or 'NONE'}",
        txn.id,
    )
    db.commit()

    return {
        "transaction_id": txn.id,
        "recovery_probability": probability,
        "reasoning": reasoning,
        "ai_source": diagnosis_source,
        "ai_recommended_action": ai_recommended_action or None,
        "financial_action_authorized": False,
        "message": "Diagnosis is advisory only; no payment action was performed.",
    }


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
@app.get("/api/transactions", response_model=List[schemas.TransactionOut])
def list_transactions(
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Transaction)
    if status:
        q = q.filter(Transaction.status == status)
    q = q.order_by(Transaction.created_at.desc()).offset(offset).limit(limit)
    return q.all()


@app.get("/api/attempts", response_model=List[schemas.RecoveryAttemptOut])
def list_attempts(limit: int = 200, db: Session = Depends(get_db)):
    q = db.query(RecoveryAttempt).order_by(RecoveryAttempt.created_at.desc()).limit(limit)
    return q.all()


@app.get("/api/audit-trail", response_model=List[schemas.AuditLogOut])
def audit_trail(limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(AuditLogEntry).order_by(AuditLogEntry.created_at.desc()).limit(limit)
    return q.all()


@app.get("/api/metrics", response_model=schemas.MetricsOut)
def metrics(db: Session = Depends(get_db)):
    all_txns = db.query(Transaction).all()

    # Revenue at risk = money still exposed right now. Once a transaction is
    # RECOVERED or STOPPED it has left the "at risk" bucket -- it's either
    # recovered revenue or a booked loss, not still-at-risk money.
    at_risk_statuses = {
        TransactionStatus.FAILED, TransactionStatus.ABANDONED, TransactionStatus.OVERDUE,
    }
    revenue_at_risk = sum(t.amount for t in all_txns if t.status in at_risk_statuses)

    # Eligible = the AI diagnosis cleared the minimum-probability bar. This is
    # the pool the recovery engine considers worth spending an attempt on,
    # and it's the denominator for both recovery-rate metrics below.
    eligible_txns = [
        t for t in all_txns
        if t.last_recovery_probability is not None
        and t.last_recovery_probability >= CONFIG.min_recovery_probability
    ]
    eligible_amount = sum(t.amount for t in eligible_txns)
    eligible_count = len(eligible_txns)

    recovered_txns = [t for t in all_txns if t.status == TransactionStatus.RECOVERED]
    revenue_recovered = sum(t.amount for t in recovered_txns)

    # Failed payments = unresolved payment-risk transactions only.
    # RECOVERED and STOPPED are deliberately excluded.
    failed_payments = len([
        t for t in all_txns
        if t.status in (
            TransactionStatus.FAILED,
            TransactionStatus.ABANDONED,
            TransactionStatus.OVERDUE,
        )
    ])

    all_attempts = db.query(RecoveryAttempt).all()
    recovery_attempts = len([a for a in all_attempts if a.action_taken == ActionType.RETRY])
    successful_recoveries = len([a for a in all_attempts if a.outcome == "SUCCESS"])

    # Attempt success rate: of the retries we actually spent, how many landed.
    attempt_success_rate = (successful_recoveries / recovery_attempts * 100) if recovery_attempts else 0.0

    # Transaction recovery rate: of everything worth trying, how many transactions recovered.
    transaction_recovery_rate = (len(recovered_txns) / eligible_count * 100) if eligible_count else 0.0

    # Revenue recovery rate: of the money worth trying to recover, how much actually came back.
    # This is the headline business metric.
    revenue_recovery_rate = (revenue_recovered / eligible_amount * 100) if eligible_amount else 0.0

    # Honest breakdown of why the rest didn't recover.
    not_recovered_reasons = {
        "low_probability": len([t for t in all_txns if t.status in at_risk_statuses
                                 and t.last_action == ActionType.DONT_RETRY]),
        "retry_limit_reached": len([t for t in all_txns if t.status == TransactionStatus.STOPPED]),
        "escalated_manual_review": len([t for t in all_txns if t.last_action == ActionType.ESCALATE]),
        "payment_attempt_failed": len([a for a in all_attempts
                                        if a.action_taken == ActionType.RETRY and a.outcome == "FAILED"]),
        "reminder_pending_response": len([t for t in all_txns if t.last_action == ActionType.REMINDER
                                           and t.status != TransactionStatus.RECOVERED]),
    }

    by_intervention = {}
    for a in all_attempts:
        key = a.action_taken.value if hasattr(a.action_taken, "value") else a.action_taken
        by_intervention[key] = by_intervention.get(key, 0) + 1

    failed_vs_recovered = {
        "recovered": len(recovered_txns),
        "failed": len([t for t in all_txns if t.status == TransactionStatus.STOPPED]),
        "pending": len([t for t in all_txns if t.status in (
            TransactionStatus.FAILED, TransactionStatus.ABANDONED, TransactionStatus.OVERDUE
        )]),
    }

    return schemas.MetricsOut(
        revenue_at_risk=round(revenue_at_risk, 2),
        eligible_for_recovery=round(eligible_amount, 2),
        eligible_transactions=eligible_count,
        revenue_recovered=round(revenue_recovered, 2),
        attempt_success_rate=round(attempt_success_rate, 1),
        transaction_recovery_rate=round(transaction_recovery_rate, 1),
        revenue_recovery_rate=round(revenue_recovery_rate, 1),
        failed_payments=failed_payments,
        recovery_attempts=recovery_attempts,
        successful_recoveries=successful_recoveries,
        by_intervention=by_intervention,
        failed_vs_recovered=failed_vs_recovered,
        not_recovered_reasons=not_recovered_reasons,
    )


@app.get("/api/transactions/{txn_id}/why", response_model=schemas.WhyOut)
def why(txn_id: str, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="transaction not found")

    latest_attempt = (
        db.query(RecoveryAttempt)
        .filter(RecoveryAttempt.transaction_id == txn_id)
        .order_by(RecoveryAttempt.created_at.desc())
        .first()
    )
    reasoning = latest_attempt.reasoning if latest_attempt else None
    if reasoning is None:
        # No attempt was spent (e.g. DONT_RETRY/ESCALATE) -- pull straight from the audit log.
        log_entry = (
            db.query(AuditLogEntry)
            .filter(AuditLogEntry.transaction_id == txn_id, AuditLogEntry.event == "AI_DIAGNOSIS")
            .order_by(AuditLogEntry.created_at.desc())
            .first()
        )
        reasoning = log_entry.detail if log_entry else None

    ai_source = None
    ai_recommended_action = None
    if reasoning:
        if reasoning.startswith("[LLM]"):
            ai_source = "llm"
        elif reasoning.startswith("[rule-based fallback]"):
            ai_source = "rule_based"

    latest_diagnosis = (
        db.query(AuditLogEntry)
        .filter(
            AuditLogEntry.transaction_id == txn_id,
            AuditLogEntry.event == "AI_DIAGNOSIS",
        )
        .order_by(AuditLogEntry.created_at.desc())
        .first()
    )
    if latest_diagnosis and latest_diagnosis.detail:
        match = re.search(r"recommendation=([A-Z_]+)", latest_diagnosis.detail)
        if match:
            ai_recommended_action = match.group(1)

    now = datetime.utcnow()
    retry_interval_ok = True
    if txn.last_attempt_at is not None:
        retry_interval_ok = (now - txn.last_attempt_at).total_seconds() >= CONFIG.min_retry_interval_minutes * 60

    safety_checks = {
        "amount_within_auto_limit": txn.amount <= CONFIG.max_transaction_amount,
        "retry_limit_not_reached": txn.retry_count < CONFIG.max_retries,
        "min_retry_interval_elapsed": retry_interval_ok,
        "probability_above_threshold": (
            txn.last_recovery_probability >= CONFIG.min_recovery_probability
            if txn.last_recovery_probability is not None else None
        ),
    }

    return schemas.WhyOut(
        transaction=txn,
        reasoning=reasoning,
        ai_source=ai_source,
        ai_recommended_action=ai_recommended_action,
        safety_checks=safety_checks,
        final_action=txn.last_action.value if txn.last_action else None,
    )


# ---------------------------------------------------------------------------
# Razorpay real test-mode checkout (manual, per-transaction demo flow)
# ---------------------------------------------------------------------------
@app.post("/api/transactions/{txn_id}/checkout", response_model=schemas.CheckoutOrderOut)
def create_checkout(txn_id: str, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="transaction not found")
    if txn.status in (TransactionStatus.RECOVERED, TransactionStatus.STOPPED):
        raise HTTPException(status_code=400, detail="transaction workflow is already stopped")
    if txn.retry_count >= CONFIG.max_retries:
        raise HTTPException(status_code=400, detail="maximum retry limit reached")
    if txn.amount > CONFIG.max_transaction_amount:
        raise HTTPException(status_code=400, detail="amount exceeds auto-recovery limit")
    if txn.last_recovery_probability is None or txn.last_recovery_probability < CONFIG.min_recovery_probability:
        raise HTTPException(status_code=400, detail="recovery probability is below the auto-recovery threshold")

    try:
        order = razorpay_client.create_checkout_order(txn.amount, txn.currency, txn.customer_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_event(db, "CHECKOUT_ORDER_CREATED", f"Razorpay test order {order['order_id']} created", txn_id)
    return schemas.CheckoutOrderOut(**order)


@app.post("/api/transactions/{txn_id}/checkout/verify")
def verify_checkout(txn_id: str, req: schemas.CheckoutVerifyRequest, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="transaction not found")

    try:
        ok = razorpay_client.verify_checkout_signature(
            req.razorpay_order_id, req.razorpay_payment_id, req.razorpay_signature
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    attempt_number = txn.retry_count + 1
    if ok:
        attempt = RecoveryAttempt(
            transaction_id=txn.id,
            attempt_number=attempt_number,
            recovery_probability=txn.last_recovery_probability or 0.0,
            action_taken=ActionType.RETRY,
            reasoning="manual Razorpay test checkout, signature verified",
            razorpay_order_id=req.razorpay_order_id,
            razorpay_payment_id=req.razorpay_payment_id,
            outcome="SUCCESS",
            amount_recovered=txn.amount,
        )
        db.add(attempt)
        txn.retry_count = attempt_number
        txn.last_attempt_at = datetime.utcnow()
        txn.status = TransactionStatus.RECOVERED
        log_event(db, "RECOVERY_ATTEMPT", f"attempt #{attempt_number} via Razorpay checkout (live_test mode) -> captured", txn_id)
        log_event(db, "PAYMENT_RECOVERED", f"₹{txn.amount:,.2f} recovered via real Razorpay test checkout", txn_id)
        log_event(db, "WORKFLOW_STOPPED", "payment successful; workflow stopped", txn_id)
        db.commit()
        return {"status": "captured"}
    else:
        log_event(db, "PAYMENT_FAILED", "Razorpay signature verification failed", txn_id)
        db.commit()
        raise HTTPException(status_code=400, detail="signature verification failed")


@app.get("/api/config")
def get_config():
    return {
        "max_retries": CONFIG.max_retries,
        "min_retry_interval_minutes": CONFIG.min_retry_interval_minutes,
        "max_transaction_amount": CONFIG.max_transaction_amount,
        "min_recovery_probability": CONFIG.min_recovery_probability,
        "razorpay_mode": razorpay_client.mode,
        "diagnosis_mode": os.environ.get("RECOVERAI_DIAGNOSIS_MODE", "auto"),
    }


# ---------------------------------------------------------------------------
# Merchant recovery policy (bounded, hard-limit-enforced)
# ---------------------------------------------------------------------------
@app.get("/api/policy", response_model=schemas.PolicyOut)
def get_policy():
    return schemas.PolicyOut(
        max_retries=CONFIG.max_retries,
        min_retry_interval_minutes=CONFIG.min_retry_interval_minutes,
        max_transaction_amount=CONFIG.max_transaction_amount,
        min_recovery_probability=CONFIG.min_recovery_probability,
        hard_limits=HARD_LIMITS,
    )


@app.post("/api/policy", response_model=schemas.PolicyOut)
def update_policy(req: schemas.PolicyUpdate, db: Session = Depends(get_db)):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="no policy fields provided")

    errors = validate_policy_update(updates)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    apply_policy_update(CONFIG, updates)
    log_event(
        db, "POLICY_UPDATED",
        "merchant updated recovery policy: " + ", ".join(f"{k}={v}" for k, v in updates.items()),
    )
    return schemas.PolicyOut(
        max_retries=CONFIG.max_retries,
        min_retry_interval_minutes=CONFIG.min_retry_interval_minutes,
        max_transaction_amount=CONFIG.max_transaction_amount,
        min_recovery_probability=CONFIG.min_recovery_probability,
        hard_limits=HARD_LIMITS,
    )


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

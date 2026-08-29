# RecoverAI

**Track 3: AI Revenue Recovery** — an agent for merchants that detects revenue slipping away (failed payments, abandoned checkouts, overdue invoices) and recovers it through a *bounded*, auditable workflow.

RecoverAI detects revenue at risk, uses an AI diagnosis step to estimate recovery probability, makes a deterministic bounded decision (retry / remind / escalate / stop), executes that decision against Razorpay's test-mode payment flow, and logs and measures exactly what happened.

## Why "bounded" matters

The AI never has unlimited control over money. Every action passes through hard, non-negotiable rules (`backend/recovery_engine.py::BoundedRecoveryConfig`):

- Max **2** retries per transaction
- Min **30 minutes** between retries
- Max **₹10,000** auto-recovered per transaction (above this → escalate to a human)
- Stop immediately after a successful payment
- Stop immediately once the retry limit is hit

The AI only ever supplies a *recovery probability* and reasoning. Deterministic code decides what happens with it.

## Architecture

```
        React-style static dashboard (frontend/index.html)
                        │  fetch()
                        ▼
                 FastAPI backend (backend/main.py)
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   SQLite/Postgres   Recovery Engine   Razorpay Test API
   (transactions,     (AI diagnosis     (mock by default,
   attempts, audit)    + bounded         real SDK path
                        decision)        included)
```

- **AI diagnosis** (`ai_diagnosis.py` + `recovery_engine.py::diagnose`) — uses an optional Anthropic LLM for probability, reasoning, and an advisory action; when unavailable it falls back to a transparent weighted scorer. The AI is never authorized to move money.
- **Bounded decision** (`recovery_engine.py::decide`) — deterministic, rule-based. Enforces every limit above regardless of what the AI's probability says.
- **Razorpay integration** (`razorpay_client.py`) — defaults to a local mock that mirrors Razorpay's real test-mode response shapes (`order_id`, `payment_id`, `status`), so no real credentials are needed for the demo. A `live_test` mode using the real `razorpay` SDK against test keys is included and can be enabled via env var.
- **Audit trail** — every diagnosis, decision, attempt, and outcome is logged with a timestamp (`AuditLogEntry`) and streamed to the dashboard's live ticker.

## Quick start

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open **http://localhost:8000** — the dashboard is served directly by the backend.

1. Click **"Generate batch"** to create 1,000 synthetic transactions (failed payments, abandoned checkouts, overdue invoices).
2. Click **"Run recovery engine"** to run every transaction through diagnosis → decision → (bounded) action → Razorpay test attempt → audit log.
3. Watch the KPI strip, recovery funnel, charts, and live audit trail update.

### CLI-only demo (no server)

```bash
cd backend
python run_batch_demo.py --count 1000
```

Prints the batch results (revenue at risk, eligible amount, recovery rate, action breakdown) straight to the terminal — useful for generating pitch numbers quickly.

## Project layout

```
recoverai/
├── backend/
│   ├── main.py              FastAPI app & endpoints
│   ├── models.py            SQLAlchemy models (Transaction, RecoveryAttempt, AuditLogEntry)
│   ├── schemas.py           Pydantic response schemas
│   ├── database.py          DB session setup (SQLite by default, Postgres-ready)
│   ├── recovery_engine.py   AI diagnosis + bounded decision logic
│   ├── razorpay_client.py   Test-mode payment client (mock + real SDK path)
│   ├── data_generator.py    Synthetic transaction dataset generator
│   ├── run_batch_demo.py    Standalone CLI batch demo
│   └── requirements.txt
├── frontend/
│   └── index.html           Dashboard (vanilla JS + Chart.js, no build step)
└── README.md
```

## API reference

| Method | Path | Description |
|---|---|---|
| POST | `/api/data/generate` | Generate a synthetic transaction batch (`{count, reset}`) |
| POST | `/api/recovery/process` | Run the recovery engine over all pending transactions |
| GET | `/api/transactions` | List transactions (filter by `status`) |
| GET | `/api/attempts` | List individual recovery attempts |
| GET | `/api/audit-trail` | List audit log entries, newest first |
| GET | `/api/metrics` | Dashboard metrics (revenue at risk/recovered, recovery rate, breakdowns) |
| GET | `/api/config` | Current bounded-recovery configuration |

## Using real Razorpay test-mode credentials

By default `RECOVERAI_RAZORPAY_MODE=mock` (no credentials needed — safe for any demo environment). To use Razorpay's actual test-mode API:

```bash
export RECOVERAI_RAZORPAY_MODE=live_test
export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
pip install razorpay
```

Note: real Razorpay checkout normally requires a client-side widget or saved payment token to actually capture a payment; `live_test` mode creates the order and returns it so a checkout step can complete it. `mock` mode is what the dashboard demo drives by default and is what most judges will want to see in a live run.

## What's "must-have" vs "nice-to-have" here

**Built:** revenue-at-risk detection, optional LLM-backed AI diagnosis with deterministic fallback, bounded recovery decision, Razorpay test-mode integration (mock + real path), success/failure handling with stopping rules, full audit trail, recovery dashboard, batch metrics over a 1,000-row synthetic dataset.

**Not built (nice-to-have, time permitting):** subscription-specific recovery flows, a trained ML model in place of the weighted scorer, real email/SMS reminder delivery, more advanced cohort analytics.

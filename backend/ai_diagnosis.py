"""
LLM-backed diagnosis layer for RecoverAI.

The LLM is ADVISORY ONLY:
    LLM -> diagnosis/probability/recommendation
    recovery_engine.decide() -> deterministic safety authorization
    Razorpay -> execution

Modes:
    RECOVERAI_DIAGNOSIS_MODE=auto
        Use Anthropic when configured; otherwise use the rule-based fallback.

    RECOVERAI_DIAGNOSIS_MODE=llm
        Require Anthropic. Errors are surfaced to the caller.

    RECOVERAI_DIAGNOSIS_MODE=rule_based
        Skip the LLM entirely.

The parser is deliberately strict because model output must never be allowed
to become an unsafe financial action.
"""

import json
import os
from typing import Any, Dict, Optional

MODE = os.environ.get("RECOVERAI_DIAGNOSIS_MODE", "auto").lower().strip()
MODEL = os.environ.get(
    "RECOVERAI_DIAGNOSIS_MODEL",
    "claude-haiku-4-5-20251001",
)

ALLOWED_ACTIONS = {
    "RETRY",
    "REMINDER",
    "ESCALATE",
    "DONT_RETRY",
}

_client = None
_client_init_attempted = False


def _get_client():
    """Lazily create the Anthropic client."""
    global _client, _client_init_attempted

    if _client_init_attempted:
        return _client

    _client_init_attempted = True

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic

        _client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        _client = None

    return _client


_SYSTEM_PROMPT = """You are RecoverAI, a payment-recovery diagnosis assistant
for an Indian merchant using Razorpay.

Your job is ONLY to analyze one failed, abandoned, or overdue transaction.

Return:
1. probability: a number from 0.0 to 1.0 representing the estimated chance
   that an appropriate recovery intervention could recover the revenue.
2. reason: concise, evidence-based explanation using only the supplied signals.
3. recommended_action: exactly one of RETRY, REMINDER, ESCALATE, DONT_RETRY.

Use these principles:
- Temporary failures and strong prior payment history generally support RETRY.
- Abandoned checkout generally supports REMINDER.
- Repeated failures or uncertain/high-value cases may support ESCALATE.
- Weak recovery signals or repeated unsuccessful attempts may support DONT_RETRY.
- Never invent customer facts, payment details, or failure reasons.
- Never claim that a payment was made or recovered.
- Never instruct the system to bypass limits.

IMPORTANT:
Your recommendation is advisory. A separate deterministic safety engine decides
whether any financial action is actually authorized.

Return ONLY valid JSON:
{
  "probability": 0.0,
  "reason": "short explanation",
  "recommended_action": "RETRY"
}
"""


def _build_user_prompt(txn) -> str:
    """Build a minimal, non-sensitive transaction context for the model."""
    failure_type = getattr(txn.failure_type, "value", str(txn.failure_type))

    lines = [
        f"Failure type: {failure_type}",
        f"Amount: INR {txn.amount:,.2f}",
        f"Currency: {txn.currency}",
        f"Existing customer: {txn.customer_is_existing}",
        f"Previous successful payments: {txn.previous_successful_payments}",
        f"Previous failed attempts on this workflow: {txn.previous_failed_attempts}",
    ]

    if failure_type == "INVOICE_OVERDUE":
        lines.append(f"Days overdue: {txn.days_overdue}")

    return "Transaction signals:\n" + "\n".join(
        f"- {line}" for line in lines
    )


def _extract_json_object(text: str) -> str:
    """Handle accidental markdown/code-fence output without accepting prose."""
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    # If the model added a tiny amount of surrounding text, extract the
    # outermost JSON object. The object itself is still validated strictly.
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM response did not contain a JSON object.")

    return text[start:end + 1]


def _parse_llm_json(text: str) -> Dict[str, Any]:
    """Validate and normalize model output."""
    data = json.loads(_extract_json_object(text))

    probability = float(data["probability"])
    reason = str(data["reason"]).strip()
    recommended_action = str(
        data.get("recommended_action", "")
    ).upper().strip()

    if not 0.0 <= probability <= 1.0:
        raise ValueError(
            f"probability must be between 0 and 1, got {probability}"
        )

    if not reason:
        raise ValueError("LLM returned an empty reason.")

    if recommended_action not in ALLOWED_ACTIONS:
        raise ValueError(
            f"invalid recommended_action: {recommended_action}"
        )

    # Keep the model output bounded for logs/UI.
    reason = reason[:500]

    return {
        "probability": probability,
        "reason": f"[LLM] {reason}",
        "recommended_action": recommended_action,
    }


def llm_diagnose(txn) -> Optional[Dict[str, Any]]:
    """
    Return a validated advisory diagnosis.

    In auto mode, None means "use the deterministic fallback".
    In llm mode, configuration/API errors are raised so the caller knows
    that the requested LLM mode could not be satisfied.
    """
    if MODE == "rule_based":
        return None

    if MODE not in {"auto", "llm"}:
        raise RuntimeError(
            "RECOVERAI_DIAGNOSIS_MODE must be auto, llm, or rule_based."
        )

    client = _get_client()

    if client is None:
        if MODE == "llm":
            raise RuntimeError(
                "LLM mode requires ANTHROPIC_API_KEY and the anthropic package."
            )
        return None

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=250,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(txn),
                }
            ],
        )

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )

        if not text:
            raise ValueError("LLM returned an empty response.")

        return _parse_llm_json(text)

    except Exception as exc:
        if MODE == "llm":
            raise RuntimeError(
                f"LLM diagnosis failed: {exc}"
            ) from exc

        # auto mode: fall back safely per transaction.
        return None

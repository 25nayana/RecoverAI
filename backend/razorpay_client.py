"""
Razorpay test-mode integration layer.

RECOVERAI_RAZORPAY_MODE=mock (default)
    Simulates the Razorpay Orders/Payments test-mode API locally. No network
    calls, no real credentials needed. Response shapes mirror Razorpay's
    actual test-mode payloads (order_id / payment_id / status) so swapping
    to live test-mode later is a one-line change.

RECOVERAI_RAZORPAY_MODE=live_test
    Uses the real `razorpay` Python SDK against Razorpay's TEST-mode keys
    (rzp_test_...). Requires RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET env vars
    and the `razorpay` package installed. No real money moves in test mode.
"""
import os
import random
import time
import uuid


MODE = os.environ.get("RECOVERAI_RAZORPAY_MODE", "mock")


class RazorpayClient:
    """Thin wrapper so recovery_engine.py never has to know which mode it's in."""

    def __init__(self):
        self.mode = MODE
        if self.mode == "live_test":
            try:
                import razorpay  # noqa: F401
                key_id = os.environ["RAZORPAY_KEY_ID"]
                key_secret = os.environ["RAZORPAY_KEY_SECRET"]
                self._client = razorpay.Client(auth=(key_id, key_secret))
            except Exception as e:
                raise RuntimeError(
                    "RECOVERAI_RAZORPAY_MODE=live_test requires the `razorpay` "
                    "package and RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET test keys. "
                    f"Falling back is disabled to avoid silent mock behavior. Error: {e}"
                )

    def attempt_payment(self, amount_rupees: float, currency: str, success_probability: float, customer_id: str):
        """
        Attempts to recover a payment. Returns a dict shaped like Razorpay's
        order+payment response:
            { order_id, payment_id, status: 'captured' | 'failed', amount, currency }
        """
        if self.mode == "mock":
            return self._mock_attempt(amount_rupees, currency, success_probability)
        return self._live_test_attempt(amount_rupees, currency, customer_id)

    # ---- mock implementation --------------------------------------------------
    def _mock_attempt(self, amount_rupees: float, currency: str, success_probability: float):
        order_id = f"order_MOCK{uuid.uuid4().hex[:14]}"
        time.sleep(0)  # placeholder for realistic latency if desired
        success = random.random() < success_probability
        payment_id = f"pay_MOCK{uuid.uuid4().hex[:14]}"
        return {
            "order_id": order_id,
            "payment_id": payment_id if success else None,
            "status": "captured" if success else "failed",
            "amount": int(round(amount_rupees * 100)),  # paise, like real Razorpay
            "currency": currency,
            "mode": "mock",
        }

    # ---- real Razorpay test-mode implementation --------------------------------
    def _live_test_attempt(self, amount_rupees: float, currency: str, customer_id: str):
        order = self._client.order.create({
            "amount": int(round(amount_rupees * 100)),
            "currency": currency,
            "notes": {"customer_id": customer_id, "source": "recoverai_recovery_engine"},
        })
        # NOTE: capturing a real payment normally requires a client-side checkout
        # flow (or saved token) to produce a payment_id. In test mode, teams
        # typically simulate this with Razorpay's test card numbers via the
        # checkout widget. This method returns the created order so the demo
        # frontend can complete the test-mode checkout and report back.
        return {
            "order_id": order["id"],
            "payment_id": None,
            "status": "created",
            "amount": order["amount"],
            "currency": order["currency"],
            "mode": "live_test",
        }

    # ---- client-facing checkout flow (used by the "pay now" demo button) ------
    def create_checkout_order(self, amount_rupees: float, currency: str, customer_id: str):
        """
        Creates a real Razorpay TEST order for the browser Checkout.js widget
        to open against. Only valid in live_test mode -- the mock client has
        no server to create a real order with, so it raises instead of
        silently returning a fake order that Checkout.js can't actually use.
        """
        if self.mode != "live_test":
            raise RuntimeError(
                "Live checkout requires RECOVERAI_RAZORPAY_MODE=live_test "
                "with real Razorpay TEST keys configured."
            )
        order = self._client.order.create({
            "amount": int(round(amount_rupees * 100)),
            "currency": currency,
            "notes": {"customer_id": customer_id, "source": "recoverai_manual_checkout_demo"},
        })
        return {
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": os.environ.get("RAZORPAY_KEY_ID"),
        }

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """
        Verifies the HMAC-SHA256 signature Razorpay's Checkout.js returns
        after a successful test payment. This is the step that actually
        proves the payment happened -- without it, the frontend could just
        claim success without Razorpay ever having confirmed anything.
        """
        if self.mode != "live_test":
            raise RuntimeError("Signature verification requires live_test mode.")
        try:
            self._client.utility.verify_payment_signature({
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            })
            return True
        except Exception:
            return False


razorpay_client = RazorpayClient()

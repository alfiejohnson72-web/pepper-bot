"""
Payments. Stripe is the provider today; others can be added by writing another
class with the same four methods and adding it to PROVIDERS.

Rules this module follows:
  * An order is only marked paid after the provider itself confirms it: either a
    webhook whose signature checks out, or a direct API lookup of the session.
    A Telegram message or a screenshot never marks anything paid.
  * Keys only come from environment variables (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

import store

log = logging.getLogger("payments")


@dataclass
class CheckoutSession:
    ref: str            # provider's id for this checkout page
    url: str
    expires_at: int | None


@dataclass
class Verified:
    """What the provider says about a checkout session."""
    ref: str
    status: str         # paid, pending, expired, failed
    amount_pence: int
    payment_id: str | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    address: str | None = None


class PaymentError(Exception):
    pass


class StripeProvider:
    name = "stripe"

    def __init__(self) -> None:
        import stripe  # imported here so the bot still runs without Stripe set up
        self.stripe = stripe
        self.client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("STRIPE_SECRET_KEY"))

    @property
    def test_mode(self) -> bool:
        return os.getenv("STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_"))

    def create_checkout(self, order: sqlite3.Row, items: list[sqlite3.Row], bot_username: str) -> CheckoutSession:
        line_items = [{
            "quantity": it["qty"],
            "price_data": {"currency": "gbp", "unit_amount": it["unit_pence"], "product_data": {"name": it["name"]}},
        } for it in items]
        params = {
            "mode": "payment",
            "line_items": line_items,
            "client_reference_id": order["ref"],
            "metadata": {"order_ref": order["ref"], "order_id": str(order["id"]), "telegram_user_id": str(order["user_id"])},
            "payment_intent_data": {"metadata": {"order_ref": order["ref"]}},
            "shipping_address_collection": {"allowed_countries": ["GB"]},
            "success_url": f"https://t.me/{bot_username}?start=paid_{order['ref']}",
            "cancel_url": f"https://t.me/{bot_username}?start=basket",
        }
        if order["delivery_pence"]:
            params["shipping_options"] = [{"shipping_rate_data": {
                "type": "fixed_amount", "display_name": "Royal Mail Tracked 48",
                "fixed_amount": {"amount": order["delivery_pence"], "currency": "gbp"}}}]
        else:
            params["shipping_options"] = [{"shipping_rate_data": {
                "type": "fixed_amount", "display_name": "Free delivery",
                "fixed_amount": {"amount": 0, "currency": "gbp"}}}]
        try:
            # The idempotency key means a double tap never creates two payment pages for one order.
            s = self.client.v1.checkout.sessions.create(
                params=params, options={"idempotency_key": f"order-{order['ref']}-{order['total_pence']}-{len(store.order_payments(order['id']))}"})
        except self.stripe.StripeError as e:
            log.error("Stripe checkout failed: %s", e)
            raise PaymentError("couldnt open the payment page") from e
        return CheckoutSession(ref=s.id, url=s.url, expires_at=s.expires_at)

    def _from_session(self, s) -> Verified:
        if s.payment_status in ("paid", "no_payment_required") and s.status == "complete":
            status = "paid"
        elif s.status == "expired":
            status = "expired"
        else:
            status = "pending"
        details = getattr(s, "collected_information", None)
        ship = (getattr(details, "shipping_details", None) if details else None) or getattr(s, "shipping_details", None)
        cust = s.customer_details
        addr = None
        if ship and ship.address:
            a = ship.address
            addr = ", ".join(x for x in [ship.name, a.line1, a.line2, a.city, a.state, a.postal_code, a.country] if x)
        elif cust and cust.address and cust.address.line1:
            a = cust.address
            addr = ", ".join(x for x in [cust.name, a.line1, a.line2, a.city, a.state, a.postal_code, a.country] if x)
        pi = s.payment_intent
        return Verified(
            ref=s.id, status=status, amount_pence=s.amount_total or 0,
            payment_id=pi if isinstance(pi, str) else (pi.id if pi else None),
            customer_name=(cust.name if cust else None) or (ship.name if ship else None),
            customer_email=cust.email if cust else None,
            address=addr,
        )

    def verify(self, ref: str) -> Verified:
        """Ask Stripe directly about a checkout session."""
        try:
            s = self.client.v1.checkout.sessions.retrieve(ref)
        except self.stripe.StripeError as e:
            raise PaymentError("couldnt reach stripe") from e
        return self._from_session(s)

    def parse_webhook(self, payload: bytes, signature: str):
        """Check the webhook really came from Stripe. Raises PaymentError if not."""
        if not self.webhook_secret:
            raise PaymentError("STRIPE_WEBHOOK_SECRET is not set")
        try:
            return self.client.construct_event(payload, signature, self.webhook_secret)
        except (ValueError, self.stripe.SignatureVerificationError) as e:
            raise PaymentError("bad signature") from e

    def refund(self, payment_id: str, amount_pence: int | None, idem: str) -> None:
        params = {"payment_intent": payment_id}
        if amount_pence:
            params["amount"] = amount_pence
        try:
            self.client.v1.refunds.create(params=params, options={"idempotency_key": idem})
        except self.stripe.StripeError as e:
            raise PaymentError(str(getattr(e, "user_message", None) or e)) from e


PROVIDERS = {"stripe": StripeProvider}
_provider: StripeProvider | None = None


def provider() -> StripeProvider | None:
    """The active provider, or None if payments aren't set up yet."""
    global _provider
    if not store.get_flag("payments_enabled"):
        return None
    if _provider is None and StripeProvider.configured():
        _provider = StripeProvider()
    return _provider


def reset() -> None:
    global _provider
    _provider = None


def checkout_link(order: sqlite3.Row, bot_username: str) -> str:
    """Get a payment page for this order, reusing a still-open one so customers never pay twice."""
    p = provider()
    if p is None:
        raise PaymentError("payments not set up")
    existing = store.open_payment_session(order["id"])
    if existing and existing["amount_pence"] == order["total_pence"]:
        return existing["checkout_url"]
    session = p.create_checkout(order, store.order_items(order["id"]), bot_username)
    store.record_payment_session(order["id"], p.name, session.ref, session.url, order["total_pence"], session.expires_at)
    return session.url


def apply_verified(v: Verified, provider_name: str = "stripe") -> tuple[sqlite3.Row | None, bool]:
    """Apply a provider-confirmed result to the order. Returns (order, newly_paid)."""
    if v.status == "paid":
        return store.mark_paid(provider_name, v.ref, amount_pence=v.amount_pence, payment_id=v.payment_id,
                               customer_name=v.customer_name, customer_email=v.customer_email, address=v.address)
    if v.status in ("expired", "failed"):
        return store.mark_payment_failed(provider_name, v.ref, v.status), False
    return None, False


def recheck_order(order: sqlite3.Row) -> tuple[sqlite3.Row | None, bool]:
    """Customer tapped "I've paid": look it up with the provider. Never trusts the customer's word."""
    p = provider()
    pay = store.latest_payment(order["id"])
    if p is None or pay is None:
        return order, False
    return apply_verified(p.verify(pay["provider_ref"]), p.name)

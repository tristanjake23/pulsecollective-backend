"""PayMongo sandbox checkout; cannot accept live keys or settle real invoices."""

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb

from ..auth import current_account, require
from ..config import settings
from ..db import connection, one
from ..limits import throttle
from ..models import Model

router = APIRouter(tags=["Patient test payments"])
API_URL = "https://api.paymongo.com/v2/checkout_sessions"
SESSION_ID = re.compile(r"cs_[A-Za-z0-9]+\Z")
PAYMENT_ID = re.compile(r"pay_[A-Za-z0-9]+\Z")
OWN_INVOICE = """
    SELECT i.id,i.clinic_id,i.invoice_no,i.due_date,i.amount,i.status,
           b.balance_due,c.name clinic_name
    FROM public.invoices i
    JOIN public.patients p ON p.id=i.patient_id AND p.clinic_id=i.clinic_id
    JOIN public.invoice_balances b ON b.id=i.id AND b.clinic_id=i.clinic_id
    JOIN public.clinics c ON c.id=i.clinic_id
    WHERE i.id=%s AND p.account_id=%s
"""


class CheckoutRequest(Model):
    """Amount, account, clinic and return URL can never come from the browser."""


def configured():
    parsed = urlsplit(settings.payment_return_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return bool(
        settings.paymongo_test_enabled
        and settings.paymongo_test_key.startswith("sk_test_")
        and settings.paymongo_test_webhook_secret
        and parsed.scheme == "https"
        and not parsed.username and not parsed.password
        and origin in settings.cors_origins
        and settings.paymongo_test_methods
        and set(settings.paymongo_test_methods) <= {"qrph", "gcash", "card", "paymaya"}
    )


def require_config():
    if not configured():
        raise HTTPException(503, "Online test payments are not enabled yet. Contact the clinic.")


def minor_amount(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
        raise HTTPException(409, "This invoice has no payable balance.")
    return int(amount)


def safe_checkout_url(value):
    if not isinstance(value, str):
        raise HTTPException(502, "Payment checkout returned an invalid address.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "checkout.paymongo.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise HTTPException(502, "Payment checkout returned an invalid address.")
    return value


def return_url(attempt_id, outcome):
    parsed = urlsplit(settings.payment_return_url)
    fragment = "portal?" + urlencode({"checkout": str(attempt_id), "payment": outcome})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", fragment))


def checkout_payload(attempt_id, amount):
    return {"data": {"attributes": {
        "line_items": [{"name": "Pulse Collective TEST payment", "amount": amount,
                        "currency": "PHP", "quantity": 1}],
        "payment_method_types": list(settings.paymongo_test_methods),
        "success_url": return_url(attempt_id, "returned"),
        "cancel_url": return_url(attempt_id, "cancelled"),
        "reference_number": "PC_TEST_" + str(attempt_id),
        "send_email_receipt": False,
        "pass_on_fees": False,
    }}}


def public_attempt(row):
    return {
        "id": row["id"], "invoice_id": row["invoice_id"],
        "state": row["state"], "test_mode": True,
        "amount": Decimal(row["amount_minor"]) / 100,
        "currency": "PHP", "payment_reference": row["payment_id"],
        "confirmed_at": row["confirmed_at"],
        "checkout_url": row["checkout_url"] if row["state"] == "pending" else None,
    }


@router.get("/patient/invoices")
def patient_invoices(request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    # The browser never gets clinic-wide invoice grants. The trusted backend
    # connection uses an explicit ownership join on the provider-verified user ID.
    with connection(request, readonly=True) as conn:
        rows = conn.execute("""
            SELECT b.id,b.invoice_no,b.issued_date,b.due_date,b.amount,
                   b.paid_amount,b.balance_due,b.status,c.name clinic_name
            FROM public.invoice_balances b
            JOIN public.patients p ON p.id=b.patient_id AND p.clinic_id=b.clinic_id
            JOIN public.clinics c ON c.id=b.clinic_id
            WHERE p.account_id=%s ORDER BY b.issued_date DESC,b.id LIMIT 100
        """, (user.id,)).fetchall()
    return {"invoices": rows, "test_checkout_available": configured()}


def reserve_checkout(request, user, invoice_id):
    fingerprint = hashlib.sha256(settings.paymongo_test_key.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    with connection(request) as conn:
        invoice = one(conn, OWN_INVOICE + " FOR UPDATE OF i", (invoice_id, user.id))
        if invoice["status"] == "paid":
            raise HTTPException(409, "This invoice is already paid.")
        amount = minor_amount(invoice["balance_due"])
        row = conn.execute("""
            SELECT * FROM private.paymongo_test_checkouts
            WHERE invoice_id=%s AND account_id=%s AND state <> 'failed'
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE
        """, (invoice_id, user.id)).fetchone()
        if row:
            if row["state"] == "test_paid":
                return row, False
            if row["amount_minor"] != amount or row["key_fingerprint"] != fingerprint:
                raise HTTPException(409, "The invoice or payment configuration changed. Contact the clinic before retrying.")
            if row["state"] == "pending":
                return row, False
            if row["state"] == "review" or now - row["created_at"] >= timedelta(hours=23):
                raise HTTPException(409, "This checkout needs clinic review before another attempt.")
            if row["lease_until"] > now:
                raise HTTPException(409, "Checkout is being prepared. Wait a moment before retrying.")
            conn.execute("UPDATE private.paymongo_test_checkouts SET lease_until=%s WHERE id=%s",
                         (now + timedelta(seconds=35), row["id"]))
            return row, True
        attempt_id = uuid4()
        row = one(conn, """
            INSERT INTO private.paymongo_test_checkouts
                (id,invoice_id,account_id,amount_minor,request_payload,key_fingerprint,lease_until)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (attempt_id, invoice_id, user.id, amount, Jsonb(checkout_payload(attempt_id, amount)),
              fingerprint, now + timedelta(seconds=35)))
        return row, True


@router.post("/patient/invoices/{invoice_id}/checkout")
def create_checkout(invoice_id: UUID, body: CheckoutRequest, request: Request,
                    user=Depends(current_account)):
    require(user, {"patient"})
    require_config()
    throttle(request, "test-checkout", 5, subject=str(user.id))
    row, create = reserve_checkout(request, user, invoice_id)
    if not create:
        return public_attempt(row)
    # No DB transaction stays open during the provider request. A retry uses the
    # same persisted payload/key, so timeouts cannot silently create duplicates.
    try:
        response = request.app.state.http.post(
            API_URL, auth=(settings.paymongo_test_key, ""),
            headers={"Idempotency-Key": str(row["id"])},
            json=row["request_payload"], timeout=httpx.Timeout(12, connect=5),
        )
    except httpx.HTTPError:
        raise HTTPException(503, "Checkout was not confirmed. Wait 35 seconds, then retry; the same request will be reused.") from None
    if response.status_code not in (200, 201):
        # A clear validation/auth failure did not create a checkout. Timeouts,
        # conflicts, throttles and 5xx keep the original idempotent attempt.
        if response.status_code in (400, 401, 403, 404, 422):
            with connection(request) as conn:
                conn.execute("UPDATE private.paymongo_test_checkouts SET state='failed' WHERE id=%s AND state='creating'",
                             (row["id"],))
        raise HTTPException(503, "PayMongo could not prepare test checkout. Contact the clinic or retry later.")
    try:
        data = response.json()["data"]
        attrs = data["attributes"]
        provider_id = data["id"]
        if not isinstance(provider_id, str) or not SESSION_ID.fullmatch(provider_id) or attrs.get("livemode") is not False:
            raise ValueError()
        url = safe_checkout_url(attrs["checkout_url"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(502, "PayMongo returned an incomplete test checkout. Contact the clinic before retrying.") from None
    with connection(request) as conn:
        stored = one(conn, """
            UPDATE private.paymongo_test_checkouts
            SET provider_session_id=%s,checkout_url=%s,
                state=CASE WHEN state='test_paid' THEN state ELSE 'pending' END
            WHERE id=%s AND (provider_session_id IS NULL OR provider_session_id=%s)
            RETURNING *
        """, (provider_id, url, row["id"], provider_id))
    return public_attempt(stored)


@router.get("/patient/checkouts/{attempt_id}")
def checkout_status(attempt_id: UUID, request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    throttle(request, "test-checkout-status", 20, subject=str(user.id))
    with connection(request, readonly=True) as conn:
        row = one(conn, """
            SELECT t.* FROM private.paymongo_test_checkouts t
            JOIN public.invoices i ON i.id=t.invoice_id
            JOIN public.patients p ON p.id=i.patient_id AND p.clinic_id=i.clinic_id
            WHERE t.id=%s AND t.account_id=%s AND p.account_id=%s
        """, (attempt_id, user.id, user.id))
    return public_attempt(row)


def verify_signature(raw, signature, secret, now=None):
    try:
        pieces = signature.split(",")
        fields = dict(piece.strip().split("=", 1) for piece in pieces)
        if len(fields) != len(pieces):
            raise ValueError()
        timestamp = fields["t"]
        if not timestamp.isdigit() or abs((time.time() if now is None else now) - int(timestamp)) > 300:
            raise ValueError()
        expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, fields.get("te", "")):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid test payment webhook signature.") from None


def paid_event(payload):
    """Normalize the documented original and new webhook envelopes."""
    try:
        event = payload["data"]
        event = event.get("attributes", event)
        if event["type"] != "checkout_session.payment.paid":
            return None
        if event.get("livemode") is not False:
            raise ValueError()
        checkout = event["data"]
        session_id = checkout["id"]
        attrs = checkout["attributes"]
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or attrs.get("livemode", False) is not False:
            raise ValueError()
        reference = attrs["reference_number"]
        if not isinstance(reference, str) or not reference.startswith("PC_TEST_"):
            raise ValueError()
        attempt_id = UUID(reference[len("PC_TEST_"):])
        paid = [p for p in attrs["payments"] if p["attributes"].get("status") == "paid"]
        if len(paid) != 1:
            raise ValueError()
        payment = paid[0]
        payment_attrs = payment["attributes"]
        amount = payment_attrs["amount"]
        if (not isinstance(payment["id"], str) or not PAYMENT_ID.fullmatch(payment["id"])
                or payment_attrs.get("livemode") is not False
                or payment_attrs.get("currency") != "PHP"
                or type(amount) is not int or amount <= 0):
            raise ValueError()
        return attempt_id, session_id, payment["id"], amount
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, "Invalid test payment event.") from None


def confirm_event(request, event):
    attempt_id, session_id, payment_id, amount = event
    with connection(request) as conn:
        row = one(conn, "SELECT * FROM private.paymongo_test_checkouts WHERE id=%s FOR UPDATE", (attempt_id,))
        if row["provider_session_id"] not in (None, session_id) or row["amount_minor"] != amount:
            raise HTTPException(409, "Test payment details do not match the checkout.")
        if row["state"] == "test_paid":
            if row["payment_id"] != payment_id:
                raise HTTPException(409, "Conflicting test payment reference.")
            return
        if row["state"] not in ("creating", "pending"):
            raise HTTPException(409, "This checkout requires clinic review.")
        conn.execute("""
            UPDATE private.paymongo_test_checkouts
            SET state='test_paid',provider_session_id=%s,payment_id=%s,confirmed_at=now()
            WHERE id=%s
        """, (session_id, payment_id, attempt_id))
        # Deliberately no INSERT into public.payments or UPDATE public.invoices.


@router.post("/payments/paymongo/test-webhook")
async def webhook(request: Request):
    require_config()
    raw = await request.body()
    verify_signature(raw, request.headers.get("paymongo-signature", ""), settings.paymongo_test_webhook_secret)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid webhook JSON.") from None
    event = paid_event(payload)
    if event is not None:
        # Keep blocking database work off the event loop.
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(confirm_event, request, event)
    return {"received": True}

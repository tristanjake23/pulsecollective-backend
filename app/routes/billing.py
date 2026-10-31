from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from ..auth import current_user, require, ADMIN, BILLING
from ..db import connection, one, insert, delete, update
from ..models import Invoice, Payment

router = APIRouter(tags=["Billing and reports"])


def detail(conn, row_id, clinic_id):
    row = one(
        conn,
        "SELECT * FROM public.invoice_balances WHERE id=%s AND clinic_id=%s",
        (row_id, clinic_id),
    )
    clinic = one(conn, "SELECT name FROM public.clinics WHERE id=%s", (clinic_id,))
    row["clinic_name"] = clinic["name"]
    row["patient_details"] = one(
        conn,
        "SELECT id,patient_code,full_name,phone,address FROM public.patients WHERE id=%s AND clinic_id=%s",
        (row["patient_id"], clinic_id),
    )
    row["items"] = conn.execute(
        "SELECT * FROM public.invoice_items WHERE invoice_id=%s AND clinic_id=%s ORDER BY id",
        (row_id, clinic_id),
    ).fetchall()
    row["payments"] = conn.execute(
        "SELECT * FROM public.payments WHERE invoice_id=%s AND clinic_id=%s ORDER BY paid_at",
        (row_id, clinic_id),
    ).fetchall()
    return row


@router.get("/invoices")
def invoices(
    request: Request, user=Depends(current_user), limit: int = Query(100, ge=1, le=200)
):
    require(user, BILLING)
    with connection(request, user, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM public.invoice_balances WHERE clinic_id=%s ORDER BY issued_date DESC,id LIMIT %s",
            (user.clinic_id, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        items = (
            conn.execute(
                "SELECT * FROM public.invoice_items WHERE invoice_id=ANY(%s::uuid[]) AND clinic_id=%s",
                (ids, user.clinic_id),
            ).fetchall()
            if ids
            else []
        )
        for r in rows:
            r["items"] = [i for i in items if i["invoice_id"] == r["id"]]
        return rows


@router.get("/invoices/{row_id}")
def invoice_read(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, BILLING)
    with connection(request, user, readonly=True) as conn:
        return detail(conn, row_id, user.clinic_id)


@router.post("/invoices", status_code=201)
def invoice_create(body: Invoice, request: Request, user=Depends(current_user)):
    require(user, BILLING)
    with connection(request, user) as conn:
        header = insert(
            conn,
            "invoices",
            {
                "clinic_id": user.clinic_id,
                "patient_id": body.patient_id,
                "due_date": body.due_date,
                "amount": 0,
            },
        )
        for item in body.items:
            insert(
                conn,
                "invoice_items",
                item.model_dump()
                | {"clinic_id": user.clinic_id, "invoice_id": header["id"]},
            )
        return detail(conn, header["id"], user.clinic_id)


@router.put("/invoices/{row_id}")
def invoice_edit(
    row_id: UUID, body: Invoice, request: Request, user=Depends(current_user)
):
    require(user, ADMIN)
    with connection(request, user) as conn:
        row = one(
            conn,
            "SELECT * FROM public.invoices WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        if row["patient_id"] != body.patient_id:
            raise HTTPException(409, "Invoice patient cannot be changed.")
        if (
            row["status"] == "paid"
            or conn.execute(
                "SELECT 1 FROM public.payments WHERE invoice_id=%s", (row_id,)
            ).fetchone()
        ):
            raise HTTPException(409, "Invoices with payments are locked.")
        conn.execute(
            "DELETE FROM public.invoice_items WHERE invoice_id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
        update(conn, "invoices", row_id, user.clinic_id, {"due_date": body.due_date})
        for item in body.items:
            insert(
                conn,
                "invoice_items",
                item.model_dump() | {"clinic_id": user.clinic_id, "invoice_id": row_id},
            )
        return detail(conn, row_id, user.clinic_id)


@router.delete("/invoices/{row_id}")
def invoice_delete(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    with connection(request, user) as conn:
        row = one(
            conn,
            "SELECT * FROM public.invoices WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        if (
            row["status"] == "paid"
            or conn.execute(
                "SELECT 1 FROM public.payments WHERE invoice_id=%s", (row_id,)
            ).fetchone()
        ):
            raise HTTPException(409, "Invoices with payments cannot be deleted.")
        conn.execute(
            "DELETE FROM public.invoice_items WHERE invoice_id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
        return delete(conn, "invoices", row_id, user.clinic_id)


@router.post("/invoices/{row_id}/payments", status_code=201)
def payment_create(
    row_id: UUID, body: Payment, request: Request, user=Depends(current_user)
):
    require(user, BILLING)
    with connection(request, user) as conn:
        # Serializes all payment callers for this invoice. The DB trigger also checks limits.
        one(
            conn,
            "SELECT id FROM public.invoices WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        existing = conn.execute(
            "SELECT * FROM public.payments WHERE clinic_id=%s AND reference=%s",
            (user.clinic_id, body.reference),
        ).fetchone()
        if existing:
            if (
                existing["invoice_id"] != row_id
                or existing["amount"] != body.amount
                or existing["method"] != body.method
            ):
                raise HTTPException(
                    409, "Payment reference is already used for different details."
                )
        else:
            conn.execute(
                "CALL public.record_payment(%s,%s,%s,%s,%s)",
                (user.clinic_id, row_id, body.amount, body.method, body.reference),
            )
        return detail(conn, row_id, user.clinic_id)


@router.get("/dashboard")
def dashboard(request: Request, user=Depends(current_user)):
    with connection(request, user, readonly=True) as conn:
        return one(
            conn,
            "SELECT * FROM public.dashboard_stats WHERE clinic_id=%s",
            (user.clinic_id,),
        )

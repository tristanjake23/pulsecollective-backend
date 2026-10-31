"""Patient-only bookings; the private SQL bridge checks ownership atomically."""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import Field, model_validator
from ..auth import current_account, current_user, require, ADMIN
from ..db import connection, one
from ..models import Model
from ..limits import throttle
from psycopg.types.json import Jsonb

router = APIRouter(tags=["Patient booking and availability"])


class Booking(Model):
    doctor_id: UUID
    mode: Literal["scheduled", "now"] = "scheduled"
    schedule_id: UUID | None = None
    reason: str = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def slot_required(self):
        if self.mode == "scheduled" and not self.schedule_id:
            raise ValueError("Choose an available slot")
        return self


@router.get("/patient/appointments")
def mine(request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    with connection(request, user, readonly=True) as conn:
        return conn.execute(
            "SELECT private.patient_appointments('list') data"
        ).fetchone()["data"]


def write_booking(request, user, action, body=None, row_id=None):
    require(user, {"patient"})
    throttle(request, "patient-booking", 8, subject=str(user.id))
    with connection(request, user) as conn:
        return conn.execute(
            "SELECT private.patient_appointments(%s,%s,%s) data",
            (action, row_id, Jsonb(body.model_dump(mode="json") if body else {})),
        ).fetchone()["data"]


@router.post("/patient/appointments", status_code=201)
def book(body: Booking, request: Request, user=Depends(current_account)):
    return write_booking(request, user, "book", body)


@router.post("/patient/appointments/{row_id}/cancel")
def cancel(row_id: UUID, request: Request, user=Depends(current_account)):
    return write_booking(request, user, "cancel", row_id=row_id)


@router.post("/patient/appointments/{row_id}/reschedule")
def reschedule(
    row_id: UUID, body: Booking, request: Request, user=Depends(current_account)
):
    return write_booking(request, user, "reschedule", body, row_id)


class Availability(Model):
    available: bool


@router.post("/doctors/{row_id}/availability")
def availability(
    row_id: UUID, body: Availability, request: Request, user=Depends(current_user)
):
    require(user, {"doctor", "clinic_admin"})
    throttle(request, "availability", 30, subject=str(user.id))
    # Owner connection only after explicit verified role/clinic/doctor ownership checks.
    with connection(request) as conn:
        doctor = one(
            conn,
            "SELECT id,profile_id FROM public.doctors WHERE id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
        if user.role == "doctor" and doctor["profile_id"] != user.id:
            raise HTTPException(403, "You can change only your own availability.")
        return one(
            conn,
            "UPDATE public.doctors SET accepting_until=CASE WHEN %s THEN now()+interval '3 minutes' ELSE NULL END WHERE id=%s RETURNING id,accepting_until",
            (body.available, row_id),
        )


@router.get("/admin/doctor-accounts")
def doctor_accounts(request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    with connection(request, readonly=True) as conn:
        return conn.execute(
            "SELECT id,full_name FROM public.profiles WHERE clinic_id=%s AND role='doctor' ORDER BY full_name",
            (user.clinic_id,),
        ).fetchall()

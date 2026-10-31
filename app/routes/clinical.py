from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from ..auth import current_user, require, ADMIN, CLINICAL
from ..db import connection, one, insert, update, delete
from ..models import (
    Appointment,
    AppointmentUpdate,
    Consultation,
    ConsultationUpdate,
    Prescription,
    TriageInput,
)

router = APIRouter(tags=["Appointments and clinical care"])
APPOINTMENT_SQL = "SELECT a.*,p.full_name patient_name,s.doctor_id,d.full_name doctor_name,s.starts_at,s.ends_at FROM public.appointments a JOIN public.patients p ON p.id=a.patient_id JOIN public.doctor_schedules s ON s.id=a.schedule_id JOIN public.doctors d ON d.id=s.doctor_id"
CONSULTATION_SQL = "SELECT c.*,a.patient_id,p.full_name patient_name,s.doctor_id,d.full_name doctor_name FROM public.consultations c JOIN public.appointments a ON a.id=c.appointment_id JOIN public.patients p ON p.id=a.patient_id JOIN public.doctor_schedules s ON s.id=a.schedule_id JOIN public.doctors d ON d.id=s.doctor_id"


@router.get("/appointments")
def appointments(
    request: Request, user=Depends(current_user), limit: int = Query(100, ge=1, le=200)
):
    with connection(request, user, readonly=True) as conn:
        return conn.execute(
            APPOINTMENT_SQL
            + " WHERE a.clinic_id=%s ORDER BY s.starts_at DESC LIMIT %s",
            (user.clinic_id, limit),
        ).fetchall()


@router.post("/appointments", status_code=201)
def book(body: Appointment, request: Request, user=Depends(current_user)):
    with connection(request, user) as conn:
        one(
            conn,
            "SELECT id FROM public.patients WHERE id=%s AND clinic_id=%s",
            (body.patient_id, user.clinic_id),
        )
        conn.execute(
            "CALL public.book_appointment(%s,%s,%s,%s)",
            (user.clinic_id, body.patient_id, body.schedule_id, body.reason),
        )
        return one(
            conn,
            APPOINTMENT_SQL
            + " WHERE a.schedule_id=%s AND a.clinic_id=%s AND a.status='scheduled' ORDER BY a.created_at DESC LIMIT 1",
            (body.schedule_id, user.clinic_id),
        )


@router.patch("/appointments/{row_id}")
def appointment_status(
    row_id: UUID, body: AppointmentUpdate, request: Request, user=Depends(current_user)
):
    with connection(request, user) as conn:
        a = one(
            conn,
            "SELECT * FROM public.appointments WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        if a["status"] in ("completed", "cancelled", "no_show"):
            raise HTTPException(409, "Closed visits cannot be changed.")
        if body.status == "scheduled" and a["status"] != "scheduled":
            raise HTTPException(
                409, "A confirmed visit cannot return to requested status."
            )
        if body.status == "no_show":
            slot = one(
                conn,
                "SELECT starts_at FROM public.doctor_schedules WHERE id=%s",
                (a["schedule_id"],),
            )
            if slot["starts_at"] > datetime.now(timezone.utc):
                raise HTTPException(409, "A future visit cannot be marked as no-show.")
        return update(conn, "appointments", row_id, user.clinic_id, body.model_dump())


@router.delete("/appointments/{row_id}")
def appointment_delete(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    with connection(request, user) as conn:
        return delete(conn, "appointments", row_id, user.clinic_id)


@router.get("/consultations")
def consultations(
    request: Request, user=Depends(current_user), limit: int = Query(100, ge=1, le=200)
):
    require(user, CLINICAL)
    with connection(request, user, readonly=True) as conn:
        return conn.execute(
            CONSULTATION_SQL
            + " WHERE c.clinic_id=%s ORDER BY c.created_at DESC LIMIT %s",
            (user.clinic_id, limit),
        ).fetchall()


@router.post("/consultations", status_code=201)
def consultation_create(
    body: Consultation, request: Request, user=Depends(current_user)
):
    require(user, CLINICAL)
    with connection(request, user) as conn:
        ap = one(
            conn,
            APPOINTMENT_SQL + " WHERE a.id=%s AND a.clinic_id=%s",
            (body.appointment_id, user.clinic_id),
        )
        if ap["status"] not in ("scheduled", "confirmed"):
            raise HTTPException(
                409, "A scheduled or confirmed appointment is required."
            )
        return insert(
            conn, "consultations", body.model_dump() | {"clinic_id": user.clinic_id}
        )


@router.put("/consultations/{row_id}")
def consultation_edit(
    row_id: UUID, body: ConsultationUpdate, request: Request, user=Depends(current_user)
):
    require(user, CLINICAL)
    with connection(request, user) as conn:
        current = one(
            conn,
            "SELECT * FROM public.consultations WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        if current["completed_at"]:
            raise HTTPException(409, "Completed clinical notes are locked.")
        return update(conn, "consultations", row_id, user.clinic_id, body.model_dump())


@router.post("/consultations/{row_id}/complete")
def complete(
    row_id: UUID, body: ConsultationUpdate, request: Request, user=Depends(current_user)
):
    require(user, CLINICAL)
    with connection(request, user) as conn:
        visit = one(
            conn,
            CONSULTATION_SQL + " WHERE c.id=%s AND c.clinic_id=%s",
            (row_id, user.clinic_id),
        )
        slot = one(
            conn,
            APPOINTMENT_SQL + " WHERE a.id=%s AND a.clinic_id=%s",
            (visit["appointment_id"], user.clinic_id),
        )
        if slot["starts_at"] > datetime.now(timezone.utc):
            raise HTTPException(409, "A future visit cannot be completed yet.")
        conn.execute(
            "CALL public.complete_consultation(%s,%s,%s)",
            (row_id, body.diagnosis, body.clinical_notes),
        )
        return one(
            conn,
            "SELECT * FROM public.consultations WHERE id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )


@router.delete("/consultations/{row_id}")
def consultation_delete(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    with connection(request, user) as conn:
        visit = one(
            conn,
            "SELECT * FROM public.consultations WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        if visit["completed_at"]:
            raise HTTPException(409, "Completed consultations cannot be deleted.")
        return delete(conn, "consultations", row_id, user.clinic_id)


RX_SQL = "SELECT r.*,a.patient_id,p.full_name patient_name,p.patient_code,p.date_of_birth,s.doctor_id,d.full_name doctor_name FROM public.prescriptions r JOIN public.consultations c ON c.id=r.consultation_id JOIN public.appointments a ON a.id=c.appointment_id JOIN public.patients p ON p.id=a.patient_id JOIN public.doctor_schedules s ON s.id=a.schedule_id JOIN public.doctors d ON d.id=s.doctor_id"


def rx_detail(conn, row_id, clinic_id):
    row = one(conn, RX_SQL + " WHERE r.id=%s AND r.clinic_id=%s", (row_id, clinic_id))
    row["items"] = conn.execute(
        "SELECT * FROM public.prescription_items WHERE prescription_id=%s AND clinic_id=%s ORDER BY id",
        (row_id, clinic_id),
    ).fetchall()
    return row


@router.get("/prescriptions")
def prescriptions(
    request: Request, user=Depends(current_user), limit: int = Query(100, ge=1, le=200)
):
    require(user, CLINICAL)
    with connection(request, user, readonly=True) as conn:
        rows = conn.execute(
            RX_SQL + " WHERE r.clinic_id=%s ORDER BY r.issued_at DESC LIMIT %s",
            (user.clinic_id, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        items = (
            conn.execute(
                "SELECT * FROM public.prescription_items WHERE prescription_id=ANY(%s::uuid[]) AND clinic_id=%s",
                (ids, user.clinic_id),
            ).fetchall()
            if ids
            else []
        )
        for row in rows:
            row["items"] = [i for i in items if i["prescription_id"] == row["id"]]
        return rows


@router.get("/prescriptions/{row_id}")
def prescription_read(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, CLINICAL)
    with connection(request, user, readonly=True) as conn:
        return rx_detail(conn, row_id, user.clinic_id)


@router.post("/prescriptions", status_code=201)
def prescription_create(
    body: Prescription, request: Request, user=Depends(current_user)
):
    require(user, CLINICAL)
    with connection(request, user) as conn:
        one(
            conn,
            "SELECT id FROM public.consultations WHERE id=%s AND clinic_id=%s",
            (body.consultation_id, user.clinic_id),
        )
        header = insert(
            conn,
            "prescriptions",
            {
                "clinic_id": user.clinic_id,
                "consultation_id": body.consultation_id,
                "instructions": body.instructions,
            },
        )
        for item in body.items:
            insert(
                conn,
                "prescription_items",
                item.model_dump()
                | {"clinic_id": user.clinic_id, "prescription_id": header["id"]},
            )
        return rx_detail(conn, header["id"], user.clinic_id)


@router.put("/prescriptions/{row_id}")
def prescription_edit(
    row_id: UUID, body: Prescription, request: Request, user=Depends(current_user)
):
    require(user, CLINICAL)
    # Item replacement requires DELETE rights, so is admin-only under current RLS.
    require(user, ADMIN)
    with connection(request, user) as conn:
        one(
            conn,
            "SELECT id FROM public.prescriptions WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        update(
            conn,
            "prescriptions",
            row_id,
            user.clinic_id,
            {
                "consultation_id": body.consultation_id,
                "instructions": body.instructions,
            },
        )
        conn.execute(
            "DELETE FROM public.prescription_items WHERE prescription_id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
        for item in body.items:
            insert(
                conn,
                "prescription_items",
                item.model_dump()
                | {"clinic_id": user.clinic_id, "prescription_id": row_id},
            )
        return rx_detail(conn, row_id, user.clinic_id)


@router.delete("/prescriptions/{row_id}")
def prescription_delete(row_id: UUID, request: Request, user=Depends(current_user)):
    require(user, ADMIN)
    with connection(request, user) as conn:
        one(
            conn,
            "SELECT id FROM public.prescriptions WHERE id=%s AND clinic_id=%s FOR UPDATE",
            (row_id, user.clinic_id),
        )
        conn.execute(
            "DELETE FROM public.prescription_items WHERE prescription_id=%s AND clinic_id=%s",
            (row_id, user.clinic_id),
        )
        return delete(conn, "prescriptions", row_id, user.clinic_id)


@router.get("/triage")
def triage_records(request: Request, user=Depends(current_user)):
    require(user, CLINICAL)
    with connection(request, user, readonly=True) as conn:
        return conn.execute(
            "SELECT * FROM public.triage_records WHERE clinic_id=%s ORDER BY created_at DESC LIMIT 100",
            (user.clinic_id,),
        ).fetchall()


@router.post("/triage", status_code=201)
def log_triage(body: TriageInput, request: Request, user=Depends(current_user)):
    require(user, CLINICAL)
    # No AI is fabricated. Store the input for clinician review, with urgency unset.
    with connection(request, user) as conn:
        return insert(
            conn,
            "triage_records",
            {
                "clinic_id": user.clinic_id,
                "patient_id": body.patient_id,
                "symptoms": body.symptoms
                + "\nDuration: "
                + body.duration
                + "\nSelected warning signs: "
                + ", ".join(body.red_flags),
                "ai_summary": None,
                "urgency": None,
                "model_name": None,
                "review_status": "needs_review",
            },
        )

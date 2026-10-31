"""Clinic intake reports plus a bounded, write-only guest warning-sign submission."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from psycopg.errors import RaiseException

from ..auth import ALL, current_account, require
from ..db import connection, one
from ..limits import throttle
from ..models import Model
from .triage import Intake, emergency

router = APIRouter(prefix="/intake-reports", tags=["Clinic intake reports"])


class ReportInput(Intake):
    doctor_id: UUID
    request_key: UUID
    summary: str = Field(min_length=5, max_length=4000)
    pain_score: int | None = Field(default=None, ge=0, le=10)
    progression: Literal["Improving", "Unchanged", "Worsening", "Not sure"] = "Not sure"
    share_with_clinic: bool = False


class GuestReportInput(ReportInput):
    callback_phone: str | None = Field(default=None, max_length=30)


class ReportStatus(Model):
    status: Literal["acknowledged", "reviewed"]


@router.post("", status_code=201)
def submit_report(body: ReportInput, request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    if not body.warning_screen_complete or not body.share_with_clinic:
        raise HTTPException(422, "Review warning signs and agree to share this report with the clinic.")
    throttle(request, "intake-report", 5, subject=str(user.id))
    # Resolve the clinic from a real public doctor; never trust a browser clinic_id.
    with connection(request, readonly=True) as conn:
        doctor = conn.execute(
            "SELECT id,clinic_id FROM public.doctors WHERE id=%s "
            "AND is_published AND NOT is_archived AND NOT is_sample",
            (body.doctor_id,),
        ).fetchone()
    if not doctor:
        raise HTTPException(422, "Choose a published clinic doctor.")
    flags = sorted(set(body.red_flags))
    expected = {
        "assigned_doctor_id": body.doctor_id, "symptoms": body.symptoms,
        "duration": body.duration, "pain_score": body.pain_score,
        "progression": body.progression, "red_flags": flags, "report_summary": body.summary,
    }
    try:
        with connection(request, user) as conn:
            row = conn.execute(
                "INSERT INTO public.triage_records "
                "(clinic_id,submitted_by,assigned_doctor_id,request_key,symptoms,duration,"
                "pain_score,progression,red_flags,report_summary,share_with_clinic) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true) "
                "ON CONFLICT (submitted_by,request_key) WHERE submitted_by IS NOT NULL "
                "DO NOTHING RETURNING *",
                (doctor["clinic_id"], user.id, body.doctor_id, body.request_key,
                 body.symptoms, body.duration, body.pain_score, body.progression, flags, body.summary),
            ).fetchone()
            if row is None:
                row = one(conn, "SELECT * FROM public.triage_records WHERE submitted_by=%s AND request_key=%s", (user.id, body.request_key))
                if any(row[key] != value for key, value in expected.items()):
                    raise HTTPException(409, "This submission reference is already used. Review your current report before submitting again.")
    except RaiseException as error:
        if error.diag.message_primary == "Report submission limit reached; contact the clinic directly":
            raise HTTPException(429, "Report submission limit reached. Contact the clinic directly.") from None
        raise
    return row


@router.post("/guest", status_code=201)
def submit_guest_report(body: GuestReportInput, request: Request):
    if not body.warning_screen_complete or not body.share_with_clinic:
        raise HTTPException(422, "Review warning signs before notifying the clinic.")
    if not emergency(body):
        raise HTTPException(422, "Guest alerts are for reported warning signs. Sign in to send other reports.")
    throttle(request, "guest-intake-report", 3)
    host = request.client.host if request.client else "unknown"
    sender = request.app.state.limits.key("guest-intake:" + host)
    flags = sorted(set(body.red_flags))
    phone = body.callback_phone.strip() if body.callback_phone else None
    expected = {"assigned_doctor_id": body.doctor_id, "symptoms": body.symptoms,
                "duration": body.duration, "pain_score": body.pain_score,
                "progression": body.progression, "red_flags": flags,
                "report_summary": body.summary, "patient_phone": phone}
    try:
        # Trusted server connection, not a public table grant. No guest read endpoint.
        with connection(request) as conn:
            doctor = conn.execute(
                "SELECT id,clinic_id FROM public.doctors WHERE id=%s "
                "AND is_published AND NOT is_archived AND NOT is_sample", (body.doctor_id,),
            ).fetchone()
            if not doctor:
                raise HTTPException(422, "Choose a published clinic doctor.")
            row = conn.execute(
                "INSERT INTO public.triage_records "
                "(clinic_id,guest_sender,assigned_doctor_id,request_key,symptoms,duration,"
                "pain_score,progression,red_flags,report_summary,share_with_clinic,patient_phone) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) "
                "ON CONFLICT (request_key) WHERE guest_sender IS NOT NULL DO NOTHING RETURNING *",
                (doctor["clinic_id"], sender, body.doctor_id, body.request_key,
                 body.symptoms, body.duration, body.pain_score, body.progression, flags, body.summary, phone),
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM public.triage_records WHERE guest_sender=%s AND request_key=%s", (sender, body.request_key)).fetchone()
                if row is None or any(row[key] != value for key, value in expected.items()):
                    raise HTTPException(409, "This submission reference was already used. Contact the clinic to confirm your earlier report.")
    except RaiseException as error:
        if error.diag.message_primary == "Report submission limit reached; contact the clinic directly":
            raise HTTPException(429, "Report submission limit reached. Contact the clinic directly; do not wait for this portal in an emergency.") from None
        raise
    # Do not return the stored symptom text, phone, sender hash or other reports.
    return {"id": row["id"], "workflow_status": row["workflow_status"]}


@router.get("")
def list_reports(
    request: Request,
    status: Literal["active", "all", "new", "acknowledged", "reviewed"] = "active",
    offset: int = Query(0, ge=0),
    user=Depends(current_account),
):
    require(user, ALL | {"patient"})
    if user.role == "patient":
        scope, params = "submitted_by=%s", [user.id]
    else:
        scope, params = "clinic_id=%s", [user.clinic_id]
        if user.role == "doctor":
            scope += " AND assigned_doctor_id IN (SELECT id FROM public.doctors WHERE profile_id=%s AND clinic_id=%s)"
            params.extend([user.id, user.clinic_id])
    scope = "(submitted_by IS NOT NULL OR guest_sender IS NOT NULL) AND " + scope
    condition = ""
    if status == "active":
        condition = " AND workflow_status IN ('new','acknowledged')"
    elif status != "all":
        condition = " AND workflow_status=%s"
    with connection(request, user, readonly=True) as conn:
        counts = conn.execute(
            "SELECT count(*) FILTER(WHERE workflow_status='new') AS new_count, "
            "count(*) FILTER(WHERE workflow_status='new' AND has_warning_signs) AS warning_count "
            "FROM public.triage_records WHERE " + scope, tuple(params),
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM public.triage_records WHERE " + scope + condition +
            " ORDER BY has_warning_signs DESC,(workflow_status='new') DESC,created_at DESC,id LIMIT 20 OFFSET %s",
            tuple(params + ([status] if status not in ("active", "all") else []) + [offset]),
        ).fetchall()
    return {"reports": rows, **counts}


@router.get("/{report_id}")
def get_report(report_id: UUID, request: Request, user=Depends(current_account)):
    require(user, ALL | {"patient"})
    with connection(request, user, readonly=True) as conn:
        return one(conn, "SELECT * FROM public.triage_records WHERE id=%s AND (submitted_by IS NOT NULL OR guest_sender IS NOT NULL)", (report_id,))


@router.patch("/{report_id}")
def update_report(report_id: UUID, body: ReportStatus, request: Request, user=Depends(current_account)):
    require(user, ALL)
    if body.status == "reviewed":
        require(user, {"clinic_admin", "doctor"})
    with connection(request, user) as conn:
        row = one(conn, "SELECT * FROM public.triage_records WHERE id=%s AND clinic_id=%s AND (submitted_by IS NOT NULL OR guest_sender IS NOT NULL) FOR UPDATE", (report_id, user.clinic_id))
        if row["workflow_status"] == body.status:
            return row
        previous = "new" if body.status == "acknowledged" else "acknowledged"
        if row["workflow_status"] != previous:
            raise HTTPException(409, "Refresh the report. Acknowledge it before marking it reviewed; reviewed reports are closed.")
        return one(conn, "UPDATE public.triage_records SET workflow_status=%s WHERE id=%s AND clinic_id=%s RETURNING *", (body.status, report_id, user.clinic_id))

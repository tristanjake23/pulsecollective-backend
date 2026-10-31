from fastapi import APIRouter, Request, Query
from ..db import connection

router = APIRouter(prefix="/public", tags=["Public directory"])


@router.get("/doctors")
def doctors(
    request: Request,
    q: str = Query("", max_length=100),
    limit: int = Query(100, ge=1, le=200),
):
    # Deliberate allowlist: no profiles, internal user identifiers, or patient data.
    with connection(request, readonly=True) as conn:
        return conn.execute(
            "SELECT id,clinic_id,full_name,specialty,clinic_name,location,schedule,consultation_fee,photo_url,is_sample,(accepting_until>now() AND NOT EXISTS(SELECT 1 FROM public.doctor_schedules busy WHERE busy.doctor_id=doctors.id AND busy.starts_at<now()+interval '21 minutes' AND busy.ends_at>now())) AS online_now FROM public.doctors WHERE NOT is_sample AND is_published AND NOT is_archived AND (full_name ILIKE %s OR specialty ILIKE %s) ORDER BY full_name LIMIT %s",
            ("%" + q + "%", "%" + q + "%", limit),
        ).fetchall()


@router.get("/specialties")
def specialties(request: Request):
    with connection(request, readonly=True) as conn:
        return conn.execute(
            "SELECT id,name FROM public.specialties ORDER BY name"
        ).fetchall()


@router.get("/schedules")
def schedules(request: Request):
    with connection(request, readonly=True) as conn:
        return conn.execute(
            "SELECT s.id,s.doctor_id,s.clinic_id,s.starts_at,s.ends_at FROM public.doctor_schedules s WHERE EXISTS (SELECT 1 FROM public.doctors d WHERE d.id=s.doctor_id AND d.clinic_id=s.clinic_id AND NOT d.is_sample AND d.is_published AND NOT d.is_archived) AND s.is_available AND s.starts_at>now() AND NOT EXISTS(SELECT 1 FROM public.appointments a WHERE a.schedule_id=s.id AND a.status IN ('scheduled','confirmed','completed')) ORDER BY s.starts_at LIMIT 200"
        ).fetchall()

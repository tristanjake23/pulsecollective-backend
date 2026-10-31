from fastapi import APIRouter, Depends, Request
from ..auth import current_account, require
from ..db import connection, one
from ..models import PatientAccount

router = APIRouter(prefix="/patient", tags=["Patient account"])
FIELDS = "id,contact_email,full_name,first_name,middle_name,last_name,date_of_birth,sex,phone,address"


@router.get("/profile")
def read_profile(request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    with connection(request, user, readonly=True) as conn:
        return one(
            conn, f"SELECT {FIELDS} FROM public.profiles WHERE id=%s", (user.id,)
        )


@router.put("/profile")
def save_profile(body: PatientAccount, request: Request, user=Depends(current_account)):
    require(user, {"patient"})
    values = body.model_dump(exclude_unset=True)
    from psycopg import sql

    keys = list(values)
    with connection(request, user) as conn:
        return one(
            conn,
            sql.SQL(
                "UPDATE public.profiles SET {} WHERE id=%s RETURNING " + FIELDS
            ).format(
                sql.SQL(",").join(
                    sql.SQL("{}=%s").format(sql.Identifier(k)) for k in keys
                )
            ),
            (*values.values(), user.id),
        )

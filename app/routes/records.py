from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import sql

from ..auth import ADMIN, ALL, current_user, require
from ..db import connection, delete, insert, one, update
from ..models import Doctor, Patient, Schedule

router = APIRouter(tags=["Clinic records"])


def register(path, table, model, write_roles=ALL):
    def list_records(
        request: Request,
        user=Depends(current_user),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        upcoming: bool = Query(False),
    ):
        with connection(request, user, readonly=True) as conn:
            if table == "doctor_schedules" and upcoming:
                return conn.execute(
                    """
                    SELECT s.*
                    FROM public.doctor_schedules s
                    WHERE s.clinic_id=%s
                      AND s.starts_at>now()
                      AND s.is_available
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.appointments a
                          WHERE a.schedule_id=s.id
                            AND a.status IN (
                                'scheduled',
                                'confirmed',
                                'completed'
                            )
                      )
                    ORDER BY s.starts_at, s.id
                    LIMIT %s OFFSET %s
                    """,
                    (user.clinic_id, limit, offset),
                ).fetchall()

            return conn.execute(
                sql.SQL(
                    "SELECT * FROM public.{} "
                    "WHERE clinic_id=%s "
                    "ORDER BY id LIMIT %s OFFSET %s"
                ).format(sql.Identifier(table)),
                (user.clinic_id, limit, offset),
            ).fetchall()

    def read_record(
        row_id: UUID,
        request: Request,
        user=Depends(current_user),
    ):
        with connection(request, user, readonly=True) as conn:
            return one(
                conn,
                sql.SQL(
                    "SELECT * FROM public.{} "
                    "WHERE id=%s AND clinic_id=%s"
                ).format(sql.Identifier(table)),
                (row_id, user.clinic_id),
            )

    def validate(conn, body, user):
        if table == "doctors" and body.profile_id:
            row = conn.execute(
                "SELECT 1 WHERE private.valid_doctor_account(%s)",
                (body.profile_id,),
            ).fetchone()

            if not row:
                raise HTTPException(
                    422,
                    "Choose an approved doctor account in this clinic.",
                )

        if table == "doctor_schedules":
            if body.starts_at <= datetime.now(timezone.utc):
                raise HTTPException(
                    422,
                    "Choose a future start time.",
                )

            doctor = one(
                conn,
                """
                SELECT id, profile_id
                FROM public.doctors
                WHERE id=%s
                  AND clinic_id=%s
                  AND NOT is_archived
                """,
                (body.doctor_id, user.clinic_id),
            )

            if (
                user.role == "doctor"
                and doctor["profile_id"] != user.id
            ):
                raise HTTPException(
                    403,
                    "You can manage only your own schedule.",
                )

    def create_record(
        body,
        request: Request,
        user=Depends(current_user),
    ):
        require(user, write_roles)

        with connection(request, user) as conn:
            validate(conn, body, user)

            values = body.model_dump() | {
                "clinic_id": user.clinic_id,
            }

            if table == "patients" and user.role == "doctor":
                doctor = one(
                    conn,
                    """
                    SELECT id
                    FROM public.doctors
                    WHERE profile_id=%s
                      AND clinic_id=%s
                    """,
                    (user.id, user.clinic_id),
                )
                values["doctor_id"] = doctor["id"]

            return insert(conn, table, values)

    create_record.__annotations__["body"] = model

    def replace_record(
        row_id: UUID,
        body,
        request: Request,
        user=Depends(current_user),
    ):
        require(user, write_roles)

        with connection(request, user) as conn:
            validate(conn, body, user)

            return update(
                conn,
                table,
                row_id,
                user.clinic_id,
                body.model_dump(),
            )

    replace_record.__annotations__["body"] = model

    def delete_record(
        row_id: UUID,
        request: Request,
        user=Depends(current_user),
    ):
        require(user, ADMIN)

        with connection(request, user) as conn:
            return delete(
                conn,
                table,
                row_id,
                user.clinic_id,
            )

    for function in (
        list_records,
        read_record,
        create_record,
        replace_record,
        delete_record,
    ):
        function.__name__ = table + "_" + function.__name__

    router.add_api_route(
        path,
        list_records,
        methods=["GET"],
    )
    router.add_api_route(
        path + "/{row_id}",
        read_record,
        methods=["GET"],
    )
    router.add_api_route(
        path,
        create_record,
        methods=["POST"],
        status_code=201,
    )
    router.add_api_route(
        path + "/{row_id}",
        replace_record,
        methods=["PUT"],
    )
    router.add_api_route(
        path + "/{row_id}",
        delete_record,
        methods=["DELETE"],
    )


register("/patients", "patients", Patient)
register("/doctors", "doctors", Doctor, ADMIN)
register("/schedules", "doctor_schedules", Schedule)
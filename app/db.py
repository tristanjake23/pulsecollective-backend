from contextlib import contextmanager
import json
from fastapi import HTTPException
from psycopg import sql


@contextmanager
def connection(request, user=None, readonly=False):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            503, "Database is not configured. Set DATABASE_URL in backend/.env."
        )
    with pool.connection(timeout=10) as conn:
        with conn.transaction():
            if readonly:
                conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            if user:
                # Trusted identity comes from Supabase get-user, never from a client user_id.
                conn.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(user.id), "role": "authenticated"}),),
                )
                conn.execute(
                    "SELECT set_config('request.jwt.claim.sub', %s, true)",
                    (str(user.id),),
                )
                conn.execute("SET LOCAL ROLE authenticated")
            yield conn


def one(conn, query, params=()):
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise HTTPException(404, "Record not found in your clinic.")
    return row


def insert(conn, table, values):
    # Table and column identifiers are supplied ONLY by server models/routes.
    keys = list(values)
    query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, keys)),
        sql.SQL(",").join(sql.Placeholder() for _ in keys),
    )
    return one(conn, query, tuple(values[k] for k in keys))


def update(conn, table, row_id, clinic_id, values):
    if not values:
        raise HTTPException(422, "No update fields provided.")
    query = sql.SQL(
        "UPDATE public.{} SET {} WHERE id=%s AND clinic_id=%s RETURNING *"
    ).format(
        sql.Identifier(table),
        sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in values),
    )
    return one(conn, query, (*values.values(), row_id, clinic_id))


def delete(conn, table, row_id, clinic_id):
    query = sql.SQL(
        "DELETE FROM public.{} WHERE id=%s AND clinic_id=%s RETURNING id"
    ).format(sql.Identifier(table))
    return one(conn, query, (row_id, clinic_id))

from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import settings
from .limits import Limits, throttle
from .security import RequestGuard
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .routes import (
    auth_routes,
    billing,
    chat,
    clinical,
    public,
    records,
    patient_portal,
    booking,
    triage,
    photos,
    triage_reports,
)


@asynccontextmanager
async def lifespan(app):
    if settings.environment == "production":
        if not settings.redis_url or len(settings.rate_limit_secret) < 32:
            raise RuntimeError(
                "Production requires REDIS_URL and a strong RATE_LIMIT_SECRET."
            )
        if "*" in settings.allowed_hosts or not settings.database_url:
            raise RuntimeError(
                "Production requires explicit ALLOWED_HOSTS and DATABASE_URL."
            )
        if (
            "sslmode=require" not in settings.database_url
            and "sslmode=verify-full" not in settings.database_url
        ):
            raise RuntimeError("Production database connections must require TLS.")
        if any(not origin.startswith("https://") for origin in settings.cors_origins):
            raise RuntimeError("Production CORS origins must use HTTPS.")
    app.state.limits = Limits(settings.redis_url, settings.rate_limit_secret)
    app.state.http = httpx.Client(
        timeout=15,
        follow_redirects=False,
    )

    app.state.pool = (
        ConnectionPool(
            settings.database_url,
            min_size=0,
            max_size=settings.pool_size,
            max_waiting=20,
            timeout=10,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "prepare_threshold": None,
                "connect_timeout": 10,
            },
        )
        if settings.database_url
        else None
    )

    try:
        yield
    finally:
        app.state.http.close()
        if app.state.limits.redis:
            app.state.limits.redis.close()
        if app.state.pool:
            app.state.pool.close()


def create_app():
    app = FastAPI(
        title="Pulse Collective Healthcare API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None if settings.environment == "production" else "/redoc",
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )

    @app.middleware("http")
    async def headers(request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                throttle(request, "api-global", 120)
            except HTTPException as error:
                return JSONResponse(
                    {"detail": error.detail}, error.status_code, headers=error.headers
                )
        try:
            response = await call_next(request)
        except Exception as error:
            # Do not echo credentials, SQL details, or submitted medical data.
            import logging

            logging.getLogger("pulse").error(
                "Unhandled request error: %s", type(error).__name__
            )
            response = JSONResponse(
                {"detail": "Server error. Please contact the clinic administrator."},
                500,
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": list(error["loc"]),
                        "msg": error["msg"],
                        "type": error["type"],
                    }
                    for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        if exc.sqlstate in ("23505", "23P01"):
            code = 409
            message = "Duplicate record or overlapping appointment slot."
        elif exc.sqlstate == "P0002":
            code = 404
            message = "Record not found."
        elif exc.sqlstate == "23503":
            code = 409
            message = (
                "Related record is missing, belongs to another "
                "clinic, or is still in use."
            )
        elif exc.sqlstate in ("23514", "22003", "22001"):
            code = 422
            message = "Record violates a database validation rule."
        elif exc.sqlstate == "42501":
            code = 403
            message = "Database permissions do not allow this operation."
        elif exc.sqlstate == "P0001":
            code = 409
            message = (
                "Operation conflicts with a clinic rule. Check "
                "appointment availability, invoice balance or status."
            )
        else:
            code = 503
            message = (
                "Database operation failed. Check the backend connection and schema."
            )

        return JSONResponse(
            status_code=code,
            content={"detail": message},
        )

    @app.exception_handler(PoolTimeout)
    async def pool_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Database connection unavailable. "
                    "Check DATABASE_URL and pooler settings."
                )
            },
        )

    @app.exception_handler(httpx.HTTPError)
    async def network_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={"detail": "External service is temporarily unavailable."},
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "database_configured": bool(settings.database_url),
            "auth_configured": bool(settings.supabase_publishable_key),
        }

    for router in (
        auth_routes.router,
        public.router,
        records.router,
        clinical.router,
        billing.router,
        chat.router,
        patient_portal.router,
        booking.router,
        triage.router,
        photos.router,
        triage_reports.router,
    ):
        app.include_router(router, prefix="/api")

    app.add_middleware(RequestGuard)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Retry-After"],
    )
    return app


app = create_app()

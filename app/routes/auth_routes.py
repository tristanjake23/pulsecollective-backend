from fastapi import APIRouter, Depends, HTTPException, Request
from ..auth import auth_call, identity, current_account, profile_json
from ..config import settings
from ..limits import throttle
from ..db import connection
from ..models import Login, Refresh, Signup, ResetPassword

router = APIRouter(prefix="/auth", tags=["Authentication"])


def session(request, data):
    user = identity(request, data["access_token"])
    return {
        key: data[key]
        for key in ("access_token", "refresh_token", "expires_in", "token_type")
        if key in data
    } | {"profile": profile_json(user)}


@router.post("/login")
def login(body: Login, request: Request):
    throttle(request, "login-ip", 10)
    throttle(request, "login-account", 10, subject=body.email.strip().lower())
    return session(
        request,
        auth_call(
            request, "POST", "token?grant_type=password", payload=body.model_dump()
        ),
    )


@router.post("/signup", status_code=201)
def signup(body: Signup, request: Request):
    throttle(request, "signup-ip", 3, 3600)
    throttle(request, "signup-account", 3, 3600, subject=body.email.lower())
    if not settings.public_signup:
        raise HTTPException(503, "Patient registration is temporarily closed.")
    # Only non-authoritative display data. The database trigger sets role=patient.
    data = auth_call(
        request,
        "POST",
        "signup",
        payload={
            "email": body.email,
            "password": body.password,
            "data": {"full_name": body.full_name},
        },
    )
    if data.get("access_token"):
        result = session(request, data)
        # Fail closed if an old or misconfigured signup trigger grants a clinic role.
        if result["profile"]["role"] != "patient":
            raise HTTPException(
                503, "Patient registration needs the security SQL update."
            )
        return result
    return {
        "message": "If registration is available for this email, check your inbox to confirm the account, then sign in."
    }


@router.post("/password-reset")
def password_reset(body: ResetPassword, request: Request):
    throttle(request, "reset-ip", 3, 3600)
    throttle(request, "reset-account", 3, 3600, subject=body.email.lower())
    # Generic reply prevents account enumeration. Uses provider's configured Site URL.
    try:
        auth_call(request, "POST", "recover", payload={"email": body.email})
    except HTTPException as error:
        if error.status_code in (429, 503):
            raise
    return {
        "message": "If this email has an account, password recovery instructions will be sent."
    }


@router.post("/refresh")
def refresh(body: Refresh, request: Request):
    throttle(request, "refresh", 30)
    return session(
        request,
        auth_call(
            request, "POST", "token?grant_type=refresh_token", payload=body.model_dump()
        ),
    )


@router.get("/me")
def me(user=Depends(current_account)):
    return profile_json(user)


@router.post("/logout")
def logout(request: Request, user=Depends(current_account)):
    import base64, json, math, time

    # Token was verified by current_account. Read expiry only to size the denylist TTL.
    try:
        part = user.token.split(".")[1]
        expiry = float(
            json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"]
        )
        ttl = max(1, math.ceil(expiry - time.time()) + 60)
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        ttl = 3660
    request.app.state.limits.revoke(user.token, ttl)
    if user.role == "doctor":
        # Verified identity; this owner query cannot change another clinician's availability.
        try:
            with connection(request) as conn:
                conn.execute(
                    "UPDATE public.doctors SET accepting_until=NULL WHERE profile_id=%s AND clinic_id=%s",
                    (user.id, user.clinic_id),
                )
        except Exception:
            import logging

            logging.getLogger("pulse").warning(
                "Availability cleanup unavailable at logout; heartbeat will expire."
            )
    auth_call(request, "POST", "logout?scope=local", token=user.token)
    return {"ok": True}

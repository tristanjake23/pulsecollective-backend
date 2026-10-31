from dataclasses import dataclass
from uuid import UUID
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import settings
from .db import connection

bearer = HTTPBearer(auto_error=False)
ROLES = {"clinic_admin", "doctor", "staff"}
ADMIN = {"clinic_admin"}
CLINICAL = {"clinic_admin", "doctor"}
BILLING = {"clinic_admin", "staff"}
ALL = ROLES


@dataclass(frozen=True)
class User:
    id: UUID
    clinic_id: UUID | None
    role: str
    full_name: str
    token: str


def auth_call(request, method, path, token=None, payload=None):
    if not settings.supabase_publishable_key or not settings.supabase_url:
        raise HTTPException(503, "Authentication is not configured.")
    headers = {"apikey": settings.supabase_publishable_key}
    if token:
        headers["Authorization"] = "Bearer " + token
    response = request.app.state.http.request(
        method,
        settings.supabase_url + "/auth/v1/" + path,
        headers=headers,
        json=payload,
    )
    if response.status_code >= 500:
        raise HTTPException(503, "Authentication provider unavailable.")
    if response.status_code >= 400:
        try:
            code = response.json().get("code", "")
        except ValueError:
            code = ""
        if response.status_code == 429:
            raise HTTPException(429, "Too many authentication requests. Please wait.")
        if code == "email_not_confirmed":
            raise HTTPException(401, "Confirm your account email before signing in.")
        if code in (
            "bad_jwt",
            "invalid_jwt",
            "refresh_token_not_found",
            "refresh_token_already_used",
        ):
            raise HTTPException(401, "Session expired. Please sign in again.")
        if code == "invalid_credentials":
            raise HTTPException(401, "Email or login password is incorrect.")
        if path == "signup":
            raise HTTPException(
                400,
                "Unable to create the account. Check password requirements or contact support.",
            )
        raise HTTPException(401, "Authentication failed or session expired.")
    return response.json() if response.content else {}


def identity(request, token):
    # Identity is always checked with the provider. Browser roles are never used.
    verified = auth_call(request, "GET", "user", token=token)
    try:
        uid = UUID(verified["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid authenticated identity.") from None
    with connection(request, readonly=True) as conn:
        profile = conn.execute(
            "SELECT id,clinic_id,role,full_name FROM public.profiles WHERE id=%s",
            (uid,),
        ).fetchone()
    if not profile:
        raise HTTPException(
            403, "Account profile is unavailable. Contact the administrator."
        )
    role = profile["role"]
    if role == "patient":
        # A patient account never acquires clinic-wide access, even with a clinic_id.
        return User(uid, None, role, profile["full_name"] or "Patient", token)
    if role not in ROLES or not profile["clinic_id"]:
        raise HTTPException(
            403,
            "Your account needs an assigned clinic and approved role. Contact the administrator.",
        )
    return User(
        uid, profile["clinic_id"], role, profile["full_name"] or "Clinic member", token
    )


def current_account(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            401, "Sign in to continue.", headers={"WWW-Authenticate": "Bearer"}
        )
    if request.app.state.limits.is_revoked(credentials.credentials):
        raise HTTPException(401, "Session ended. Please sign in again.")
    return identity(request, credentials.credentials)


def current_user(user=Depends(current_account)):
    require(user, ROLES)
    return user


def require(user, roles):
    if user.role not in roles:
        raise HTTPException(
            403, "Oops, this action requires an authorized clinic role."
        )


def profile_json(user):
    return {
        "id": str(user.id),
        "clinic_id": str(user.clinic_id) if user.clinic_id else None,
        "role": user.role,
        "full_name": user.full_name,
    }

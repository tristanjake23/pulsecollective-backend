import base64
import json
import logging
import math
import time

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request
from ..auth import auth_call, identity, current_account, profile_json
from ..auth_models import (
    CaptchaLogin, CaptchaSignup, EmailRequest, EmailCode, ResetComplete,
)
from ..config import settings
from ..limits import throttle
from ..db import connection
from ..models import Refresh

router = APIRouter(prefix="/auth", tags=["Authentication"])
EMAIL_MESSAGE = "If this email is eligible, a verification code will be sent. Check your inbox and spam folder."
RESET_MESSAGE = "If this email has an account, a password reset code will be sent. Check your inbox and spam folder."


def session(request, data):
    user = identity(request, data["access_token"])
    return {
        key: data[key]
        for key in ("access_token", "refresh_token", "expires_in", "token_type")
        if key in data
    } | {"profile": profile_json(user)}


def captcha_payload(token):
    # Supabase Auth validates this with the CAPTCHA provider server-side.
    # The Cloudflare SECRET belongs in Supabase settings, never in this payload.
    return {"gotrue_meta_security": {"captcha_token": token}} if token else {}


def email_limits(request, action, email):
    throttle(request, action + "-ip", 3, 3600)
    throttle(request, action + "-account", 3, 3600, subject=email)
    throttle(request, action + "-cooldown", 1, 60, subject=email)


def verification_limits(request, action, email):
    # Server limits apply even if somebody bypasses the UI cooldown.
    throttle(request, action + "-ip", 10)
    throttle(request, action + "-account", 5, subject=email)


def revoke_token(request, token):
    # Called only with a token verified by Supabase/current_account.
    try:
        part = token.split(".")[1]
        expiry = float(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
        ttl = max(1, math.ceil(expiry - time.time()) + 60)
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        ttl = 3660
    request.app.state.limits.revoke(token, ttl)


def verify_code(request, email, code, kind):
    try:
        data = auth_call(request, "POST", "verify", payload={
            "email": email, "token": code, "type": kind,
        })
    except HTTPException as error:
        if error.status_code in (400, 401, 403, 404, 422):
            raise HTTPException(400, "The code is invalid or expired. Request a new code.") from None
        raise
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(502, "Verification could not be completed. Request a new code.")
    verified = auth_call(request, "GET", "user", token=token)
    if not verified.get("id") or str(verified.get("email", "")).strip().lower() != email:
        raise HTTPException(401, "Verification failed. Request a new code.")
    return token


def close_otp_session(request, token, scope="local"):
    # No OTP-issued session is sent to the browser or used as a portal login.
    revoke_token(request, token)
    try:
        auth_call(request, "POST", "logout?scope=" + scope, token=token)
    except (HTTPException, httpx.HTTPError):
        logging.getLogger("pulse.auth").warning("OTP session provider cleanup unavailable")


@router.post("/login")
def login(body: CaptchaLogin, request: Request):
    email = body.email.strip().lower()
    throttle(request, "login-ip", 10)
    throttle(request, "login-account", 10, subject=email)
    return session(request, auth_call(request, "POST", "token?grant_type=password", payload={
        "email": email, "password": body.password, **captcha_payload(body.captcha_token),
    }))


@router.post("/signup", status_code=201)
def signup(body: CaptchaSignup, request: Request):
    email_limits(request, "signup", body.email)
    if not settings.public_signup:
        raise HTTPException(503, "Patient registration is temporarily closed.")
    data = auth_call(request, "POST", "signup", payload={
        "email": body.email, "password": body.password,
        "data": {"full_name": body.full_name}, **captcha_payload(body.captcha_token),
    })
    # Retains the old behavior during rollout while Confirm email is still OFF.
    if data.get("access_token"):
        result = session(request, data)
        if result["profile"]["role"] != "patient":
            raise HTTPException(503, "Patient registration needs the security SQL update.")
        return result
    return {"message": EMAIL_MESSAGE}


@router.post("/resend-confirmation")
def resend_confirmation(body: EmailRequest, request: Request):
    email_limits(request, "confirm-send", body.email)
    try:
        auth_call(request, "POST", "resend", payload={
            "email": body.email, "type": "signup", **captcha_payload(body.captcha_token),
        })
    except HTTPException as error:
        if error.status_code in (429, 503):
            raise
        # An existing/confirmed/unknown account gets the same public message.
    return {"message": EMAIL_MESSAGE}


@router.post("/verify-email")
def verify_email(body: EmailCode, request: Request):
    verification_limits(request, "confirm-verify", body.email)
    token = verify_code(request, body.email, body.code, "signup")
    close_otp_session(request, token)
    return {"message": "Email confirmed. You can now sign in with your password."}


@router.post("/password-reset")
def password_reset(body: EmailRequest, request: Request):
    email_limits(request, "reset", body.email)
    try:
        auth_call(request, "POST", "recover", payload={
            "email": body.email, **captcha_payload(body.captcha_token),
        })
    except HTTPException as error:
        if error.status_code in (429, 503):
            raise
    return {"message": RESET_MESSAGE}


@router.post("/password-reset/complete")
def complete_password_reset(body: ResetComplete, request: Request):
    verification_limits(request, "reset-verify", body.email)
    # Only a RECOVERY code can authorize a password change. A signup code cannot.
    token = verify_code(request, body.email, body.code, "recovery")
    updated = False
    try:
        auth_call(request, "PUT", "user", token=token, payload={"password": body.new_password})
        updated = True
    except HTTPException as error:
        if error.status_code in (429, 503):
            raise
        raise HTTPException(400, "Password could not be updated. Check password requirements and request a new code.") from None
    finally:
        close_otp_session(request, token, "global" if updated else "local")
    return {"message": "Password updated. Sign in using your new password."}


@router.post("/refresh")
def refresh(body: Refresh, request: Request):
    throttle(request, "refresh", 30)
    return session(request, auth_call(request, "POST", "token?grant_type=refresh_token", payload=body.model_dump()))


@router.get("/me")
def me(user=Depends(current_account)):
    return profile_json(user)


@router.post("/logout")
def logout(request: Request, user=Depends(current_account)):
    revoke_token(request, user.token)
    if user.role == "doctor":
        try:
            with connection(request) as conn:
                conn.execute(
                    "UPDATE public.doctors SET accepting_until=NULL WHERE profile_id=%s AND clinic_id=%s",
                    (user.id, user.clinic_id),
                )
        except Exception:
            logging.getLogger("pulse").warning("Availability cleanup unavailable at logout; heartbeat will expire.")
    auth_call(request, "POST", "logout?scope=local", token=user.token)
    return {"ok": True}

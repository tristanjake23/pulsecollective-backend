"""Auth-only additions. Existing clinical and billing models are unchanged."""

from typing import Annotated
from pydantic import ConfigDict, Field
from .models import Login, Signup, ResetPassword

CaptchaToken = Annotated[str, Field(min_length=1, max_length=2048)]


class CaptchaLogin(Login):
    captcha_token: CaptchaToken | None = None


class CaptchaSignup(Signup):
    captcha_token: CaptchaToken | None = None


class EmailRequest(ResetPassword):
    captcha_token: CaptchaToken | None = None


class EmailCode(ResetPassword):
    # Accommodates the project's configured OTP length (6 to 10 digits).
    code: Annotated[str, Field(pattern=r"^[0-9]{6,10}$")]


class ResetComplete(EmailCode):
    # Password bytes must never be silently stripped or lowercased.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    new_password: Annotated[str, Field(min_length=12, max_length=128)]

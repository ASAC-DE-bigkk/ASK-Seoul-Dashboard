"""인증·회원 API 요청 계약."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    terms_accepted: bool


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = False
    next: str | None = None


class MfaLoginRequest(BaseModel):
    challenge: str = Field(min_length=20, max_length=300)
    code: str = Field(min_length=6, max_length=40)
    next: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResendVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=20, max_length=300)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=300)
    password: str = Field(min_length=1, max_length=128)


class NicknamePatch(BaseModel):
    nickname: str = Field(min_length=3, max_length=40)


class PasswordPatch(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class MfaSetupRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    current_code: str = Field(default="", max_length=40)


class MfaConfirmRequest(BaseModel):
    setup_token: str = Field(min_length=20, max_length=300)
    code: str = Field(min_length=6, max_length=12)


class MfaManageRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=40)


class PreferencePatch(BaseModel):
    ontology: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)


class PaymentCreate(BaseModel):
    plan_code: str = Field(min_length=2, max_length=20)


class PermissionEntry(BaseModel):
    page_key: str = Field(min_length=1, max_length=50)
    allowed: bool


class UserAdminPatch(BaseModel):
    role: str | None = None
    status: str | None = None
    membership_ends_at: str | None = None
    permissions: list[PermissionEntry] | None = Field(default=None, max_length=100)


class MfaAdminResetRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


class PermissionPatch(BaseModel):
    permissions: list[PermissionEntry] = Field(max_length=100)


class PolicyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    policy_type: str
    scope_type: str
    scope_role: str | None = None
    scope_user_public_id: str | None = None
    priority: int = Field(default=100, ge=0, le=10_000)
    config: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class PolicyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    config: dict[str, Any] | None = None
    active: bool | None = None


class IpBlockCreate(BaseModel):
    network: str = Field(min_length=3, max_length=64)
    reason: str = Field(default="", max_length=300)
    expires_at: str | None = None


class PaymentReview(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)

"""인증·회원·권한·정책·모의결제 RDB 모델.

정수 PK는 PostgreSQL sequence/identity, MySQL AUTO_INCREMENT, SQLite rowid로 매핑된다.
외부 노출 식별자는 추측 방지를 위해 별도 UUID(public_id)를 사용한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.utcnow()


def new_public_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "auth_schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AuthControl(Base):
    """관리자 역할/상태 전이를 RDB 전체에서 직렬화하는 단일 제어 행."""

    __tablename__ = "auth_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class User(Base):
    __tablename__ = "auth_users"
    __table_args__ = (
        Index("ix_auth_users_status_role", "status", "role"),
        Index("ix_auth_users_membership_end", "membership_ends_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=new_public_id
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    nickname: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="guest", index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    terms_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    membership_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mfa_enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mfa_seed_salt: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    mfa_last_counter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    preferences: Mapped[Optional["UserPreference"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user_active", "user_id", "revoked_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    remembered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    user: Mapped[User] = relationship(back_populates="sessions")


class AccountToken(Base):
    __tablename__ = "auth_account_tokens"
    __table_args__ = (
        Index("ix_auth_tokens_lookup", "purpose", "token_hash", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MfaChallenge(Base):
    __tablename__ = "auth_mfa_challenges"
    __table_args__ = (
        Index("ix_auth_mfa_challenge_lookup", "token_hash", "expires_at", "consumed_at"),
        Index("ix_auth_mfa_challenge_user", "user_id", "purpose", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    setup_salt: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    remember: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MfaRecoveryCode(Base):
    __tablename__ = "auth_mfa_recovery_codes"
    __table_args__ = (
        UniqueConstraint("user_id", "code_hash", name="uq_auth_mfa_recovery_code"),
        Index("ix_auth_mfa_recovery_active", "user_id", "used_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PageResource(Base):
    __tablename__ = "auth_page_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    path_pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RolePagePermission(Base):
    __tablename__ = "auth_role_page_permissions"
    __table_args__ = (
        UniqueConstraint("role", "page_id", name="uq_auth_role_page"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("auth_page_resources.id", ondelete="CASCADE"), nullable=False
    )
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserPagePermission(Base):
    __tablename__ = "auth_user_page_permissions"
    __table_args__ = (
        UniqueConstraint("user_id", "page_id", name="uq_auth_user_page"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("auth_page_resources.id", ondelete="CASCADE"), nullable=False
    )
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)


class UserPreference(Base):
    __tablename__ = "auth_user_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    ontology: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ui: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="preferences")


class AccessPolicy(Base):
    __tablename__ = "auth_access_policies"
    __table_args__ = (
        Index("ix_auth_policy_effective", "policy_type", "scope_type", "active", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=new_public_id
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_type: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    scope_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class IpBlock(Base):
    __tablename__ = "auth_ip_blocks"
    __table_args__ = (
        Index("ix_auth_ip_blocks_active_expiry", "active", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PaymentPlan(Base):
    __tablename__ = "auth_payment_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KRW")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PaymentRequest(Base):
    __tablename__ = "auth_payment_requests"
    __table_args__ = (
        Index("ix_auth_payment_status_requested", "status", "requested_at"),
        Index("ix_auth_payment_user_status", "user_id", "status"),
        Index("uq_auth_payment_pending_key", "pending_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=new_public_id
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("auth_payment_plans.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    pending_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    review_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    notification_result: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    plan: Mapped[PaymentPlan] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class DashboardLayout(Base):
    __tablename__ = "auth_dashboard_layouts"
    __table_args__ = (
        UniqueConstraint("user_id", "page_public_id", name="uq_auth_user_layout_page"),
        Index("ix_auth_layout_user_order", "user_id", "order_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    page_public_id: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    charts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class NotificationDelivery(Base):
    __tablename__ = "auth_notification_deliveries"
    __table_args__ = (
        Index("ix_auth_notification_created", "created_at"),
        Index("ix_auth_notification_status_attempt", "status", "last_attempt_at"),
        Index(
            "ix_auth_notification_request_status",
            "payment_request_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_request_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_payment_requests.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    target: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AuditLog(Base):
    __tablename__ = "auth_audit_logs"
    __table_args__ = (
        Index("ix_auth_audit_event_created", "event_type", "created_at"),
        Index("ix_auth_audit_target_created", "target_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    target_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True
    )
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

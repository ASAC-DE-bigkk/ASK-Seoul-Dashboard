"""인증·권한·정책·결제 도메인 서비스."""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.notifications import NotificationMessage, NotificationService

from .config import AuthSettings
from .database import Database
from .emailer import EmailResult, EmailSender
from .models import (
    AccessPolicy,
    AccountToken,
    AuditLog,
    AuthSession,
    Base,
    IpBlock,
    NotificationDelivery,
    PageResource,
    PaymentPlan,
    PaymentRequest,
    RolePagePermission,
    SchemaVersion,
    User,
    UserPagePermission,
    UserPreference,
    utcnow,
)
from .security import (
    DUMMY_PASSWORD_HASH,
    ROLE_LABELS,
    hash_password,
    iso_utc,
    mask_public_id,
    normalize_email,
    normalize_nickname,
    random_token,
    role_can_manage,
    token_digest,
    validate_password,
    verify_password,
)


SCHEMA_VERSION = 1

PAGE_DEFINITIONS = (
    ("catalog", "데이터 마켓플레이스", "/catalog", "published gold 카탈로그와 API"),
    ("charts", "Charts Studio", "/charts", "사용자별 gold 시각화 레이아웃"),
    ("profile", "프로필", "/profile", "내 정보·비밀번호·온톨로지 설정"),
    ("billing", "이용권", "/profile#billing", "모의 결제 요청과 이용권 확인"),
    ("api_docs", "API 문서", "/docs", "Swagger/OpenAPI 문서"),
    ("admin_users", "회원 관리", "/admin#users", "회원 승인·역할·이용권"),
    ("admin_access", "접근 관리", "/admin#access", "역할/사용자 페이지 권한"),
    ("admin_policies", "정책 관리", "/admin#policies", "공통·개인·IP·요청량 정책"),
    ("admin_payments", "결제 승인", "/admin#payments", "모의 결제 요청 승인"),
)

DEFAULT_ROLE_ACCESS = {
    "guest": {"catalog", "profile", "billing"},
    "member": {"catalog", "charts", "profile", "billing"},
    "operator": {
        "catalog",
        "charts",
        "profile",
        "api_docs",
        "admin_users",
        "admin_access",
        "admin_policies",
        "admin_payments",
    },
    "admin": {key for key, *_ in PAGE_DEFINITIONS},
}

DEFAULT_RATE_LIMIT = {
    "anonymous": {"second": 8, "minute": 120, "hour": 2_000, "day": 10_000},
    "authenticated": {"second": 20, "minute": 600, "hour": 10_000, "day": 50_000},
    "login": {"minute": 5, "hour": 20},
    "register": {"hour": 5, "day": 15},
    "forgot_password": {"hour": 5, "day": 10},
    "action": "reject",
}

DEFAULT_ONTOLOGY = {
    "hidden_chart_types": [],
    "chart_label_overrides": {},
    "value_label_overrides": {},
    "source_label_overrides": {},
    "default_domain": "all",
}

PAYMENT_PLANS = (
    ("daily", "일일 이용권", 1, 1_000),
    ("weekly", "주간 이용권", 7, 5_000),
    ("monthly", "월간 이용권", 30, 15_000),
    ("annual", "연간 이용권", 365, 150_000),
)

NICK_ADJECTIVES = (
    "푸른",
    "맑은",
    "고요한",
    "빛나는",
    "든든한",
    "빠른",
    "따뜻한",
    "슬기로운",
)
NICK_NOUNS = ("한강", "해치", "수달", "참새", "은행나무", "달빛", "남산", "느티나무")


class DomainError(Exception):
    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def audit(
    db: Session,
    event_type: str,
    *,
    actor: User | None = None,
    target: User | None = None,
    ip_hash: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            event_type=event_type,
            actor_user_id=actor.id if actor else None,
            target_user_id=target.id if target else None,
            ip_hash=ip_hash,
            details=details or {},
        )
    )


def initialize_database(database: Database, settings: AuthSettings) -> None:
    Base.metadata.create_all(database.engine)
    with database.session() as db:
        version = db.scalar(select(SchemaVersion).order_by(SchemaVersion.version.desc()))
        if version is None:
            db.add(SchemaVersion(version=SCHEMA_VERSION))
        elif version.version != SCHEMA_VERSION:
            raise RuntimeError(
                f"인증 DB schema version {version.version}은 앱 버전 {SCHEMA_VERSION}과 다릅니다."
            )
        _seed_pages(db)
        _seed_plans(db)
        _seed_policies(db)
        _bootstrap_admin(db, settings)


def _seed_pages(db: Session) -> None:
    existing = {p.key: p for p in db.scalars(select(PageResource)).all()}
    for key, label, path, description in PAGE_DEFINITIONS:
        if key not in existing:
            db.add(
                PageResource(
                    key=key,
                    label=label,
                    path_pattern=path,
                    description=description,
                )
            )
    db.flush()
    pages = {p.key: p for p in db.scalars(select(PageResource)).all()}
    for role, allowed_keys in DEFAULT_ROLE_ACCESS.items():
        for key, page in pages.items():
            row = db.scalar(
                select(RolePagePermission).where(
                    RolePagePermission.role == role,
                    RolePagePermission.page_id == page.id,
                )
            )
            if row is None:
                db.add(
                    RolePagePermission(
                        role=role, page_id=page.id, allowed=key in allowed_keys
                    )
                )


def _seed_plans(db: Session) -> None:
    existing = {p.code for p in db.scalars(select(PaymentPlan)).all()}
    for code, label, days, price in PAYMENT_PLANS:
        if code not in existing:
            db.add(
                PaymentPlan(
                    code=code,
                    label=label,
                    duration_days=days,
                    price_amount=price,
                    currency="KRW",
                )
            )


def _seed_policies(db: Session) -> None:
    found = db.scalar(
        select(AccessPolicy).where(
            AccessPolicy.policy_type == "request_limit",
            AccessPolicy.scope_type == "system",
            AccessPolicy.name == "기본 요청 제한",
        )
    )
    if found is None:
        db.add(
            AccessPolicy(
                name="기본 요청 제한",
                policy_type="request_limit",
                scope_type="system",
                priority=10,
                config=DEFAULT_RATE_LIMIT,
            )
        )
    ontology = db.scalar(
        select(AccessPolicy).where(
            AccessPolicy.policy_type == "ontology",
            AccessPolicy.scope_type == "system",
            AccessPolicy.name == "기본 온톨로지",
        )
    )
    if ontology is None:
        db.add(
            AccessPolicy(
                name="기본 온톨로지",
                policy_type="ontology",
                scope_type="system",
                priority=10,
                config=DEFAULT_ONTOLOGY,
            )
        )


def _bootstrap_admin(db: Session, settings: AuthSettings) -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    email = normalize_email(settings.bootstrap_admin_email)
    if db.scalar(select(User).where(User.email == email)):
        return
    validate_password(settings.bootstrap_admin_password, email=email)
    admin = User(
        email=email,
        password_hash=hash_password(settings.bootstrap_admin_password),
        nickname=_unique_nickname(db),
        role="admin",
        status="active",
        email_verified_at=utcnow(),
        approved_at=utcnow(),
    )
    db.add(admin)
    db.flush()
    audit(db, "bootstrap_admin_created", target=admin)


def _unique_nickname(db: Session) -> str:
    for _ in range(100):
        value = f"{random.choice(NICK_ADJECTIVES)}-{random.choice(NICK_NOUNS)}-{random.randint(1000, 9999)}"
        if not db.scalar(select(User.id).where(User.nickname == value)):
            return value
    return f"seoul-{random_token(8)[:10]}"


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def user_payload(db: Session, user: User, *, include_email: bool = True) -> dict:
    allowed = AccessService(db).allowed_pages(user)
    return {
        "masked_id": mask_public_id(user.public_id),
        "email": user.email if include_email else "",
        "nickname": user.nickname,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "status": user.status,
        "email_verified": user.email_verified_at is not None,
        "membership_ends_at": iso_utc(user.membership_ends_at),
        "last_login_at": iso_utc(user.last_login_at),
        "created_at": iso_utc(user.created_at),
        "allowed_pages": allowed,
    }


class AccessService:
    def __init__(self, db: Session):
        self.db = db

    def allowed_pages(self, user: User) -> list[str]:
        pages = {p.id: p.key for p in self.db.scalars(select(PageResource).where(PageResource.active)).all()}
        allowed: dict[int, bool] = {
            row.page_id: row.allowed
            for row in self.db.scalars(
                select(RolePagePermission).where(RolePagePermission.role == user.role)
            ).all()
        }
        for row in self.db.scalars(
            select(UserPagePermission).where(UserPagePermission.user_id == user.id)
        ).all():
            allowed[row.page_id] = row.allowed
        return sorted(pages[page_id] for page_id, value in allowed.items() if value and page_id in pages)

    def can_access(self, user: User, page_key: str) -> bool:
        if user.status != "active":
            return False
        if user.role in {"guest", "member"} and page_key not in {"profile", "billing"}:
            if user.membership_ends_at is not None and user.membership_ends_at < utcnow():
                return False
        return page_key in self.allowed_pages(user)

    def effective_policy(self, user: User | None, policy_type: str) -> dict:
        conditions = [
            and_(AccessPolicy.scope_type == "system"),
        ]
        if user is not None:
            conditions.extend(
                [
                    and_(
                        AccessPolicy.scope_type == "role",
                        AccessPolicy.scope_role == user.role,
                    ),
                    and_(
                        AccessPolicy.scope_type == "user",
                        AccessPolicy.scope_user_id == user.id,
                    ),
                ]
            )
        rows = list(self.db.scalars(
            select(AccessPolicy)
            .where(
                AccessPolicy.policy_type == policy_type,
                AccessPolicy.active.is_(True),
                or_(*conditions),
            )
        ).all())
        scope_order = {"system": 0, "role": 1, "user": 2}
        rows.sort(
            key=lambda row: (
                scope_order.get(row.scope_type, 99),
                row.priority,
                row.id,
            )
        )
        result: dict = {}
        for row in rows:
            result = _deep_merge(result, row.config or {})
        return result

    def effective_ontology(self, user: User) -> dict:
        result = self.effective_policy(user, "ontology")
        preference = self.db.scalar(
            select(UserPreference).where(UserPreference.user_id == user.id)
        )
        if preference:
            result = _deep_merge(result, preference.ontology or {})
        return result


class AuthService:
    def __init__(
        self,
        db: Session,
        settings: AuthSettings,
        email_sender: EmailSender | None = None,
    ):
        self.db = db
        self.settings = settings
        self.email_sender = email_sender or EmailSender()

    def register(self, email_value: str, password: str, ip_hash: str) -> tuple[User, EmailResult]:
        try:
            email = normalize_email(email_value)
            validate_password(password, email=email)
        except ValueError as exc:
            raise DomainError(400, "invalid registration", str(exc)) from exc
        if self.db.scalar(select(User.id).where(User.email == email)):
            raise DomainError(409, "registration unavailable", "이미 가입된 이메일입니다.")
        user = User(
            email=email,
            password_hash=hash_password(password),
            nickname=_unique_nickname(self.db),
            role="guest",
            status="pending",
        )
        self.db.add(user)
        self.db.flush()
        audit(self.db, "user_registered", target=user, ip_hash=ip_hash)

        if not self.email_sender.configured:
            return user, EmailResult(configured=False, delivered=False)
        token = self._issue_token(user, "verify_email", self.settings.verify_token_hours * 60)
        url = f"{self.settings.public_base_url}/api/v1/auth/verify-email?token={token}"
        result = self.email_sender.send(
            to_email=user.email,
            subject="[ASK SEOUL] 이메일 인증",
            text=(
                "ASK SEOUL 가입 이메일 인증 링크입니다.\n\n"
                f"{url}\n\n"
                f"{self.settings.verify_token_hours}시간 후 만료되며 한 번만 사용할 수 있습니다."
            ),
        )
        audit(
            self.db,
            "verification_email_requested",
            target=user,
            ip_hash=ip_hash,
            details={"configured": result.configured, "delivered": result.delivered},
        )
        return user, result

    def verify_email_token(self, raw_token: str, ip_hash: str) -> User:
        token = self._consume_token(raw_token, "verify_email")
        user = self.db.get(User, token.user_id)
        if user is None:
            raise DomainError(400, "invalid token", "유효하지 않은 인증 링크입니다.")
        user.email_verified_at = utcnow()
        if self.settings.auto_approve_verified and user.status == "pending":
            user.status = "active"
            user.approved_at = utcnow()
        audit(self.db, "email_verified", target=user, ip_hash=ip_hash)
        return user

    def login(
        self,
        email_value: str,
        password: str,
        *,
        remember: bool,
        ip_hash: str,
        user_agent_hash: str,
    ) -> tuple[User, str, str, datetime]:
        try:
            email = normalize_email(email_value)
        except ValueError:
            email = ""
        user = self.db.scalar(select(User).where(User.email == email)) if email else None
        if user is None:
            # 존재하지 않는 계정도 Argon2 비용을 소모해 시간 기반 열거를 줄인다.
            verify_password(DUMMY_PASSWORD_HASH, password)
            raise DomainError(401, "login failed", "이메일 또는 비밀번호를 확인하세요.")
        now = utcnow()
        if user.locked_until and user.locked_until > now:
            raise DomainError(429, "account locked", "잠시 후 다시 시도하세요.")
        ok, needs_rehash = verify_password(user.password_hash, password)
        if not ok:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = now + timedelta(minutes=15)
                user.failed_login_count = 0
            audit(self.db, "login_failed", target=user, ip_hash=ip_hash)
            raise DomainError(401, "login failed", "이메일 또는 비밀번호를 확인하세요.")
        if user.status == "pending":
            raise DomainError(
                403,
                "approval pending",
                "이메일 인증 또는 관리자 승인을 기다리고 있습니다.",
            )
        if user.status != "active":
            raise DomainError(403, "account unavailable", "사용할 수 없는 계정입니다.")
        if needs_rehash:
            user.password_hash = hash_password(password)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        raw_token = random_token()
        csrf_token = random_token(24)
        expires = now + (
            timedelta(days=self.settings.remember_days)
            if remember
            else timedelta(hours=self.settings.session_hours)
        )
        self.db.add(
            AuthSession(
                token_hash=token_digest(raw_token, self.settings.session_pepper),
                csrf_hash=token_digest(csrf_token, self.settings.session_pepper),
                user_id=user.id,
                expires_at=expires,
                ip_hash=ip_hash,
                user_agent_hash=user_agent_hash,
            )
        )
        audit(self.db, "login_succeeded", actor=user, target=user, ip_hash=ip_hash)
        return user, raw_token, csrf_token, expires

    def current_session(
        self,
        raw_token: str | None,
        *,
        ip_hash: str = "",
        user_agent_hash: str = "",
    ) -> tuple[AuthSession, User] | None:
        if not raw_token:
            return None
        digest = token_digest(raw_token, self.settings.session_pepper)
        row = self.db.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == digest,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > utcnow(),
            )
        )
        if row is None:
            return None
        user = self.db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        if (
            self.settings.bind_session_user_agent
            and row.user_agent_hash
            and row.user_agent_hash != user_agent_hash
        ):
            row.revoked_at = utcnow()
            audit(self.db, "session_binding_mismatch", target=user, ip_hash=ip_hash)
            return None
        if self.settings.bind_session_ip and row.ip_hash and row.ip_hash != ip_hash:
            row.revoked_at = utcnow()
            audit(self.db, "session_binding_mismatch", target=user, ip_hash=ip_hash)
            return None
        if row.last_seen_at < utcnow() - timedelta(minutes=5):
            row.last_seen_at = utcnow()
        return row, user

    def logout(self, session: AuthSession | None, ip_hash: str) -> None:
        if session and session.revoked_at is None:
            session.revoked_at = utcnow()
            user = self.db.get(User, session.user_id)
            audit(self.db, "logout", actor=user, target=user, ip_hash=ip_hash)

    def request_password_reset(
        self,
        email_value: str,
        ip_hash: str,
        enqueue: Callable[..., Any] | None = None,
    ) -> EmailResult:
        try:
            email = normalize_email(email_value)
        except ValueError:
            return EmailResult(configured=self.email_sender.configured, delivered=False)
        user = self.db.scalar(select(User).where(User.email == email))
        if user is None or user.status == "rejected":
            return EmailResult(configured=self.email_sender.configured, delivered=False)
        if not self.email_sender.configured:
            audit(self.db, "password_reset_unavailable", target=user, ip_hash=ip_hash)
            return EmailResult(configured=False, delivered=False)
        token = self._issue_token(user, "reset_password", self.settings.reset_token_minutes)
        url = f"{self.settings.public_base_url}/auth/reset-password?token={token}"
        subject = "[ASK SEOUL] 비밀번호 재설정"
        text = (
            "비밀번호 재설정 링크입니다.\n\n"
            f"{url}\n\n"
            f"{self.settings.reset_token_minutes}분 후 만료되며 한 번만 사용할 수 있습니다."
        )
        if enqueue is not None:
            enqueue(
                self.email_sender.send,
                to_email=user.email,
                subject=subject,
                text=text,
            )
            result = EmailResult(configured=True, delivered=False)
        else:
            result = self.email_sender.send(
                to_email=user.email,
                subject=subject,
                text=text,
            )
        audit(
            self.db,
            "password_reset_requested",
            target=user,
            ip_hash=ip_hash,
            details={
                "configured": result.configured,
                "delivered": result.delivered,
                "queued": enqueue is not None,
            },
        )
        return result

    def reset_password(self, raw_token: str, new_password: str, ip_hash: str) -> User:
        token = self._consume_token(raw_token, "reset_password")
        user = self.db.get(User, token.user_id)
        if user is None:
            raise DomainError(400, "invalid token", "유효하지 않은 재설정 링크입니다.")
        try:
            validate_password(new_password, email=user.email, nickname=user.nickname)
        except ValueError as exc:
            raise DomainError(400, "invalid password", str(exc)) from exc
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        self.revoke_user_sessions(user.id)
        audit(self.db, "password_reset_completed", target=user, ip_hash=ip_hash)
        return user

    def change_password(
        self, user: User, current_password: str, new_password: str, ip_hash: str
    ) -> None:
        ok, _ = verify_password(user.password_hash, current_password)
        if not ok:
            raise DomainError(400, "password mismatch", "현재 비밀번호가 올바르지 않습니다.")
        try:
            validate_password(new_password, email=user.email, nickname=user.nickname)
        except ValueError as exc:
            raise DomainError(400, "invalid password", str(exc)) from exc
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        self.revoke_user_sessions(user.id)
        audit(self.db, "password_changed", actor=user, target=user, ip_hash=ip_hash)

    def update_nickname(self, user: User, value: str, ip_hash: str) -> User:
        try:
            nickname = normalize_nickname(value)
        except ValueError as exc:
            raise DomainError(400, "invalid nickname", str(exc)) from exc
        duplicate = self.db.scalar(
            select(User.id).where(User.nickname == nickname, User.id != user.id)
        )
        if duplicate:
            raise DomainError(409, "nickname conflict", "이미 사용 중인 닉네임입니다.")
        user.nickname = nickname
        audit(self.db, "nickname_changed", actor=user, target=user, ip_hash=ip_hash)
        return user

    def _issue_token(self, user: User, purpose: str, ttl_minutes: int) -> str:
        self.db.execute(
            delete(AccountToken).where(
                AccountToken.user_id == user.id,
                AccountToken.purpose == purpose,
                AccountToken.consumed_at.is_(None),
            )
        )
        raw = random_token()
        self.db.add(
            AccountToken(
                user_id=user.id,
                purpose=purpose,
                token_hash=token_digest(raw, self.settings.session_pepper),
                expires_at=utcnow() + timedelta(minutes=ttl_minutes),
            )
        )
        return raw

    def _consume_token(self, raw: str, purpose: str) -> AccountToken:
        digest = token_digest(raw, self.settings.session_pepper)
        token = self.db.scalar(
            select(AccountToken).where(
                AccountToken.token_hash == digest,
                AccountToken.purpose == purpose,
                AccountToken.consumed_at.is_(None),
                AccountToken.expires_at > utcnow(),
            )
        )
        if token is None:
            raise DomainError(400, "invalid token", "링크가 만료되었거나 이미 사용되었습니다.")
        token.consumed_at = utcnow()
        return token

    def revoke_user_sessions(self, user_id: int) -> None:
        for row in self.db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        ).all():
            row.revoked_at = utcnow()


class PaymentService:
    def __init__(self, db: Session, notifier: NotificationService | None = None):
        self.db = db
        self.notifier = notifier or NotificationService()

    def list_plans(self) -> list[PaymentPlan]:
        return self.db.scalars(
            select(PaymentPlan).where(PaymentPlan.active.is_(True)).order_by(PaymentPlan.duration_days)
        ).all()

    def request(self, user: User, plan_code: str, ip_hash: str) -> tuple[PaymentRequest, dict]:
        if user.role not in {"guest", "member"}:
            raise DomainError(400, "payment unnecessary", "운영자 이상 계정은 이용권 결제가 필요하지 않습니다.")
        plan = self.db.scalar(
            select(PaymentPlan).where(PaymentPlan.code == plan_code, PaymentPlan.active.is_(True))
        )
        if plan is None:
            raise DomainError(404, "plan not found", "선택한 이용권이 없습니다.")
        pending = self.db.scalar(
            select(PaymentRequest).where(
                PaymentRequest.user_id == user.id, PaymentRequest.status == "pending"
            )
        )
        if pending:
            raise DomainError(409, "payment pending", "이미 승인 대기 중인 결제 요청이 있습니다.")
        request = PaymentRequest(user_id=user.id, plan_id=plan.id)
        self.db.add(request)
        self.db.flush()
        notification = self.notifier.send(
            NotificationMessage(
                title="ASK SEOUL 이용권 승인 요청",
                body="운영자 또는 최고관리자의 확인이 필요한 모의 결제 요청입니다.",
                severity="notice",
                fields={
                    "request_id": request.public_id,
                    "user": user.nickname,
                    "masked_id": mask_public_id(user.public_id),
                    "role": ROLE_LABELS[user.role],
                    "plan": plan.label,
                    "duration_days": plan.duration_days,
                    "amount": f"{plan.price_amount:,} {plan.currency}",
                },
            )
        )
        result = notification.as_dict()
        request.notification_result = result
        for item in notification.deliveries:
            self.db.add(
                NotificationDelivery(
                    payment_request_id=request.id,
                    channel=item.channel,
                    target=item.target,
                    status="delivered" if item.delivered else (
                        "not_configured" if not item.configured else "failed"
                    ),
                    error=item.error,
                )
            )
        audit(
            self.db,
            "payment_requested",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"request_id": request.public_id, "plan": plan.code, **result},
        )
        return request, result

    def review(
        self,
        actor: User,
        request_public_id: str,
        *,
        approve: bool,
        note: str,
        ip_hash: str,
    ) -> PaymentRequest:
        row = self.db.scalar(
            select(PaymentRequest).where(PaymentRequest.public_id == request_public_id)
        )
        if row is None:
            raise DomainError(404, "payment not found", "결제 요청을 찾을 수 없습니다.")
        if row.status != "pending":
            raise DomainError(409, "already reviewed", "이미 처리된 결제 요청입니다.")
        target = self.db.get(User, row.user_id)
        if target is None or not role_can_manage(actor.role, target.role):
            raise DomainError(403, "forbidden", "이 결제 요청을 처리할 권한이 없습니다.")
        row.status = "approved" if approve else "rejected"
        row.reviewed_at = utcnow()
        row.reviewed_by_id = actor.id
        row.review_note = note[:500]
        if approve:
            plan = self.db.get(PaymentPlan, row.plan_id)
            base = max(utcnow(), target.membership_ends_at or utcnow())
            target.membership_ends_at = base + timedelta(days=plan.duration_days)
            if target.status == "pending":
                target.status = "active"
                target.approved_at = utcnow()
                target.approved_by_id = actor.id
        audit(
            self.db,
            "payment_reviewed",
            actor=actor,
            target=target,
            ip_hash=ip_hash,
            details={"request_id": row.public_id, "status": row.status},
        )
        return row


def serialize_policy(db: Session, row: AccessPolicy) -> dict:
    scope_user = db.get(User, row.scope_user_id) if row.scope_user_id else None
    return {
        "id": row.public_id,
        "name": row.name,
        "policy_type": row.policy_type,
        "scope_type": row.scope_type,
        "scope_role": row.scope_role,
        "scope_user_public_id": scope_user.public_id if scope_user else None,
        "priority": row.priority,
        "config": row.config,
        "active": row.active,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


def serialize_payment(row: PaymentRequest) -> dict:
    return {
        "id": row.public_id,
        "status": row.status,
        "requested_at": iso_utc(row.requested_at),
        "reviewed_at": iso_utc(row.reviewed_at),
        "review_note": row.review_note,
        "notification_result": row.notification_result,
        "plan": {
            "code": row.plan.code,
            "label": row.plan.label,
            "duration_days": row.plan.duration_days,
            "price_amount": row.plan.price_amount,
            "currency": row.plan.currency,
        },
        "user": {
            "nickname": row.user.nickname,
            "masked_id": mask_public_id(row.user.public_id),
            "role": row.user.role,
            "role_label": ROLE_LABELS.get(row.user.role, row.user.role),
        },
    }

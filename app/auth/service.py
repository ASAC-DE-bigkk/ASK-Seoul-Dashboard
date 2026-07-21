"""인증·권한·정책·결제 도메인 서비스."""
from __future__ import annotations

import random
import hmac
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    and_,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.notifications import NotificationMessage, NotificationService

from .config import AuthSettings
from .database import Database
from .emailer import EmailResult, EmailSender
from .models import (
    AccessPolicy,
    AccountToken,
    AuditLog,
    AuthControl,
    AuthSession,
    Base,
    IpBlock,
    MfaChallenge,
    MfaRecoveryCode,
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
    normalize_recovery_code,
    random_token,
    recovery_code,
    role_can_manage,
    derive_totp_secret,
    stable_digest,
    token_digest,
    verify_totp,
    validate_password,
    verify_password,
)


SCHEMA_VERSION = 6
TERMS_VERSION = "2026-07-17"
LOCAL_ANALYST_EMAIL = "local-analyst@localhost.invalid"
LOCAL_ANALYST_NICKNAME = "local-analyst"

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
    ("admin_audit", "감사 로그", "/admin#audit", "보안·관리 이벤트 조회"),
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
        "admin_audit",
    },
    "admin": {key for key, *_ in PAGE_DEFINITIONS},
}
ADMIN_PAGE_KEYS = {
    "admin_users",
    "admin_access",
    "admin_policies",
    "admin_payments",
    "admin_audit",
}

DEFAULT_RATE_LIMIT = {
    "anonymous": {"second": 8, "minute": 120, "hour": 2_000, "day": 10_000},
    "authenticated": {"second": 20, "minute": 600, "hour": 10_000, "day": 50_000},
    "login": {"minute": 5, "hour": 20},
    "register": {"hour": 5, "day": 15},
    "verify_email": {"hour": 5, "day": 15},
    "forgot_password": {"hour": 5, "day": 10},
    "password_reset": {"hour": 10, "day": 30},
    "mfa": {"minute": 10, "hour": 50},
    "charts_query": {"second": 8, "minute": 120, "hour": 1_000, "day": 5_000},
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
    existing_tables = set(inspect(database.engine).get_table_names())
    existing_auth_tables = {
        name for name in existing_tables if name.startswith("auth_")
    }
    if (
        existing_auth_tables
        and "auth_schema_version" not in existing_tables
    ):
        raise RuntimeError(
            "버전 정보가 없는 기존 auth 스키마는 자동 마이그레이션하지 않습니다."
        )
    existing_version: int | None = None
    if "auth_schema_version" in existing_tables:
        with database.engine.connect() as connection:
            existing_version = connection.scalar(
                select(SchemaVersion.version)
                .order_by(SchemaVersion.version.desc())
                .limit(1)
            )
        if existing_version is None:
            raise RuntimeError(
                "기존 auth 스키마의 버전 행이 비어 있어 자동 기동할 수 없습니다."
            )
        if existing_version < 1 or existing_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"인증 DB schema version {existing_version}은 앱 버전 "
                f"{SCHEMA_VERSION}과 다릅니다."
            )
        if existing_version == SCHEMA_VERSION:
            verify_database_schema(database)
    Base.metadata.create_all(database.engine)
    with database.session() as db:
        _seed_control(db)
        # 마이그레이션과 기본 데이터 시드를 다중 인스턴스 사이에서 직렬화한다.
        claimed = db.execute(
            update(AuthControl)
            .where(AuthControl.id == 1)
            .values(revision=AuthControl.revision + 1, updated_at=utcnow())
        )
        if claimed.rowcount != 1:
            raise RuntimeError("인증 DB 전역 제어 행을 획득할 수 없습니다.")
        version = db.scalar(select(SchemaVersion).order_by(SchemaVersion.version.desc()))
        if version is None:
            if existing_auth_tables:
                raise RuntimeError(
                    "기존 auth 스키마의 버전 행이 비어 있어 자동 기동할 수 없습니다."
                )
            try:
                with db.begin_nested():
                    db.add(SchemaVersion(version=SCHEMA_VERSION))
                    db.flush()
            except IntegrityError:
                version = db.scalar(
                    select(SchemaVersion).order_by(SchemaVersion.version.desc())
                )
                if version is None or version.version != SCHEMA_VERSION:
                    raise RuntimeError("인증 DB schema version 초기화가 충돌했습니다.")
        elif version.version < SCHEMA_VERSION:
            _migrate_schema(database, db, version.version)
        elif version.version > SCHEMA_VERSION:
            raise RuntimeError(
                f"인증 DB schema version {version.version}은 앱 버전 {SCHEMA_VERSION}과 다릅니다."
            )
        _seed_pages(db)
        _seed_plans(db)
        _seed_policies(db)
        _bootstrap_admin(db, settings)


def verify_database_schema(database: Database, *, deep: bool = True) -> None:
    """worker와 dry-run에서 쓰기 없이 현재 DB 계약만 확인한다."""
    inspector = inspect(database.engine)
    existing_tables = set(inspector.get_table_names())
    required_tables = set(Base.metadata.tables)
    missing = sorted(required_tables - existing_tables)
    if missing:
        raise RuntimeError(
            "인증 DB 스키마가 초기화되지 않았습니다. "
            "scripts/init_auth_db.py를 먼저 실행하세요. "
            f"누락 테이블: {', '.join(missing)}"
        )
    with database.engine.connect() as connection:
        version = connection.scalar(
            select(SchemaVersion.version)
            .order_by(SchemaVersion.version.desc())
            .limit(1)
        )
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"인증 DB schema version {version!r}은 앱 버전 {SCHEMA_VERSION}과 다릅니다. "
            "migration job을 먼저 실행하세요."
        )
    if not deep:
        return

    issues: list[str] = []
    for table_name, expected in Base.metadata.tables.items():
        actual_columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        missing_columns = sorted(
            column.name for column in expected.columns if column.name not in actual_columns
        )
        if missing_columns:
            issues.append(f"{table_name} columns={','.join(missing_columns)}")

        expected_pk = tuple(column.name for column in expected.primary_key.columns)
        actual_pk = tuple(
            inspector.get_pk_constraint(table_name).get(
                "constrained_columns", ()
            )
            or ()
        )
        if expected_pk != actual_pk:
            issues.append(f"{table_name} primary_key")

        expected_indexes = {
            index.name for index in expected.indexes if index.name is not None
        }
        actual_indexes = {
            item["name"] for item in inspector.get_indexes(table_name)
        }
        missing_indexes = sorted(expected_indexes - actual_indexes)
        if missing_indexes:
            issues.append(f"{table_name} indexes={','.join(missing_indexes)}")

        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in expected.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table_name)
        }
        missing_unique = sorted(expected_unique - actual_unique)
        if missing_unique:
            formatted = "|".join(",".join(columns) for columns in missing_unique)
            issues.append(f"{table_name} unique={formatted}")

        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].column.table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "").upper(),
            )
            for constraint in expected.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table") or "",
                tuple(item.get("referred_columns") or ()),
                str((item.get("options") or {}).get("ondelete") or "").upper(),
            )
            for item in inspector.get_foreign_keys(table_name)
        }
        missing_foreign_keys = expected_foreign_keys - actual_foreign_keys
        if missing_foreign_keys:
            issues.append(f"{table_name} foreign_keys")
    if issues:
        raise RuntimeError(
            "인증 DB 스키마 계약이 손상되었습니다: " + "; ".join(issues)
        )


def prepare_database_for_app(
    database: Database, settings: AuthSettings
) -> None:
    """운영 앱은 스키마를 변경하지 않고 배포 전 migration 결과만 검증한다."""
    if settings.production:
        verify_database_schema(database)
        return
    initialize_database(database, settings)


def _migrate_schema(database: Database, db: Session, current_version: int) -> None:
    """배포된 v1 DB의 additive 변경만 수행한다.

    파괴적 변경은 지원하지 않으며 향후 복잡한 변경은 Alembic revision으로 승격한다.
    """
    version = current_version
    if version == 1:
        columns = {
            item["name"] for item in inspect(database.engine).get_columns("auth_users")
        }
        dialect = database.engine.dialect
        additions = (
            ("mfa_enabled_at", DateTime()),
            ("mfa_seed_salt", String(64)),
            ("mfa_last_counter", Integer()),
        )
        for name, column_type in additions:
            if name in columns:
                continue
            type_sql = column_type.compile(dialect=dialect)
            db.execute(
                text(f"ALTER TABLE auth_users ADD COLUMN {name} {type_sql} NULL")
            )
        db.add(SchemaVersion(version=2))
        version = 2
    if version == 2:
        columns = {
            item["name"] for item in inspect(database.engine).get_columns("auth_users")
        }
        dialect = database.engine.dialect
        additions = (
            ("terms_accepted_at", DateTime()),
            ("terms_version", String(20)),
        )
        for name, column_type in additions:
            if name in columns:
                continue
            type_sql = column_type.compile(dialect=dialect)
            db.execute(
                text(f"ALTER TABLE auth_users ADD COLUMN {name} {type_sql} NULL")
            )
        db.add(SchemaVersion(version=3))
        version = 3
    if version == 3:
        payment_columns = {
            item["name"]
            for item in inspect(database.engine).get_columns("auth_payment_requests")
        }
        delivery_columns = {
            item["name"]
            for item in inspect(database.engine).get_columns(
                "auth_notification_deliveries"
            )
        }
        dialect = database.engine.dialect
        if "pending_key" not in payment_columns:
            type_sql = String(64).compile(dialect=dialect)
            db.execute(
                text(
                    f"ALTER TABLE auth_payment_requests ADD COLUMN pending_key {type_sql} NULL"
                )
            )
        if "attempts" not in delivery_columns:
            type_sql = Integer().compile(dialect=dialect)
            db.execute(
                text(
                    "ALTER TABLE auth_notification_deliveries "
                    f"ADD COLUMN attempts {type_sql} NOT NULL DEFAULT 0"
                )
            )
        if "last_attempt_at" not in delivery_columns:
            type_sql = DateTime().compile(dialect=dialect)
            db.execute(
                text(
                    "ALTER TABLE auth_notification_deliveries "
                    f"ADD COLUMN last_attempt_at {type_sql} NULL"
                )
            )
        seen_users: set[int] = set()
        pending_rows = db.scalars(
            select(PaymentRequest)
            .where(PaymentRequest.status == "pending")
            .order_by(PaymentRequest.requested_at.desc(), PaymentRequest.id.desc())
        ).all()
        for row in pending_rows:
            if row.user_id in seen_users:
                row.status = "rejected"
                row.reviewed_at = utcnow()
                row.review_note = "schema migration: 중복 승인 대기 요청 자동 정리"
                continue
            row.pending_key = f"user:{row.user_id}"
            seen_users.add(row.user_id)
        db.flush()
        index_names = {
            item["name"]
            for item in inspect(database.engine).get_indexes(
                "auth_payment_requests"
            )
        }
        if "uq_auth_payment_pending_key" not in index_names:
            db.execute(
                text(
                    "CREATE UNIQUE INDEX uq_auth_payment_pending_key "
                    "ON auth_payment_requests (pending_key)"
                )
            )
        db.add(SchemaVersion(version=4))
        version = 4
    if version == 4:
        # auth_control table은 initialize_database의 create_all에서 먼저 생성된다.
        db.add(SchemaVersion(version=5))
        version = 5
    if version == 5:
        session_columns = {
            item["name"]
            for item in inspect(database.engine).get_columns("auth_sessions")
        }
        if "remembered" not in session_columns:
            type_sql = Boolean().compile(dialect=database.engine.dialect)
            db.execute(
                text(
                    "ALTER TABLE auth_sessions "
                    f"ADD COLUMN remembered {type_sql} NOT NULL DEFAULT FALSE"
                )
            )
        # v5까지는 remember 여부를 별도 저장하지 않았다. 기본 일반 세션보다
        # 긴 기존 세션만 보수적으로 persistent 세션으로 분류한다.
        for row in db.scalars(select(AuthSession)).all():
            row.remembered = row.expires_at - row.created_at > timedelta(days=1)
        notification_indexes = {
            item["name"]
            for item in inspect(database.engine).get_indexes(
                "auth_notification_deliveries"
            )
        }
        for name, columns in (
            ("ix_auth_notification_created", "created_at"),
            (
                "ix_auth_notification_status_attempt",
                "status, last_attempt_at",
            ),
            (
                "ix_auth_notification_request_status",
                "payment_request_id, status",
            ),
        ):
            if name not in notification_indexes:
                db.execute(
                    text(
                        f"CREATE INDEX {name} "
                        f"ON auth_notification_deliveries ({columns})"
                    )
                )
        db.add(SchemaVersion(version=6))
        version = 6
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"schema {current_version}에서 {SCHEMA_VERSION}으로 마이그레이션할 수 없습니다."
        )


def _seed_pages(db: Session) -> None:
    existing = {p.key: p for p in db.scalars(select(PageResource)).all()}
    for key, label, path, description in PAGE_DEFINITIONS:
        if key not in existing:
            try:
                with db.begin_nested():
                    db.add(
                        PageResource(
                            key=key,
                            label=label,
                            path_pattern=path,
                            description=description,
                        )
                    )
                    db.flush()
            except IntegrityError:
                pass
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
                try:
                    with db.begin_nested():
                        db.add(
                            RolePagePermission(
                                role=role,
                                page_id=page.id,
                                allowed=key in allowed_keys,
                            )
                        )
                        db.flush()
                except IntegrityError:
                    pass


def _seed_plans(db: Session) -> None:
    existing = {p.code for p in db.scalars(select(PaymentPlan)).all()}
    for code, label, days, price in PAYMENT_PLANS:
        if code not in existing:
            try:
                with db.begin_nested():
                    db.add(
                        PaymentPlan(
                            code=code,
                            label=label,
                            duration_days=days,
                            price_amount=price,
                            currency="KRW",
                        )
                    )
                    db.flush()
            except IntegrityError:
                pass


def _seed_policies(db: Session) -> None:
    found = db.scalar(
        select(AccessPolicy).where(
            AccessPolicy.policy_type == "request_limit",
            AccessPolicy.scope_type == "system",
            AccessPolicy.name == "기본 요청 제한",
        )
    )
    if found is None:
        try:
            with db.begin_nested():
                db.add(
                    AccessPolicy(
                        name="기본 요청 제한",
                        policy_type="request_limit",
                        scope_type="system",
                        priority=10,
                        config=DEFAULT_RATE_LIMIT,
                    )
                )
                db.flush()
        except IntegrityError:
            pass
    else:
        found.config = _deep_merge(DEFAULT_RATE_LIMIT, found.config or {})
    ontology = db.scalar(
        select(AccessPolicy).where(
            AccessPolicy.policy_type == "ontology",
            AccessPolicy.scope_type == "system",
            AccessPolicy.name == "기본 온톨로지",
        )
    )
    if ontology is None:
        try:
            with db.begin_nested():
                db.add(
                    AccessPolicy(
                        name="기본 온톨로지",
                        policy_type="ontology",
                        scope_type="system",
                        priority=10,
                        config=DEFAULT_ONTOLOGY,
                    )
                )
                db.flush()
        except IntegrityError:
            pass
    else:
        ontology.config = _deep_merge(DEFAULT_ONTOLOGY, ontology.config or {})


def _seed_control(db: Session) -> None:
    if db.get(AuthControl, 1) is not None:
        return
    try:
        with db.begin_nested():
            db.add(AuthControl(id=1, revision=0))
            db.flush()
    except IntegrityError:
        pass


def _bootstrap_admin(db: Session, settings: AuthSettings) -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    email = normalize_email(settings.bootstrap_admin_email)
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        if existing.role == "admin" and existing.status == "active":
            return
        raise RuntimeError(
            "bootstrap 이메일이 기존 비관리자 계정과 충돌합니다. "
            "scripts/create_admin.py의 명시적 승격 절차를 사용하세요."
        )
    if (db.scalar(select(func.count(User.id))) or 0) > 0:
        raise RuntimeError(
            "bootstrap 최고관리자는 사용자 0명인 신규 DB에서만 생성할 수 있습니다. "
            "bootstrap 환경변수를 제거하고 scripts/create_admin.py를 사용하세요."
        )
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
    try:
        with db.begin_nested():
            db.add(admin)
            db.flush()
    except IntegrityError:
        return
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


def user_payload(
    db: Session,
    user: User,
    *,
    include_email: bool = True,
    allowed_pages: list[str] | None = None,
) -> dict:
    allowed = (
        allowed_pages
        if allowed_pages is not None
        else AccessService(db).allowed_pages(user)
    )
    return {
        "masked_id": mask_public_id(user.public_id),
        "email": user.email if include_email else "",
        "nickname": user.nickname,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "status": user.status,
        "email_verified": user.email_verified_at is not None,
        "mfa_enabled": user.mfa_enabled_at is not None,
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
        if user.role == "admin":
            return sorted(pages.values())
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
        result = {
            pages[page_id]
            for page_id, value in allowed.items()
            if value and page_id in pages
        }
        # 저장된 override가 오래되거나 직접 변조되어도 하위 역할은 운영 영역을
        # 획득할 수 없다는 서버 불변식을 최종 계산 단계에서 강제한다.
        if user.role in {"guest", "member"}:
            result -= ADMIN_PAGE_KEYS
            if (
                user.membership_ends_at is not None
                and user.membership_ends_at < utcnow()
            ):
                result &= {"profile", "billing"}
        return sorted(result)

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

    def _lock_user(self, user: User) -> None:
        """사용자별 보안 상태 전이를 같은 RDB 행 쓰기로 직렬화한다."""
        self.db.flush()
        locked_at = utcnow()
        claimed = self.db.execute(
            update(User)
            .where(User.id == user.id)
            .values(updated_at=locked_at)
        )
        if claimed.rowcount != 1:
            raise DomainError(404, "user not found", "회원을 찾을 수 없습니다.")
        self.db.refresh(user)

    def ensure_local_analyst(self) -> User:
        """로컬 자동 로그인 전용 일반 회원을 확보한다.

        이 계정은 비밀번호로 로그인하지 않으며 local_auto의 fail-closed 설정과
        loopback 요청 검사를 모두 통과한 미들웨어에서만 세션을 발급받는다.
        """
        if not self.settings.local_auto:
            raise RuntimeError("로컬 분석 계정은 AUTH_MODE=local_auto에서만 사용할 수 있습니다.")
        user = self.db.scalar(
            select(User).where(User.email == LOCAL_ANALYST_EMAIL)
        )
        if user is None:
            nickname = LOCAL_ANALYST_NICKNAME
            if self.db.scalar(select(User.id).where(User.nickname == nickname)):
                nickname = _unique_nickname(self.db)
            now = utcnow()
            candidate = User(
                email=LOCAL_ANALYST_EMAIL,
                password_hash=hash_password(random_token(48)),
                nickname=nickname,
                role="member",
                status="active",
                email_verified_at=now,
                approved_at=now,
                terms_accepted_at=now,
                terms_version=TERMS_VERSION,
            )
            try:
                with self.db.begin_nested():
                    self.db.add(candidate)
                    self.db.flush()
                user = candidate
                audit(
                    self.db,
                    "local_analyst_created",
                    target=user,
                    details={"auth_mode": "local_auto"},
                )
            except IntegrityError:
                user = self.db.scalar(
                    select(User).where(User.email == LOCAL_ANALYST_EMAIL)
                )
        if user is None:
            raise RuntimeError("로컬 분석 계정을 생성할 수 없습니다.")
        if user.role != "member" or user.status != "active":
            raise RuntimeError(
                "예약된 로컬 분석 계정의 역할 또는 상태가 올바르지 않습니다."
            )
        return user

    def register(
        self,
        email_value: str,
        password: str,
        ip_hash: str,
        *,
        terms_accepted: bool,
        enqueue: Callable[..., Any] | None = None,
    ) -> tuple[User | None, EmailResult]:
        if not terms_accepted:
            raise DomainError(
                400,
                "terms required",
                "서비스 이용약관과 개인정보 처리 안내에 동의해야 합니다.",
            )
        try:
            email = normalize_email(email_value)
            validate_password(password, email=email)
        except ValueError as exc:
            raise DomainError(400, "invalid registration", str(exc)) from exc
        password_hash = hash_password(password)
        existing = self.db.scalar(select(User).where(User.email == email))
        if existing is not None:
            audit(
                self.db,
                "registration_duplicate_attempt",
                target=existing,
                ip_hash=ip_hash,
            )
            return None, EmailResult(
                configured=self.email_sender.configured, delivered=False
            )
        user = User(
            email=email,
            password_hash=password_hash,
            nickname=_unique_nickname(self.db),
            role="guest",
            status="pending",
            terms_accepted_at=utcnow(),
            terms_version=TERMS_VERSION,
        )
        try:
            with self.db.begin_nested():
                self.db.add(user)
                self.db.flush()
        except IntegrityError:
            # 동시 가입의 email unique 충돌도 계정 존재 여부를 드러내지 않는다.
            existing = self.db.scalar(select(User).where(User.email == email))
            if existing is not None:
                audit(
                    self.db,
                    "registration_duplicate_attempt",
                    target=existing,
                    ip_hash=ip_hash,
                )
                return None, EmailResult(
                    configured=self.email_sender.configured, delivered=False
                )
            raise DomainError(
                409,
                "registration conflict",
                "가입 요청이 충돌했습니다. 잠시 후 다시 시도하세요.",
            )
        audit(self.db, "user_registered", target=user, ip_hash=ip_hash)

        if not self.email_sender.configured:
            return user, EmailResult(configured=False, delivered=False)
        token = self._issue_token(user, "verify_email", self.settings.verify_token_hours * 60)
        # fragment는 브라우저가 서버 access log/Referer에 토큰을 보내지 않는다.
        url = f"{self.settings.public_base_url}/auth/verify-email#token={token}"
        subject = "[ASK SEOUL] 이메일 인증"
        text_body = (
            "ASK SEOUL 가입 이메일 인증 링크입니다.\n\n"
            f"{url}\n\n"
            f"{self.settings.verify_token_hours}시간 후 만료되며 한 번만 사용할 수 있습니다."
        )
        if enqueue is not None:
            enqueue(
                self.email_sender.send,
                to_email=user.email,
                subject=subject,
                text=text_body,
            )
            result = EmailResult(configured=True, delivered=False)
        else:
            result = self.email_sender.send(
                to_email=user.email,
                subject=subject,
                text=text_body,
            )
        audit(
            self.db,
            "verification_email_requested",
            target=user,
            ip_hash=ip_hash,
            details={
                "configured": result.configured,
                "delivered": result.delivered,
                "queued": enqueue is not None,
            },
        )
        return user, result

    def resend_verification(
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
        if (
            user is None
            or user.email_verified_at is not None
            or user.status in {"rejected", "suspended"}
        ):
            return EmailResult(configured=self.email_sender.configured, delivered=False)
        if not self.email_sender.configured:
            audit(self.db, "verification_email_unavailable", target=user, ip_hash=ip_hash)
            return EmailResult(configured=False, delivered=False)
        token = self._issue_token(
            user, "verify_email", self.settings.verify_token_hours * 60
        )
        url = f"{self.settings.public_base_url}/auth/verify-email#token={token}"
        subject = "[ASK SEOUL] 이메일 인증"
        text_body = (
            "ASK SEOUL 가입 이메일 인증 링크입니다.\n\n"
            f"{url}\n\n"
            f"{self.settings.verify_token_hours}시간 후 만료되며 한 번만 사용할 수 있습니다."
        )
        if enqueue is not None:
            enqueue(
                self.email_sender.send,
                to_email=user.email,
                subject=subject,
                text=text_body,
            )
            result = EmailResult(configured=True, delivered=False)
        else:
            result = self.email_sender.send(
                to_email=user.email,
                subject=subject,
                text=text_body,
            )
        audit(
            self.db,
            "verification_email_resent",
            target=user,
            ip_hash=ip_hash,
            details={"configured": result.configured, "queued": enqueue is not None},
        )
        return result

    def verify_email_token(self, raw_token: str, ip_hash: str) -> User:
        token = self._consume_token(raw_token, "verify_email")
        user = self.db.get(User, token.user_id)
        if user is None:
            raise DomainError(400, "invalid token", "유효하지 않은 인증 링크입니다.")
        self._lock_user(user)
        user.email_verified_at = utcnow()
        if self.settings.auto_approve_verified and user.status == "pending":
            user.status = "active"
            user.approved_at = utcnow()
        audit(self.db, "email_verified", target=user, ip_hash=ip_hash)
        return user

    def authenticate_password(
        self,
        email_value: str,
        password: str,
        *,
        ip_hash: str,
    ) -> User:
        try:
            email = normalize_email(email_value)
        except ValueError:
            email = ""
        user = self.db.scalar(select(User).where(User.email == email)) if email else None
        if user is None:
            # 존재하지 않는 계정도 Argon2 비용을 소모해 시간 기반 열거를 줄인다.
            verify_password(DUMMY_PASSWORD_HASH, password)
            raise DomainError(401, "login failed", "이메일 또는 비밀번호를 확인하세요.")
        self._lock_user(user)
        now = utcnow()
        if user.locked_until and user.locked_until > now:
            audit(self.db, "login_blocked_locked", target=user, ip_hash=ip_hash)
            self.db.commit()
            raise DomainError(429, "account locked", "잠시 후 다시 시도하세요.")
        ok, needs_rehash = verify_password(user.password_hash, password)
        if not ok:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = now + timedelta(minutes=15)
                user.failed_login_count = 0
            audit(self.db, "login_failed", target=user, ip_hash=ip_hash)
            # 인증 실패 자체가 정상적인 보안 이벤트이므로 API 예외 롤백 전에 확정한다.
            self.db.commit()
            raise DomainError(401, "login failed", "이메일 또는 비밀번호를 확인하세요.")
        if needs_rehash:
            user.password_hash = hash_password(password)
        user.failed_login_count = 0
        user.locked_until = None
        if user.status != "active":
            audit(
                self.db,
                "login_blocked_account_status",
                target=user,
                ip_hash=ip_hash,
                details={"status": user.status},
            )
            # 올바른 비밀번호가 확인되었으므로 누적 실패 상태와 필요한 rehash는
            # 계정 승인 여부와 무관하게 보존한다.
            self.db.commit()
            if user.status == "pending":
                raise DomainError(
                    403,
                    "approval pending",
                    "이메일 인증 또는 관리자 승인을 기다리고 있습니다.",
                )
            raise DomainError(403, "account unavailable", "사용할 수 없는 계정입니다.")
        audit(self.db, "password_factor_succeeded", actor=user, target=user, ip_hash=ip_hash)
        return user

    def create_session(
        self,
        user: User,
        *,
        remember: bool,
        ip_hash: str,
        user_agent_hash: str,
    ) -> tuple[str, str, datetime]:
        now = utcnow()
        self._lock_user(user)
        user.last_login_at = now
        active_sessions = list(
            self.db.scalars(
                select(AuthSession)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.last_seen_at.desc(), AuthSession.id.desc())
            ).all()
        )
        evicted = active_sessions[
            max(0, self.settings.max_sessions_per_user - 1) :
        ]
        for row in evicted:
            row.revoked_at = now
        if evicted:
            audit(
                self.db,
                "session_limit_evicted",
                actor=user,
                target=user,
                ip_hash=ip_hash,
                details={"count": len(evicted)},
            )
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
                remembered=remember,
                ip_hash=ip_hash,
                user_agent_hash=user_agent_hash,
            )
        )
        audit(self.db, "login_succeeded", actor=user, target=user, ip_hash=ip_hash)
        return raw_token, csrf_token, expires

    def login(
        self,
        email_value: str,
        password: str,
        *,
        remember: bool,
        ip_hash: str,
        user_agent_hash: str,
    ) -> tuple[User, str, str, datetime]:
        """비-MFA 호환 경로. API 라우터는 MFA challenge를 먼저 판정한다."""
        user = self.authenticate_password(
            email_value, password, ip_hash=ip_hash
        )
        if user.mfa_enabled_at is not None:
            raise DomainError(401, "mfa required", "다중요소 인증이 필요합니다.")
        token, csrf, expires = self.create_session(
            user,
            remember=remember,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
        )
        return user, token, csrf, expires

    def issue_mfa_challenge(
        self,
        user: User,
        *,
        purpose: str,
        remember: bool,
        ip_hash: str,
        user_agent_hash: str,
        setup_salt: str | None = None,
    ) -> str:
        self._lock_user(user)
        self.db.execute(
            delete(MfaChallenge).where(
                MfaChallenge.user_id == user.id,
                MfaChallenge.purpose == purpose,
                MfaChallenge.consumed_at.is_(None),
            )
        )
        raw = random_token()
        self.db.add(
            MfaChallenge(
                token_hash=token_digest(raw, self.settings.session_pepper),
                user_id=user.id,
                purpose=purpose,
                setup_salt=setup_salt,
                remember=remember,
                ip_hash=ip_hash,
                user_agent_hash=user_agent_hash,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        audit(
            self.db,
            "mfa_challenge_created",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"purpose": purpose},
        )
        # Some trusted callers issue and verify in one transaction. Session
        # autoflush is disabled, so make the returned challenge query-visible.
        self.db.flush()
        return raw

    def verify_login_mfa(
        self,
        raw_challenge: str,
        code: str,
        *,
        ip_hash: str,
        user_agent_hash: str,
    ) -> tuple[User, str, str, datetime, bool]:
        challenge = self._mfa_challenge(
            raw_challenge,
            "login",
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
        )
        user = self.db.get(User, challenge.user_id)
        if user is None or user.status != "active" or user.mfa_enabled_at is None:
            raise DomainError(400, "invalid challenge", "유효하지 않은 MFA 요청입니다.")
        self._lock_user(user)
        challenge = self._refresh_active_challenge(challenge)
        method = self._verify_second_factor(user, code)
        if method is None:
            self._mfa_failure(challenge, user, ip_hash)
        challenge.consumed_at = utcnow()
        audit(
            self.db,
            "mfa_login_succeeded",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"method": method},
        )
        token, csrf, expires = self.create_session(
            user,
            remember=challenge.remember,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
        )
        return user, token, csrf, expires, challenge.remember

    def begin_mfa_setup(
        self,
        user: User,
        *,
        current_password: str,
        current_code: str,
        ip_hash: str,
        user_agent_hash: str,
    ) -> dict:
        self._lock_user(user)
        ok, _ = verify_password(user.password_hash, current_password)
        if not ok:
            raise DomainError(400, "password mismatch", "현재 비밀번호가 올바르지 않습니다.")
        if user.mfa_enabled_at is not None:
            method = self._verify_second_factor(user, current_code)
            if method is None:
                raise DomainError(400, "mfa mismatch", "현재 MFA 코드가 올바르지 않습니다.")
        setup_salt = random_token(24)
        challenge = self.issue_mfa_challenge(
            user,
            purpose="setup",
            remember=False,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            setup_salt=setup_salt,
        )
        secret = derive_totp_secret(
            self.settings.mfa_master_key, user.public_id, setup_salt
        )
        issuer = quote("ASK SEOUL", safe="")
        account = quote(user.email, safe="")
        uri = (
            f"otpauth://totp/{issuer}:{account}?secret={secret}"
            f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
        )
        return {"setup_token": challenge, "secret": secret, "otpauth_uri": uri}

    def mfa_security_state_digest(self, user: User) -> str:
        state = "\x1f".join(
            (
                user.public_id,
                user.password_hash,
                user.role,
                user.status,
                user.mfa_seed_salt or "",
                iso_utc(user.mfa_enabled_at) or "",
            )
        )
        return stable_digest(state, self.settings.session_pepper)

    def confirm_mfa_setup(
        self,
        user: User,
        raw_challenge: str,
        code: str,
        *,
        ip_hash: str,
        user_agent_hash: str,
        expected_state_digest: str | None = None,
    ) -> list[str]:
        challenge = self._mfa_challenge(
            raw_challenge,
            "setup",
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
        )
        if challenge.user_id != user.id or not challenge.setup_salt:
            raise DomainError(403, "forbidden", "다른 계정의 MFA 설정 요청입니다.")
        self._lock_user(user)
        challenge = self._refresh_active_challenge(challenge)
        if user.status != "active":
            raise DomainError(403, "account inactive", "활성 계정만 MFA를 설정할 수 있습니다.")
        if expected_state_digest and not hmac.compare_digest(
            self.mfa_security_state_digest(user),
            expected_state_digest,
        ):
            raise DomainError(
                409,
                "mfa state changed",
                "보안 상태가 변경되었습니다. MFA 설정을 새로 시작하세요.",
            )
        secret = derive_totp_secret(
            self.settings.mfa_master_key, user.public_id, challenge.setup_salt
        )
        counter = verify_totp(secret, code)
        if counter is None:
            self._mfa_failure(challenge, user, ip_hash)
        user.mfa_seed_salt = challenge.setup_salt
        user.mfa_enabled_at = utcnow()
        user.mfa_last_counter = counter
        challenge.consumed_at = utcnow()
        codes = self._replace_recovery_codes(user)
        self.revoke_user_sessions(user.id)
        audit(
            self.db,
            "mfa_enabled",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"recovery_code_count": len(codes)},
        )
        return codes

    def disable_mfa(
        self,
        user: User,
        *,
        current_password: str,
        code: str,
        ip_hash: str,
    ) -> None:
        self._lock_user(user)
        ok, _ = verify_password(user.password_hash, current_password)
        if not ok:
            raise DomainError(400, "password mismatch", "현재 비밀번호가 올바르지 않습니다.")
        if user.mfa_enabled_at is None:
            raise DomainError(409, "mfa disabled", "MFA가 설정되어 있지 않습니다.")
        method = self._verify_second_factor(user, code)
        if method is None:
            raise DomainError(400, "mfa mismatch", "MFA 코드가 올바르지 않습니다.")
        if (
            self.settings.require_mfa_for_privileged
            and user.role in {"operator", "admin"}
        ):
            raise DomainError(
                403,
                "mfa required",
                "운영자와 최고관리자는 이 환경에서 MFA를 해제할 수 없습니다.",
            )
        user.mfa_enabled_at = None
        user.mfa_seed_salt = None
        user.mfa_last_counter = None
        self.db.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
        )
        self.revoke_user_sessions(user.id)
        audit(
            self.db,
            "mfa_disabled",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"verified_by": method},
        )

    def force_reset_mfa(
        self,
        user: User,
        *,
        actor: User,
        reason: str,
        ip_hash: str,
        break_glass: bool = False,
    ) -> None:
        """관리자/CLI 복구 절차에서 기존 2차 요소와 세션을 전부 폐기한다."""
        reason = reason.strip()
        if len(reason) < 10:
            raise DomainError(
                400,
                "reset reason required",
                "MFA 초기화 사유를 10자 이상 입력해야 합니다.",
            )
        self._lock_user(user)
        if user.mfa_enabled_at is None:
            raise DomainError(409, "mfa disabled", "MFA가 설정되어 있지 않습니다.")
        user.mfa_enabled_at = None
        user.mfa_seed_salt = None
        user.mfa_last_counter = None
        self.db.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
        )
        self.db.execute(
            delete(MfaChallenge).where(MfaChallenge.user_id == user.id)
        )
        self.revoke_user_sessions(user.id)
        audit(
            self.db,
            "mfa_force_reset",
            actor=actor,
            target=user,
            ip_hash=ip_hash,
            details={"reason": reason[:500], "break_glass": break_glass},
        )

    def force_reenroll_mfa(
        self,
        user: User,
        *,
        actor: User,
        setup_salt: str,
        code: str,
        reason: str,
        ip_hash: str,
        expected_state_digest: str,
    ) -> list[str]:
        """CLI break-glass용 원자적 재등록. 새 TOTP 검증 전에는 기존 MFA를 건드리지 않는다."""
        secret = derive_totp_secret(
            self.settings.mfa_master_key, user.public_id, setup_salt
        )
        counter = verify_totp(secret, code)
        if counter is None:
            raise DomainError(
                400,
                "mfa mismatch",
                "새 인증 앱의 MFA 코드가 올바르지 않습니다.",
            )
        self._lock_user(user)
        if user.status != "active" or user.role not in {"operator", "admin"}:
            raise DomainError(
                403,
                "privileged account required",
                "활성 운영자 또는 최고관리자 계정만 CLI에서 재등록할 수 있습니다.",
            )
        if not hmac.compare_digest(
            self.mfa_security_state_digest(user),
            expected_state_digest,
        ):
            raise DomainError(
                409,
                "mfa state changed",
                "보안 상태가 변경되었습니다. MFA 재등록을 새로 시작하세요.",
            )
        self.force_reset_mfa(
            user,
            actor=actor,
            reason=reason,
            ip_hash=ip_hash,
            break_glass=True,
        )
        user.mfa_seed_salt = setup_salt
        user.mfa_enabled_at = utcnow()
        user.mfa_last_counter = counter
        codes = self._replace_recovery_codes(user)
        audit(
            self.db,
            "mfa_enabled",
            actor=actor,
            target=user,
            ip_hash=ip_hash,
            details={
                "recovery_code_count": len(codes),
                "break_glass": True,
            },
        )
        return codes

    def regenerate_recovery_codes(
        self,
        user: User,
        *,
        current_password: str,
        code: str,
        ip_hash: str,
    ) -> list[str]:
        self._lock_user(user)
        ok, _ = verify_password(user.password_hash, current_password)
        if not ok:
            raise DomainError(400, "password mismatch", "현재 비밀번호가 올바르지 않습니다.")
        if user.mfa_enabled_at is None:
            raise DomainError(409, "mfa disabled", "MFA가 설정되어 있지 않습니다.")
        method = self._verify_second_factor(user, code)
        if method is None:
            raise DomainError(400, "mfa mismatch", "MFA 코드가 올바르지 않습니다.")
        codes = self._replace_recovery_codes(user)
        audit(
            self.db,
            "mfa_recovery_codes_rotated",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"verified_by": method},
        )
        return codes

    def _replace_recovery_codes(self, user: User) -> list[str]:
        self.db.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
        )
        codes = [recovery_code() for _ in range(10)]
        for value in codes:
            self.db.add(
                MfaRecoveryCode(
                    user_id=user.id,
                    code_hash=token_digest(
                        normalize_recovery_code(value), self.settings.session_pepper
                    ),
                )
            )
        return codes

    def _verify_second_factor(self, user: User, code: str) -> str | None:
        if user.mfa_enabled_at is None or not user.mfa_seed_salt:
            return None
        secret = derive_totp_secret(
            self.settings.mfa_master_key, user.public_id, user.mfa_seed_salt
        )
        counter = verify_totp(
            secret, code, last_counter=user.mfa_last_counter
        )
        if counter is not None:
            user.mfa_last_counter = counter
            return "totp"
        normalized = normalize_recovery_code(code)
        if not normalized:
            return None
        recovery = self.db.scalar(
            select(MfaRecoveryCode).where(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.code_hash
                == token_digest(normalized, self.settings.session_pepper),
                MfaRecoveryCode.used_at.is_(None),
            )
        )
        if recovery is None:
            return None
        recovery.used_at = utcnow()
        return "recovery_code"

    def _mfa_challenge(
        self,
        raw: str,
        purpose: str,
        *,
        ip_hash: str,
        user_agent_hash: str,
    ) -> MfaChallenge:
        row = self.db.scalar(
            select(MfaChallenge).where(
                MfaChallenge.token_hash
                == token_digest(raw, self.settings.session_pepper),
                MfaChallenge.purpose == purpose,
                MfaChallenge.consumed_at.is_(None),
                MfaChallenge.expires_at > utcnow(),
            )
        )
        if row is None or row.attempts >= 5:
            raise DomainError(400, "invalid challenge", "MFA 요청이 만료되었거나 유효하지 않습니다.")
        if row.ip_hash != ip_hash or row.user_agent_hash != user_agent_hash:
            raise DomainError(400, "invalid challenge", "MFA 요청 환경이 일치하지 않습니다.")
        return row

    def _refresh_active_challenge(self, row: MfaChallenge) -> MfaChallenge:
        locked = self.db.scalar(
            select(MfaChallenge)
            .where(MfaChallenge.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            locked is None
            or locked.consumed_at is not None
            or locked.expires_at <= utcnow()
            or locked.attempts >= 5
        ):
            raise DomainError(
                400,
                "invalid challenge",
                "MFA 요청이 만료되었거나 유효하지 않습니다.",
            )
        return locked

    def _mfa_failure(
        self, challenge: MfaChallenge, user: User, ip_hash: str
    ) -> None:
        challenge.attempts += 1
        if challenge.attempts >= 5:
            challenge.consumed_at = utcnow()
        audit(
            self.db,
            "mfa_failed",
            target=user,
            ip_hash=ip_hash,
            details={"purpose": challenge.purpose, "attempts": challenge.attempts},
        )
        self.db.commit()
        raise DomainError(401, "mfa failed", "MFA 코드 또는 복구 코드가 올바르지 않습니다.")

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
            self.settings.require_mfa_for_privileged
            and user.role in {"operator", "admin"}
            and user.mfa_enabled_at is None
        ):
            row.revoked_at = utcnow()
            audit(
                self.db,
                "session_revoked_mfa_required",
                target=user,
                ip_hash=ip_hash,
            )
            return None
        now = utcnow()
        idle_limit = (
            timedelta(days=self.settings.remember_idle_days)
            if row.remembered
            else timedelta(minutes=self.settings.session_idle_minutes)
        )
        if row.last_seen_at < now - idle_limit:
            row.revoked_at = now
            audit(
                self.db,
                "session_idle_expired",
                target=user,
                ip_hash=ip_hash,
                details={"remembered": row.remembered},
            )
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
        if row.last_seen_at < now - timedelta(minutes=5):
            row.last_seen_at = now
        return row, user

    def logout(self, session: AuthSession | None, ip_hash: str) -> None:
        if session and session.revoked_at is None:
            session.revoked_at = utcnow()
            user = self.db.get(User, session.user_id)
            audit(self.db, "logout", actor=user, target=user, ip_hash=ip_hash)

    def session_reference(self, row: AuthSession) -> str:
        return stable_digest(
            f"session:{row.id}:{row.token_hash}",
            self.settings.session_pepper,
        )[:24]

    def active_sessions(
        self, user: User, current_session_id: int
    ) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > utcnow(),
            )
            .order_by(AuthSession.last_seen_at.desc())
        ).all()
        return [
            {
                "id": self.session_reference(row),
                "current": row.id == current_session_id,
                "created_at": iso_utc(row.created_at),
                "last_seen_at": iso_utc(row.last_seen_at),
                "expires_at": iso_utc(row.expires_at),
                "remembered": row.remembered,
                "ip_fingerprint": row.ip_hash[:10],
                "client_fingerprint": row.user_agent_hash[:10],
            }
            for row in rows
        ]

    def revoke_session_reference(
        self, user: User, reference: str, ip_hash: str
    ) -> AuthSession:
        rows = self.db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        row = next(
            (
                item
                for item in rows
                if hmac.compare_digest(self.session_reference(item), reference)
            ),
            None,
        )
        if row is None:
            raise DomainError(404, "session not found", "활성 세션을 찾을 수 없습니다.")
        row.revoked_at = utcnow()
        audit(
            self.db,
            "session_revoked",
            actor=user,
            target=user,
            ip_hash=ip_hash,
        )
        return row

    def revoke_other_sessions(
        self, user: User, current_session_id: int, ip_hash: str
    ) -> int:
        rows = self.db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.id != current_session_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        now = utcnow()
        for row in rows:
            row.revoked_at = now
        audit(
            self.db,
            "other_sessions_revoked",
            actor=user,
            target=user,
            ip_hash=ip_hash,
            details={"count": len(rows)},
        )
        return len(rows)

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
        url = f"{self.settings.public_base_url}/auth/reset-password#token={token}"
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
        self._lock_user(user)
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
        self._lock_user(user)
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
        self._lock_user(user)
        duplicate = self.db.scalar(
            select(User.id).where(User.nickname == nickname, User.id != user.id)
        )
        if duplicate:
            raise DomainError(409, "nickname conflict", "이미 사용 중인 닉네임입니다.")
        try:
            with self.db.begin_nested():
                user.nickname = nickname
                self.db.flush()
        except IntegrityError as exc:
            raise DomainError(
                409,
                "nickname conflict",
                "이미 사용 중인 닉네임입니다.",
            ) from exc
        audit(self.db, "nickname_changed", actor=user, target=user, ip_hash=ip_hash)
        return user

    def _issue_token(self, user: User, purpose: str, ttl_minutes: int) -> str:
        self._lock_user(user)
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
        now = utcnow()
        token = self.db.scalar(
            select(AccountToken).where(
                AccountToken.token_hash == digest,
                AccountToken.purpose == purpose,
                AccountToken.consumed_at.is_(None),
                AccountToken.expires_at > now,
            )
        )
        if token is None:
            raise DomainError(400, "invalid token", "링크가 만료되었거나 이미 사용되었습니다.")
        claimed = self.db.execute(
            update(AccountToken)
            .where(
                AccountToken.id == token.id,
                AccountToken.consumed_at.is_(None),
                AccountToken.expires_at > now,
            )
            .values(consumed_at=now)
        )
        if claimed.rowcount != 1:
            raise DomainError(400, "invalid token", "링크가 만료되었거나 이미 사용되었습니다.")
        self.db.refresh(token)
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
        request = PaymentRequest(
            user_id=user.id,
            plan_id=plan.id,
            pending_key=f"user:{user.id}",
        )
        try:
            with self.db.begin_nested():
                self.db.add(request)
                self.db.flush()
        except IntegrityError as exc:
            raise DomainError(
                409,
                "payment pending",
                "이미 승인 대기 중인 결제 요청이 있습니다.",
            ) from exc
        notification = self.notifier.configuration()
        result = {
            **notification.as_dict(),
            "queued": notification.configured,
        }
        request.notification_result = result
        for item in notification.deliveries:
            self.db.add(
                NotificationDelivery(
                    payment_request_id=request.id,
                    channel=item.channel,
                    target=item.target,
                    status="queued" if item.configured else "not_configured",
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
        note = note.strip()
        if not approve and not note:
            raise DomainError(
                400,
                "review note required",
                "결제 요청을 거절할 때는 사유를 입력해야 합니다.",
            )
        row = self.db.scalar(
            select(PaymentRequest).where(
                PaymentRequest.public_id == request_public_id
            )
        )
        if row is None:
            raise DomainError(404, "payment not found", "결제 요청을 찾을 수 없습니다.")
        if row.status != "pending":
            raise DomainError(409, "already reviewed", "이미 처리된 결제 요청입니다.")
        target = self.db.get(User, row.user_id)
        if target is None or not role_can_manage(actor.role, target.role):
            raise DomainError(403, "forbidden", "이 결제 요청을 처리할 권한이 없습니다.")
        reviewed_at = utcnow()
        next_status = "approved" if approve else "rejected"
        try:
            claimed = self.db.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == row.id,
                    PaymentRequest.status == "pending",
                )
                .values(
                    status=next_status,
                    pending_key=None,
                    reviewed_at=reviewed_at,
                    reviewed_by_id=actor.id,
                    review_note=note[:500],
                )
            )
        except OperationalError as exc:
            raise DomainError(
                409,
                "review conflict",
                "다른 운영자가 같은 결제 요청을 처리 중입니다. 새로고침 후 확인하세요.",
            ) from exc
        if claimed.rowcount != 1:
            raise DomainError(409, "already reviewed", "이미 처리된 결제 요청입니다.")
        self.db.expire(row)
        target = self.db.scalar(
            select(User).where(User.id == row.user_id).with_for_update()
        )
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
            details={"request_id": row.public_id, "status": next_status},
        )
        return row

    def requeue_notification(
        self,
        actor: User,
        request_public_id: str,
        ip_hash: str,
    ) -> tuple[PaymentRequest, dict]:
        row = self.db.scalar(
            select(PaymentRequest).where(
                PaymentRequest.public_id == request_public_id
            )
        )
        if row is None:
            raise DomainError(404, "payment not found", "결제 요청을 찾을 수 없습니다.")
        target = self.db.get(User, row.user_id)
        if target is None or not role_can_manage(actor.role, target.role):
            raise DomainError(403, "forbidden", "이 결제 요청 알림을 처리할 권한이 없습니다.")
        processing = self.db.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.payment_request_id == row.id,
                NotificationDelivery.status.in_(("queued", "processing")),
            )
        )
        if processing is not None:
            raise DomainError(
                409,
                "notification in progress",
                "알림 전송이 진행 중입니다. 잠시 후 상태를 새로고침하세요.",
            )
        notification = self.notifier.configuration()
        result = {
            **notification.as_dict(),
            "queued": notification.configured,
        }
        # 기존 delivered/failed 이력은 삭제하지 않고 새 전송 batch를 append한다.
        for item in notification.deliveries:
            self.db.add(
                NotificationDelivery(
                    payment_request_id=row.id,
                    channel=item.channel,
                    target=item.target,
                    status="queued" if item.configured else "not_configured",
                )
            )
        row.notification_result = result
        audit(
            self.db,
            "payment_notification_requeued",
            actor=actor,
            target=target,
            ip_hash=ip_hash,
            details={"request_id": row.public_id, "configured": result["configured"]},
        )
        return row, result


def _payment_notification_message(row: PaymentRequest) -> NotificationMessage:
    return NotificationMessage(
        title="ASK SEOUL 이용권 승인 요청",
        body="운영자 또는 최고관리자의 확인이 필요한 모의 결제 요청입니다.",
        severity="notice",
        fields={
            "request_id": row.public_id,
            "user": row.user.nickname,
            "masked_id": mask_public_id(row.user.public_id),
            "role": ROLE_LABELS[row.user.role],
            "plan": row.plan.label,
            "duration_days": row.plan.duration_days,
            "amount": f"{row.plan.price_amount:,} {row.plan.currency}",
        },
    )


def deliver_payment_notification(
    database: Database,
    request_public_id: str,
    notifier: NotificationService | None = None,
) -> None:
    """DB commit 이후 실행되는 결제 승인 알림 outbox 처리."""
    with database.session() as db:
        row = db.scalar(
            select(PaymentRequest).where(
                PaymentRequest.public_id == request_public_id
            ).with_for_update()
        )
        if row is None:
            return
        delivery_ids = list(
            db.scalars(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.payment_request_id == row.id,
                    NotificationDelivery.status == "queued",
                )
                .order_by(NotificationDelivery.id)
            ).all()
        )
        if not delivery_ids:
            return
        claimed_at = utcnow()
        claimed = db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id.in_(delivery_ids),
                NotificationDelivery.status == "queued",
            )
            .values(
                status="processing",
                attempts=NotificationDelivery.attempts + 1,
                last_attempt_at=claimed_at,
            )
        )
        if claimed.rowcount == 0:
            return
        message = _payment_notification_message(row)
    result = (notifier or NotificationService()).send(message)
    with database.session() as db:
        row = db.scalar(
            select(PaymentRequest).where(
                PaymentRequest.public_id == request_public_id
            )
        )
        if row is None:
            return
        deliveries = [
            db.get(NotificationDelivery, delivery_id)
            for delivery_id in delivery_ids
        ]
        remaining = list(result.deliveries)
        updated = False
        for stored in deliveries:
            if stored is None:
                continue
            match_index = next(
                (
                    index
                    for index, item in enumerate(remaining)
                    if item.channel == stored.channel
                    and (not stored.target or item.target == stored.target)
                ),
                None,
            )
            if match_index is None:
                stored.status = "failed"
                stored.error = "notification result missing"
                updated = True
                continue
            actual = remaining.pop(match_index)
            updated = True
            stored.status = (
                "delivered"
                if actual.delivered
                else ("failed" if actual.configured else "not_configured")
            )
            stored.target = actual.target
            stored.error = actual.error
        if updated:
            row.notification_result = {
                **result.as_dict(),
                "queued": False,
            }


def recover_stale_notifications(
    database: Database, *, older_than_minutes: int = 5
) -> int:
    """중단된 worker가 남긴 processing delivery를 재처리 가능한 queued로 되돌린다."""
    with database.session() as db:
        result = db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.status == "processing",
                NotificationDelivery.last_attempt_at
                < utcnow() - timedelta(minutes=max(1, older_than_minutes)),
            )
            .values(status="queued", error="stale processing claim recovered")
        )
        return result.rowcount


def process_queued_notifications(
    database: Database, *, limit: int = 100
) -> int:
    """대기 중인 결제 알림을 제한된 개수만큼 처리한다."""
    with database.session() as db:
        request_ids = list(
            db.scalars(
                select(PaymentRequest.public_id)
                .join(
                    NotificationDelivery,
                    NotificationDelivery.payment_request_id == PaymentRequest.id,
                )
                .where(NotificationDelivery.status == "queued")
                .distinct()
                .limit(max(1, min(limit, 1000)))
            ).all()
        )
    for request_id in request_ids:
        deliver_payment_notification(database, request_id)
    return len(request_ids)


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

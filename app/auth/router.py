"""인증·프로필·회원관리·정책·모의결제 API."""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .dependencies import (
    current_session,
    current_user,
    get_db,
    require_admin,
    require_operator,
)
from .models import (
    AccessPolicy,
    AuditLog,
    AuthControl,
    AuthSession,
    IpBlock,
    MfaRecoveryCode,
    PageResource,
    PaymentRequest,
    RolePagePermission,
    User,
    UserPagePermission,
    UserPreference,
    utcnow,
)
from .schemas import (
    ForgotPasswordRequest,
    IpBlockCreate,
    LoginRequest,
    MfaAdminResetRequest,
    MfaConfirmRequest,
    MfaLoginRequest,
    MfaManageRequest,
    MfaSetupRequest,
    NicknamePatch,
    PasswordPatch,
    PaymentCreate,
    PaymentReview,
    PermissionPatch,
    PolicyCreate,
    PolicyPatch,
    PreferencePatch,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserAdminPatch,
    VerifyEmailRequest,
)
from .security import (
    ROLES,
    ROLE_LABELS,
    USER_STATUSES,
    ip_in_networks,
    iso_utc,
    mask_public_id,
    parse_network,
    role_can_manage,
    safe_next_path,
)
from .service import (
    AccessService,
    ADMIN_PAGE_KEYS,
    AuthService,
    DomainError,
    PaymentService,
    audit,
    deliver_payment_notification,
    serialize_payment,
    serialize_policy,
    user_payload,
)


router = APIRouter(prefix="/api/v1", tags=["auth"])
RATE_WINDOWS = {"second", "minute", "hour", "day"}


def _settings(request: Request):
    return request.app.state.auth_settings


def _validate_policy_config(policy_type: str, config: dict) -> None:
    if policy_type == "request_limit":
        allowed_categories = {
            "anonymous",
            "authenticated",
            "login",
            "register",
            "verify_email",
            "forgot_password",
            "password_reset",
            "mfa",
            "charts_query",
        }
        unknown = set(config) - allowed_categories - {"action"}
        if unknown:
            raise DomainError(400, "invalid policy", f"알 수 없는 요청 제한 항목: {', '.join(sorted(unknown))}")
        if config.get("action", "reject") not in {"reject", "slow_down", "drop"}:
            raise DomainError(400, "invalid policy", "요청 제한 action이 올바르지 않습니다.")
        for category in allowed_categories & set(config):
            limits = config[category]
            if not isinstance(limits, dict) or set(limits) - RATE_WINDOWS:
                raise DomainError(400, "invalid policy", f"{category} 시간창 설정이 올바르지 않습니다.")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 10_000_000
                for value in limits.values()
            ):
                raise DomainError(400, "invalid policy", f"{category} 한도는 0 이상의 정수여야 합니다.")
    elif policy_type == "ontology":
        _validate_ontology_config(config)


def _validate_string_map(
    value,
    *,
    name: str,
    allowed_keys: set[str] | None = None,
    max_items: int = 200,
) -> None:
    if not isinstance(value, dict) or len(value) > max_items:
        raise DomainError(400, "invalid ontology", f"{name} 설정이 올바르지 않습니다.")
    for key, label in value.items():
        if (
            not isinstance(key, str)
            or len(key) > 120
            or (allowed_keys is not None and key not in allowed_keys)
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 80
        ):
            raise DomainError(400, "invalid ontology", f"{name} 항목이 올바르지 않습니다.")


def _validate_ontology_config(config: dict) -> None:
    from app.charts.ontology import CHART_TYPES, registry

    allowed = {
        "hidden_chart_types",
        "chart_label_overrides",
        "value_label_overrides",
        "source_label_overrides",
        "default_domain",
    }
    if set(config) - allowed:
        raise DomainError(400, "invalid ontology", "알 수 없는 온톨로지 설정이 포함되어 있습니다.")
    hidden = config.get("hidden_chart_types", [])
    if (
        not isinstance(hidden, list)
        or len(hidden) > len(CHART_TYPES)
        or any(not isinstance(item, str) for item in hidden)
        or len(hidden) != len(set(hidden))
        or any(item not in CHART_TYPES for item in hidden)
    ):
        raise DomainError(400, "invalid ontology", "온톨로지의 차트 타입이 올바르지 않습니다.")
    default_domain = config.get("default_domain", "all")
    domains = {source["domain"] for source in registry.sources()}
    if not isinstance(default_domain, str) or (
        default_domain != "all" and default_domain not in domains
    ):
        raise DomainError(400, "invalid ontology", "온톨로지의 기본 도메인이 올바르지 않습니다.")
    _validate_string_map(
        config.get("chart_label_overrides", {}),
        name="차트 라벨",
        allowed_keys=set(CHART_TYPES),
    )
    sources = {source["name"] for source in registry.sources()}
    _validate_string_map(
        config.get("source_label_overrides", {}),
        name="소스 라벨",
        allowed_keys=sources,
    )
    value_labels = config.get("value_label_overrides", {})
    if not isinstance(value_labels, dict) or len(value_labels) > 100:
        raise DomainError(400, "invalid ontology", "값 라벨 설정이 올바르지 않습니다.")
    for field, mapping in value_labels.items():
        if not isinstance(field, str) or not field or len(field) > 120:
            raise DomainError(400, "invalid ontology", "값 라벨 필드가 올바르지 않습니다.")
        _validate_string_map(mapping, name=f"{field} 값 라벨")


def _set_session_cookies(
    response: JSONResponse,
    request: Request,
    raw_token: str,
    csrf_token: str,
    expires_at: datetime,
    persistent: bool,
) -> None:
    settings = _settings(request)
    max_age = max(1, int((expires_at - utcnow()).total_seconds()))
    cookie_expires = expires_at.replace(tzinfo=timezone.utc)
    lifetime = (
        {"max_age": max_age, "expires": cookie_expires}
        if persistent
        else {}
    )
    response.set_cookie(
        settings.cookie_name,
        raw_token,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        **lifetime,
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        path="/",
        secure=settings.cookie_secure,
        httponly=False,
        samesite="lax",
        **lifetime,
    )


def _clear_session_cookies(response: JSONResponse, request: Request) -> None:
    settings = _settings(request)
    response.delete_cookie(
        settings.cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=False,
        samesite="lax",
    )
    response.headers["Clear-Site-Data"] = '"cache", "storage"'


@router.post("/auth/register")
def register(
    req: RegisterRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user, email = AuthService(db, _settings(request)).register(
        req.email,
        req.password,
        request.state.ip_hash,
        terms_accepted=req.terms_accepted,
        enqueue=background_tasks.add_task,
    )
    return {
        "registered": True,
        "status": "pending",
        "email_configured": email.configured,
        "detail": (
            "가입 가능한 이메일이면 요청을 접수했습니다. 인증 메일을 확인하거나 관리자 승인을 기다려 주세요."
        ),
    }


@router.post("/auth/resend-verification")
def resend_verification(
    req: ResendVerificationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    AuthService(db, _settings(request)).resend_verification(
        req.email, request.state.ip_hash, background_tasks.add_task
    )
    return {
        "accepted": True,
        "detail": "인증이 필요한 가입 계정이면 새 인증 이메일을 보냈습니다.",
    }


@router.post("/auth/verify-email")
def verify_email(
    req: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)
):
    AuthService(db, _settings(request)).verify_email_token(
        req.token, request.state.ip_hash
    )
    return {"verified": True, "detail": "이메일 인증이 완료되었습니다."}


@router.post("/auth/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    service = AuthService(db, _settings(request))
    user = service.authenticate_password(
        req.email, req.password, ip_hash=request.state.ip_hash
    )
    allowed = AccessService(db).allowed_pages(user)
    next_path = safe_next_path(
        req.next,
        ("/catalog", "/charts", "/profile", "/admin", "/docs"),
    )
    page_map = {
        "/catalog": "catalog",
        "/charts": "charts",
        "/profile": "profile",
        "/admin": "admin_users",
        "/docs": "api_docs",
    }
    root = "/" + next_path.lstrip("/").split("/", 1)[0]
    if page_map.get(root) not in allowed:
        next_path = "/profile" if "profile" in allowed else "/"
    if user.mfa_enabled_at is not None:
        challenge = service.issue_mfa_challenge(
            user,
            purpose="login",
            remember=req.remember,
            ip_hash=request.state.ip_hash,
            user_agent_hash=request.state.user_agent_hash,
        )
        return {
            "authenticated": False,
            "mfa_required": True,
            "challenge": challenge,
            "next": next_path,
        }
    if (
        _settings(request).require_mfa_for_privileged
        and user.role in {"operator", "admin"}
    ):
        audit(
            db,
            "login_blocked_mfa_enrollment_required",
            target=user,
            ip_hash=request.state.ip_hash,
        )
        # 올바른 비밀번호로 통과한 보안 상태(실패 횟수 초기화)를 보존한다.
        db.commit()
        raise DomainError(
            403,
            "mfa enrollment required",
            "운영자와 최고관리자는 MFA 설정 후 로그인할 수 있습니다. 운영자에게 초기 설정을 요청하세요.",
        )
    token, csrf, expires = service.create_session(
        user,
        remember=req.remember,
        ip_hash=request.state.ip_hash,
        user_agent_hash=request.state.user_agent_hash,
    )
    response = JSONResponse(
        {
            "authenticated": True,
            "user": user_payload(db, user),
            "next": next_path,
        }
    )
    _set_session_cookies(
        response,
        request,
        token,
        csrf,
        expires,
        persistent=req.remember,
    )
    return response


@router.post("/auth/mfa/verify")
def verify_login_mfa(
    req: MfaLoginRequest, request: Request, db: Session = Depends(get_db)
):
    user, token, csrf, expires, persistent = AuthService(
        db, _settings(request)
    ).verify_login_mfa(
        req.challenge,
        req.code,
        ip_hash=request.state.ip_hash,
        user_agent_hash=request.state.user_agent_hash,
    )
    allowed = AccessService(db).allowed_pages(user)
    next_path = safe_next_path(
        req.next,
        ("/catalog", "/charts", "/profile", "/admin", "/docs"),
    )
    page_map = {
        "/catalog": "catalog",
        "/charts": "charts",
        "/profile": "profile",
        "/admin": "admin_users",
        "/docs": "api_docs",
    }
    root = "/" + next_path.lstrip("/").split("/", 1)[0]
    if page_map.get(root) not in allowed:
        next_path = "/profile" if "profile" in allowed else "/"
    response = JSONResponse(
        {
            "authenticated": True,
            "user": user_payload(db, user),
            "next": next_path,
        }
    )
    _set_session_cookies(
        response,
        request,
        token,
        csrf,
        expires,
        persistent=persistent,
    )
    return response


@router.post("/auth/logout")
def logout(
    request: Request,
    auth_session: AuthSession = Depends(current_session),
    db: Session = Depends(get_db),
):
    AuthService(db, _settings(request)).logout(auth_session, request.state.ip_hash)
    response = JSONResponse({"authenticated": False})
    _clear_session_cookies(response, request)
    return response


@router.get("/auth/session")
def session_info(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user is None:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": user_payload(db, user)}


@router.post("/auth/forgot-password")
def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    result = AuthService(db, _settings(request)).request_password_reset(
        req.email, request.state.ip_hash, background_tasks.add_task
    )
    detail = (
        "계정이 존재하고 이메일 전송이 설정되어 있다면 재설정 링크를 보냈습니다."
        if result.configured
        else "이메일 전송이 설정되지 않았습니다. 관리자에게 문의하세요."
    )
    return {"accepted": True, "email_configured": result.configured, "detail": detail}


@router.post("/auth/reset-password")
def reset_password(
    req: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)
):
    AuthService(db, _settings(request)).reset_password(
        req.token, req.password, request.state.ip_hash
    )
    response = JSONResponse(
        {"reset": True, "detail": "비밀번호가 변경되었습니다. 다시 로그인하세요."}
    )
    _clear_session_cookies(response, request)
    return response


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return user_payload(db, user)


@router.patch("/me/nickname")
def patch_nickname(
    req: NicknamePatch,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    updated = AuthService(db, _settings(request)).update_nickname(
        user, req.nickname, request.state.ip_hash
    )
    return user_payload(db, updated)


@router.patch("/me/password")
def patch_password(
    req: PasswordPatch,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    AuthService(db, _settings(request)).change_password(
        user, req.current_password, req.new_password, request.state.ip_hash
    )
    response = JSONResponse(
        {"changed": True, "detail": "비밀번호가 변경되어 모든 세션이 종료되었습니다."}
    )
    _clear_session_cookies(response, request)
    return response


@router.get("/me/sessions")
def list_my_sessions(
    request: Request,
    user: User = Depends(current_user),
    auth_session: AuthSession = Depends(current_session),
    db: Session = Depends(get_db),
):
    return AuthService(db, _settings(request)).active_sessions(user, auth_session.id)


@router.delete("/me/sessions/{session_id}")
def revoke_my_session(
    session_id: str,
    request: Request,
    user: User = Depends(current_user),
    auth_session: AuthSession = Depends(current_session),
    db: Session = Depends(get_db),
):
    row = AuthService(db, _settings(request)).revoke_session_reference(
        user, session_id, request.state.ip_hash
    )
    current = row.id == auth_session.id
    response = JSONResponse({"revoked": True, "current": current})
    if current:
        _clear_session_cookies(response, request)
    return response


@router.post("/me/sessions/revoke-others")
def revoke_other_sessions(
    request: Request,
    user: User = Depends(current_user),
    auth_session: AuthSession = Depends(current_session),
    db: Session = Depends(get_db),
):
    count = AuthService(db, _settings(request)).revoke_other_sessions(
        user, auth_session.id, request.state.ip_hash
    )
    return {"revoked": count}


@router.get("/me/mfa")
def mfa_status(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    recovery_count = db.scalar(
        select(func.count(MfaRecoveryCode.id)).where(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.used_at.is_(None),
        )
    )
    return {
        "enabled": user.mfa_enabled_at is not None,
        "enabled_at": iso_utc(user.mfa_enabled_at),
        "recovery_codes_remaining": recovery_count or 0,
        "required_for_role": (
            _settings(request).require_mfa_for_privileged
            and user.role in {"operator", "admin"}
        ),
    }


@router.post("/me/mfa/setup")
def begin_mfa_setup(
    req: MfaSetupRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return AuthService(db, _settings(request)).begin_mfa_setup(
        user,
        current_password=req.current_password,
        current_code=req.current_code,
        ip_hash=request.state.ip_hash,
        user_agent_hash=request.state.user_agent_hash,
    )


@router.post("/me/mfa/confirm")
def confirm_mfa_setup(
    req: MfaConfirmRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    codes = AuthService(db, _settings(request)).confirm_mfa_setup(
        user,
        req.setup_token,
        req.code,
        ip_hash=request.state.ip_hash,
        user_agent_hash=request.state.user_agent_hash,
    )
    response = JSONResponse(
        {
            "enabled": True,
            "recovery_codes": codes,
            "detail": "MFA를 설정했습니다. 복구 코드는 지금 한 번만 표시됩니다.",
        }
    )
    _clear_session_cookies(response, request)
    return response


@router.post("/me/mfa/recovery-codes")
def rotate_mfa_recovery_codes(
    req: MfaManageRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    codes = AuthService(db, _settings(request)).regenerate_recovery_codes(
        user,
        current_password=req.current_password,
        code=req.code,
        ip_hash=request.state.ip_hash,
    )
    return {
        "recovery_codes": codes,
        "detail": "기존 복구 코드를 폐기하고 새 코드를 생성했습니다.",
    }


@router.delete("/me/mfa")
def disable_mfa(
    req: MfaManageRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    AuthService(db, _settings(request)).disable_mfa(
        user,
        current_password=req.current_password,
        code=req.code,
        ip_hash=request.state.ip_hash,
    )
    response = JSONResponse(
        {"enabled": False, "detail": "MFA를 해제했으며 모든 세션을 종료했습니다."}
    )
    _clear_session_cookies(response, request)
    return response


@router.get("/me/preferences")
def get_preferences(
    user: User = Depends(current_user), db: Session = Depends(get_db)
):
    row = db.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    from app.charts.ontology import CHART_TYPES, registry

    effective = AccessService(db).effective_ontology(user)
    meta = registry.meta(effective)

    return {
        "personal": {
            "ontology": row.ontology if row else {},
            "ui": row.ui if row else {},
        },
        "effective_ontology": effective,
        "available_chart_types": {
            key: value["label"] for key, value in CHART_TYPES.items()
        },
        "available_domains": ["all", *sorted(meta["domains"])],
    }


@router.put("/me/preferences")
def put_preferences(
    req: PreferencePatch,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _validate_ontology_config(req.ontology)
    if set(req.ui) - {"dense"} or (
        "dense" in req.ui and not isinstance(req.ui["dense"], bool)
    ):
        raise DomainError(400, "invalid ui preference", "화면 설정이 올바르지 않습니다.")
    row = db.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    if row is None:
        row = UserPreference(user_id=user.id)
        db.add(row)
    row.ontology = req.ontology
    row.ui = req.ui
    audit(
        db,
        "preferences_changed",
        actor=user,
        target=user,
        ip_hash=request.state.ip_hash,
        details={"ontology_keys": sorted(req.ontology), "ui_keys": sorted(req.ui)},
    )
    db.flush()
    return {
        "saved": True,
        "effective_ontology": AccessService(db).effective_ontology(user),
    }


@router.get("/billing/plans")
def billing_plans(
    _user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return [
        {
            "code": row.code,
            "label": row.label,
            "duration_days": row.duration_days,
            "price_amount": row.price_amount,
            "currency": row.currency,
        }
        for row in PaymentService(db).list_plans()
    ]


@router.get("/billing/requests")
def my_payment_requests(
    user: User = Depends(current_user), db: Session = Depends(get_db)
):
    rows = db.scalars(
        select(PaymentRequest)
        .where(PaymentRequest.user_id == user.id)
        .order_by(PaymentRequest.requested_at.desc())
    ).all()
    return [serialize_payment(row) for row in rows]


@router.post("/billing/requests")
def create_payment_request(
    req: PaymentCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row, notification = PaymentService(db).request(
        user, req.plan_code, request.state.ip_hash
    )
    detail = "결제 요청이 접수되었습니다. 운영자 또는 최고관리자의 승인 후 이용권이 반영됩니다."
    if not notification["configured"]:
        detail += " 운영자의 알림 설정이 이뤄지지 않았습니다."
    elif notification.get("queued"):
        detail += " 운영 알림 전송을 예약했습니다."
        background_tasks.add_task(
            deliver_payment_notification,
            request.app.state.database,
            row.public_id,
        )
    return {
        "request": serialize_payment(row),
        "notification": notification,
        "detail": detail,
    }


def _target_user(db: Session, public_id: str) -> User:
    user = db.scalar(select(User).where(User.public_id == public_id))
    if user is None:
        raise DomainError(404, "user not found", "회원을 찾을 수 없습니다.")
    return user


def _serialize_management_control(db: Session, actor: User) -> None:
    """권한·회원 관리 쓰기를 RDB 전체의 같은 제어 행으로 직렬화한다."""
    claimed = db.execute(
        update(AuthControl)
        .where(AuthControl.id == 1)
        .values(
            revision=AuthControl.revision + 1,
            updated_at=utcnow(),
        )
    )
    if claimed.rowcount != 1:
        raise DomainError(503, "control unavailable", "회원 관리 제어 행을 찾을 수 없습니다.")
    db.refresh(actor)
    if actor.status != "active" or actor.role not in {"operator", "admin"}:
        raise DomainError(403, "forbidden", "회원 관리 권한이 더 이상 유효하지 않습니다.")


def _serialize_user_management(db: Session, actor: User, target: User) -> None:
    """관리자 수 불변식을 위해 모든 회원 역할/상태 변경을 같은 행에 직렬화한다."""
    _serialize_management_control(db, actor)
    db.refresh(target)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise DomainError(400, "invalid date", "날짜 형식이 올바르지 않습니다.") from exc


def _admin_user_payload(db: Session, user: User) -> dict:
    data = user_payload(db, user)
    data["id"] = user.public_id
    data["approved_at"] = iso_utc(user.approved_at)
    data["locked_until"] = iso_utc(user.locked_until)
    overrides = {
        row.page_id: row.allowed
        for row in db.scalars(
            select(UserPagePermission).where(UserPagePermission.user_id == user.id)
        ).all()
    }
    pages = {p.id: p.key for p in db.scalars(select(PageResource)).all()}
    data["permission_overrides"] = {
        pages[page_id]: value for page_id, value in overrides.items() if page_id in pages
    }
    return data


def _admin_user_payloads(db: Session, users: list[User]) -> list[dict]:
    """회원 목록의 역할/override를 한 번씩 읽어 N+1 권한 조회를 피한다."""
    if not users:
        return []
    pages = {page.id: page for page in db.scalars(select(PageResource)).all()}
    active_page_ids = {
        page_id for page_id, page in pages.items() if page.active
    }
    roles = {user.role for user in users}
    role_values: dict[str, dict[int, bool]] = {role: {} for role in roles}
    for row in db.scalars(
        select(RolePagePermission).where(RolePagePermission.role.in_(roles))
    ).all():
        role_values.setdefault(row.role, {})[row.page_id] = row.allowed
    user_ids = [user.id for user in users]
    overrides: dict[int, dict[int, bool]] = {user_id: {} for user_id in user_ids}
    for row in db.scalars(
        select(UserPagePermission).where(
            UserPagePermission.user_id.in_(user_ids)
        )
    ).all():
        overrides[row.user_id][row.page_id] = row.allowed

    payloads = []
    for user in users:
        if user.role == "admin":
            allowed = sorted(
                pages[page_id].key for page_id in active_page_ids
            )
        else:
            effective = dict(role_values.get(user.role, {}))
            effective.update(overrides[user.id])
            allowed_set = {
                pages[page_id].key
                for page_id, value in effective.items()
                if value and page_id in active_page_ids
            }
            if user.role in {"guest", "member"}:
                allowed_set -= ADMIN_PAGE_KEYS
                if (
                    user.membership_ends_at is not None
                    and user.membership_ends_at < utcnow()
                ):
                    allowed_set &= {"profile", "billing"}
            allowed = sorted(allowed_set)
        data = user_payload(db, user, allowed_pages=allowed)
        data["id"] = user.public_id
        data["approved_at"] = iso_utc(user.approved_at)
        data["locked_until"] = iso_utc(user.locked_until)
        data["permission_overrides"] = {
            pages[page_id].key: value
            for page_id, value in overrides[user.id].items()
            if page_id in pages
        }
        payloads.append(data)
    return payloads


def _replace_user_permissions(
    db: Session,
    actor: User,
    target: User,
    permissions,
) -> None:
    keys = [item.page_key for item in permissions]
    if len(keys) != len(set(keys)):
        raise DomainError(400, "duplicate permission", "같은 페이지 권한이 중복되었습니다.")
    if not role_can_manage(actor.role, target.role):
        raise DomainError(403, "forbidden", "이 회원의 접근 권한을 변경할 수 없습니다.")
    pages = {p.key: p for p in db.scalars(select(PageResource)).all()}
    db.execute(
        delete(UserPagePermission).where(UserPagePermission.user_id == target.id)
    )
    if target.role == "admin":
        return
    for item in permissions:
        page = pages.get(item.page_key)
        if page is None:
            raise DomainError(400, "page not found", f"알 수 없는 페이지: {item.page_key}")
        if (
            target.role in {"guest", "member"}
            and item.allowed
            and item.page_key in ADMIN_PAGE_KEYS
        ):
            raise DomainError(403, "reserved page", "운영 페이지는 운영자 이상에게만 허용됩니다.")
        db.add(
            UserPagePermission(
                user_id=target.id,
                page_id=page.id,
                allowed=item.allowed,
            )
        )


@router.get("/admin/users")
def admin_users(
    status: str | None = None,
    q: str | None = None,
    before: str | None = None,
    limit: int = 50,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    limit = max(1, min(limit, 100))
    query = select(User).order_by(User.id.desc()).limit(limit + 1)
    if status:
        if status not in USER_STATUSES:
            raise DomainError(400, "invalid status", "알 수 없는 회원 상태입니다.")
        query = query.where(User.status == status)
    if q and q.strip():
        pattern = f"%{q.strip().casefold()[:100]}%"
        query = query.where(
            or_(
                func.lower(User.email).like(pattern),
                func.lower(User.nickname).like(pattern),
                User.public_id == q.strip(),
            )
        )
    if before:
        cursor = db.scalar(select(User.id).where(User.public_id == before))
        if cursor is None:
            raise DomainError(400, "invalid cursor", "회원 목록 커서가 올바르지 않습니다.")
        query = query.where(User.id < cursor)
    if actor.role == "operator":
        query = query.where(User.role.in_(("guest", "member")))
    rows = list(db.scalars(query).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row, item in zip(rows, _admin_user_payloads(db, rows)):
        item["is_self"] = row.id == actor.id
        items.append(item)
    return {
        "items": items,
        "next_before": rows[-1].public_id if has_more and rows else None,
    }


@router.patch("/admin/users/{public_id}")
def patch_admin_user(
    public_id: str,
    req: UserAdminPatch,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    target = _target_user(db, public_id)
    _serialize_user_management(db, actor, target)
    if target.id == actor.id:
        raise DomainError(400, "self management blocked", "자신의 역할이나 상태는 이 화면에서 바꿀 수 없습니다.")
    if not role_can_manage(actor.role, target.role):
        raise DomainError(403, "forbidden", "이 회원을 관리할 권한이 없습니다.")
    before = {"role": target.role, "status": target.status, "membership": iso_utc(target.membership_ends_at)}
    authentication_changed = False
    privilege_promoted = False
    if req.role is not None:
        if req.role not in ROLES:
            raise DomainError(400, "invalid role", "알 수 없는 역할입니다.")
        if actor.role != "admin" and not role_can_manage(actor.role, req.role):
            raise DomainError(403, "forbidden", "부여할 수 없는 역할입니다.")
        if (
            _settings(request).require_mfa_for_privileged
            and target.role != req.role
            and req.role in {"operator", "admin"}
            and target.mfa_enabled_at is None
        ):
            raise DomainError(
                409,
                "mfa enrollment required",
                "권한 계정으로 승격하기 전에 대상 사용자가 MFA를 설정해야 합니다.",
            )
        privilege_promoted = (
            target.role in {"guest", "member"}
            and req.role in {"operator", "admin"}
        )
        authentication_changed = authentication_changed or target.role != req.role
        target.role = req.role
    if req.status is not None:
        if req.status not in USER_STATUSES:
            raise DomainError(400, "invalid status", "알 수 없는 회원 상태입니다.")
        authentication_changed = authentication_changed or target.status != req.status
        target.status = req.status
        if req.status == "active":
            target.approved_at = utcnow()
            target.approved_by_id = actor.id
    removes_active_admin = (
        before["role"] == "admin"
        and before["status"] == "active"
        and (target.role != "admin" or target.status != "active")
    )
    if removes_active_admin:
        other_admins = db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin",
                User.status == "active",
                User.id != target.id,
            )
        )
        if not other_admins:
            raise DomainError(
                409,
                "last admin protected",
                "마지막 활성 최고관리자는 역할을 내리거나 정지할 수 없습니다.",
            )
    if req.membership_ends_at is not None:
        target.membership_ends_at = _parse_datetime(req.membership_ends_at)
    if req.permissions is not None:
        _replace_user_permissions(db, actor, target, req.permissions)
    disabled_user_limits = 0
    cancelled_payments = 0
    if privilege_promoted:
        disabled_user_limits = db.execute(
            update(AccessPolicy)
            .where(
                AccessPolicy.scope_type == "user",
                AccessPolicy.scope_user_id == target.id,
                AccessPolicy.policy_type == "request_limit",
                AccessPolicy.active.is_(True),
            )
            .values(active=False, updated_at=utcnow())
        ).rowcount
        cancelled_payments = db.execute(
            update(PaymentRequest)
            .where(
                PaymentRequest.user_id == target.id,
                PaymentRequest.status == "pending",
            )
            .values(
                status="rejected",
                pending_key=None,
                reviewed_at=utcnow(),
                reviewed_by_id=actor.id,
                review_note="권한 계정 승격으로 승인 대기 요청 자동 취소",
            )
        ).rowcount
    if authentication_changed:
        AuthService(db, _settings(request)).revoke_user_sessions(target.id)
    db.flush()
    audit(
        db,
        "admin_user_changed",
        actor=actor,
        target=target,
        ip_hash=request.state.ip_hash,
        details={
            "before": before,
            "after": {
                "role": target.role,
                "status": target.status,
                "membership": iso_utc(target.membership_ends_at),
            },
            "permission_count": (
                len(req.permissions) if req.permissions is not None else None
            ),
            "disabled_user_request_limits": disabled_user_limits,
            "cancelled_pending_payments": cancelled_payments,
        },
    )
    return _admin_user_payload(db, target)


@router.post("/admin/users/{public_id}/mfa-reset")
def reset_user_mfa(
    public_id: str,
    req: MfaAdminResetRequest,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _target_user(db, public_id)
    _serialize_user_management(db, actor, target)
    if target.id == actor.id:
        raise DomainError(
            400,
            "self reset blocked",
            "자기 MFA 복구는 복구 코드 또는 운영 CLI 절차를 사용하세요.",
        )
    if target.role not in {"guest", "member"}:
        raise DomainError(
            403,
            "privileged reset blocked",
            "운영자·최고관리자 MFA는 운영 CLI break-glass 절차로만 재등록할 수 있습니다.",
        )
    AuthService(db, _settings(request)).force_reset_mfa(
        target,
        actor=actor,
        reason=req.reason,
        ip_hash=request.state.ip_hash,
    )
    return {
        "reset": True,
        "detail": "기존 MFA·복구 코드·세션을 폐기했습니다. 사용자는 다시 로그인해 MFA를 등록해야 합니다.",
    }


@router.get("/admin/access")
def access_matrix(
    actor: User = Depends(require_operator), db: Session = Depends(get_db)
):
    pages = db.scalars(select(PageResource).order_by(PageResource.id)).all()
    rows = db.scalars(select(RolePagePermission)).all()
    matrix = {role: {} for role in ROLES}
    by_id = {page.id: page.key for page in pages}
    for row in rows:
        if row.page_id in by_id:
            matrix.setdefault(row.role, {})[by_id[row.page_id]] = row.allowed
    manageable_roles = (
        ["guest", "member"]
        if actor.role == "operator"
        else ["guest", "member", "operator"]
    )
    return {
        "pages": [
            {
                "key": page.key,
                "label": page.label,
                "path": page.path_pattern,
                "description": page.description,
            }
            for page in pages
        ],
        "role_permissions": matrix,
        "manageable_roles": manageable_roles,
    }


@router.put("/admin/access/roles/{role}")
def put_role_permissions(
    role: str,
    req: PermissionPatch,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    _serialize_management_control(db, actor)
    if role not in ROLES:
        raise DomainError(404, "role not found", "역할을 찾을 수 없습니다.")
    if role == "admin":
        raise DomainError(403, "admin access immutable", "최고관리자는 항상 모든 페이지에 접근합니다.")
    if actor.role == "operator" and role not in {"guest", "member"}:
        raise DomainError(403, "forbidden", "운영자는 일반회원과 게스트 기본 권한만 변경할 수 있습니다.")
    keys = [item.page_key for item in req.permissions]
    if len(keys) != len(set(keys)):
        raise DomainError(400, "duplicate permission", "같은 페이지 권한이 중복되었습니다.")
    pages = {p.key: p for p in db.scalars(select(PageResource)).all()}
    for item in req.permissions:
        page = pages.get(item.page_key)
        if page is None:
            raise DomainError(400, "page not found", f"알 수 없는 페이지: {item.page_key}")
        if role in {"guest", "member"} and item.allowed and item.page_key in ADMIN_PAGE_KEYS:
            raise DomainError(403, "reserved page", "운영 페이지는 운영자 이상에게만 허용됩니다.")
        row = db.scalar(
            select(RolePagePermission).where(
                RolePagePermission.role == role, RolePagePermission.page_id == page.id
            )
        )
        if row is None:
            db.add(RolePagePermission(role=role, page_id=page.id, allowed=item.allowed))
        else:
            row.allowed = item.allowed
    db.flush()
    audit(
        db,
        "role_permissions_changed",
        actor=actor,
        ip_hash=request.state.ip_hash,
        details={"role": role, "count": len(req.permissions)},
    )
    return {"saved": True}


@router.put("/admin/access/users/{public_id}")
def put_user_permissions(
    public_id: str,
    req: PermissionPatch,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    target = _target_user(db, public_id)
    _serialize_user_management(db, actor, target)
    if not role_can_manage(actor.role, target.role):
        raise DomainError(403, "forbidden", "이 회원의 접근 권한을 변경할 수 없습니다.")
    if target.role == "admin":
        raise DomainError(403, "admin access immutable", "최고관리자는 항상 모든 페이지에 접근합니다.")
    _replace_user_permissions(db, actor, target, req.permissions)
    db.flush()
    audit(
        db,
        "user_permissions_changed",
        actor=actor,
        target=target,
        ip_hash=request.state.ip_hash,
        details={"count": len(req.permissions)},
    )
    return {"saved": True, "allowed_pages": AccessService(db).allowed_pages(target)}


@router.get("/admin/policies")
def list_policies(
    actor: User = Depends(require_operator), db: Session = Depends(get_db)
):
    rows = db.scalars(select(AccessPolicy).order_by(AccessPolicy.priority, AccessPolicy.id)).all()
    if actor.role == "operator":
        visible = []
        for row in rows:
            if row.scope_type == "system":
                continue
            if row.scope_type == "role" and row.scope_role not in {"guest", "member"}:
                continue
            if row.scope_type == "user":
                target = db.get(User, row.scope_user_id)
                if target is None or not role_can_manage(actor.role, target.role):
                    continue
            visible.append(row)
        rows = visible
    return [serialize_policy(db, row) for row in rows]


@router.post("/admin/policies")
def create_policy(
    req: PolicyCreate,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    if req.policy_type not in {"ontology", "request_limit"}:
        raise DomainError(400, "invalid policy type", "알 수 없는 정책 유형입니다.")
    _validate_policy_config(req.policy_type, req.config)
    if req.scope_type not in {"system", "role", "user"}:
        raise DomainError(400, "invalid policy scope", "알 수 없는 정책 범위입니다.")
    if req.scope_type == "system" and actor.role != "admin":
        raise DomainError(403, "forbidden", "시스템 공통 정책은 최고관리자만 만들 수 있습니다.")
    scope_user_id = None
    if req.scope_type == "role":
        if req.scope_role not in ROLES:
            raise DomainError(400, "invalid role", "정책 역할이 올바르지 않습니다.")
        if actor.role == "operator" and req.scope_role not in {"guest", "member"}:
            raise DomainError(403, "forbidden", "운영자는 하위 역할 정책만 만들 수 있습니다.")
    if req.scope_type == "user":
        if not req.scope_user_public_id:
            raise DomainError(400, "user required", "사용자 정책에는 대상 사용자가 필요합니다.")
        target = _target_user(db, req.scope_user_public_id)
        if not role_can_manage(actor.role, target.role):
            raise DomainError(403, "forbidden", "이 회원에게 정책을 할당할 수 없습니다.")
        scope_user_id = target.id
    row = AccessPolicy(
        name=req.name,
        policy_type=req.policy_type,
        scope_type=req.scope_type,
        scope_role=req.scope_role if req.scope_type == "role" else None,
        scope_user_id=scope_user_id,
        priority=req.priority,
        config=req.config,
        active=req.active,
        created_by_id=actor.id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "policy_created",
        actor=actor,
        ip_hash=request.state.ip_hash,
        details={"policy_id": row.public_id, "type": row.policy_type, "scope": row.scope_type},
    )
    return serialize_policy(db, row)


@router.patch("/admin/policies/{policy_id}")
def patch_policy(
    policy_id: str,
    req: PolicyPatch,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    row = db.scalar(select(AccessPolicy).where(AccessPolicy.public_id == policy_id))
    if row is None:
        raise DomainError(404, "policy not found", "정책을 찾을 수 없습니다.")
    if row.scope_type == "system" and actor.role != "admin":
        raise DomainError(403, "forbidden", "시스템 정책은 최고관리자만 변경할 수 있습니다.")
    if row.scope_role and actor.role == "operator" and row.scope_role not in {"guest", "member"}:
        raise DomainError(403, "forbidden", "이 정책을 변경할 수 없습니다.")
    if row.scope_type == "user" and actor.role == "operator":
        target = db.get(User, row.scope_user_id)
        if target is None or not role_can_manage(actor.role, target.role):
            raise DomainError(403, "forbidden", "이 사용자 정책을 변경할 수 없습니다.")
    if req.name is not None:
        row.name = req.name
    if req.priority is not None:
        row.priority = req.priority
    if req.config is not None:
        _validate_policy_config(row.policy_type, req.config)
        row.config = req.config
    if req.active is not None:
        row.active = req.active
    audit(
        db,
        "policy_changed",
        actor=actor,
        ip_hash=request.state.ip_hash,
        details={"policy_id": row.public_id},
    )
    return serialize_policy(db, row)


@router.get("/admin/ip-blocks")
def list_ip_blocks(
    _actor: User = Depends(require_admin), db: Session = Depends(get_db)
):
    rows = db.scalars(select(IpBlock).order_by(IpBlock.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "network": row.network,
            "reason": row.reason,
            "active": row.active,
            "expires_at": iso_utc(row.expires_at),
            "created_at": iso_utc(row.created_at),
        }
        for row in rows
    ]


@router.post("/admin/ip-blocks")
def create_ip_block(
    req: IpBlockCreate,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        network = parse_network(req.network)
    except ValueError as exc:
        raise DomainError(400, "invalid network", str(exc)) from exc
    if db.scalar(select(IpBlock.id).where(IpBlock.network == network)):
        raise DomainError(409, "block exists", "이미 등록된 IP 차단 정책입니다.")
    if ip_in_networks(request.state.client_ip, (network,)):
        raise DomainError(
            400,
            "self block prevented",
            "현재 관리자 접속 IP가 포함된 네트워크는 앱 화면에서 차단할 수 없습니다.",
        )
    expires_at = _parse_datetime(req.expires_at)
    if expires_at is not None and expires_at <= utcnow():
        raise DomainError(400, "invalid expiry", "차단 만료 시각은 현재보다 이후여야 합니다.")
    row = IpBlock(
        network=network,
        reason=req.reason,
        expires_at=expires_at,
        created_by_id=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise DomainError(
            409,
            "block exists",
            "이미 등록된 IP 차단 정책입니다.",
        ) from exc
    audit(
        db,
        "ip_block_created",
        actor=actor,
        ip_hash=request.state.ip_hash,
        details={"network": network, "reason": req.reason},
    )
    return {"created": True, "network": network}


@router.delete("/admin/ip-blocks/{block_id}")
def delete_ip_block(
    block_id: int,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.get(IpBlock, block_id)
    if row is None:
        raise DomainError(404, "block not found", "IP 차단 정책을 찾을 수 없습니다.")
    row.active = False
    audit(
        db,
        "ip_block_disabled",
        actor=actor,
        ip_hash=request.state.ip_hash,
        details={"network": row.network},
    )
    return {"disabled": True}


@router.get("/admin/payments")
def admin_payments(
    status: str = "pending",
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    if status not in {"pending", "approved", "rejected"}:
        raise DomainError(400, "invalid payment status", "결제 상태가 올바르지 않습니다.")
    rows = db.scalars(
        select(PaymentRequest)
        .where(PaymentRequest.status == status)
        .order_by(PaymentRequest.requested_at.desc())
        .limit(200)
    ).all()
    if actor.role == "operator":
        rows = [row for row in rows if role_can_manage(actor.role, row.user.role)]
    return [serialize_payment(row) for row in rows]


@router.post("/admin/payments/{request_id}/review")
def review_payment(
    request_id: str,
    req: PaymentReview,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    row = PaymentService(db).review(
        actor,
        request_id,
        approve=req.approve,
        note=req.note,
        ip_hash=request.state.ip_hash,
    )
    return serialize_payment(row)


@router.post("/admin/payments/{request_id}/notify")
def retry_payment_notification(
    request_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    row, notification = PaymentService(db).requeue_notification(
        actor, request_id, request.state.ip_hash
    )
    if notification["configured"]:
        background_tasks.add_task(
            deliver_payment_notification,
            request.app.state.database,
            row.public_id,
        )
    return {
        "queued": notification["configured"],
        "configured": notification["configured"],
        "detail": (
            "운영 알림 재전송을 예약했습니다."
            if notification["configured"]
            else "운영자의 알림 설정이 이뤄지지 않았습니다."
        ),
    }


@router.get("/admin/audit")
def list_audit_events(
    event_type: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    limit = max(1, min(limit, 200))
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if event_type:
        query = query.where(AuditLog.event_type == event_type[:60])
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    if actor.role == "operator":
        manageable_ids = select(User.id).where(User.role.in_(("guest", "member")))
        query = query.where(
            or_(
                AuditLog.actor_user_id == actor.id,
                AuditLog.target_user_id.in_(manageable_ids),
            )
        )
    rows = list(db.scalars(query).all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    identity_ids = {
        user_id
        for row in rows
        for user_id in (row.actor_user_id, row.target_user_id)
        if user_id is not None
    }
    identities = (
        {
            user.id: user
            for user in db.scalars(
                select(User).where(User.id.in_(identity_ids))
            ).all()
        }
        if identity_ids
        else {}
    )

    def identity(user_id: int | None) -> dict | None:
        user = identities.get(user_id)
        if user is None:
            return None
        return {
            "nickname": user.nickname,
            "masked_id": mask_public_id(user.public_id),
            "role_label": ROLE_LABELS.get(user.role, user.role),
        }

    return {
        "items": [
            {
                "id": row.id,
                "event_type": row.event_type,
                "actor": identity(row.actor_user_id),
                "target": identity(row.target_user_id),
                "ip_fingerprint": row.ip_hash[:10],
                "details": row.details,
                "created_at": iso_utc(row.created_at),
            }
            for row in rows
        ],
        "next_before_id": rows[-1].id if has_more and rows else None,
    }

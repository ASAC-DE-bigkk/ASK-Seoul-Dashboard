"""인증·프로필·회원관리·정책·모의결제 API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
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
    AuthSession,
    IpBlock,
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
    NicknamePatch,
    PasswordPatch,
    PaymentCreate,
    PaymentReview,
    PermissionPatch,
    PolicyCreate,
    PolicyPatch,
    PreferencePatch,
    RegisterRequest,
    ResetPasswordRequest,
    UserAdminPatch,
)
from .security import (
    ROLES,
    USER_STATUSES,
    iso_utc,
    parse_network,
    role_can_manage,
    safe_next_path,
)
from .service import (
    AccessService,
    AuthService,
    DomainError,
    PaymentService,
    audit,
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
            "forgot_password",
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
        from app.charts.ontology import CHART_TYPES, registry

        hidden = config.get("hidden_chart_types", [])
        if not isinstance(hidden, list) or any(item not in CHART_TYPES for item in hidden):
            raise DomainError(400, "invalid policy", "온톨로지의 차트 타입이 올바르지 않습니다.")
        default_domain = config.get("default_domain", "all")
        domains = {source["domain"] for source in registry.sources()}
        if default_domain != "all" and default_domain not in domains:
            raise DomainError(400, "invalid policy", "온톨로지의 기본 도메인이 올바르지 않습니다.")


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
def register(req: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    user, email = AuthService(db, _settings(request)).register(
        req.email, req.password, request.state.ip_hash
    )
    if email.configured and email.delivered:
        detail = "인증 이메일을 보냈습니다. 이메일 인증 후 로그인할 수 있습니다."
    elif email.configured:
        detail = "가입은 접수되었지만 인증 이메일 전송에 실패했습니다. 관리자 승인을 기다려 주세요."
    else:
        detail = "가입이 접수되었습니다. 이메일 인증이 설정되지 않아 관리자 승인을 기다려야 합니다."
    return {
        "registered": True,
        "status": user.status,
        "email_configured": email.configured,
        "email_delivered": email.delivered,
        "detail": detail,
    }


@router.get("/auth/verify-email")
def verify_email(token: str, request: Request, db: Session = Depends(get_db)):
    AuthService(db, _settings(request)).verify_email_token(token, request.state.ip_hash)
    return RedirectResponse("/auth/login?verified=1", status_code=303)


@router.post("/auth/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user, token, csrf, expires = AuthService(db, _settings(request)).login(
        req.email,
        req.password,
        remember=req.remember,
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
        persistent=req.remember,
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
    from app.charts.ontology import CHART_TYPES, registry

    hidden = req.ontology.get("hidden_chart_types", [])
    if not isinstance(hidden, list) or any(item not in CHART_TYPES for item in hidden):
        raise DomainError(400, "invalid ontology", "알 수 없는 차트 타입이 포함되어 있습니다.")
    default_domain = req.ontology.get("default_domain", "all")
    domains = {source["domain"] for source in registry.sources()}
    if default_domain != "all" and default_domain not in domains:
        raise DomainError(400, "invalid ontology", "알 수 없는 기본 도메인입니다.")
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row, notification = PaymentService(db).request(
        user, req.plan_code, request.state.ip_hash
    )
    detail = "결제 요청이 접수되었습니다. 운영자 또는 최고관리자의 승인 후 이용권이 반영됩니다."
    if not notification["configured"]:
        detail += " 운영자의 알림 설정이 이뤄지지 않았습니다."
    elif not notification["delivered"]:
        detail += " 운영 알림 전송에 실패했으므로 관리자 화면에서 직접 확인해야 합니다."
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


@router.get("/admin/users")
def admin_users(
    status: str | None = None,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    query = select(User).order_by(User.created_at.desc())
    if status:
        query = query.where(User.status == status)
    rows = db.scalars(query).all()
    if actor.role == "operator":
        rows = [row for row in rows if role_can_manage(actor.role, row.role)]
    return [_admin_user_payload(db, row) for row in rows]


@router.patch("/admin/users/{public_id}")
def patch_admin_user(
    public_id: str,
    req: UserAdminPatch,
    request: Request,
    actor: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    target = _target_user(db, public_id)
    if target.id == actor.id:
        raise DomainError(400, "self management blocked", "자신의 역할이나 상태는 이 화면에서 바꿀 수 없습니다.")
    if not role_can_manage(actor.role, target.role):
        raise DomainError(403, "forbidden", "이 회원을 관리할 권한이 없습니다.")
    before = {"role": target.role, "status": target.status, "membership": iso_utc(target.membership_ends_at)}
    authentication_changed = False
    if req.role is not None:
        if req.role not in ROLES:
            raise DomainError(400, "invalid role", "알 수 없는 역할입니다.")
        if actor.role != "admin" and not role_can_manage(actor.role, req.role):
            raise DomainError(403, "forbidden", "부여할 수 없는 역할입니다.")
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
    if req.membership_ends_at is not None:
        target.membership_ends_at = _parse_datetime(req.membership_ends_at)
    if authentication_changed:
        AuthService(db, _settings(request)).revoke_user_sessions(target.id)
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
        },
    )
    return _admin_user_payload(db, target)


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
    manageable_roles = ["guest", "member"] if actor.role == "operator" else [
        "guest", "member", "operator", "admin"
    ]
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
    if role not in ROLES:
        raise DomainError(404, "role not found", "역할을 찾을 수 없습니다.")
    if actor.role == "operator" and role not in {"guest", "member"}:
        raise DomainError(403, "forbidden", "운영자는 일반회원과 게스트 기본 권한만 변경할 수 있습니다.")
    pages = {p.key: p for p in db.scalars(select(PageResource)).all()}
    for item in req.permissions:
        page = pages.get(item.page_key)
        if page is None:
            raise DomainError(400, "page not found", f"알 수 없는 페이지: {item.page_key}")
        row = db.scalar(
            select(RolePagePermission).where(
                RolePagePermission.role == role, RolePagePermission.page_id == page.id
            )
        )
        if row is None:
            db.add(RolePagePermission(role=role, page_id=page.id, allowed=item.allowed))
        else:
            row.allowed = item.allowed
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
    if not role_can_manage(actor.role, target.role):
        raise DomainError(403, "forbidden", "이 회원의 접근 권한을 변경할 수 없습니다.")
    pages = {p.key: p for p in db.scalars(select(PageResource)).all()}
    db.execute(delete(UserPagePermission).where(UserPagePermission.user_id == target.id))
    for item in req.permissions:
        page = pages.get(item.page_key)
        if page is None:
            raise DomainError(400, "page not found", f"알 수 없는 페이지: {item.page_key}")
        db.add(
            UserPagePermission(
                user_id=target.id, page_id=page.id, allowed=item.allowed
            )
        )
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
        rows = [
            row
            for row in rows
            if row.scope_type != "system"
            and (row.scope_role in {None, "guest", "member"})
        ]
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
    row = IpBlock(
        network=network,
        reason=req.reason,
        expires_at=_parse_datetime(req.expires_at),
        created_by_id=actor.id,
    )
    db.add(row)
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
    rows = db.scalars(
        select(PaymentRequest)
        .where(PaymentRequest.status == status)
        .order_by(PaymentRequest.requested_at.desc())
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

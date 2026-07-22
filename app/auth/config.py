"""인증 시스템 설정.

시크릿은 환경변수 또는 런타임 전용 파일에서만 읽으며, 저장소에 기본 자격증명을 두지 않는다.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name}은 true 또는 false여야 합니다.")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 정수여야 합니다.") from exc


def _runtime_secret(path: Path) -> str:
    """개발 환경 전용 영속 secret. 운영은 반드시 secret manager/env를 사용한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _host_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = hostname.casefold()
    for pattern in allowed_hosts:
        value = pattern.casefold()
        if value == host:
            return True
        if value.startswith("*.") and host.endswith(value[1:]):
            return True
    return False


@dataclass(frozen=True)
class AuthSettings:
    env: str
    mode: str
    database_url: str
    public_base_url: str
    allowed_hosts: tuple[str, ...]
    session_pepper: str
    cookie_secure: bool
    cookie_name: str
    csrf_cookie_name: str
    session_hours: int
    session_idle_minutes: int
    max_sessions_per_user: int
    remember_days: int
    remember_idle_days: int
    reset_token_minutes: int
    verify_token_hours: int
    auto_approve_verified: bool
    trusted_proxy_headers: bool
    bind_session_user_agent: bool
    bind_session_ip: bool
    mfa_master_key: str
    require_mfa_for_privileged: bool
    max_request_bytes: int
    bootstrap_admin_email: str
    bootstrap_admin_password: str
    local_auto_client_cidrs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def local_auto(self) -> bool:
        return self.mode == "local_auto"


def load_settings() -> AuthSettings:
    env = os.environ.get("AUTH_ENV", "development").strip().lower()
    if env not in {"development", "test", "production"}:
        raise RuntimeError(
            "AUTH_ENV는 development, test, production 중 하나여야 합니다."
        )
    mode = os.environ.get("AUTH_MODE", "required").strip().lower()
    if mode not in {"required", "local_auto"}:
        raise RuntimeError("AUTH_MODE는 required 또는 local_auto여야 합니다.")
    production = env == "production"
    pepper = os.environ.get("AUTH_SESSION_PEPPER", "").strip()
    if not pepper:
        if production:
            raise RuntimeError("production에서는 AUTH_SESSION_PEPPER가 필수입니다.")
        pepper = _runtime_secret(RUNTIME_DIR / ".auth_session_secret")

    cookie_secure = _bool("AUTH_COOKIE_SECURE", production)
    cookie_name = "__Host-askseoul_session" if cookie_secure else "askseoul_session"
    csrf_cookie = "__Host-askseoul_csrf" if cookie_secure else "askseoul_csrf"
    allowed = tuple(
        item.strip()
        for item in os.environ.get("AUTH_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
        if item.strip()
    )
    public_base_url = os.environ.get(
        "AUTH_PUBLIC_BASE_URL", "http://127.0.0.1:8765"
    ).rstrip("/")
    public_url = urlsplit(public_base_url)
    if (
        public_url.scheme not in {"http", "https"}
        or not public_url.hostname
        or public_url.username
        or public_url.password
        or public_url.query
        or public_url.fragment
        or public_url.path not in {"", "/"}
    ):
        raise RuntimeError("AUTH_PUBLIC_BASE_URL은 경로가 없는 유효한 http(s) origin이어야 합니다.")
    effective_allowed = allowed or ("127.0.0.1", "localhost")
    if not _host_allowed(public_url.hostname, effective_allowed):
        raise RuntimeError("AUTH_PUBLIC_BASE_URL host가 AUTH_ALLOWED_HOSTS에 포함되어야 합니다.")
    if production:
        if public_url.scheme != "https":
            raise RuntimeError("production의 AUTH_PUBLIC_BASE_URL은 https여야 합니다.")
        if not cookie_secure:
            raise RuntimeError("production에서는 AUTH_COOKIE_SECURE=true가 필수입니다.")
        if len(pepper) < 32:
            raise RuntimeError("production의 AUTH_SESSION_PEPPER는 32자 이상이어야 합니다.")
        if any(item == "*" for item in effective_allowed):
            raise RuntimeError("production에서는 AUTH_ALLOWED_HOSTS=*를 사용할 수 없습니다.")
    mfa_master_key = os.environ.get("AUTH_MFA_MASTER_KEY", "").strip()
    require_mfa = _bool("AUTH_REQUIRE_MFA_FOR_PRIVILEGED", production)
    if production and not require_mfa:
        raise RuntimeError(
            "production에서는 AUTH_REQUIRE_MFA_FOR_PRIVILEGED=true가 필수입니다."
        )
    if not mfa_master_key:
        if production and require_mfa:
            raise RuntimeError(
                "권한 계정 MFA 강제 시 AUTH_MFA_MASTER_KEY가 필수입니다."
            )
        mfa_master_key = hmac.new(
            pepper.encode("utf-8"),
            b"ask-seoul-development-mfa-master",
            hashlib.sha256,
        ).hexdigest()
    elif production and len(mfa_master_key) < 32:
        raise RuntimeError("production의 AUTH_MFA_MASTER_KEY는 32자 이상이어야 합니다.")
    bootstrap_admin_email = os.environ.get(
        "AUTH_BOOTSTRAP_ADMIN_EMAIL", ""
    ).strip()
    bootstrap_admin_password = os.environ.get(
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD", ""
    )
    if bool(bootstrap_admin_email) != bool(bootstrap_admin_password):
        raise RuntimeError(
            "AUTH_BOOTSTRAP_ADMIN_EMAIL과 AUTH_BOOTSTRAP_ADMIN_PASSWORD는 "
            "함께 설정하거나 함께 비워야 합니다."
        )
    if production and bootstrap_admin_email:
        raise RuntimeError(
            "production에서는 bootstrap 자격증명을 환경변수에 보관하지 않습니다. "
            "보호된 터미널에서 scripts/create_admin.py를 사용하세요."
        )
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if production and not database_url:
        raise RuntimeError("production에서는 DATABASE_URL을 명시해야 합니다.")
    if not database_url:
        database_url = f"sqlite:///{(RUNTIME_DIR / 'ask_seoul.db').as_posix()}"
    try:
        parsed_database_url = make_url(database_url)
    except ArgumentError as exc:
        raise RuntimeError("DATABASE_URL 형식이 올바르지 않습니다.") from exc
    trusted_proxy_headers = _bool("AUTH_TRUST_PROXY_HEADERS", False)
    local_auto_client_cidrs: tuple[str, ...] = ()
    raw_client_cidrs = os.environ.get("AUTH_LOCAL_AUTO_CLIENT_CIDRS", "").strip()
    if raw_client_cidrs:
        if mode != "local_auto":
            raise RuntimeError(
                "AUTH_LOCAL_AUTO_CLIENT_CIDRS는 AUTH_MODE=local_auto에서만 사용할 수 있습니다."
            )
        parsed_cidrs = []
        for item in raw_client_cidrs.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise RuntimeError(
                    f"AUTH_LOCAL_AUTO_CLIENT_CIDRS 항목이 올바른 CIDR이 아닙니다: {item}"
                ) from exc
            # 컨테이너 loopback publish 전제 하에서의 보조 신뢰 대역이므로 사설/loopback만
            # 허용한다. 공인 IP 대역(예: 0.0.0.0/0)은 원격 자동 로그인을 열어 위험하다.
            if not (network.is_private or network.is_loopback):
                raise RuntimeError(
                    "AUTH_LOCAL_AUTO_CLIENT_CIDRS는 사설/loopback 대역만 허용합니다(공인 IP 금지)."
                )
            parsed_cidrs.append(str(network))
        local_auto_client_cidrs = tuple(parsed_cidrs)
    if mode == "local_auto":
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if env != "development":
            raise RuntimeError(
                "AUTH_MODE=local_auto는 AUTH_ENV=development에서만 사용할 수 있습니다."
            )
        if public_url.scheme != "http" or public_url.hostname not in loopback_hosts:
            raise RuntimeError(
                "AUTH_MODE=local_auto의 AUTH_PUBLIC_BASE_URL은 loopback HTTP origin이어야 합니다."
            )
        if any(host not in loopback_hosts for host in effective_allowed):
            raise RuntimeError(
                "AUTH_MODE=local_auto의 AUTH_ALLOWED_HOSTS에는 loopback host만 허용됩니다."
            )
        if trusted_proxy_headers:
            raise RuntimeError(
                "AUTH_MODE=local_auto에서는 AUTH_TRUST_PROXY_HEADERS=false가 필수입니다."
            )
        if parsed_database_url.get_backend_name() != "sqlite":
            raise RuntimeError(
                "AUTH_MODE=local_auto는 로컬 SQLite DATABASE_URL에서만 사용할 수 있습니다."
            )
        if cookie_secure:
            raise RuntimeError(
                "AUTH_MODE=local_auto에서는 AUTH_COOKIE_SECURE=false가 필수입니다."
            )
        if bootstrap_admin_email:
            raise RuntimeError(
                "AUTH_MODE=local_auto에서는 bootstrap 관리자 자격증명을 사용할 수 없습니다."
            )
    if production:
        backend = parsed_database_url.get_backend_name()
        query = parsed_database_url.query
        if backend == "postgresql" and query.get("sslmode") != "verify-full":
            raise RuntimeError(
                "production PostgreSQL은 sslmode=verify-full이 필수입니다."
            )
        if backend in {"mysql", "mariadb"}:
            check_hostname = str(
                query.get("ssl_check_hostname", "")
            ).strip().lower()
            if not query.get("ssl_ca") or check_hostname not in {
                "1",
                "true",
                "yes",
                "on",
            }:
                raise RuntimeError(
                    "production MySQL/MariaDB는 ssl_ca와 "
                    "ssl_check_hostname=true가 필수입니다."
                )
    return AuthSettings(
        env=env,
        mode=mode,
        database_url=database_url,
        public_base_url=public_base_url,
        allowed_hosts=effective_allowed,
        session_pepper=pepper,
        cookie_secure=cookie_secure,
        cookie_name=cookie_name,
        csrf_cookie_name=csrf_cookie,
        session_hours=max(1, _int("AUTH_SESSION_HOURS", 12)),
        session_idle_minutes=max(5, _int("AUTH_SESSION_IDLE_MINUTES", 120)),
        max_sessions_per_user=max(1, _int("AUTH_MAX_SESSIONS_PER_USER", 10)),
        remember_days=max(1, _int("AUTH_REMEMBER_DAYS", 30)),
        remember_idle_days=max(1, _int("AUTH_REMEMBER_IDLE_DAYS", 7)),
        reset_token_minutes=max(5, _int("AUTH_RESET_TOKEN_MINUTES", 30)),
        verify_token_hours=max(1, _int("AUTH_VERIFY_TOKEN_HOURS", 24)),
        auto_approve_verified=_bool("AUTH_AUTO_APPROVE_VERIFIED", True),
        trusted_proxy_headers=trusted_proxy_headers,
        bind_session_user_agent=_bool("AUTH_BIND_SESSION_USER_AGENT", True),
        bind_session_ip=_bool("AUTH_BIND_SESSION_IP", False),
        mfa_master_key=mfa_master_key,
        require_mfa_for_privileged=require_mfa,
        max_request_bytes=max(16_384, _int("AUTH_MAX_REQUEST_BYTES", 262_144)),
        bootstrap_admin_email=bootstrap_admin_email,
        bootstrap_admin_password=bootstrap_admin_password,
        local_auto_client_cidrs=local_auto_client_cidrs,
    )

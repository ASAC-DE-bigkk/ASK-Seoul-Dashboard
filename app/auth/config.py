"""인증 시스템 설정.

시크릿은 환경변수 또는 런타임 전용 파일에서만 읽으며, 저장소에 기본 자격증명을 두지 않는다.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _runtime_secret(path: Path) -> str:
    """개발 환경 전용 영속 secret. 운영은 반드시 secret manager/env를 사용한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


@dataclass(frozen=True)
class AuthSettings:
    env: str
    database_url: str
    public_base_url: str
    allowed_hosts: tuple[str, ...]
    session_pepper: str
    cookie_secure: bool
    cookie_name: str
    csrf_cookie_name: str
    session_hours: int
    remember_days: int
    reset_token_minutes: int
    verify_token_hours: int
    auto_approve_verified: bool
    trusted_proxy_headers: bool
    bind_session_user_agent: bool
    bind_session_ip: bool
    max_request_bytes: int
    bootstrap_admin_email: str
    bootstrap_admin_password: str

    @property
    def production(self) -> bool:
        return self.env == "production"


def load_settings() -> AuthSettings:
    env = os.environ.get("AUTH_ENV", "development").strip().lower()
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
    return AuthSettings(
        env=env,
        database_url=os.environ.get(
            "DATABASE_URL", f"sqlite:///{(RUNTIME_DIR / 'ask_seoul.db').as_posix()}"
        ),
        public_base_url=os.environ.get(
            "AUTH_PUBLIC_BASE_URL", "http://127.0.0.1:8765"
        ).rstrip("/"),
        allowed_hosts=allowed or ("127.0.0.1", "localhost"),
        session_pepper=pepper,
        cookie_secure=cookie_secure,
        cookie_name=cookie_name,
        csrf_cookie_name=csrf_cookie,
        session_hours=max(1, _int("AUTH_SESSION_HOURS", 12)),
        remember_days=max(1, _int("AUTH_REMEMBER_DAYS", 30)),
        reset_token_minutes=max(5, _int("AUTH_RESET_TOKEN_MINUTES", 30)),
        verify_token_hours=max(1, _int("AUTH_VERIFY_TOKEN_HOURS", 24)),
        auto_approve_verified=_bool("AUTH_AUTO_APPROVE_VERIFIED", True),
        trusted_proxy_headers=_bool("AUTH_TRUST_PROXY_HEADERS", False),
        bind_session_user_agent=_bool("AUTH_BIND_SESSION_USER_AGENT", True),
        bind_session_ip=_bool("AUTH_BIND_SESSION_IP", False),
        max_request_bytes=max(16_384, _int("AUTH_MAX_REQUEST_BYTES", 262_144)),
        bootstrap_admin_email=os.environ.get("AUTH_BOOTSTRAP_ADMIN_EMAIL", "").strip(),
        bootstrap_admin_password=os.environ.get("AUTH_BOOTSTRAP_ADMIN_PASSWORD", ""),
    )

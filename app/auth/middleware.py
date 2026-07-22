"""전역 세션·인가·CSRF·요청 제한·보안 헤더 미들웨어."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
import time
from html import escape
from urllib.parse import quote
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from .models import IpBlock, User, utcnow
from .security import ip_in_networks, stable_digest, token_digest
from .service import (
    AUTO_BLOCK_REASONS,
    AccessService,
    AuthService,
    AutoBlockService,
    LOCAL_ANALYST_EMAIL,
)


logger = logging.getLogger(__name__)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
WINDOWS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}

# 이상행동 자동 차단 임계값(고정 윈도, IP 단위).
# undefined_api: 존재하지 않는 /api/* 경로(404) 반복 호출.
# tamper: 세션 조작 신호(변형 토큰·세션 바인딩 불일치·위조 CSRF 쌍).
UNDEFINED_API_LIMITS = {"minute": 30}
TAMPER_LIMITS = {"minute": 8}

_SESSION_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def malformed_session_token(raw: str) -> bool:
    """정상 발급 토큰(urlsafe base64)에서 나올 수 없는 형태인지 판정한다."""
    return (
        len(raw) < 24
        or len(raw) > 256
        or any(ch not in _SESSION_TOKEN_CHARS for ch in raw)
    )


def rate_limit_category(path: str, *, authenticated: bool) -> str:
    category = "authenticated" if authenticated else "anonymous"
    exact = {
        "/api/v1/auth/login": "login",
        "/api/v1/auth/register": "register",
        "/api/v1/auth/verify-email": "verify_email",
        "/api/v1/auth/resend-verification": "verify_email",
        "/api/v1/auth/forgot-password": "forgot_password",
        "/api/v1/auth/reset-password": "password_reset",
        "/api/v1/auth/mfa/verify": "mfa",
        "/api/v1/charts/query": "charts_query",
    }
    if path in exact:
        return exact[path]
    if path.startswith("/api/v1/charts/sources/") and path.endswith("/availability"):
        return "charts_query"
    return category


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


class RequestBodyLimitMiddleware:
    """Content-Length가 없거나 거짓이어도 ASGI body stream에서 상한을 강제한다."""

    def __init__(self, app, *, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        exceeded = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
            return message

        async def limited_send(message):
            if not exceeded:
                await send(message)

        await self.app(scope, limited_receive, limited_send)
        if exceeded:
            response = problem(413, "request too large", "요청 본문이 너무 큽니다.")
            await response(scope, receive, send)


class InMemoryRateLimiter:
    """단일 프로세스 보호용 fixed-window limiter.

    분산/다중 인스턴스 환경의 정본 제한은 WAF/edge 또는 Redis 계층에서 수행한다.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def check(self, subject: str, limits: dict[str, int]) -> tuple[bool, int]:
        now = int(time.time())
        exceeded_retry = 0
        with self._lock:
            if time.monotonic() - self._last_cleanup > 60:
                oldest = now - 86_400
                self._counts = defaultdict(
                    int,
                    {
                        key: value
                        for key, value in self._counts.items()
                        if key[2] >= oldest
                    },
                )
                self._last_cleanup = time.monotonic()
            active_keys: list[tuple[str, str, int]] = []
            for label, seconds in WINDOWS.items():
                limit = int(limits.get(label, 0) or 0)
                if limit <= 0:
                    continue
                bucket = now - (now % seconds)
                key = (subject, label, bucket)
                active_keys.append(key)
                if self._counts[key] >= limit:
                    exceeded_retry = max(exceeded_retry, bucket + seconds - now)
            if exceeded_retry == 0:
                for key in active_keys:
                    self._counts[key] += 1
        return exceeded_retry == 0, max(1, exceeded_retry)


class AuthSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        settings,
        inline_script_hashes_by_path: dict[str, tuple[str, ...]] | None = None,
    ):
        super().__init__(app)
        self.settings = settings
        self.inline_script_hashes_by_path = inline_script_hashes_by_path or {}
        self.rate_limiter = InMemoryRateLimiter()
        self.undefined_api_limiter = InMemoryRateLimiter()
        self.tamper_limiter = InMemoryRateLimiter()
        self._ip_cache: tuple[float, tuple[str, ...]] = (0.0, ())

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = self.settings
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_bytes:
                    return self._secure(problem(413, "request too large", "요청 본문이 너무 큽니다."), request)
            except ValueError:
                return self._secure(problem(400, "invalid content length", "잘못된 요청입니다."), request)

        client_ip = self._client_ip(request)
        ip_hash = stable_digest(client_ip, settings.session_pepper)
        ua_hash = stable_digest(
            request.headers.get("user-agent", "")[:500], settings.session_pepper
        )
        request.state.client_ip = client_ip
        request.state.ip_hash = ip_hash
        request.state.user_agent_hash = ua_hash
        request.state.user = None
        request.state.auth_session = None
        request.state.local_session_credentials = None

        database = request.app.state.database
        try:
            with database.session() as db:
                if self._blocked_ip(db, client_ip):
                    return self._secure(
                        self._blocked_ip_response(db, request, client_ip), request
                    )

                raw_session = request.cookies.get(settings.cookie_name)
                auth = AuthService(db, settings)
                session_failure: dict = {}
                current = auth.current_session(
                    raw_session,
                    ip_hash=ip_hash,
                    user_agent_hash=ua_hash,
                    failure=session_failure,
                )
                request.state.session_failure = session_failure
                if current:
                    auth_session, user = current
                    if settings.local_auto and user.email == LOCAL_ANALYST_EMAIL:
                        # 이미 발급된 로컬 세션도 오래된 page permission을 즉시
                        # 복구한다. 쿠키 만료/삭제를 기다리게 하지 않는다.
                        user = auth.ensure_local_analyst()
                    request.state.auth_session = auth_session
                    request.state.user = user
                elif raw_session:
                    failure_reason = session_failure.get("reason", "")
                    if failure_reason == "inactive_user":
                        # 정지된 회원은 유효했던 쿠키로도 백엔드에 도달하지 못하게
                        # 미들웨어에서 사유와 함께 근원 차단한다.
                        block = AutoBlockService(db).active_user_block(
                            session_failure.get("user_id", 0)
                        )
                        if block is not None:
                            return self._secure(
                                self._suspension_response(request, block.reason),
                                request,
                            )
                    if failure_reason == "binding_mismatch" or malformed_session_token(
                        raw_session
                    ):
                        blocked = self._register_tamper(db, request, client_ip)
                        if blocked is not None:
                            return self._secure(blocked, request)

                page_key = page_key_for_path(request.url.path)
                if current is None and self._should_auto_login(
                    request, client_ip, page_key
                ):
                    user = auth.ensure_local_analyst()
                    token, csrf, _expires = auth.create_session(
                        user,
                        remember=False,
                        ip_hash=ip_hash,
                        user_agent_hash=ua_hash,
                    )
                    db.flush()
                    current = auth.current_session(
                        token,
                        ip_hash=ip_hash,
                        user_agent_hash=ua_hash,
                    )
                    if current is None:
                        raise RuntimeError("로컬 분석 세션을 생성할 수 없습니다.")
                    auth_session, user = current
                    request.state.auth_session = auth_session
                    request.state.user = user
                    request.state.local_session_credentials = (token, csrf)

                rate_response = await self._rate_limit(
                    db, request, request.state.user, client_ip
                )
                if rate_response:
                    return self._secure(rate_response, request)

                if request.method not in SAFE_METHODS:
                    csrf_response = self._csrf_check(request)
                    if csrf_response:
                        if getattr(request.state, "csrf_forged", False):
                            blocked = self._register_tamper(db, request, client_ip)
                            if blocked is not None:
                                return self._secure(blocked, request)
                        return self._secure(csrf_response, request)

                if page_key:
                    if request.state.user is None:
                        return self._secure(
                            self._unauthenticated(request, session_failure), request
                        )
                    access = AccessService(db)
                    allowed = access.can_access(request.state.user, page_key)
                    if request.url.path in {"/admin", "/static/auth/admin.html"}:
                        allowed = any(
                            access.can_access(request.state.user, key)
                            for key in (
                                "admin_users",
                                "admin_access",
                                "admin_policies",
                                "admin_payments",
                                "admin_audit",
                                "admin_security",
                                "service_health",
                            )
                        )
                    if not allowed:
                        return self._secure(self._forbidden(request), request)

            response = await call_next(request)
            if (
                response.status_code == 404
                and request.url.path.startswith("/api/")
                and not self._detection_exempt(client_ip)
            ):
                allowed_calls, _ = self.undefined_api_limiter.check(
                    f"ip:{client_ip}", UNDEFINED_API_LIMITS
                )
                if not allowed_calls:
                    with database.session() as db:
                        blocked = self._trigger_auto_block(
                            db, request, client_ip, "undefined_api_flood"
                        )
                    return self._secure(blocked, request)
        except SQLAlchemyError as exc:
            logger.error(
                "auth database request failed path=%s error_type=%s",
                request.url.path,
                type(exc).__name__,
            )
            return self._secure(
                problem(
                    503,
                    "database unavailable",
                    "인증 데이터베이스에 연결할 수 없습니다.",
                ),
                request,
            )
        return self._secure(response, request)

    def _client_is_local(self, client_ip: str) -> bool:
        """loopback 클라이언트, 또는 local_auto에서 명시 허용된 사설 대역(컨테이너 게이트웨이).

        컨테이너로 로컬 실행 시 앱이 보는 클라이언트 IP는 Docker 게이트웨이(사설)라
        loopback이 아니다. AUTH_LOCAL_AUTO_CLIENT_CIDRS로 그 대역을 명시 허용하되,
        컨테이너를 127.0.0.1에만 publish한다는 전제에서만 안전하다.
        """
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        if address.is_loopback:
            return True
        for cidr in self.settings.local_auto_client_cidrs:
            try:
                if address in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def _should_auto_login(
        self, request: Request, client_ip: str, page_key: str | None
    ) -> bool:
        if not self.settings.local_auto or request.method not in {"GET", "HEAD"}:
            return False
        if page_key is None and request.url.path not in {
            "/api/v1/auth/session",
            "/home",
        }:
            return False
        return self._client_is_local(client_ip)

    def _detection_exempt(self, client_ip: str) -> bool:
        """로컬 분석 모드의 loopback/허용 대역은 자동 차단 대상에서 제외한다(자기 잠금 방지)."""
        return self.settings.local_auto and self._client_is_local(client_ip)

    def _register_tamper(self, db, request: Request, client_ip: str) -> Response | None:
        """세션 조작 신호를 누적하고 임계 초과 시 자동 차단 응답을 반환한다."""
        if self._detection_exempt(client_ip):
            return None
        allowed, _ = self.tamper_limiter.check(f"ip:{client_ip}", TAMPER_LIMITS)
        if allowed:
            return None
        return self._trigger_auto_block(db, request, client_ip, "session_tampering")

    def _trigger_auto_block(
        self, db, request: Request, client_ip: str, reason_code: str
    ) -> Response:
        service = AutoBlockService(db)
        service.block_ip(client_ip, reason_code, ip_hash=request.state.ip_hash)
        user = getattr(request.state, "user", None)
        if user is not None:
            service.block_user(user, reason_code, ip_hash=request.state.ip_hash)
        # 다음 요청부터 IP 차단이 즉시 적용되도록 차단 목록 캐시를 비운다.
        self._ip_cache = (0.0, ())
        reason = AUTO_BLOCK_REASONS.get(reason_code, reason_code)
        return self._suspension_response(request, reason)

    def _blocked_ip_response(self, db, request: Request, client_ip: str) -> Response:
        row = AutoBlockService(db).active_ip_block(client_ip)
        # 자동 차단 사유는 표준 문구(AUTO_BLOCK_REASONS)뿐이라 노출해도 안전하다.
        # 수동 차단의 reason은 운영 내부 메모일 수 있으므로 당사자에게 노출하지 않는다.
        if row is not None and row.source == "auto" and row.reason:
            reason = row.reason
        else:
            reason = "차단된 네트워크"
        return self._suspension_response(request, reason)

    def _suspension_response(self, request: Request, reason: str) -> Response:
        message = (
            f"[{reason}]로 정지되었습니다. "
            "자세한 사항은 사이트 운영자에게 문의 바랍니다."
        )
        if self._wants_html(request):
            return HTMLResponse(
                "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>접근 정지 · ASK SEOUL</title></head>"
                "<body style=\"margin:0;display:grid;place-items:center;min-height:100vh;"
                "background:#0f1115;color:#e8eaed;font-family:system-ui,sans-serif\">"
                "<div style=\"max-width:28rem;padding:2rem;text-align:center\">"
                "<div style=\"font-size:2.2rem;margin-bottom:.8rem\">&#9940;</div>"
                f"<p style=\"line-height:1.7\">{escape(message)}</p>"
                "</div></body></html>",
                status_code=403,
            )
        return problem(403, "access blocked", message)

    def _client_ip(self, request: Request) -> str:
        direct = request.client.host if request.client else "0.0.0.0"
        if not self.settings.trusted_proxy_headers:
            return direct
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",", 1)[0].strip()
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            return direct

    def _blocked_ip(self, db, client_ip: str) -> bool:
        now_mono = time.monotonic()
        cached_at, networks = self._ip_cache
        if now_mono - cached_at > 5:
            now = utcnow()
            rows = db.scalars(
                select(IpBlock).where(
                    IpBlock.active.is_(True),
                    (IpBlock.expires_at.is_(None) | (IpBlock.expires_at > now)),
                )
            ).all()
            networks = tuple(row.network for row in rows)
            self._ip_cache = (now_mono, networks)
        return ip_in_networks(client_ip, networks)

    async def _rate_limit(self, db, request: Request, user: User | None, client_ip: str):
        config = AccessService(db).effective_policy(user, "request_limit")
        path = request.url.path
        category = rate_limit_category(path, authenticated=user is not None)
        limits = config.get(category, {})
        subject = f"user:{user.id}" if user else f"ip:{client_ip}"
        if category not in {"anonymous", "authenticated"}:
            subject = f"{subject}:endpoint:{category}"
        allowed, retry_after = self.rate_limiter.check(subject, limits)
        if allowed:
            return None
        action = config.get("action", "reject")
        if action == "slow_down":
            await asyncio.sleep(min(2, retry_after))
        if action == "drop":
            return Response(status_code=429, headers={"Retry-After": str(retry_after)})
        response = problem(429, "rate limit exceeded", "요청이 너무 많습니다. 잠시 후 다시 시도하세요.")
        response.headers["Retry-After"] = str(retry_after)
        return response

    def _csrf_check(self, request: Request) -> Response | None:
        site = request.headers.get("sec-fetch-site", "")
        if site in {"cross-site", "same-site"}:
            return problem(403, "cross-site request blocked", "교차 사이트 요청이 차단되었습니다.")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin or referer
        if source:
            source_url = urlsplit(source)
            target = urlsplit(self.settings.public_base_url)
            source_origin = (
                source_url.scheme,
                source_url.hostname,
                source_url.port or (443 if source_url.scheme == "https" else 80),
            )
            target_origin = (
                target.scheme,
                target.hostname,
                target.port or (443 if target.scheme == "https" else 80),
            )
            origin_ok = source_origin == target_origin
            if not origin_ok and self.settings.local_auto:
                # 로컬 자동 인증(development + loopback 전용)에서는 127.0.0.1과 localhost를
                # 함께 허용한다. 브라우저가 어느 loopback 이름으로 접속하든 charts POST가
                # origin mismatch로 막히지 않게 한다. scheme·port가 public_base_url과 같고
                # host가 loopback 전용 AUTH_ALLOWED_HOSTS에 있을 때만 통과시킨다.
                allowed_hosts = {host.casefold() for host in self.settings.allowed_hosts}
                origin_ok = (
                    source_origin[0] == target_origin[0]
                    and source_origin[2] == target_origin[2]
                    and (source_url.hostname or "").casefold() in allowed_hosts
                )
            if not origin_ok:
                return problem(403, "origin mismatch", "요청 출처가 올바르지 않습니다.")
        elif self.settings.production:
            return problem(403, "origin required", "요청 출처를 확인할 수 없습니다.")

        session = getattr(request.state, "auth_session", None)
        if session is None:
            return None
        cookie = request.cookies.get(self.settings.csrf_cookie_name, "")
        header = request.headers.get("x-csrf-token", "")
        if not cookie or not header or cookie != header:
            return problem(403, "csrf validation failed", "CSRF 토큰이 올바르지 않습니다.")
        if token_digest(cookie, self.settings.session_pepper) != session.csrf_hash:
            # 쿠키·헤더 쌍은 맞췄지만 세션과 무관한 값 — 위조 시도로 본다.
            request.state.csrf_forged = True
            return problem(403, "csrf validation failed", "CSRF 토큰이 올바르지 않습니다.")
        return None

    @staticmethod
    def _wants_html(request: Request) -> bool:
        return "text/html" in request.headers.get("accept", "")

    def _unauthenticated(
        self, request: Request, failure: dict | None = None
    ) -> Response:
        reason = (failure or {}).get("reason", "")
        expired_at = (failure or {}).get("expired_at") or ""
        if reason in {"expired", "idle_expired"}:
            # 세션 만료는 단순 미로그인과 구분해 끊긴 시각을 안내한다.
            if self._wants_html(request):
                url = f"/auth/login?next={request.url.path}&expired=1"
                if expired_at:
                    url += f"&at={quote(expired_at)}"
                return RedirectResponse(url, status_code=303)
            return JSONResponse(
                status_code=401,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "session expired",
                    "status": 401,
                    "detail": "세션이 만료되어 접속이 끊겼습니다. 다시 로그인해 주세요.",
                    "expired_at": expired_at,
                },
            )
        if self._wants_html(request):
            next_path = request.url.path
            return RedirectResponse(f"/auth/login?next={next_path}", status_code=303)
        return problem(401, "authentication required", "로그인이 필요합니다.")

    def _forbidden(self, request: Request) -> Response:
        if self._wants_html(request):
            return RedirectResponse("/profile?denied=1", status_code=303)
        return problem(403, "forbidden", "이 영역에 접근할 권한이 없습니다.")

    def _secure(self, response: Response, request: Request) -> Response:
        local_credentials = getattr(
            request.state, "local_session_credentials", None
        )
        if local_credentials:
            token, csrf = local_credentials
            response.set_cookie(
                self.settings.cookie_name,
                token,
                path="/",
                secure=self.settings.cookie_secure,
                httponly=True,
                samesite="lax",
            )
            response.set_cookie(
                self.settings.csrf_cookie_name,
                csrf,
                path="/",
                secure=self.settings.cookie_secure,
                httponly=False,
                samesite="lax",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        inline_hashes = (
            self.inline_script_hashes_by_path.get(request.url.path, ())
            if response.headers.get("content-type", "").startswith("text/html")
            else ()
        )
        script_sources = [
            "'self'",
            *(f"'sha256-{value}'" for value in inline_hashes),
            "https://cdn.jsdelivr.net",
        ]
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src {' '.join(script_sources)}; "
            "script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self' https://cdn.jsdelivr.net; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Origin-Agent-Cluster", "?1")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Vary", "Origin, Sec-Fetch-Site")
        path = request.url.path
        if path.startswith("/auth"):
            response.headers["Referrer-Policy"] = "no-referrer"
        protected_page = page_key_for_path(path) is not None
        private_api = (
            path.startswith("/api/v1/")
            and not path.startswith("/api/v1/public/")
        )
        if path.startswith("/auth") or protected_page or private_api:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if self.settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def page_key_for_path(path: str) -> str | None:
    exact = {
        "/catalog": "catalog",
        "/static/index.html": "catalog",
        "/charts": "charts",
        "/static/charts/index.html": "charts",
        "/profile": "profile",
        "/static/auth/profile.html": "profile",
        "/admin": "admin_users",
        "/static/auth/admin.html": "admin_users",
        "/docs": "api_docs",
        "/redoc": "api_docs",
        "/openapi.json": "api_docs",
    }
    if path in exact:
        return exact[path]
    if path.startswith("/docs/") or path.startswith("/redoc/"):
        return "api_docs"
    prefixes = (
        ("/api/v1/catalog", "catalog"),
        ("/api/v1/charts", "charts"),
        ("/api/v1/me", "profile"),
        ("/api/v1/billing", "billing"),
        ("/api/v1/admin/users", "admin_users"),
        ("/api/v1/admin/access", "admin_access"),
        ("/api/v1/admin/policies", "admin_policies"),
        ("/api/v1/admin/ip-blocks", "admin_policies"),
        ("/api/v1/admin/payments", "admin_payments"),
        ("/api/v1/admin/audit", "admin_audit"),
        ("/api/v1/admin/auto-blocks", "admin_security"),
    )
    for prefix, key in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return key
    return None

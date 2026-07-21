"""전역 세션·인가·CSRF·요청 제한·보안 헤더 미들웨어."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
import time
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from .models import IpBlock, User, utcnow
from .security import ip_in_networks, stable_digest, token_digest
from .service import AccessService, AuthService


logger = logging.getLogger(__name__)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
WINDOWS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}


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
                    return self._secure(problem(403, "request blocked", "차단된 네트워크입니다."), request)

                raw_session = request.cookies.get(settings.cookie_name)
                auth = AuthService(db, settings)
                current = auth.current_session(
                    raw_session,
                    ip_hash=ip_hash,
                    user_agent_hash=ua_hash,
                )
                if current:
                    auth_session, user = current
                    request.state.auth_session = auth_session
                    request.state.user = user

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
                        return self._secure(csrf_response, request)

                if page_key:
                    if request.state.user is None:
                        return self._secure(self._unauthenticated(request), request)
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
                            )
                        )
                    if not allowed:
                        return self._secure(self._forbidden(request), request)

            response = await call_next(request)
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

    def _should_auto_login(
        self, request: Request, client_ip: str, page_key: str | None
    ) -> bool:
        if not self.settings.local_auto or request.method not in {"GET", "HEAD"}:
            return False
        if page_key is None and request.url.path != "/api/v1/auth/session":
            return False
        try:
            return ipaddress.ip_address(client_ip).is_loopback
        except ValueError:
            return False

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
            if (
                source_url.scheme,
                source_url.hostname,
                source_url.port or (443 if source_url.scheme == "https" else 80),
            ) != (
                target.scheme,
                target.hostname,
                target.port or (443 if target.scheme == "https" else 80),
            ):
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
            return problem(403, "csrf validation failed", "CSRF 토큰이 올바르지 않습니다.")
        return None

    @staticmethod
    def _wants_html(request: Request) -> bool:
        return "text/html" in request.headers.get("accept", "")

    def _unauthenticated(self, request: Request) -> Response:
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
    )
    for prefix, key in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return key
    return None

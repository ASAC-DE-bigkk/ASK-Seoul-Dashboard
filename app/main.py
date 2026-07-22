"""ASK SEOUL 카탈로그·Charts Studio·독립 인증 시스템.

실행:  .venv/Scripts/uvicorn app.main:app --port 8765
문서:  http://127.0.0.1:8765/docs (Swagger, 자동 생성)
화면:  http://127.0.0.1:8765/
"""
from __future__ import annotations

import json
import base64
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import router as auth_router
from .auth.config import load_settings
from .auth.database import Database
from .auth.emailer import EmailSender
from .auth.middleware import AuthSecurityMiddleware, RequestBodyLimitMiddleware
from .auth.service import (
    DomainError,
    prepare_database_for_app,
    verify_database_schema,
)
from .charts import router as charts_router
from .models import (
    CatalogResponse, CatalogSnapshotResponse, QualityResponse, SampleResponse,
    SchemaResponse, TableDetail, TableSummary,
)

HERE = Path(__file__).parent
logger = logging.getLogger(__name__)
SNAPSHOT_PATH = HERE.parent / "snapshot" / "catalog_snapshot.json"
SWAGGER_VERSION = "5.32.8"
SWAGGER_INIT_SCRIPT = """\
window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayRequestDuration: true,
  supportedSubmitMethods: [],
  presets: [SwaggerUIBundle.presets.apis],
  layout: "BaseLayout"
});"""

_snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
_by_name = {t["name"]: t for t in _snapshot["tables"]}
_auth_settings = load_settings()
_database = Database(
    _auth_settings.database_url,
    strict_file_permissions=_auth_settings.production,
)


def _inline_script_hash_map() -> dict[str, tuple[str, ...]]:
    """요청 경로별 HTML inline script만 허용하는 CSP SHA-256 목록."""
    pattern = re.compile(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    static_root = HERE / "static"

    def hashes_for(path: Path) -> tuple[str, ...]:
        hashes: set[str] = set()
        for content in pattern.findall(path.read_text(encoding="utf-8")):
            digest = hashlib.sha256(content.encode("utf-8")).digest()
            hashes.add(base64.b64encode(digest).decode("ascii"))
        return tuple(sorted(hashes))

    result = {
        f"/static/{path.relative_to(static_root).as_posix()}": hashes_for(path)
        for path in static_root.rglob("*.html")
    }
    route_files = {
        "/": "landing.html",
        "/catalog": "index.html",
        "/charts": "charts/index.html",
        "/auth/login": "auth/login.html",
        "/auth/register": "auth/register.html",
        "/auth/resend-verification": "auth/resend-verification.html",
        "/auth/forgot-password": "auth/forgot-password.html",
        "/auth/verify-email": "auth/verify-email.html",
        "/auth/mfa": "auth/mfa.html",
        "/auth/reset-password": "auth/reset-password.html",
        "/legal/terms": "legal/terms.html",
        "/legal/privacy": "legal/privacy.html",
        "/profile": "auth/profile.html",
        "/admin": "auth/admin.html",
    }
    result.update(
        {
            route: result[f"/static/{relative}"]
            for route, relative in route_files.items()
        }
    )
    docs_digest = hashlib.sha256(SWAGGER_INIT_SCRIPT.encode("utf-8")).digest()
    result["/docs"] = (base64.b64encode(docs_digest).decode("ascii"),)
    return result


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 선택 기능도 부분 설정/오타를 묵인하지 않고 기동 단계에서 검증한다.
    EmailSender()
    prepare_database_for_app(_database, _auth_settings)
    yield


app = FastAPI(
    title="ASK SEOUL — Data Platform API",
    description="인증·인가가 적용된 데이터 카탈로그와 Charts Studio API.",
    version="0.3.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.state.auth_settings = _auth_settings
app.state.database = _database

# 마지막에 추가된 middleware가 바깥쪽에서 실행된다. Host 검증을 가장 먼저 적용한다.
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=_auth_settings.max_request_bytes,
)
app.add_middleware(
    AuthSecurityMiddleware,
    settings=_auth_settings,
    inline_script_hashes_by_path=_inline_script_hash_map(),
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(_auth_settings.allowed_hosts),
    www_redirect=False,
)


def _summary(t: dict) -> dict:
    return {
        **{k: t[k] for k in ("name", "relation", "description", "tags",
                             "contract_enforced", "materialized", "row_count", "date_range")},
        "domain": t.get("domain", "culture"),
        "external": t.get("external", True),
        "column_count": len(t["columns"]),
        "quality_source_count": len(t["quality"]),
        "serving_tier": t.get("serving_tier"),
        "refresh": t.get("refresh"),
        "tests": t.get("tests", []),
        "served_url": t.get("served_url"),
    }


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    """RFC 7807 Problem Details — 팀 에러 규약(problem_failure_callback)과 같은 사상."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    return _problem(exc.status, exc.title, exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        ".".join(str(part) for part in item["loc"] if part not in {"body", "query"})
        for item in exc.errors()
    ]
    detail = "요청 형식이 올바르지 않습니다."
    if fields:
        detail += " 확인할 항목: " + ", ".join(sorted(set(fields)))
    return _problem(422, "validation failed", detail)


def _get_or_404(name: str) -> dict | JSONResponse:
    t = _by_name.get(name)
    if t is None:
        return _problem(404, "table not found",
                        f"'{name}' 은 카탈로그에 없습니다. GET /api/v1/catalog/tables 로 목록을 확인하세요.")
    return t


@app.get("/health", response_model=None)
def health(request: Request) -> dict | JSONResponse:
    try:
        verify_database_schema(request.app.state.database, deep=False)
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.error(
            "auth database readiness failed path=/health error_type=%s",
            type(exc).__name__,
        )
        return _problem(
            503,
            "database unavailable",
            "인증 데이터베이스에 연결할 수 없습니다.",
        )
    return {
        "status": "ok",
        "database": "ok",
        "generated_at": _snapshot["generated_at"],
        "table_count": _snapshot["table_count"],
        "auth": "enabled",
    }


@app.get("/docs", include_in_schema=False)
def api_docs() -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ASK SEOUL API 문서</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_VERSION}/swagger-ui.css"
        integrity="sha384-9Q2fpS+xeS4ffJy6CagnwoUl+4ldAYhOs9pgZuEKxypVModhmZFzeMlvVsAjf7uT"
        crossorigin="anonymous">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_VERSION}/swagger-ui-bundle.js"
          integrity="sha384-IKpAWwsTL0pcw7/Amtnt2eXF4P1BK64WNuY2E/RG15SWLUW5HXzFuyqCSAr/DP8C"
          crossorigin="anonymous"></script>
  <script>{SWAGGER_INIT_SCRIPT}</script>
</body>
</html>"""
    )


@app.get("/api/v1/public/summary", summary="랜딩 페이지용 공개 집계")
def public_summary() -> dict:
    tables = [_summary(t) for t in _snapshot["tables"]]
    return {
        "generated_at": _snapshot["generated_at"],
        "dataset_count": len(tables),
        "domain_count": len({t["domain"] for t in tables}),
        "total_rows": sum(t["row_count"] for t in tables),
        "contract_count": sum(1 for t in tables if t["contract_enforced"]),
    }


@app.get("/api/v1/catalog/tables", response_model=CatalogResponse,
         summary="published 테이블 목록 (카드용 요약)")
def list_tables(external: bool | None = None) -> dict:
    """external=true 면 외부 공개 대상만(#269). 내부 마트(SLO 등)는 external=false 로 제외된다."""
    rows = _snapshot["tables"]
    if external is not None:
        rows = [t for t in rows if t.get("external", True) is external]
    return {
        "generated_at": _snapshot["generated_at"],
        "domain": _snapshot["domain"],
        "domains": _snapshot.get("domains", {}),
        "table_count": len(rows),
        "tables": [_summary(t) for t in rows],
    }


@app.get(
    "/api/v1/catalog/snapshot",
    response_model=CatalogSnapshotResponse,
    summary="마켓플레이스 초기 렌더용 전체 snapshot",
)
def catalog_snapshot() -> dict:
    return {
        "generated_at": _snapshot["generated_at"],
        "domain": _snapshot["domain"],
        "domains": _snapshot.get("domains", {}),
        "table_count": _snapshot["table_count"],
        "tables": [
            {
                **_summary(table),
                "columns": table["columns"],
                "quality": table["quality"],
                "lineage": table["lineage"],
                "sample": table["sample"],
            }
            for table in _snapshot["tables"]
        ],
    }


@app.get("/api/v1/catalog/tables/{name}", response_model=TableDetail,
         responses={404: {"description": "unknown table"}},
         summary="테이블 상세 (스키마·품질·계보·샘플 전부)")
def table_detail(name: str):
    t = _get_or_404(name)
    if isinstance(t, JSONResponse):
        return t
    return {**_summary(t), "columns": t["columns"], "quality": t["quality"],
            "lineage": t["lineage"], "sample": t["sample"]}


@app.get("/api/v1/catalog/tables/{name}/schema", response_model=SchemaResponse,
         summary="스키마만 (컬럼·타입·설명)")
def table_schema(name: str):
    t = _get_or_404(name)
    if isinstance(t, JSONResponse):
        return t
    return {"name": t["name"], "columns": t["columns"]}


@app.get("/api/v1/catalog/tables/{name}/quality", response_model=QualityResponse,
         summary="품질 — 상류 silver quality_status 분포")
def table_quality(name: str):
    t = _get_or_404(name)
    if isinstance(t, JSONResponse):
        return t
    return {"name": t["name"], "quality": t["quality"], "tests": t.get("tests", [])}


@app.get("/api/v1/catalog/tables/{name}/sample", response_model=SampleResponse,
         summary="샘플 5행")
def table_sample(name: str):
    t = _get_or_404(name)
    if isinstance(t, JSONResponse):
        return t
    return {"name": t["name"], "rows": t["sample"]}


@app.get("/", include_in_schema=False)
def landing() -> FileResponse:
    return FileResponse(HERE / "static" / "landing.html")


@app.get("/catalog", include_in_schema=False)
def catalog_page() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.get("/charts", include_in_schema=False)
def charts_page() -> FileResponse:
    return FileResponse(HERE / "static" / "charts" / "index.html")


@app.get("/auth/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "login.html")


@app.get("/auth/register", include_in_schema=False)
def register_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "register.html")


@app.get("/auth/resend-verification", include_in_schema=False)
def resend_verification_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "resend-verification.html")


@app.get("/auth/forgot-password", include_in_schema=False)
def forgot_password_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "forgot-password.html")


@app.get("/auth/verify-email", include_in_schema=False)
def verify_email_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "verify-email.html")


@app.get("/auth/mfa", include_in_schema=False)
def mfa_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "mfa.html")


@app.get("/auth/reset-password", include_in_schema=False)
def reset_password_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "reset-password.html")


@app.get("/legal/terms", include_in_schema=False)
def terms_page() -> FileResponse:
    return FileResponse(HERE / "static" / "legal" / "terms.html")


@app.get("/legal/privacy", include_in_schema=False)
def privacy_page() -> FileResponse:
    return FileResponse(HERE / "static" / "legal" / "privacy.html")


@app.get("/profile", include_in_schema=False)
def profile_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "profile.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "admin.html")


app.include_router(auth_router)
app.include_router(charts_router)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

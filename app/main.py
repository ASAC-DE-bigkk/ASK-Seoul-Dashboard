"""ASK SEOUL 카탈로그·Charts Studio·독립 인증 시스템.

실행:  .venv/Scripts/uvicorn app.main:app --port 8765
문서:  http://127.0.0.1:8765/docs (Swagger, 자동 생성)
화면:  http://127.0.0.1:8765/
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import router as auth_router
from .auth.config import load_settings
from .auth.database import Database
from .auth.middleware import AuthSecurityMiddleware
from .auth.service import DomainError, initialize_database
from .charts import router as charts_router
from .models import (
    CatalogResponse, QualityResponse, SampleResponse, SchemaResponse,
    TableDetail, TableSummary,
)

HERE = Path(__file__).parent
SNAPSHOT_PATH = HERE.parent / "snapshot" / "catalog_snapshot.json"

_snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
_by_name = {t["name"]: t for t in _snapshot["tables"]}
_auth_settings = load_settings()
_database = Database(_auth_settings.database_url)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database(_database, _auth_settings)
    yield


app = FastAPI(
    title="ASK SEOUL — Data Platform API",
    description="인증·인가가 적용된 데이터 카탈로그와 Charts Studio API.",
    version="0.3.0",
    lifespan=lifespan,
)
app.state.auth_settings = _auth_settings
app.state.database = _database

# 마지막에 추가된 middleware가 바깥쪽에서 실행된다. Host 검증을 가장 먼저 적용한다.
app.add_middleware(AuthSecurityMiddleware, settings=_auth_settings)
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
        "column_count": len(t["columns"]),
        "quality_source_count": len(t["quality"]),
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


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "generated_at": _snapshot["generated_at"],
        "table_count": _snapshot["table_count"],
        "auth": "enabled",
    }


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
def list_tables() -> dict:
    return {
        "generated_at": _snapshot["generated_at"],
        "domain": _snapshot["domain"],
        "domains": _snapshot.get("domains", {}),
        "table_count": _snapshot["table_count"],
        "tables": [_summary(t) for t in _snapshot["tables"]],
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
    return {"name": t["name"], "quality": t["quality"]}


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


@app.get("/auth/forgot-password", include_in_schema=False)
def forgot_password_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "forgot-password.html")


@app.get("/auth/reset-password", include_in_schema=False)
def reset_password_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "reset-password.html")


@app.get("/profile", include_in_schema=False)
def profile_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "profile.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(HERE / "static" / "auth" / "admin.html")


app.include_router(auth_router)
app.include_router(charts_router)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

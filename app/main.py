"""culture 데이터 카탈로그 API — 스냅샷을 서빙만 하는 얇은 조회 창구.

실행:  .venv/Scripts/uvicorn app.main:app --port 8765
문서:  http://127.0.0.1:8765/docs (Swagger, 자동 생성)
화면:  http://127.0.0.1:8765/
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .charts import router as charts_router
from .models import (
    CatalogResponse, QualityResponse, SampleResponse, SchemaResponse,
    TableDetail, TableSummary,
)

HERE = Path(__file__).parent
SNAPSHOT_PATH = HERE.parent / "snapshot" / "catalog_snapshot.json"

app = FastAPI(
    title="ASK SEOUL — Data Catalog API (demo)",
    description="dbt manifest/catalog + Trino 실측 스냅샷을 서빙하는 조회 전용 카탈로그 API. "
                "W3 '품질·카탈로그 API화'의 축소판 — culture는 rich(계약·계보·품질), "
                "그 외 도메인은 Trino 실측 basic 메타.",
    version="0.2.0",
)

_snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
_by_name = {t["name"]: t for t in _snapshot["tables"]}


def _summary(t: dict) -> dict:
    return {
        **{k: t[k] for k in ("name", "relation", "description", "tags",
                             "contract_enforced", "materialized", "row_count", "date_range")},
        "domain": t.get("domain", "culture"),
        "external": t.get("external", True),
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


def _get_or_404(name: str) -> dict | JSONResponse:
    t = _by_name.get(name)
    if t is None:
        return _problem(404, "table not found",
                        f"'{name}' 은 카탈로그에 없습니다. GET /api/v1/catalog/tables 로 목록을 확인하세요.")
    return t


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "generated_at": _snapshot["generated_at"],
            "table_count": _snapshot["table_count"]}


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


app.include_router(charts_router)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

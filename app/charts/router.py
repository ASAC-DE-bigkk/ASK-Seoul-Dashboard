"""차트 스튜디오 API — /api/v1/charts/…  (조회 + 레이아웃 CRUD).

main.py 는 이 router 를 include 만 한다. 에러는 본체와 같은 RFC 7807 problem+json.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import layouts, querybuilder, trino
from .models import (
    PageCreate, PageDetail, PagePatch, PageSummary,
    QueryRequest, QueryResponse, ReorderRequest, SourceDetail, SourcesResponse,
)
from .ontology import registry

router = APIRouter(prefix="/api/v1/charts", tags=["charts"])


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


@router.get("/meta", summary="온톨로지 메타 — 도표 타입(슬롯 계약)·값 라벨·도메인")
def charts_meta() -> dict:
    return registry.meta()


@router.get("/sources", response_model=SourcesResponse,
            summary="차트 소스 목록 (gold 테이블 + role 요약)")
def list_sources(domain: str = "all") -> dict:
    sources = registry.sources(domain)
    return {
        "generated_at": registry.generated_at,
        "source_count": len(sources),
        "sources": sources,
    }


@router.get("/sources/{name}", response_model=SourceDetail,
            responses={404: {"description": "unknown source"}},
            summary="소스 상세 — 필드·role·기본 도표 힌트")
def source_detail(name: str):
    s = registry.get(name)
    if s is None:
        return _problem(404, "source not found",
                        f"'{name}' 은 차트 소스에 없습니다. GET /api/v1/charts/sources 로 확인하세요.")
    return s


@router.post("/query", response_model=QueryResponse,
             responses={400: {"description": "spec 오류"},
                        502: {"description": "질의 실패"},
                        503: {"description": "Trino 접속 불가(캐시도 없음)"}},
             summary="온톨로지 스펙 → gold 집계 질의")
def run_query(req: QueryRequest):
    source = registry.get(req.source)
    if source is None:
        return _problem(404, "source not found", f"'{req.source}' 은 차트 소스에 없습니다.")
    try:
        sql = querybuilder.build(source, req.model_dump())
    except querybuilder.SpecError as exc:
        return _problem(400, "invalid query spec", str(exc))
    try:
        result = trino.execute(sql, force=req.force)
    except trino.QueryFailed as exc:
        return _problem(502, "query failed", str(exc))
    except trino.TrinoUnavailable as exc:
        return _problem(503, "trino unavailable",
                        f"Trino 접속 불가이고 캐시도 없습니다 — 스택 기동 후 재시도하세요. ({exc})")
    return {
        "source": req.source,
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": len(result["rows"]),
        "mode": result["mode"],
        "sql": sql,
        "elapsed_ms": result.get("elapsed_ms", 0),
        "cached_at": result.get("cached_at"),
    }


# ── 레이아웃 페이지 CRUD ─────────────────────────────────────

@router.get("/layouts", response_model=list[PageSummary], summary="레이아웃 페이지 목록(순서 보존)")
def list_layout_pages():
    return layouts.list_pages()


@router.post("/layouts", response_model=PageDetail, summary="레이아웃 페이지 추가")
def create_layout_page(req: PageCreate):
    return layouts.create_page(req.name)


@router.get("/layouts/{page_id}", response_model=PageDetail,
            responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 상세")
def get_layout_page(page_id: str):
    try:
        return layouts.get_page(page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.patch("/layouts/{page_id}", response_model=PageDetail,
              responses={404: {"description": "unknown page"}},
              summary="레이아웃 저장 — 이름 변경/차트·배치 반영")
def patch_layout_page(page_id: str, req: PagePatch):
    try:
        charts = None
        if req.charts is not None:
            charts = [c.model_dump() for c in req.charts]
        return layouts.update_page(page_id, name=req.name, charts=charts)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.delete("/layouts/{page_id}", status_code=204,
               responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 삭제")
def delete_layout_page(page_id: str):
    try:
        layouts.delete_page(page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.post("/layouts/{page_id}/duplicate", response_model=PageDetail,
             responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 복제")
def duplicate_layout_page(page_id: str):
    try:
        return layouts.duplicate_page(page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.post("/layouts-reorder", response_model=list[PageSummary],
             responses={400: {"description": "id 목록 불일치"}},
             summary="레이아웃 페이지 순서 변경")
def reorder_layout_pages(req: ReorderRequest):
    try:
        return layouts.reorder(req.ids)
    except layouts.NotFound as exc:
        return _problem(400, "reorder mismatch", str(exc))

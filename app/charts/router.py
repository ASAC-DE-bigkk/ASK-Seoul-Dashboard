"""차트 스튜디오 API — /api/v1/charts/…  (조회 + 레이아웃 CRUD).

main.py 는 이 router 를 include 만 한다. 에러는 본체와 같은 RFC 7807 problem+json.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, get_db, require_member
from app.auth.models import User
from app.auth.service import AccessService
from . import layouts, querybuilder, trino
from .models import (
    PageCreate, PageDetail, PagePatch, PageSummary,
    QueryRequest, QueryResponse, ReorderRequest, SourceAvailability,
    SourceDetail, SourcesResponse,
)
from .ontology import CHART_TYPES, field_matches_slot, registry

router = APIRouter(prefix="/api/v1/charts", tags=["charts"])


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _ontology(db: Session, user: User) -> dict:
    return AccessService(db).effective_ontology(user)


def _personalize_source(source: dict, ontology: dict) -> dict:
    data = {**source}
    hidden = set(ontology.get("hidden_chart_types", []))
    data["supports"] = [item for item in source.get("supports", []) if item not in hidden]
    labels = ontology.get("source_label_overrides", {})
    if source["name"] in labels:
        data["label"] = str(labels[source["name"]])[:80]
    return data


def _validate_charts(charts) -> None:
    ids = [chart.id for chart in charts]
    if len(ids) != len(set(ids)):
        raise querybuilder.SpecError("한 레이아웃 안에서 차트 ID는 중복될 수 없습니다")
    for chart in charts:
        source = registry.get(chart.source)
        if source is None:
            raise querybuilder.SpecError(f"알 수 없는 차트 소스입니다: {chart.source}")
        chart_type = CHART_TYPES.get(chart.type)
        if chart_type is None or chart.type not in source.get("supports", []):
            raise querybuilder.SpecError(
                f"{chart.source} 소스에서 지원하지 않는 차트 타입입니다: {chart.type}"
            )
        fields = {field["name"]: field for field in source["fields"]}
        slots = {slot["name"]: slot for slot in chart_type["slots"]}
        unknown_slots = set(chart.bindings) - set(slots)
        if unknown_slots:
            raise querybuilder.SpecError(
                f"알 수 없는 바인딩 슬롯입니다: {', '.join(sorted(unknown_slots))}"
            )
        # 구간(bins) 계약 — binnable 슬롯 + 숫자 측정값 + 유한한 양수 폭에서만 성립
        unknown_bins = set(chart.bins) - set(slots)
        if unknown_bins:
            raise querybuilder.SpecError(
                f"알 수 없는 구간 슬롯입니다: {', '.join(sorted(unknown_bins))}"
            )
        for bin_slot, width in chart.bins.items():
            if not slots[bin_slot].get("binnable"):
                raise querybuilder.SpecError(f"{bin_slot} 슬롯은 구간(bin)을 지원하지 않습니다")
            if not chart.bindings.get(bin_slot):
                raise querybuilder.SpecError(f"구간 슬롯 {bin_slot}에 바인딩된 필드가 없습니다")
            querybuilder._bin_width(width)
        used_bindings: dict[str, str] = {}
        for slot_name, slot in slots.items():
            field_name = chart.bindings.get(slot_name)
            count_optional = chart.agg == "count" and slot.get("count_optional")
            if slot.get("required") and not count_optional and not field_name:
                raise querybuilder.SpecError(f"{chart.type}의 {slot_name} 바인딩이 필요합니다")
            if not field_name:
                continue
            field = fields.get(field_name)
            if field is None:
                raise querybuilder.SpecError(
                    f"{chart.source}에 없는 바인딩 필드입니다: {field_name}"
                )
            if not field.get("chartable", True):
                raise querybuilder.SpecError(
                    f"{field_name} 필드는 분석 축/값으로 안전하지 않아 차트 슬롯에 사용할 수 없습니다"
                )
            if slot_name in chart.bins:
                if field["role"] != "measure":
                    raise querybuilder.SpecError(
                        f"구간(bin)은 숫자 측정값에만 적용됩니다: {field_name}"
                    )
            elif not field_matches_slot(field, slot):
                if field["role"] == "measure" and slot.get("binnable"):
                    raise querybuilder.SpecError(
                        f"{field_name}(숫자)을 {slot_name} 축으로 쓰려면 구간 폭(bins.{slot_name})이 필요합니다"
                    )
                raise querybuilder.SpecError(
                    f"{field_name} 필드는 {slot_name} 슬롯에 사용할 수 없습니다"
                )
            previous = used_bindings.get(field_name)
            if previous:
                raise querybuilder.SpecError(
                    f"{previous}와 {slot_name} 슬롯은 서로 다른 필드를 사용해야 합니다"
                )
            used_bindings[field_name] = slot_name
        if chart.agg not in querybuilder.AGGS:
            raise querybuilder.SpecError(f"허용되지 않는 집계입니다: {chart.agg}")
        if chart.agg not in chart_type.get("aggs", querybuilder.AGGS):
            raise querybuilder.SpecError(
                f"{chart.type} 차트에는 {chart.agg} 집계를 사용할 수 없습니다"
            )
        option_contract = chart_type.get("options", {})
        unknown_options = set(chart.options) - set(option_contract)
        if unknown_options:
            raise querybuilder.SpecError(
                f"알 수 없는 차트 옵션입니다: {', '.join(sorted(unknown_options))}"
            )
        for key, value in chart.options.items():
            default = option_contract[key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise querybuilder.SpecError(f"{key} 옵션은 boolean이어야 합니다")
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
            ):
                raise querybuilder.SpecError(f"{key} 옵션은 유한한 숫자이며 정수여야 합니다")
            elif key == "interval_ms" and not 200 <= value <= 5_000:
                raise querybuilder.SpecError("interval_ms 옵션은 200~5000이어야 합니다")
            elif key == "top_n" and not 1 <= value <= 5_000:
                raise querybuilder.SpecError("top_n 옵션은 1~5000이어야 합니다")
        effective_options = {**option_contract, **chart.options}
        for constraint in chart_type.get("agg_constraints", []):
            when = constraint.get("when", {})
            if all(effective_options.get(key) == value for key, value in when.items()):
                if chart.agg not in constraint.get("allowed", []):
                    raise querybuilder.SpecError(
                        f"현재 {chart.type} 옵션에는 {chart.agg} 집계를 사용할 수 없습니다"
                    )
        if chart.type == "race" and effective_options.get("cumulative"):
            value_name = chart.bindings.get("value")
            value_field = fields.get(value_name) if value_name else None
            if chart.agg == "count" or not (
                value_field and value_field.get("cumulative_safe", False)
            ):
                raise querybuilder.SpecError(
                    "이 측정값은 시간 누적 시 중복 합산될 수 있어 누적 경주를 사용할 수 없습니다"
                )
        for field_name, bound_slot in used_bindings.items():
            field = fields[field_name]
            if field["role"] != "measure":
                continue
            # 집계 제약은 '집계되는 슬롯'(value/x/y 처럼 measure 를 받는 슬롯)에만 적용 —
            # groupable/구간 축으로 바인딩된 measure 는 그룹 키라 집계되지 않는다.
            if "measure" not in slots[bound_slot]["accepts"]:
                continue
            allowed = field.get("allowed_aggs")
            if allowed is not None and chart.agg not in allowed:
                raise querybuilder.SpecError(
                    f"{field_name} 필드에는 {chart.agg} 집계를 사용할 수 없습니다"
                )
        for item in chart.filters:
            if item.field not in fields or item.op not in querybuilder.OPS:
                raise querybuilder.SpecError("차트 필터 필드 또는 연산자가 올바르지 않습니다")
            # 레이아웃 저장 시에도 query 단계와 같은 값 형식 검사를 수행한다.
            querybuilder.validate_filter(item.model_dump(), fields)
        if set(chart.grid) - {"x", "y", "w", "h"}:
            raise querybuilder.SpecError("grid에는 x, y, w, h만 사용할 수 있습니다")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > 1000
            for value in chart.grid.values()
        ):
            raise querybuilder.SpecError("grid 값은 0~1000 정수여야 합니다")
        if chart.grid.get("w", 1) < 1 or chart.grid.get("h", 1) < 1:
            raise querybuilder.SpecError("grid의 w와 h는 1 이상이어야 합니다")


@router.get("/meta", summary="온톨로지 메타 — 사용자 설정이 반영된 도표 타입·값 라벨·도메인")
def charts_meta(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    return registry.meta(_ontology(db, user))


@router.get("/sources", response_model=SourcesResponse,
            summary="차트 소스 목록 (gold 테이블 + role 요약)")
def list_sources(
    domain: str = "all",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    ontology = _ontology(db, user)
    sources = [_personalize_source(source, ontology) for source in registry.sources(domain)]
    return {
        "generated_at": registry.generated_at,
        "source_count": len(sources),
        "sources": sources,
    }


@router.get("/sources/{name}", response_model=SourceDetail,
            responses={404: {"description": "unknown source"}},
            summary="소스 상세 — 필드·role·기본 도표 힌트")
def source_detail(
    name: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    s = registry.get(name)
    if s is None:
        return _problem(404, "source not found",
                        f"'{name}' 은 차트 소스에 없습니다. GET /api/v1/charts/sources 로 확인하세요.")
    return _personalize_source(s, _ontology(db, user))


@router.get(
    "/sources/{name}/availability",
    response_model=SourceAvailability,
    responses={404: {"description": "unknown source"},
               502: {"description": "availability 질의 실패"},
               503: {"description": "Trino 접속 불가"}},
    summary="실데이터 필드 가용성 — null이 아닌 값 개수",
)
def source_availability(
    name: str,
    _user: User = Depends(current_user),
):
    source = registry.get(name)
    if source is None:
        return _problem(404, "source not found", f"'{name}' 은 차트 소스에 없습니다.")
    aliases = {
        field["name"]: f"field_{index}"
        for index, field in enumerate(source["fields"])
    }
    try:
        sql = querybuilder.build(
            source,
            {
                "measures": [
                    {"field": field_name, "agg": "count", "alias": alias}
                    for field_name, alias in aliases.items()
                ],
                "limit": 1,
            },
        )
        result = trino.execute(sql, max_rows=1)
    except querybuilder.SpecError as exc:
        return _problem(400, "invalid availability spec", str(exc))
    except trino.QueryFailed as exc:
        return _problem(502, "availability query failed", str(exc))
    except trino.TrinoUnavailable as exc:
        return _problem(503, "trino unavailable", str(exc))
    row = result["rows"][0] if result["rows"] else []
    positions = {column: index for index, column in enumerate(result["columns"])}
    return {
        "source": name,
        "fields": {
            field_name: int(row[positions[alias]] or 0)
            if alias in positions and positions[alias] < len(row)
            else 0
            for field_name, alias in aliases.items()
        },
        "mode": result["mode"],
        "elapsed_ms": result.get("elapsed_ms", 0),
    }


@router.post("/query", response_model=QueryResponse,
             responses={400: {"description": "spec 오류"},
                        502: {"description": "질의 실패"},
                        503: {"description": "Trino 접속 불가(캐시도 없음)"}},
             summary="온톨로지 스펙 → gold 집계 질의")
def run_query(
    req: QueryRequest,
    _user: User = Depends(current_user),
):
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
def list_layout_pages(
    user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return layouts.list_pages(db, user.id)


@router.post("/layouts", response_model=PageDetail, summary="레이아웃 페이지 추가")
def create_layout_page(
    req: PageCreate,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    try:
        return layouts.create_page(db, user.id, req.name)
    except layouts.LimitExceeded as exc:
        return _problem(409, "layout limit exceeded", str(exc))


@router.get("/layouts/{page_id}", response_model=PageDetail,
            responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 상세")
def get_layout_page(
    page_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        return layouts.get_page(db, user.id, page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.patch("/layouts/{page_id}", response_model=PageDetail,
              responses={404: {"description": "unknown page"}},
              summary="레이아웃 저장 — 이름 변경/차트·배치 반영")
def patch_layout_page(
    page_id: str,
    req: PagePatch,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    try:
        charts = None
        if req.charts is not None:
            _validate_charts(req.charts)
            charts = [c.model_dump() for c in req.charts]
        return layouts.update_page(db, user.id, page_id, name=req.name, charts=charts)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")
    except querybuilder.SpecError as exc:
        return _problem(400, "invalid layout", str(exc))


@router.delete("/layouts/{page_id}", status_code=204,
               responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 삭제")
def delete_layout_page(
    page_id: str,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    try:
        layouts.delete_page(db, user.id, page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")


@router.post("/layouts/{page_id}/duplicate", response_model=PageDetail,
             responses={404: {"description": "unknown page"}}, summary="레이아웃 페이지 복제")
def duplicate_layout_page(
    page_id: str,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    try:
        return layouts.duplicate_page(db, user.id, page_id)
    except layouts.NotFound:
        return _problem(404, "page not found", f"레이아웃 '{page_id}' 이 없습니다.")
    except layouts.LimitExceeded as exc:
        return _problem(409, "layout limit exceeded", str(exc))


@router.post("/layouts-reorder", response_model=list[PageSummary],
             responses={400: {"description": "id 목록 불일치"}},
             summary="레이아웃 페이지 순서 변경")
def reorder_layout_pages(
    req: ReorderRequest,
    user: User = Depends(require_member),
    db: Session = Depends(get_db),
):
    try:
        return layouts.reorder(db, user.id, req.ids)
    except layouts.NotFound as exc:
        return _problem(400, "reorder mismatch", str(exc))

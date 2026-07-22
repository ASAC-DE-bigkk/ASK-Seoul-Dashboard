"""입력 표준 필터 강제 — 매핑 완전성(새 입력부는 등록 필수) + 공격 페이로드 배터리.

① 완전성: 실제 FastAPI 라우트를 인트로스펙션해 app/inputguard.ROUTE_INPUT_FILTERS 와 대조.
   라우트·파라미터를 추가하고 매핑에 등록하지 않으면 여기서 실패한다 — "각 입력부가
   표준 필터를 거치도록 지정"의 CI 집행 지점.
② 배터리: SQL 주입·제어문자·경로조작 표본이 각 표준 필터에서 거부되는지 실측.
   식별자=화이트리스트/패턴, 값=이스케이프, 사람이 읽는 텍스트=제어문자 금지.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.inputguard import (
    ROUTE_INPUT_FILTERS,
    assert_safe_text,
    is_ident,
    is_safe_segment,
)
from app.charts import querybuilder
from app.charts.models import ChartConfig, PageCreate, QueryRequest
from app.charts.ontology import registry
from app.charts.router import _validate_charts
from app.main import app

SQLI_SAMPLES = [
    "gu\"; drop table gold_license_dong_summary; --",
    "gu' or '1'='1",
    "gu`;--",
    "gu) union select 1",
    "../../../etc/passwd",
    "gu\x00",
]


def _live_routes() -> dict[str, dict[str, str]]:
    live: dict[str, dict[str, str]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            inputs: dict[str, str] = {}
            for p in route.dependant.path_params:
                inputs[f"path:{p.name}"] = ""
            for p in route.dependant.query_params:
                inputs[f"query:{p.name}"] = ""
            if route.dependant.body_params:
                inputs["body"] = ""
            live[f"{method} {route.path}"] = inputs
    return live


def test_every_route_and_input_is_registered_in_the_filter_map() -> None:
    live = _live_routes()
    missing_routes = sorted(set(live) - set(ROUTE_INPUT_FILTERS))
    assert not missing_routes, (
        "표준 필터 매핑에 없는 라우트 — app/inputguard.ROUTE_INPUT_FILTERS 에 등록하세요: "
        + ", ".join(missing_routes)
    )
    stale_routes = sorted(set(ROUTE_INPUT_FILTERS) - set(live))
    assert not stale_routes, "매핑에만 있는 라우트(삭제/개명 반영 필요): " + ", ".join(stale_routes)
    for key, inputs in live.items():
        declared = set(ROUTE_INPUT_FILTERS[key])
        actual = set(inputs)
        assert actual == declared, (
            f"{key}: 입력 선언 불일치 — 실제 {sorted(actual)} vs 매핑 {sorted(declared)}"
        )


def test_body_filter_names_match_actual_schemas() -> None:
    """body 필터는 PYDANTIC:<스키마명> 이며 실제 라우트의 본문 모델명과 일치해야 한다."""
    def model_name(param) -> str:
        annotation = getattr(getattr(param, "field_info", None), "annotation", None) \
            or getattr(param, "type_", None)
        return getattr(annotation, "__name__", str(annotation))

    by_route = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.dependant.body_params:
            name = model_name(route.dependant.body_params[0])
            for method in route.methods - {"HEAD", "OPTIONS"}:
                by_route[f"{method} {route.path}"] = name
    for key, inputs in ROUTE_INPUT_FILTERS.items():
        declared = inputs.get("body")
        if declared is None:
            continue
        assert declared.startswith("PYDANTIC:"), f"{key}: body 필터는 PYDANTIC:<스키마> 여야 합니다"
        assert declared.split(":", 1)[1] == by_route.get(key), (
            f"{key}: body 스키마 불일치 — 매핑 {declared} vs 실제 {by_route.get(key)}"
        )


def test_identifier_filters_reject_injection_samples() -> None:
    source = registry.get("gold_license_dong_summary")
    assert source is not None
    for payload in SQLI_SAMPLES:
        assert not is_ident(payload)
        assert not is_safe_segment(payload)
        # 소스에 없는 식별자 → 화이트리스트 거부 (dims/measures/filters/order 전 경로)
        with pytest.raises(querybuilder.SpecError):
            querybuilder.build(source, {
                "dims": [payload],
                "measures": [{"field": None, "agg": "count", "alias": "count"}],
            })
        with pytest.raises(querybuilder.SpecError):
            querybuilder.build(source, {
                "dims": ["gu"],
                "measures": [{"field": payload, "agg": "sum"}],
            })
        with pytest.raises(querybuilder.SpecError):
            querybuilder.build(source, {
                "dims": ["gu"],
                "measures": [{"field": None, "agg": "count", "alias": "count"}],
                "order_by": [{"field": payload, "dir": "desc"}],
            })
        # pydantic 1차 방어(IDENT 패턴) — API 경계에서 이미 422
        with pytest.raises(ValidationError):
            QueryRequest(source=payload, measures=[{"agg": "count"}])
        with pytest.raises(ValidationError):
            QueryRequest(source="gold_license_dong_summary",
                         dims=[payload], measures=[{"agg": "count"}])


def test_literal_values_are_escaped_not_promoted_to_sql() -> None:
    """필터 '값'은 구조가 아니라 리터럴 — 이스케이프되어 식별자/구문으로 승격되지 않는다."""
    source = registry.get("gold_license_dong_summary")
    sql = querybuilder.build(source, {
        "dims": ["gu"],
        "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "gu", "op": "eq", "value": "강남'; drop table x; --구"}],
    })
    literal = "'강남''; drop table x; --구'"
    assert literal in sql                       # '' 이스케이프로 단일 리터럴 안에 봉인
    assert "drop" not in sql.replace(literal, "")  # 리터럴 밖으로는 한 글자도 새지 않음
    with pytest.raises(querybuilder.SpecError, match="제어문자"):
        querybuilder.build(source, {
            "dims": ["gu"],
            "measures": [{"field": None, "agg": "count", "alias": "count"}],
            "filters": [{"field": "gu", "op": "eq", "value": "강남\n구"}],
        })


def test_safe_text_rejects_control_characters() -> None:
    for bad in ("제목\x00", "제목\n둘째줄", "제목\x1b[31m"):
        with pytest.raises(ValueError, match="제어문자"):
            assert_safe_text(bad, field="title")
        with pytest.raises(ValidationError):
            ChartConfig(id="ok-id", type="bar", source="gold_license_dong_summary",
                        title=bad, bindings={"axis": "gu", "value": "business_count"})
        with pytest.raises(ValidationError):
            PageCreate(name=bad)
    assert assert_safe_text("동네 상권 요약 📊", field="title") == "동네 상권 요약 📊"


def test_chart_config_rejects_unsafe_ids_and_bindings() -> None:
    with pytest.raises(ValidationError):
        ChartConfig(id="id with space", type="bar", source="gold_license_dong_summary")
    with pytest.raises(ValidationError):
        ChartConfig(id="ok", type="bar", source="gold_license_dong_summary",
                    bindings={"axis": "gu\"; --"})
    with pytest.raises(ValidationError):
        QueryRequest(source="gold_license_dong_summary",
                     dims=[{"field": "business_count", "bin_width": 0}],
                     measures=[{"agg": "count"}])


def test_bin_width_cannot_break_sql_shape() -> None:
    """구간 폭은 검증된 유한 양수 float 로만 SQL 에 들어간다(repr 숫자 리터럴)."""
    source = registry.get("gold_license_dong_summary")
    for bad in (0, -3, float("inf"), float("nan"), "30; drop", True, None):
        with pytest.raises(querybuilder.SpecError):
            querybuilder.build(source, {
                "dims": [{"field": "business_count", "bin_width": bad}],
                "measures": [{"field": None, "agg": "count", "alias": "count"}],
            })
    sql = querybuilder.build(source, {
        "dims": [{"field": "business_count", "bin_width": 250}],
        "measures": [{"field": None, "agg": "count", "alias": "count"}],
    })
    assert "/ 250.0) * 250.0" in sql


def test_catalog_name_is_whitelist_lookup() -> None:
    """카탈로그 {name} 은 스냅샷 화이트리스트 조회 — 미존재/조작 입력은 404 로 끝난다."""
    from app.main import _get_or_404
    from fastapi.responses import JSONResponse

    for payload in SQLI_SAMPLES:
        result = _get_or_404(payload)
        assert isinstance(result, JSONResponse) and result.status_code == 404
    assert not isinstance(_get_or_404("gold_license_dong_summary"), JSONResponse)

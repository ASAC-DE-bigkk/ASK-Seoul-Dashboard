"""필터 조건 확장 계약 — 그룹(AND/OR)·신규 연산자·HAVING·상한·캐시 호환을 CI 로 고정.

핵심 불변식:
  ① 평면 leaf 배열의 SQL 은 종전과 byte-동일(캐시 키 = SQL 해시 — 렌더 드리프트 금지)
  ② 그룹은 항상 괄호, 그룹 안 그룹 금지(2단), 빈 그룹 거부
  ③ is_null/not_null 은 원본 컬럼(try_cast 의미 왜곡 금지), 값 동봉 거부
  ④ contains 계열은 서버 이스케이프(백슬래시 → % → _ 순서) + ESCAPE 상수
  ⑤ HAVING 은 SELECT 와 동일 _measure_expr 화이트리스트(금지 집계 우회 불가)
  ⑥ 총 leaf 예산·그룹당 상한 — models(422)·querybuilder(SpecError) 이중검증
  ⑦ 판별 봉인 — FilterSpec 에 'logic' 필드가 생기면 판별 자체가 무너진다(회귀 잠금)
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.charts import querybuilder
from app.charts.models import (
    ChartConfig, FilterGroup, FilterSpec, HavingSpec, QueryRequest,
)
from app.charts.ontology import registry
from app.charts.router import _validate_charts

SRC = "gold_license_dong_summary"
COUNT = [{"field": None, "agg": "count", "alias": "count"}]


def _build(**kw):
    return querybuilder.build(registry.get(SRC), {"dims": ["gu"], "measures": COUNT, **kw})


def test_flat_filters_render_byte_identical_to_legacy() -> None:
    sql = _build(filters=[
        {"field": "gu", "op": "eq", "value": "강남구"},
        {"field": "business_count", "op": "gte", "value": 100},
        {"field": "admin_dong", "op": "in", "value": ["신사동", "역삼동"]},
    ])
    assert ("where cast(\"gu\" as varchar) = '강남구'"
            " and try_cast(\"business_count\" as double) >= 100"
            " and cast(\"admin_dong\" as varchar) in ('신사동', '역삼동')") in sql


def test_or_group_is_parenthesized_and_mixes_with_top_and() -> None:
    sql = _build(filters=[
        {"logic": "or", "filters": [
            {"field": "gu", "op": "eq", "value": "강남구"},
            {"field": "gu", "op": "eq", "value": "서초구"}]},
        {"field": "business_count", "op": "gt", "value": 100},
    ])
    assert ("where (cast(\"gu\" as varchar) = '강남구' or cast(\"gu\" as varchar) = '서초구')"
            " and try_cast(\"business_count\" as double) > 100") in sql
    # 최상위 결합 논리 계약(filters_logic) — 기본 and, or 지정 시 전환
    sql_or = _build(filters=[
        {"field": "gu", "op": "eq", "value": "강남구"},
        {"field": "gu", "op": "eq", "value": "서초구"},
    ], filters_logic="or")
    assert "= '강남구' or cast" in sql_or


def test_group_rules_rejected() -> None:
    with pytest.raises(querybuilder.SpecError, match="그룹 안에 그룹"):
        _build(filters=[{"logic": "or", "filters": [
            {"logic": "and", "filters": [{"field": "gu", "op": "eq", "value": "x"}]}]}])
    with pytest.raises(querybuilder.SpecError, match="비어있을 수 없습니다"):
        _build(filters=[{"logic": "or", "filters": []}])
    with pytest.raises(querybuilder.SpecError, match="결합 논리"):
        _build(filters=[{"field": "gu", "op": "eq", "value": "x"}], filters_logic="xor")
    # pydantic 경계에서도 동일 거부(빈 그룹 min_length·미지 logic Literal)
    with pytest.raises(ValidationError):
        QueryRequest(source=SRC, measures=[{"agg": "count"}],
                     filters=[{"logic": "or", "filters": []}])
    with pytest.raises(ValidationError):
        QueryRequest(source=SRC, measures=[{"agg": "count"}],
                     filters=[{"logic": "nand", "filters": [{"field": "gu", "op": "eq", "value": "x"}]}])


def test_null_ops_target_raw_column_and_reject_values() -> None:
    sql = _build(filters=[{"field": "business_count", "op": "is_null"}])
    assert 'where "business_count" is null' in sql
    assert 'try_cast("business_count" as double) is null' not in sql
    sql2 = _build(filters=[{"field": "gu", "op": "not_null"}])
    assert '"gu" is not null' in sql2
    with pytest.raises(querybuilder.SpecError, match="값을 쓸 수 없습니다"):
        _build(filters=[{"field": "gu", "op": "is_null", "value": "강남구"}])


def test_contains_family_escapes_wildcards_with_server_constant() -> None:
    sql = _build(filters=[{"field": "admin_dong", "op": "contains", "value": "50%_동\\"}])
    assert "like '%50\\%\\_동\\\\%' escape '\\'" in sql
    starts = _build(filters=[{"field": "admin_dong", "op": "starts_with", "value": "신사"}])
    assert "like '신사%' escape '\\'" in starts
    ends = _build(filters=[{"field": "admin_dong", "op": "ends_with", "value": "1가"}])
    assert "like '%1가' escape '\\'" in ends
    # raw like 는 사용자 와일드카드 유지(고급) — 이스케이프 안 함
    raw = _build(filters=[{"field": "admin_dong", "op": "like", "value": "신사%"}])
    assert "like '신사%'" in raw and "escape" not in raw.split("like '신사%'")[1][:20]


def test_strict_ineq_not_between_not_like() -> None:
    sql = _build(filters=[{"field": "business_count", "op": "lt", "value": 50}])
    assert 'try_cast("business_count" as double) < 50' in sql
    sql2 = _build(filters=[{"field": "business_count", "op": "not_between", "value": [10, 20]}])
    assert 'not (try_cast("business_count" as double) between 10 and 20)' in sql2
    sql3 = _build(filters=[{"field": "admin_dong", "op": "not_like", "value": "%동"}])
    assert 'not like \'%동\'' in sql3


def test_code_and_id_roles_compare_string_strict() -> None:
    sql = _build(filters=[{"field": "gu_code", "op": "eq", "value": 11680}])
    assert "cast(\"gu_code\" as varchar) = '11680'" in sql
    sql2 = _build(filters=[{"field": "admin_dong_code", "op": "in", "value": [1168051000, "1162068500"]}])
    assert "in ('1168051000', '1162068500')" in sql2
    with pytest.raises(querybuilder.SpecError, match="연산자를 사용할 수 없습니다"):
        _build(filters=[{"field": "gu_code", "op": "gte", "value": "11000"}])  # 코드 대소비교 차단


def test_last_n_uses_build_time_literal_by_granularity() -> None:
    flow = registry.get("gold_license_flow_monthly")
    sql = querybuilder.build(flow, {
        "dims": ["ym"], "measures": [{"field": "cnt", "agg": "sum"}],
        "filters": [{"field": "ym", "op": "last_n", "value": 3}]})
    assert 'cast("ym" as varchar) >= \'20' in sql        # 'YYYY-MM' 리터럴(캐시 자정 회전)
    with pytest.raises(querybuilder.SpecError, match="정수"):
        querybuilder.build(flow, {"dims": ["ym"], "measures": [{"field": "cnt", "agg": "sum"}],
                                  "filters": [{"field": "ym", "op": "last_n", "value": "많이"}]})
    with pytest.raises(querybuilder.SpecError, match="연산자를 사용할 수 없습니다"):
        _build(filters=[{"field": "gu", "op": "last_n", "value": 3}])  # 비-time 차단


def test_having_reuses_measure_whitelist_and_requires_dims() -> None:
    sql = _build(having=[{"agg": "count", "op": "gte", "value": 10}])
    assert "group by 1 having cast(count(*) as double) >= 10" in sql
    sql2 = _build(having=[{"field": "business_count", "agg": "sum", "op": "between", "value": [10, 99]}])
    assert 'having cast(sum("business_count") as double) between 10 and 99' in sql2
    src = registry.get(SRC)
    with pytest.raises(querybuilder.SpecError):
        querybuilder.build(src, {"measures": COUNT,
                                 "having": [{"agg": "count", "op": "gte", "value": 1}]})
    # 금지 집계 우회 불가 — SELECT 에서 막힌 비가산 sum 은 HAVING 에서도 막힌다
    lifespan = registry.get("gold_license_lifespan")
    with pytest.raises(querybuilder.SpecError, match="sum 집계"):
        querybuilder.build(lifespan, {
            "dims": ["category"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
            "having": [{"field": "avg_days", "agg": "sum", "op": "gte", "value": 1}]})
    with pytest.raises(querybuilder.SpecError, match="허용되지 않는 연산자"):
        _build(having=[{"agg": "count", "op": "like", "value": 1}])


def test_leaf_budget_enforced_in_models_and_builder() -> None:
    # 그룹 증폭: 노드 3개 × 그룹당 20 leaf = 60 > 50 → pydantic 422
    nodes = [{"logic": "or", "filters": [
        {"field": "gu", "op": "eq", "value": f"구{i}"} for i in range(20)]} for _ in range(3)]
    with pytest.raises(ValidationError, match="50개 이하"):
        QueryRequest(source=SRC, measures=[{"agg": "count"}], filters=nodes)
    with pytest.raises(ValidationError):
        ChartConfig(id="x", type="bar", source=SRC, filters=nodes)
    # querybuilder 이중검증(심층방어)
    with pytest.raises(querybuilder.SpecError, match="50개 이하"):
        _build(filters=nodes)
    with pytest.raises(querybuilder.SpecError, match="20개 이하"):
        _build(filters=[{"logic": "or", "filters": [
            {"field": "gu", "op": "eq", "value": f"구{i}"} for i in range(21)]}])


def test_discriminator_sealed_and_mixed_payload_rejected() -> None:
    # 판별 기준('logic' 키)이 무너지지 않도록 leaf 모델에 logic 필드 부재를 잠근다
    assert "logic" not in FilterSpec.model_fields
    leaf = FilterSpec(field="gu", op="eq", value="x")
    assert "logic" not in leaf.model_dump()
    # {field, op, logic} 혼합 dict → 그룹 경로로 판별 → 잔여 키 forbid 422
    with pytest.raises(ValidationError):
        QueryRequest(source=SRC, measures=[{"agg": "count"}],
                     filters=[{"field": "gu", "op": "eq", "value": "x", "logic": "or"}])
    # 정상 그룹 왕복 — model_dump 가 querybuilder 워커 입력 모양과 일치
    req = QueryRequest(source=SRC, measures=[{"agg": "count"}], filters=[
        {"logic": "or", "filters": [{"field": "gu", "op": "eq", "value": "강남구"}]}])
    node = req.filters[0]
    assert isinstance(node, FilterGroup) and node.model_dump()["logic"] == "or"


def test_layout_save_validates_groups_and_having_like_query_path() -> None:
    ok = ChartConfig(
        id="filters-group", type="bar", source=SRC,
        bindings={"axis": "gu", "value": "business_count"}, agg="sum",
        filters=[{"logic": "or", "filters": [
            {"field": "gu", "op": "eq", "value": "강남구"},
            {"field": "admin_dong", "op": "contains", "value": "신사"}]}],
        having=[{"agg": "count", "op": "gte", "value": 3}],
    )
    _validate_charts([ok])
    with pytest.raises(querybuilder.SpecError, match="having.*축이 있는"):
        _validate_charts([ChartConfig(
            id="having-stat", type="stat", source=SRC,
            bindings={"value": "business_count"}, agg="sum",
            having=[{"agg": "count", "op": "gte", "value": 3}])])
    with pytest.raises(querybuilder.SpecError, match="연산자를 사용할 수 없습니다"):
        _validate_charts([ChartConfig(
            id="bad-op", type="bar", source=SRC,
            bindings={"axis": "gu", "value": "business_count"}, agg="sum",
            filters=[{"field": "gu_code", "op": "contains", "value": "116"}])])


def test_injection_samples_rejected_inside_groups_and_having() -> None:
    payload = 'gu"; drop table x; --'
    with pytest.raises(ValidationError):   # leaf.field IDENT 패턴(1차)
        QueryRequest(source=SRC, measures=[{"agg": "count"}],
                     filters=[{"logic": "or", "filters": [{"field": payload, "op": "eq", "value": "x"}]}])
    with pytest.raises(ValidationError):   # having.field IDENT 패턴(1차)
        QueryRequest(source=SRC, measures=[{"agg": "count"}],
                     having=[{"field": payload, "agg": "sum", "op": "gte", "value": 1}])
    with pytest.raises(querybuilder.SpecError):  # 화이트리스트(2차)
        _build(filters=[{"logic": "or", "filters": [{"field": payload, "op": "eq", "value": "x"}]}])
    # 그룹 내부 leaf 의 '값'도 리터럴 봉인 유지
    sql = _build(filters=[{"logic": "or", "filters": [
        {"field": "gu", "op": "eq", "value": "강남'; drop table x; --구"}]}])
    literal = "'강남''; drop table x; --구'"
    assert literal in sql and "drop" not in sql.replace(literal, "")


def test_having_spec_value_shapes() -> None:
    with pytest.raises(querybuilder.SpecError, match="유한한 숫자"):
        _build(having=[{"agg": "count", "op": "gte", "value": "많이"}])
    with pytest.raises(querybuilder.SpecError, match="최소, 최대"):
        _build(having=[{"agg": "count", "op": "between", "value": [1]}])
    assert isinstance(HavingSpec(agg="count", op="gte", value=10), HavingSpec)

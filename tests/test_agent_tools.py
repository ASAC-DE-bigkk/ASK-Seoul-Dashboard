"""agent_tools(온톨로지 → MCP/AI 도구 표면) 계약 회귀.

DB 없이 도는 테스트만 둔다 — 스펙 조립·게이팅·에러 분기·라벨/승격 규칙은 전부 순수 함수라
라이브 Trino 없이 검증된다. 실행 경로(run_query)는 SQL 조립까지만 확인하거나 error 계약을 본다.
"""
from __future__ import annotations

import json

import pytest

from app.charts import agent_tools, ontology, querybuilder

SOURCE = "gold_license_flow_monthly"


def _has(name: str) -> bool:
    return ontology.registry.get(name) is not None


pytestmark = pytest.mark.skipif(
    not _has(SOURCE), reason="스냅샷에 기준 소스가 없습니다"
)


# ── 도구 계약 ────────────────────────────────────────────────
def test_tool_schemas_match_dispatch():
    """광고한 도구 = 실행 가능한 도구(둘이 갈라지면 MCP 호스트가 없는 도구를 부른다)."""
    advertised = {t["name"] for t in agent_tools.tool_schemas()}
    assert advertised == set(agent_tools.TOOL_DISPATCH)


def test_tool_schemas_are_json_serializable_and_typed():
    for tool in agent_tools.tool_schemas():
        assert tool["input_schema"]["type"] == "object"
        # MCP inputSchema 로 그대로 나가므로 직렬화 가능해야 한다
        json.dumps(tool, ensure_ascii=False)


def test_limit_schema_is_deployment_independent():
    """스키마 상한은 런타임 env(CONFIG)가 아니라 고정 상수여야 계약이 배포마다 흔들리지 않는다."""
    for tool in agent_tools.tool_schemas():
        limit = tool["input_schema"]["properties"].get("limit")
        if limit:
            assert limit["maximum"] == querybuilder.MAX_LIMIT


# ── call_tool 방어 ───────────────────────────────────────────
def test_call_tool_unknown_tool_returns_error():
    assert agent_tools.call_tool("nope", {})["error"] == "unknown_tool"


def test_call_tool_drops_unadvertised_kwargs():
    """force 같은 숨은 인자가 도구 인자로 주입돼도 조용히 버려야 한다(캐시 우회 차단)."""
    out = agent_tools.call_tool("plan_query", {"source": SOURCE, "force": True, "bogus": 1})
    assert out.get("error") != "bad_arguments"


def test_call_tool_rejects_non_dict_arguments():
    assert agent_tools.call_tool("list_sources", ["not", "a", "dict"])["error"] == "bad_arguments"


def test_call_tool_never_raises_on_bad_types():
    """도구 내부 예외도 결과 dict 로 — 에이전트 루프가 죽으면 안 된다."""
    out = agent_tools.call_tool("plan_query", {"source": SOURCE, "dims": [{"bad": "shape"}]})
    assert "error" in out or out.get("ok") is False


# ── 소스 게이팅 ──────────────────────────────────────────────
def test_unknown_source_is_rejected_everywhere():
    for fn in (agent_tools.describe_source, agent_tools.plan_query, agent_tools.run_query):
        out = fn("no_such_source")
        assert out.get("error") == "unknown_source"


def test_source_allowlist_gates_listing_and_access(monkeypatch):
    conf = agent_tools.AgentToolsConfig(
        allowed_sources=frozenset({SOURCE}), allowed_domains=None, max_rows=10, max_turns=3
    )
    monkeypatch.setattr(agent_tools, "CONFIG", conf)
    listed = {s["source"] for s in agent_tools.list_sources()["sources"]}
    assert listed == {SOURCE}
    assert agent_tools.describe_source("gold_license_lifespan")["error"] == "unknown_source"


def test_identifier_pattern_blocks_injection_shaped_source_names():
    assert agent_tools.describe_source('x"; drop table y --')["error"] == "unknown_source"


# ── 온톨로지 계약 노출 ───────────────────────────────────────
def test_describe_source_exposes_aggregation_contract():
    fields = {f["name"]: f for f in agent_tools.describe_source(SOURCE)["fields"]}
    cnt = fields["cnt"]
    assert cnt["role"] == "measure"
    assert "allowed_aggs" in cnt and "additivity" in cnt
    assert cnt["additivity"] in ("additive", "semi_additive", "non_additive")


def test_describe_source_only_advertises_executable_rollups():
    """rollup 은 이 소스에 실재하는 컬럼만 광고한다(실행 불가 role 사슬 광고 금지)."""
    detail = agent_tools.describe_source(SOURCE)
    names = {f["name"] for f in detail["fields"]}
    for field in detail["fields"]:
        assert "rollup_to" not in field
        for col in field.get("rollup_columns", []):
            assert col in names


def test_describe_source_caps_value_labels():
    detail = agent_tools.describe_source(SOURCE)
    for field, mapping in detail["value_labels"].items():
        assert len(mapping) <= agent_tools._VALUE_LABEL_CAP
    # 잘린 필드는 전체 개수를 함께 알려준다(조용한 절단 금지)
    for field, total in detail.get("value_labels_truncated", {}).items():
        assert total > agent_tools._VALUE_LABEL_CAP


def test_manifest_has_grounding_keys():
    manifest = agent_tools.ontology_manifest()
    for key in ("role_vocabulary", "role_concepts", "geo_part_of", "chart_types", "aggregations"):
        assert manifest[key]


# ── 안전 계약 ────────────────────────────────────────────────
def test_non_additive_measure_cannot_be_summed():
    """비가산 필드 sum 은 SQL 조립 전에 거부된다(환각 방어의 코드화)."""
    if not _has("gold_license_gu_specialization"):
        pytest.skip("LQ 소스 없음")
    out = agent_tools.plan_query(
        "gold_license_gu_specialization", dims=["gu"], measures=[{"field": "lq", "agg": "sum"}]
    )
    assert out["error"] == "spec_error" and "hint" in out


def test_assert_select_only_rejects_non_select():
    agent_tools.assert_select_only("select 1")
    agent_tools.assert_select_only("  WITH t as (select 1) select * from t")
    for bad in ("delete from t", "update t set a=1", "drop table t"):
        with pytest.raises(ValueError):
            agent_tools.assert_select_only(bad)


def test_plan_query_renders_sql_without_executing():
    out = agent_tools.plan_query(SOURCE, dims=["ym"], measures=[{"field": "cnt", "agg": "sum"}])
    assert out["ok"] is True
    assert out["sql"].lower().startswith("select")
    assert "rows" not in out  # 실행하지 않는다


def test_plan_query_applies_limit_ceiling(monkeypatch):
    conf = agent_tools.AgentToolsConfig(None, None, max_rows=5, max_turns=3)
    monkeypatch.setattr(agent_tools, "CONFIG", conf)
    assert agent_tools.plan_query(SOURCE, limit=9999)["sql"].rstrip().endswith("limit 5")


# ── 식별=코드 승격 ───────────────────────────────────────────
def test_display_dim_is_promoted_to_code_dim():
    """이름 축은 코드 축으로 승격돼야 동명이지역이 합산되지 않는다."""
    source = ontology.registry.get(SOURCE)
    by_name = {f["name"]: f for f in source["fields"]}
    display = next((n for n, f in by_name.items() if f.get("id_field") in by_name), None)
    if display is None:
        pytest.skip("이 소스에 표시↔식별 쌍이 없습니다")
    dims, promoted = agent_tools.promote_dims_to_codes(source, [display])
    assert dims == [by_name[display]["id_field"]]
    assert promoted == {display: by_name[display]["id_field"]}


def test_promotion_leaves_plain_and_binned_dims_untouched():
    source = ontology.registry.get(SOURCE)
    binned = {"field": "cnt", "bin_width": 10.0}
    dims, promoted = agent_tools.promote_dims_to_codes(source, ["ym", binned])
    assert dims == ["ym", binned] and promoted == {}


# ── 명명 지표 ────────────────────────────────────────────────
def test_named_metrics_resolve_against_the_live_registry():
    """지표는 하드코딩 이름이라 스냅샷과 갈라질 수 있다 — 참조 무결성을 지킨다."""
    for name, spec in agent_tools.NAMED_METRICS.items():
        source = ontology.registry.get(spec["source"])
        if source is None:
            pytest.skip(f"{name}: 스냅샷에 소스 없음")
        fields = {f["name"]: f for f in source["fields"]}
        if spec.get("kind") == "ratio":
            # 가중 비율은 원자 분자·분모가 실재하고 둘 다 가산이어야 성립한다.
            for part in ("num", "den"):
                column = spec[part]
                assert column in fields, f"{name}: {part} 컬럼 {column} 없음"
                assert fields[column].get("additive"), f"{name}: {part} {column} 이 비가산"
            continue
        assert spec["measure"] in fields, f"{name}: 측정값 {spec['measure']} 없음"
        allowed = fields[spec["measure"]].get("allowed_aggs")
        assert allowed is None or spec["agg"] in allowed, f"{name}: {spec['agg']} 불가"


def test_named_metrics_are_executable_specs():
    """지표 정의가 실제로 조립 가능한 스펙인지 — 이름만 맞고 빌드가 깨지면 소용없다."""
    for name, spec in agent_tools.NAMED_METRICS.items():
        source = ontology.registry.get(spec["source"])
        if source is None:
            continue
        built = querybuilder.build(source, {
            "dims": list(spec.get("require_dims") or []),
            "measures": [agent_tools._metric_measure(name, spec)],
            "filters": [spec["filter"]] if spec.get("filter") else [],
            "limit": 5,
        })
        assert built.lower().startswith("select")


def test_weighted_ratio_differs_from_unweighted_avg():
    """가중 비율은 비가중 평균과 다른 식이어야 한다(SHARE §7.1 — 단순평균 왜곡 금지)."""
    source = ontology.registry.get("gold_license_cohort_survival")
    if source is None:
        pytest.skip("코호트 소스 없음")
    weighted = querybuilder.build(source, {
        "dims": ["years_elapsed"],
        "measures": [{"agg": "ratio", "num": "survivors", "den": "cohort_n"}], "limit": 5})
    naive = querybuilder.build(source, {
        "dims": ["years_elapsed"],
        "measures": [{"field": "survival_rate", "agg": "avg"}], "limit": 5})
    assert "nullif" in weighted and 'sum("survivors")' in weighted
    assert weighted != naive


def test_ratio_rejects_non_additive_parts():
    source = ontology.registry.get("gold_license_cohort_survival")
    if source is None:
        pytest.skip("코호트 소스 없음")
    with pytest.raises(querybuilder.SpecError):
        querybuilder.build(source, {"dims": [], "measures": [
            {"agg": "ratio", "num": "survival_rate", "den": "cohort_n"}]})
    with pytest.raises(querybuilder.SpecError):
        querybuilder.build(source, {"dims": [], "measures": [{"agg": "ratio", "num": "survivors"}]})


# ── 가산성 집행 (재고 × 시간축) ───────────────────────────────
def test_stock_measure_cannot_be_summed_over_time():
    """재고성 측정값 + 시간축 + sum = 이중계산 → 거부. 같은 필드도 시간축이 없으면 통과."""
    source = ontology.registry.get("gold_culture_boxoffice_daily")
    if source is None:
        pytest.skip("소스 없음")
    fields = {f["name"]: f for f in source["fields"]}
    if "seat_count" not in fields:
        pytest.skip("seat_count 없음")
    assert agent_tools.additivity(fields["seat_count"]) == "semi_additive"
    with pytest.raises(querybuilder.SpecError):
        querybuilder.build(source, {"dims": ["snapshot_date"],
                                    "measures": [{"field": "seat_count", "agg": "sum"}]})
    # avg 는 시간축에서도 허용된다
    querybuilder.build(source, {"dims": ["snapshot_date"],
                                "measures": [{"field": "seat_count", "agg": "avg"}]})


def test_non_additive_keeps_its_own_error_message():
    """비가산 필드는 '재고' 가 아니라 '집계 불가' 사유로 거부돼야 한다(오진단 방지)."""
    source = ontology.registry.get("gold_culture_booking_curve")
    if source is None:
        pytest.skip("소스 없음")
    fields = {f["name"]: f for f in source["fields"]}
    if not (fields.get("days_to_peak") and not fields["days_to_peak"].get("additive")):
        pytest.skip("대상 필드 없음")
    with pytest.raises(querybuilder.SpecError) as excinfo:
        querybuilder.build(source, {"dims": ["event_start_date"],
                                    "measures": [{"field": "days_to_peak", "agg": "sum"}]})
    assert "재고" not in str(excinfo.value)


def test_ontology_never_recommends_a_spec_it_would_reject():
    """preferred_agg 는 빌더가 거부하는 조합을 스스로 추천하면 안 된다(자기정합성)."""
    for source in ontology.registry.sources():
        fields = {f["name"]: f for f in source["fields"]}
        time_dims = [n for n, f in fields.items() if f["role"] in ("time", "sequence")]
        if not time_dims:
            continue
        for field in source["fields"]:
            if field["role"] != "measure" or not field.get("preferred_agg"):
                continue
            querybuilder.build(source, {
                "dims": [time_dims[0]],
                "measures": [{"field": field["name"], "agg": field["preferred_agg"]}],
                "limit": 5})


def test_unknown_metric_is_rejected():
    assert agent_tools.run_metric("no_such_metric")["error"] == "unknown_metric"


# ── 형식적 내보내기 ──────────────────────────────────────────
def test_skos_export_uses_broader_and_is_acyclic():
    graph = agent_tools.ontology_export("skos")["@graph"]
    assert any(n.get("@type") == "skos:ConceptScheme" for n in graph)
    assert any("skos:broader" in n for n in graph)
    for role in agent_tools.GEO_PARENT:  # 전이폐포가 끝나야 한다(순환 금지)
        assert len(agent_tools.geo_rollup_chain(role)) < 10


def test_jsonld_export_carries_context():
    doc = agent_tools.ontology_export("jsonld")
    assert "@context" in doc and "geo_part_of" in doc["@context"]


# ── last_n 닫힌 구간 (미래 예보 유입 차단) ───────────────────
@pytest.mark.parametrize("field_name", ["ym"])
def test_last_n_is_a_closed_window(field_name):
    """하한만 걸면 예보 테이블에서 '최근 N'이 미래를 끌어온다 — 상한이 함께 있어야 한다."""
    source = ontology.registry.get(SOURCE)
    fields = {f["name"]: f for f in source["fields"]}
    if field_name not in fields:
        pytest.skip("시간 필드 없음")
    sql = querybuilder.build(source, {
        "dims": [], "measures": [{"agg": "count"}],
        "filters": [{"field": field_name, "op": "last_n", "value": 7}], "limit": 5})
    where = sql.split(" where ")[1].split(" limit")[0]
    assert ">=" in where and ("<=" in where or "<" in where), where


def test_last_n_covers_every_supported_granularity():
    seen = set()
    for source in ontology.registry.sources():
        for field in source["fields"]:
            gran = field.get("granularity")
            if field["role"] != "time" or gran in seen or gran is None:
                continue
            if "last_n" not in (field.get("allowed_filter_ops") or []):
                continue
            seen.add(gran)
            sql = querybuilder.build(source, {
                "dims": [], "measures": [{"agg": "count"}],
                "filters": [{"field": field["name"], "op": "last_n", "value": 3}],
                "limit": 5})
            where = sql.split(" where ")[1].split(" limit")[0]
            assert where.count(field["name"]) >= 2, f"{gran}: 상·하한 둘 다 필요 — {where}"
    assert seen, "last_n 을 지원하는 시간 필드가 없다"


# ── 소스별 값 라벨 (교차 오염 차단) ──────────────────────────
def test_value_labels_are_scoped_to_their_source():
    """전역 병합본을 쓰면 같은 필드명을 쓰는 다른 소스의 라벨이 새어 들어온다."""
    for name in ("gold_license_flow_monthly", "gold_detail_area_profile"):
        source = ontology.registry.get(name)
        if source is None:
            continue
        own_fields = {f["name"] for f in source["fields"]}
        labels = ontology.registry.value_labels_for(name)
        assert set(labels).issubset(own_fields), f"{name}: 남의 필드 라벨이 섞였다"


def test_gu_code_labels_follow_the_authoritative_mois_dictionary():
    """자치구 코드는 MOIS 표준이 정본 — 일부 gold 의 어긋난 실측이 표기를 오염시키면 안 된다."""
    for name in ("gold_detail_area_profile", "gold_weather_x_culture_event_risk_daily"):
        source = ontology.registry.get(name)
        if source is None:
            continue
        labels = ontology.registry.value_labels_for(name).get("gu_code", {})
        for code, expected in (("11680", "강남구"), ("11215", "광진구")):
            if code in labels:
                assert labels[code] == expected, f"{name}: {code} -> {labels[code]}"


# ── 비용 게이트 ──────────────────────────────────────────────
def test_estimate_groups_multiplies_known_cardinalities():
    source = ontology.registry.get(SOURCE)
    fields = {f["name"]: f for f in source["fields"]}
    dims = [n for n, f in fields.items() if f.get("distinct_count")][:2]
    if len(dims) < 2:
        pytest.skip("통계 있는 축이 부족")
    expected = fields[dims[0]]["distinct_count"] * fields[dims[1]]["distinct_count"]
    assert agent_tools.estimate_groups(source, dims) == expected


def test_estimate_groups_returns_none_without_stats():
    """통계가 없으면 '모른다'여야 한다 — 모른다고 막으면 정상 질의가 대량 차단된다."""
    source = ontology.registry.get(SOURCE)
    assert agent_tools.estimate_groups(source, ["does_not_exist"]) is None


def test_cost_gate_rejects_before_execution(monkeypatch):
    monkeypatch.setattr(agent_tools, "CONFIG",
                        agent_tools.AgentToolsConfig(None, None, 50, 3, max_groups=1))
    source = ontology.registry.get(SOURCE)
    dims = [f["name"] for f in source["fields"] if f.get("distinct_count")][:1]
    if not dims:
        pytest.skip("통계 있는 축 없음")
    out = agent_tools.run_query(SOURCE, dims=dims, measures=[{"agg": "count"}])
    assert out["error"] == "cost_rejected" and "hint" in out
    assert out["estimated_groups"] >= 1


# ── 검색·역해결 ──────────────────────────────────────────────
def test_search_ontology_finds_sources_by_korean_and_english():
    for query in ("개폐업", "subway"):
        out = agent_tools.search_ontology(query, k=5)
        assert out["count"] >= 1, query
        assert all(r["score"] > 0 for r in out["results"])


def test_search_ontology_requires_a_query():
    assert agent_tools.search_ontology("")["error"] == "bad_arguments"


def test_resolve_label_maps_korean_name_to_code():
    out = agent_tools.resolve_label("강남구", field="gu_code")
    assert out["count"] >= 1
    assert any(c["code"] == "11680" and c["exact"] for c in out["candidates"])


def test_resolve_label_reports_homonyms_instead_of_guessing():
    """동명이지역은 후보를 모두 돌려줘야 한다 — 하나를 임의로 고르면 조용히 틀린 지역을 센다."""
    out = agent_tools.resolve_label("신사동")
    if out["count"] < 2:
        pytest.skip("스냅샷에 동명이지역이 없음")
    assert out["ambiguous"] is True
    assert len({c["code"] for c in out["candidates"]}) > 1


# ── 출처·계약 버전 ───────────────────────────────────────────
def test_describe_source_exposes_provenance():
    provenance = agent_tools.describe_source(SOURCE)["provenance"]
    assert "refresh_mode" in provenance and "generated_at" in provenance
    # lineage 는 일부 도메인만 수집됐다 — '없음'과 '수집 안 됨'을 구분해야 한다
    assert isinstance(provenance["lineage_captured"], bool)


def test_manifest_carries_a_stable_contract_hash():
    first = agent_tools.ontology_manifest()
    assert first["contract_version"] == agent_tools.CONTRACT_VERSION
    assert first["contract_hash"] == agent_tools.contract_hash()

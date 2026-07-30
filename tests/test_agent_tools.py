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
        assert spec["measure"] in fields, f"{name}: 측정값 {spec['measure']} 없음"
        allowed = fields[spec["measure"]].get("allowed_aggs")
        assert allowed is None or spec["agg"] in allowed, f"{name}: {spec['agg']} 불가"


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

"""온톨로지 → MCP/AI 도구 표면 (LLM 미포함 조작·설정부).

목적: 기존 온톨로지(``ontology.py``)·안전 SQL 빌더(``querybuilder.py``)·읽기전용 실행기
(``backends.py``)를 **한 줄도 고치지 않고**, 그 위에 얇은 "도구 표면"만 얹어 MCP 서버나
Anthropic tool-use 루프가 곧바로 붙을 수 있게 한다. 이 모듈에는 LLM 의존성이 없다 —
``anthropic``/``mcp`` SDK 를 import 하지 않으므로 서버 기동에 부담을 주지 않는다.
실제 LLM 접속 예시는 ``examples/ai_analyst_example.py``·``examples/mcp_ontology_server.py`` 참고.

세 개의 도구(list_sources·describe_source·run_query)는 온톨로지의 세 가지 질문에 대응한다:
  - **무엇이 있나?**   list_sources     → 소스 목록(도메인·행수·지원 도표)
  - **무엇으로 나눌 수 있나?** describe_source → 필드 role·집계 가산성·필터 연산자·값 라벨·롤업 관계
  - **그래서 답은?**   run_query        → 스펙(축·집계·필터)을 안전 SQL 로 실행, 행+SQL 반환

핵심 안전 불변식은 전부 **기존 경계를 재사용**한다:
  · 식별자는 레지스트리 화이트리스트(querybuilder), 값은 이스케이프, 실행은 읽기전용(backends)
  · 그래서 AI 가 실수하거나 프롬프트 주입을 당해도 raw SQL·임의 조회가 성립하지 않는다.

추가로 이 모듈은 기존 평면 온톨로지에 **없던** 두 가지 선언형 보강을 얹는다(§보강):
  (1) geo role 간 part-of 관계(동⊂구⊂시도⊂국가) — 롤업/드릴다운의 근거.
  (2) 평면 role 어휘 위의 얕은 상위어(taxonomy) — role 을 개념으로 묶는다.
둘 다 SQL 조립·캐시 키·기존 계약을 건드리지 않는다(순수 메타데이터 첨가).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import backends, ontology, querybuilder
from .trino import MAX_ROWS as TRINO_MAX_ROWS

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── 설정부 (env 기반, 트러스티드 구성) ─────────────────────────────
@dataclass(frozen=True)
class AgentToolsConfig:
    """AI/MCP 도구 표면의 질의 예산·허용 목록. 코드에 값이 아니라 env 로만 주입된다.

    - CHARTS_AGENT_ALLOWED_SOURCES : 콤마 목록 또는 "*"(전체). AI 가 볼 소스를 좁힌다.
    - CHARTS_AGENT_ALLOWED_DOMAINS : 콤마 목록 또는 "*". 도메인 단위 허용.
    - CHARTS_AGENT_MAX_ROWS        : run_query 행 상한(하드 상한 TRINO_MAX_ROWS 로 클램프).
    - CHARTS_AGENT_MAX_TURNS       : 오케스트레이션 루프 최대 반복(예제 루프가 참조).
    """

    allowed_sources: frozenset[str] | None
    allowed_domains: frozenset[str] | None
    max_rows: int
    max_turns: int

    @staticmethod
    def _parse_set(name: str) -> frozenset[str] | None:
        raw = os.environ.get(name, "").strip()
        if not raw or raw == "*":
            return None  # 미설정 = 전체 허용
        return frozenset(part.strip() for part in raw.split(",") if part.strip())

    @classmethod
    def from_env(cls) -> "AgentToolsConfig":
        try:
            max_rows = int(os.environ.get("CHARTS_AGENT_MAX_ROWS", "200"))
        except ValueError:
            max_rows = 200
        max_rows = max(1, min(max_rows, TRINO_MAX_ROWS))
        try:
            max_turns = max(1, int(os.environ.get("CHARTS_AGENT_MAX_TURNS", "8")))
        except ValueError:
            max_turns = 8
        return cls(
            allowed_sources=cls._parse_set("CHARTS_AGENT_ALLOWED_SOURCES"),
            allowed_domains=cls._parse_set("CHARTS_AGENT_ALLOWED_DOMAINS"),
            max_rows=max_rows,
            max_turns=max_turns,
        )


CONFIG = AgentToolsConfig.from_env()


# ── 보강 (1): geo role 간 part-of 관계 (기존 온톨로지에 없던 명시 관계) ──
# 평면 온톨로지에서 동/구/시도/국가는 서로 무관한 role 이었다. 여기서 처음으로
# "동은 구의 일부"라는 관계를 선언한다 — 롤업(상위 집계)·드릴다운의 근거가 되고,
# AI 가 "구별 합계"를 요청받았을 때 동 단위 축을 구 단위로 올릴 수 있음을 안다.
GEO_PARENT: dict[str, str] = {
    "geo_dong": "geo_gu",
    "geo_dong_code": "geo_gu_code",
    "geo_legal_dong": "geo_gu",
    "geo_legal_code": "geo_gu_code",
    "geo_gu": "geo_sido",
    "geo_gu_code": "geo_sido",
    "geo_sido": "geo_country",
}

# ── 보강 (2): 평면 role 어휘 위의 얕은 상위어(taxonomy) ──
# role 을 개념(concept)으로 묶는다. "무엇이 시간축이고 무엇이 공간축인가"를 AI 가
# 열거가 아니라 개념 단위로 추론할 수 있게 한다. 정식 OWL 계층이 아니라 1단 상위어다.
ROLE_CONCEPT: dict[str, str] = {
    "time": "temporal_dimension",
    "sequence": "temporal_dimension",
    "category": "categorical_dimension",
    "ordinal": "categorical_dimension",
    "geo_gu": "spatial_dimension",
    "geo_gu_code": "spatial_dimension",
    "geo_dong": "spatial_dimension",
    "geo_dong_code": "spatial_dimension",
    "geo_legal_dong": "spatial_dimension",
    "geo_legal_code": "spatial_dimension",
    "geo_sido": "spatial_dimension",
    "geo_country": "spatial_dimension",
    "geo_lat": "spatial_dimension",
    "geo_lng": "spatial_dimension",
    "measure": "measure",
    "id": "identifier",
}
# 개념 → 상위 개념 (dimension 하위에 temporal/categorical/spatial)
CONCEPT_PARENT: dict[str, str] = {
    "temporal_dimension": "dimension",
    "categorical_dimension": "dimension",
    "spatial_dimension": "dimension",
}


def geo_rollup_chain(role: str) -> list[str]:
    """geo role 의 상위 집계 사슬. 예: geo_dong → [geo_dong, geo_gu, geo_sido, geo_country]."""
    chain = [role]
    while role in GEO_PARENT:
        role = GEO_PARENT[role]
        chain.append(role)
    return chain


def role_concept_chain(role: str) -> list[str]:
    """role → 개념 → 상위 개념 사슬. 예: time → [temporal_dimension, dimension]."""
    chain: list[str] = []
    concept = ROLE_CONCEPT.get(role)
    while concept:
        chain.append(concept)
        concept = CONCEPT_PARENT.get(concept)
    return chain


def geo_parent_columns(source: dict, field_name: str) -> list[str]:
    """이 소스 안에 실재하는 상위 geo 컬럼들 — 롤업을 '기존 화이트리스트 컬럼'으로 실행 가능하게.

    geo_rollup_chain 은 role 사슬(설명용)이지만, 실제 GROUP BY 로 올리려면 그 role 을 가진
    '컬럼이 이 소스에 있어야' 한다. 예: admin_dong_code(geo_dong_code)의 상위는 geo_gu_code —
    소스에 gu_code 가 있으면 [gu_code] 를 돌려주고, AI 는 그 컬럼으로 그룹핑해 구 단위로 올린다.
    querybuilder 를 건드리지 않고 '이미 화이트리스트에 있는 상위 컬럼 선택'으로 롤업을 실현한다.
    """
    field = next((f for f in source["fields"] if f["name"] == field_name), None)
    if field is None:
        return []
    by_role: dict[str, list[str]] = {}
    for f in source["fields"]:
        by_role.setdefault(f["role"], []).append(f["name"])
    cols: list[str] = []
    for parent_role in geo_rollup_chain(field["role"])[1:]:
        cols.extend(by_role.get(parent_role, []))
    return cols


def additivity(field: dict) -> str | None:
    """3치 가산성(Kimball) — additive / semi_additive / non_additive. 기존 메타에서 유도.

    additive=false → non_additive(비율·평균·순위·LQ). additive=true 이지만 cumulative_safe=false
    → semi_additive(공간·범주엔 합산 가능하나 시간축 누적 합산은 왜곡: 재고·활성 건수형).
    둘 다 true → 완전 가산(개·폐업 flow 처럼 시간 누적도 안전).
    """
    if field.get("role") != "measure":
        return None
    if not field.get("additive", True):
        return "non_additive"
    return "additive" if field.get("cumulative_safe") else "semi_additive"


# ── 내부 헬퍼 ─────────────────────────────────────────────────
def _source_allowed(source: dict) -> bool:
    if CONFIG.allowed_sources is not None and source["name"] not in CONFIG.allowed_sources:
        return False
    if CONFIG.allowed_domains is not None and source.get("domain") not in CONFIG.allowed_domains:
        return False
    return True


def _resolve(name: Any) -> dict | None:
    if not isinstance(name, str) or not _IDENT.match(name):
        return None
    source = ontology.registry.get(name)
    if source is None or not _source_allowed(source):
        return None
    return source


def _source_value_labels(source: dict) -> dict[str, dict[str, str]]:
    """이 소스의 필드에 해당하는 코드→한글 사전만 추린다(meta.value_labels 는 전역 키)."""
    meta = ontology.registry.meta()
    field_names = {f["name"] for f in source["fields"]}
    return {k: v for k, v in meta["value_labels"].items() if k in field_names}


def _label_rows(source: dict, columns: list[str], rows: list[list]) -> list[dict]:
    """행을 dict 로 바꾸고, 값 라벨 사전이 있는 열은 ``<열>__label`` 을 덧붙인다.

    식별=코드·표기=한글 원칙을 결과에도 적용 — AI 는 코드로 집계된 원본과 한글 표기를
    동시에 본다(신사동·강남구처럼 동명이지역도 코드로 구분된 채 라벨만 붙는다).
    """
    labels = _source_value_labels(source)
    out: list[dict] = []
    for row in rows:
        item: dict[str, Any] = {}
        for col, val in zip(columns, row):
            item[col] = val
            mapping = labels.get(col)
            if mapping is not None and val is not None:
                item[f"{col}__label"] = mapping.get(str(val), str(val))
        out.append(item)
    return out


# ── 도구 (1): 무엇이 있나 ─────────────────────────────────────
def list_sources(domain: str | None = None) -> dict:
    """분석 가능한 소스 목록. 각 소스의 도메인·행수·지원 도표(=온톨로지가 판정한 표현 가능성)."""
    out = []
    for s in ontology.registry.sources(domain):
        if not _source_allowed(s):
            continue
        out.append({
            "source": s["name"],
            "label": s["label"],
            "domain": s["domain"],
            "row_count": s.get("row_count", 0),
            "supports": s["supports"],
            "description": (s.get("description") or "")[:200],
        })
    return {"count": len(out), "sources": out}


# ── 도구 (2): 무엇으로 나눌 수 있나 ───────────────────────────
def describe_source(source: str) -> dict:
    """한 소스의 온톨로지 상세 — 필드 role·가산성·허용 집계/연산자·통계·값 라벨·롤업 관계."""
    s = _resolve(source)
    if s is None:
        return {"error": "unknown_source", "message": f"소스를 찾을 수 없거나 허용되지 않았습니다: {source!r}"}
    meta = ontology.registry.meta()
    fields = []
    for f in s["fields"]:
        item: dict[str, Any] = {
            "name": f["name"],
            "role": f["role"],
            "concept": ROLE_CONCEPT.get(f["role"], f["role"]),
            "label": f.get("label", f["name"]),
            "type": f.get("type", ""),
        }
        for key in (
            "granularity", "additive", "preferred_agg", "allowed_aggs",
            "allowed_filter_ops", "distinct_count", "min", "max", "groupable",
            "id_field", "label_field", "chartable",
        ):
            if key in f and f[key] is not None:
                item[key] = f[key]
        rollup = geo_rollup_chain(f["role"])
        if len(rollup) > 1:
            item["rollup_to"] = rollup[1:]  # 상위 role 사슬(설명용: 구→시도→국가)
        parent_cols = geo_parent_columns(s, f["name"])
        if parent_cols:
            item["rollup_columns"] = parent_cols  # 실행 가능한 상위 축(이 소스에 실재하는 컬럼)
        add = additivity(f)
        if add:
            item["additivity"] = add  # additive | semi_additive | non_additive (Kimball 3치)
        fields.append(item)
    return {
        "source": s["name"],
        "label": s["label"],
        "domain": s["domain"],
        "relation": s["relation"],
        "backend": s.get("backend", "trino"),
        "row_count": s.get("row_count", 0),
        "date_range": s.get("date_range"),
        "supports": s["supports"],
        "default_chart": s.get("default_chart"),
        "fields": fields,
        "value_labels": _source_value_labels(s),
        "chart_contracts": {
            t: meta["chart_types"][t] for t in s["supports"] if t in meta["chart_types"]
        },
    }


# ── 도구 (3): 그래서 답은 ─────────────────────────────────────
def run_query(
    source: str,
    dims: list | None = None,
    measures: list | None = None,
    filters: list | None = None,
    filters_logic: str = "and",
    having: list | None = None,
    order_by: list | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """온톨로지 스펙(축·집계·필터·구간·having)을 안전 SQL 로 실행. raw SQL 불가.

    반환: {sql, mode, columns, rows, labeled_rows, row_count, truncated, elapsed_ms}.
    스펙이 화이트리스트에 어긋나면 {error:"spec_error"}, 소스가 없거나 실행 실패면 각각의 error.
    """
    s = _resolve(source)
    if s is None:
        return {"error": "unknown_source", "message": f"소스를 찾을 수 없거나 허용되지 않았습니다: {source!r}"}

    eff_limit = CONFIG.max_rows if limit is None else min(int(limit), CONFIG.max_rows)
    spec = {
        "dims": dims or [],
        "measures": measures or [{"agg": "count"}],  # 측정값 미지정 시 건수
        "filters": filters or [],
        "filters_logic": filters_logic,
        "having": having or [],
        "order_by": order_by or [],
        "limit": max(1, eff_limit),
    }
    try:
        sql = querybuilder.build(s, spec)
    except querybuilder.SpecError as exc:
        return {"error": "spec_error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — 스펙 형태 오류를 도구 결과로 통일(루프가 죽지 않게)
        return {"error": "spec_error", "message": f"{type(exc).__name__}: {exc}"}

    try:
        result = backends.execute(s, sql, max_rows=CONFIG.max_rows, force=force)
    except Exception as exc:  # noqa: BLE001 — 실행 실패도 도구 결과로(루프가 관찰·재시도 가능)
        return {"error": "query_failed", "message": str(exc), "sql": sql}

    rows = result["rows"]
    columns = result["columns"]
    return {
        "source": s["name"],
        "sql": sql,
        "mode": result.get("mode"),
        "columns": columns,
        "rows": rows,
        "labeled_rows": _label_rows(s, columns, rows),
        "row_count": len(rows),
        "truncated": len(rows) >= CONFIG.max_rows,
        "elapsed_ms": result.get("elapsed_ms"),
    }


# ── 명명 지표(named metrics) 레지스트리 (선언형 — dbt metrics 의 얇은 대응) ──
# 온톨로지 필드 위에 '이름 붙은 지표'를 얹는다. 비율 KPI(생존율·LQ 등)는 gold 에 이미 필드로
# materialize 되어 있으므로 여기서는 (소스·측정값·집계·단위·필터)만 이름에 매핑한다.
NAMED_METRICS: dict[str, dict] = {
    "business_opened": {"source": "gold_license_flow_monthly", "measure": "cnt", "agg": "sum",
                        "filter": {"field": "event_type", "op": "eq", "value": "opened"},
                        "unit": "count", "label": "개업 건수"},
    "business_closed": {"source": "gold_license_flow_monthly", "measure": "cnt", "agg": "sum",
                        "filter": {"field": "event_type", "op": "eq", "value": "closed"},
                        "unit": "count", "label": "폐업 건수"},
    "cohort_survival_rate": {"source": "gold_license_cohort_survival", "measure": "survival_rate",
                             "agg": "avg", "unit": "ratio", "label": "코호트 생존율(평균)"},
    "industry_lq": {"source": "gold_license_gu_specialization", "measure": "lq", "agg": "max",
                    "unit": "index", "label": "자치구 특화지수(LQ, 최대)"},
}


def list_metrics() -> dict:
    """명명 지표 목록(이름→소스·측정값·집계·단위). 도구/RAG 가 KPI 를 이름으로 부른다."""
    return {"count": len(NAMED_METRICS),
            "metrics": [{"name": k, **{f: v[f] for f in ("label", "unit", "source", "measure", "agg")}}
                        for k, v in NAMED_METRICS.items()]}


def run_metric(metric: str, dims: list | None = None, limit: int | None = None) -> dict:
    """명명 지표를 축(dims)별로 실행 — run_query 로 위임(온톨로지 계약·안전 경계 그대로)."""
    m = NAMED_METRICS.get(metric)
    if m is None:
        return {"error": "unknown_metric", "message": f"알 수 없는 지표: {metric!r}"}
    return run_query(m["source"], dims=dims or [],
                     measures=[{"field": m["measure"], "agg": m["agg"], "alias": metric}],
                     filters=[m["filter"]] if m.get("filter") else [], limit=limit)


# ── 형식적 내보내기: JSON-LD @context / SKOS 개념 스킴 (정직한 '시맨틱' 노출, 추론기 불필요) ──
NS = "https://ask-seoul.example/ontology#"


def jsonld_context() -> dict:
    """최소 JSON-LD @context — 역할·관계·측정 의미를 네임스페이스에 매핑한다."""
    return {
        "@vocab": NS, "skos": "http://www.w3.org/2004/02/skos/core#",
        "qb": "http://purl.org/linked-data/cube#",
        "role": {"@id": NS + "role"}, "concept": {"@id": "skos:broader"},
        "geo_part_of": {"@id": NS + "partOf"}, "identity_of": {"@id": NS + "identityOf"},
        "measure": {"@id": "qb:MeasureProperty"}, "dimension": {"@id": "qb:DimensionProperty"},
    }


def ontology_export(fmt: str = "jsonld") -> dict:
    """온톨로지를 형식적 문서로 내보낸다 — fmt='jsonld'(매니페스트+@context) | 'skos'(개념 스킴).

    OWL/추론기 없이 '역할 어휘 = SKOS 개념 스킴, geo part-of = skos:broader' 라는 정직한 형식만 준다.
    """
    if fmt == "skos":
        concepts: list[dict] = []
        for role in sorted(set(ROLE_CONCEPT) | set(GEO_PARENT)):
            node = {"@id": NS + role, "@type": "skos:Concept", "skos:notation": role}
            broader = GEO_PARENT.get(role) or ROLE_CONCEPT.get(role)
            if broader:
                node["skos:broader"] = {"@id": NS + broader}
            concepts.append(node)
        for concept, parent in CONCEPT_PARENT.items():
            concepts.append({"@id": NS + concept, "@type": "skos:Concept",
                             "skos:broader": {"@id": NS + parent}})
        return {"@context": {"skos": "http://www.w3.org/2004/02/skos/core#"},
                "@graph": [{"@id": NS + "roleScheme", "@type": "skos:ConceptScheme",
                            "skos:prefLabel": "Charts Studio role vocabulary"}] + concepts}
    doc = {"@context": jsonld_context(), "@type": "Ontology"}
    doc.update(ontology_manifest())
    return doc


# ── LLM/MCP 도구 스키마 (Anthropic tool-use = MCP inputSchema 동형) ──
_SPEC_MEASURE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "field": {"type": "string", "description": "측정 대상 필드명(count 는 생략 가능)"},
        "agg": {"type": "string", "enum": sorted(querybuilder.AGGS),
                "description": "집계. 필드의 allowed_aggs 안에서만. 비가산(additive=false)은 sum 금지"},
        "alias": {"type": "string"},
    },
}
_SPEC_FILTER = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "field": {"type": "string"},
        "op": {"type": "string", "description": "필드의 allowed_filter_ops 안에서만"},
        "value": {"description": "리터럴 값(서버가 이스케이프). in/between 은 배열"},
    },
    "required": ["field", "op"],
}
_SPEC_DIM = {
    "anyOf": [
        {"type": "string", "description": "그룹 축 필드명"},
        {"type": "object", "additionalProperties": False,
         "properties": {"field": {"type": "string"}, "bin_width": {"type": "number"}},
         "required": ["field", "bin_width"],
         "description": "숫자 measure 를 구간 폭으로 그룹핑(히스토그램형)"},
    ]
}


def tool_schemas() -> list[dict]:
    """Anthropic ``tools`` 배열 = MCP tool 정의(name/description/input_schema). 그대로 넘겨 쓴다."""
    return [
        {
            "name": "list_sources",
            "description": "분석 가능한 온톨로지 소스 목록과 각 소스가 지원하는 도표 유형을 반환한다.",
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"domain": {"type": "string", "description": "도메인 필터(선택)"}},
            },
        },
        {
            "name": "describe_source",
            "description": ("한 소스의 필드별 role·집계 가산성(additive)·허용 집계/필터 연산자·"
                            "실측 통계(distinct/min/max)·값 라벨(코드→한글)·geo 롤업 관계를 반환한다. "
                            "run_query 스펙을 짜기 전에 반드시 호출한다."),
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"source": {"type": "string"}}, "required": ["source"],
            },
        },
        {
            "name": "run_query",
            "description": ("온톨로지 스펙(축 dims·집계 measures·필터 filters·정렬·구간·having)을 "
                            "안전한 읽기전용 SQL 로 실행하고 행과 렌더된 SQL 을 반환한다. raw SQL 은 불가. "
                            "additive=false 필드에 sum 을 쓰거나 허용되지 않은 연산자를 쓰면 spec_error."),
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "dims": {"type": "array", "items": _SPEC_DIM},
                    "measures": {"type": "array", "items": _SPEC_MEASURE},
                    "filters": {"type": "array", "items": _SPEC_FILTER},
                    "filters_logic": {"type": "string", "enum": ["and", "or"]},
                    "having": {"type": "array", "items": {"type": "object"}},
                    "order_by": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"field": {"type": "string"},
                                       "dir": {"type": "string", "enum": ["asc", "desc"]}},
                        "required": ["field"]}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": CONFIG.max_rows},
                },
                "required": ["source"],
            },
        },
        {
            "name": "list_metrics",
            "description": "이름 붙은 지표(KPI) 목록 — 개업/폐업 건수, 코호트 생존율, 자치구 LQ 등. run_metric 으로 축별 실행.",
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        },
        {
            "name": "run_metric",
            "description": "명명 지표를 축(dims)별로 실행한다(run_query 위임). 예: run_metric('business_opened', dims=['gu_code']).",
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "metric": {"type": "string", "enum": sorted(NAMED_METRICS)},
                    "dims": {"type": "array", "items": _SPEC_DIM},
                    "limit": {"type": "integer", "minimum": 1, "maximum": CONFIG.max_rows},
                },
                "required": ["metric"],
            },
        },
    ]


# 도구 이름 → 실행 함수 (MCP 서버·수동 루프가 공유하는 디스패치 정본)
TOOL_DISPATCH = {
    "list_sources": list_sources,
    "describe_source": describe_source,
    "run_query": run_query,
    "list_metrics": list_metrics,
    "run_metric": run_metric,
}


def call_tool(name: str, arguments: dict | None) -> dict:
    """도구 이름+인자 → 결과 dict. 알 수 없는 도구는 error 로(루프가 죽지 않게)."""
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": "unknown_tool", "message": f"알 수 없는 도구: {name!r}"}
    try:
        return fn(**(arguments or {}))
    except TypeError as exc:
        return {"error": "bad_arguments", "message": str(exc)}


# ── 온톨로지 매니페스트 (기계 판독형 요약 — 스키마 압축기 겸 보강 노출) ──
def ontology_manifest() -> dict:
    """온톨로지 전체를 프롬프트 예산에 맞게 압축한 기계 판독형 요약.

    역할 어휘(+얕은 상위어)·geo part-of 관계·도표 슬롯 계약·도메인·식별/표기 규칙을 한 번에 준다.
    큰 온톨로지를 매 소스 나열 없이 AI/MCP 호스트에 접지시키는 용도(스키마 요약기).
    """
    meta = ontology.registry.meta()
    return {
        "generated_at": meta["generated_at"],
        "identity_rule": "코드↔한글 동반 컬럼이 있으면 집계·식별은 코드(GROUP BY), 표기는 한글(value_labels).",
        "role_vocabulary": sorted(set(ROLE_CONCEPT) | set(GEO_PARENT)),
        "role_concepts": ROLE_CONCEPT,
        "concept_parents": CONCEPT_PARENT,
        "geo_part_of": GEO_PARENT,
        "chart_types": {
            name: {"label": spec["label"],
                   "slots": [{"name": s["name"], "accepts": s.get("accepts"),
                              "required": s.get("required", False)} for s in spec["slots"]]}
            for name, spec in meta["chart_types"].items()
        },
        "domains": meta["domains"],
        "domain_labels": meta["domain_labels"],
        "aggregations": sorted(querybuilder.AGGS),
        "budget": {"max_rows": CONFIG.max_rows, "max_turns": CONFIG.max_turns},
    }

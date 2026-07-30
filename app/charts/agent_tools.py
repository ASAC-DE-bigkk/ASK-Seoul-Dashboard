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

import inspect
import json
import math
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
    max_groups: int = 500_000

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
        try:
            max_groups = max(1, int(os.environ.get("CHARTS_AGENT_MAX_GROUPS", "500000")))
        except ValueError:
            max_groups = 500_000
        return cls(
            allowed_sources=cls._parse_set("CHARTS_AGENT_ALLOWED_SOURCES"),
            allowed_domains=cls._parse_set("CHARTS_AGENT_ALLOWED_DOMAINS"),
            max_rows=max_rows,
            max_turns=max_turns,
            max_groups=max_groups,
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
    if not isinstance(field_name, str):
        return []
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
    """3치 가산성(Kimball) — additive / semi_additive / non_additive.

    온톨로지의 ``additive_over``(가산 가능한 축 종류)가 정본이다 — querybuilder 가 집행하는
    규칙과 **같은 근거**를 보고해야 "된다고 해놓고 400" 이 나지 않는다.
    시간축이 빠져 있으면 semi_additive(재고성: 한 시점의 수위라 시간 합산 시 이중계산).
    """
    if field.get("role") != "measure":
        return None
    if not field.get("additive", True):
        return "non_additive"
    additive_over = field.get("additive_over")
    if additive_over is not None and "time" not in additive_over:
        return "semi_additive"
    return "additive"


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
    """이 소스의 코드→한글 사전(소스 스코프 정본).

    전역 병합본을 필드명으로 거르면 같은 필드명을 쓰는 다른 소스의 라벨이 새어 들어온다
    (실측 충돌 210건) — 레지스트리의 소스별 사전을 그대로 쓴다.
    """
    return ontology.registry.value_labels_for(source["name"])


_VALUE_LABEL_CAP = 40  # describe_source 인라인 값 라벨 상한(프롬프트 예산 방어)


def _capped_value_labels(source: dict) -> dict:
    """describe_source 인라인 value_labels 를 필드당 상한으로 자른다(전체는 ontology_export).

    대형 코드 사전(예 dataset 180+)을 통째로 인라인하면 매 호출이 수십 KB 로 프롬프트 예산을
    잡아먹는다 — 앞 N개 + distinct 개수만 준다.
    """
    capped: dict[str, dict] = {}
    truncated: dict[str, int] = {}
    for field, mapping in _source_value_labels(source).items():
        if len(mapping) > _VALUE_LABEL_CAP:
            capped[field] = dict(list(mapping.items())[:_VALUE_LABEL_CAP])
            truncated[field] = len(mapping)
        else:
            capped[field] = dict(mapping)
    out: dict[str, Any] = {"value_labels": capped}
    if truncated:
        out["value_labels_truncated"] = truncated  # 필드→전체 distinct 수(상한 초과분)
    return out


def _coerce_limit(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return CONFIG.max_rows


def promote_dims_to_codes(source: dict, dims: list) -> tuple[list, dict[str, str]]:
    """표시 필드로 들어온 축을 코드 필드로 승격한다(프론트 app.js effectiveBindings 와 동일 규칙).

    온톨로지 원칙 '식별=코드·표기=한글'은 GROUP BY 가 코드일 때만 성립한다. 이름으로 그룹핑하면
    동명이지역(신사동: 강남구·관악구)이 한 행으로 합산돼 조용히 틀린다. 표시 필드에 id_field 가
    있으면(=실측 코드 라벨 사전이 있는 쌍) 그 코드로 바꾸고, 무엇을 바꿨는지 함께 돌려준다.
    """
    by_name = {f["name"]: f for f in source["fields"]}
    promoted: dict[str, str] = {}
    out: list = []
    # 축 목록이 리스트가 아니면(문자열 등) 문자 단위로 순회돼 엉뚱한 축이 만들어진다 —
    # 형태 오류는 빌더가 제 사유로 거부하도록 그대로 넘긴다.
    if not isinstance(dims, list):
        return list(dims) if isinstance(dims, (tuple, set)) else [], promoted
    for d in dims:
        if isinstance(d, str):
            field = by_name.get(d)
            code = field.get("id_field") if field else None
            if code and code in by_name:
                promoted[d] = code
                out.append(code)
                continue
        out.append(d)
    return out, promoted


def assert_select_only(sql: str) -> None:
    """심층방어 — 실행 직전 SELECT/WITH 문만 통과. 1차 보증은 querybuilder 화이트리스트 +
    실행기 읽기전용이며, 이 함수는 회귀 방지용 최후 관문(구조만 검사, 리터럴 내용은 보지 않음)."""
    head = sql.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("읽기 전용 SELECT/WITH 만 허용됩니다")


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
        parent_cols = geo_parent_columns(s, f["name"])
        if parent_cols:
            # 실행 가능한 상위 축(이 소스에 실재하는 컬럼)만 광고한다 — 역할 사슬 전체(구→시도→국가)는
            # 실행 불가라 오도하므로 여기서 노출하지 않고 manifest.geo_part_of 로만 서술한다.
            item["rollup_columns"] = parent_cols
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
        # 출처·신선도 — 낡았거나 통계가 부분적이면 그 사실을 알고 답해야 한다.
        # lineage 는 일부 도메인만 수집돼 있어 '의존성 없음'과 '수집 안 됨'을 구분한다.
        "provenance": {
            **ontology.registry.provenance,
            "materialized": s.get("materialized"),
            "contract_enforced": s.get("contract_enforced"),
            "lineage": s.get("lineage"),
            "lineage_captured": s.get("lineage") is not None,
            "quality": s.get("quality"),
            "tags": s.get("tags") or [],
        },
        "fields": fields,
        **_capped_value_labels(s),
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
) -> dict:
    """온톨로지 스펙(축·집계·필터·구간·having)을 안전 SQL 로 실행. raw SQL 불가.

    반환: {sql, mode, columns, rows, labeled_rows, row_count, truncated, limit, elapsed_ms}.
    truncated 는 '요청 한도 초과 여부'를 센티널(limit+1 조회)로 정확히 판정한다. order_by 를
    안 주고 차원이 있으면 차원 오름차순으로 고정해 절단 슬라이스가 결정적이고 백엔드 간 일치한다.
    스펙이 화이트리스트에 어긋나면 {error:"spec_error"}(+hint), 소스 없음/실행 실패는 각각의 error.
    """
    s = _resolve(source)
    if s is None:
        return {"error": "unknown_source", "message": f"소스를 찾을 수 없거나 허용되지 않았습니다: {source!r}"}

    dims, promoted = promote_dims_to_codes(s, dims or [])  # 이름 축 → 코드 축(동명이지역 분리)
    eff_limit = CONFIG.max_rows if limit is None else min(_coerce_limit(limit), CONFIG.max_rows)
    eff_limit = max(1, eff_limit)
    order_by = order_by or []
    if not order_by and dims:  # 결정적 절단: 차원 오름차순 고정(재현성·백엔드 일치)
        order_by = [{"field": (d["field"] if isinstance(d, dict) else d), "dir": "asc"} for d in dims]
    spec = {
        "dims": dims,
        "measures": measures or [{"agg": "count"}],  # 측정값 미지정 시 건수
        "filters": filters or [],
        "filters_logic": filters_logic,
        "having": having or [],
        "order_by": order_by,
        "limit": eff_limit + 1,  # 센티널: has_more 정확 판정 후 eff_limit 로 트림
    }
    try:
        sql = querybuilder.build(s, spec)
    except querybuilder.SpecError as exc:
        return {"error": "spec_error", "message": str(exc),
                "hint": "describe_source 로 필드의 role·allowed_aggs·allowed_filter_ops 를 확인하세요."}
    except Exception as exc:  # noqa: BLE001 — 스펙 형태 오류를 도구 결과로 통일(루프가 죽지 않게)
        return {"error": "spec_error", "message": f"{type(exc).__name__}: {exc}",
                "hint": "describe_source 로 스펙 형태를 확인하세요."}

    try:
        assert_select_only(sql)  # 심층방어(회귀 방지)
    except ValueError as exc:
        return {"error": "unsafe_sql", "message": str(exc), "sql": sql}

    # 사전 비용 게이트 — 실측 통계로 그룹 폭발을 실행 전에 막는다(통계가 없으면 통과).
    estimated = estimate_groups(s, dims)
    if estimated is not None and estimated > CONFIG.max_groups:
        rollups = sorted({
            column
            for dim in dims if isinstance(dim, str)
            for column in geo_parent_columns(s, dim)
        })
        hint = "축을 줄이거나 필터를 추가하세요."
        if rollups:
            hint = f"상위 축({', '.join(rollups)})으로 롤업하거나 필터를 추가하세요."
        return {"error": "cost_rejected", "sql": sql,
                "message": f"예상 그룹 수 {estimated:,} 가 상한 {CONFIG.max_groups:,} 를 넘습니다",
                "estimated_groups": estimated, "hint": hint}
    try:
        result = backends.execute(s, sql, max_rows=eff_limit + 1)
    except Exception as exc:  # noqa: BLE001 — 실행 실패도 도구 결과로(루프가 관찰·재시도 가능)
        return {"error": "query_failed", "message": str(exc), "sql": sql}

    columns = result["columns"]
    rows = result["rows"]
    truncated = len(rows) > eff_limit  # 센티널 초과 = 더 있음
    rows = rows[:eff_limit]
    return {
        "source": s["name"],
        "sql": sql,
        # 이름 축을 코드 축으로 승격했으면 알린다(결론 표기는 __label 을 쓰라는 신호)
        **({"promoted_dims": promoted} if promoted else {}),
        "mode": result.get("mode"),
        "columns": columns,
        "rows": rows,
        "labeled_rows": _label_rows(s, columns, rows),
        "row_count": len(rows),
        "truncated": truncated,
        "limit": eff_limit,
        # mode 만으로는 'cache/stale 이 얼마나 낡았는지'를 알 수 없다 — 나이를 함께 준다.
        "cached_at": result.get("cached_at"),
        "elapsed_ms": result.get("elapsed_ms"),
    }


def plan_query(
    source: str,
    dims: list | None = None,
    measures: list | None = None,
    filters: list | None = None,
    filters_logic: str = "and",
    having: list | None = None,
    order_by: list | None = None,
    limit: int | None = None,
) -> dict:
    """실행 없이 스펙을 검증하고 렌더된 SQL 만 반환(dry-run). spec_error 를 한 턴에서
    자가수정하게 해준다 — Trino 를 때리기 전에 형태를 확인. 반환 {ok, sql} 또는 {ok:false, error}."""
    s = _resolve(source)
    if s is None:
        return {"ok": False, "error": "unknown_source",
                "message": f"소스를 찾을 수 없거나 허용되지 않았습니다: {source!r}"}
    eff_limit = CONFIG.max_rows if limit is None else min(_coerce_limit(limit), CONFIG.max_rows)
    spec = {
        "dims": dims or [], "measures": measures or [{"agg": "count"}],
        "filters": filters or [], "filters_logic": filters_logic,
        "having": having or [], "order_by": order_by or [], "limit": max(1, eff_limit),
    }
    try:
        sql = querybuilder.build(s, spec)
    except querybuilder.SpecError as exc:
        return {"ok": False, "error": "spec_error", "message": str(exc),
                "hint": "describe_source 로 필드의 role·allowed_aggs·allowed_filter_ops 를 확인하세요."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "spec_error", "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "source": s["name"], "sql": sql, "backend": s.get("backend", "trino")}


# ── 사전 비용 게이트 (실측 통계로 실행 전에 판정 — Trino 를 때리기 전에 막는다) ──
def estimate_groups(source: dict, dims: list) -> int | None:
    """그룹 카디널리티 추정 = ∏ distinct_count(축). 통계가 없으면 None(모르면 막지 않는다).

    스냅샷 refresh 가 partial_basic 이라 상당수 컬럼에 통계가 없다 — 추정 불가를 '위험'으로
    간주해 막으면 정상 질의가 대량 차단된다. 아는 만큼만 곱하고 모르면 판단을 보류한다.
    """
    fields = {f["name"]: f for f in source["fields"]}
    # 상한에서 곱셈을 멈춘다 — 아주 작은 구간 폭(1e-300 등)은 수백 자리 정수를 만들고,
    # 그 값은 포맷·연산 모두에서 낭비다. 어차피 상한을 넘으면 결론(거부)은 같다.
    ceiling = max(CONFIG.max_groups * 10, 1)
    total = 1
    known = False
    for dim in dims:
        if isinstance(dim, dict):  # 구간 축 — (최대-최소)/폭 만큼 버킷이 생긴다
            field = fields.get(dim.get("field"))
            width = dim.get("bin_width")
            try:
                width = float(width)
            except (TypeError, ValueError):
                continue
            # 폭이 0·음수·비유한이면 구간 자체가 성립하지 않는다(빌더도 거부한다) — 세지 않는다.
            if field is None or not math.isfinite(width) or width <= 0:
                continue
            lo, hi = field.get("min"), field.get("max")
            if lo is None or hi is None or float(hi) <= float(lo):
                continue
            # 극소 폭(1e-300 등)은 나눗셈이 inf 가 되고 math.ceil(inf) 는 OverflowError 다 —
            # 빌더가 통과시키는 값이므로 여기서 크래시하면 도구가 예외를 던지게 된다(I5).
            ratio = (float(hi) - float(lo)) / width
            if not math.isfinite(ratio):
                return ceiling
            buckets = max(1, math.ceil(ratio))
        else:
            field = fields.get(dim)
            distinct = field.get("distinct_count") if field else None
            if not distinct:
                continue
            buckets = max(1, int(distinct))
        total *= buckets
        known = True
        if total >= ceiling:
            return ceiling
    return total if known else None


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
    # 가중 비율 — 종전 avg(survival_rate)는 코호트 크기를 무시한 비가중 평균이라 틀렸다(SHARE §7.1).
    # require_dims: 경과연차를 고정하지 않고 전 구간을 합치면 '몇 년 차 생존율'인지 없는 수가 된다.
    "cohort_survival_rate": {"source": "gold_license_cohort_survival", "kind": "ratio",
                             "num": "survivors", "den": "cohort_n", "unit": "ratio",
                             "require_dims": ["years_elapsed"],
                             "label": "코호트 생존율(가중)"},
    "early_close_ratio": {"source": "gold_license_lifespan", "kind": "ratio",
                          "num": "closed_within_1y", "den": "n_closed", "unit": "ratio",
                          "label": "1년 내 폐업 비율(가중)"},
    # lq 는 원자 분자·분모가 gold 에 없는 파생 지수라 가중 재집계가 불가능하다 — 최대값으로만 읽는다.
    "industry_lq": {"source": "gold_license_gu_specialization", "measure": "lq", "agg": "max",
                    "unit": "index", "label": "자치구 특화지수(LQ, 최대)"},
}


def _metric_measure(name: str, spec: dict) -> dict:
    """지표 정의 → run_query measures 항목."""
    if spec.get("kind") == "ratio":
        return {"agg": "ratio", "num": spec["num"], "den": spec["den"], "alias": name}
    return {"field": spec["measure"], "agg": spec["agg"], "alias": name}


def list_metrics() -> dict:
    """명명 지표 목록(이름→소스·집계 형태·단위). 도구/RAG 가 KPI 를 이름으로 부른다.

    허용되지 않은(또는 스냅샷에 없는) 소스의 지표는 광고하지 않는다 — 부를 수 없는 지표를
    목록에 남기면 에이전트가 존재하지 않는 능력을 시도한다.
    """
    out = []
    for name, spec in NAMED_METRICS.items():
        if _resolve(spec["source"]) is None:
            continue
        item = {"name": name, "label": spec["label"], "unit": spec["unit"],
                "source": spec["source"], "kind": spec.get("kind", "simple")}
        if spec.get("kind") == "ratio":
            item.update(num=spec["num"], den=spec["den"])
        else:
            item.update(measure=spec["measure"], agg=spec["agg"])
        if spec.get("require_dims"):
            item["require_dims"] = spec["require_dims"]
        out.append(item)
    return {"count": len(out), "metrics": out}


def run_metric(metric: str, dims: list | None = None, limit: int | None = None) -> dict:
    """명명 지표를 축(dims)별로 실행 — run_query 로 위임(온톨로지 계약·안전 경계 그대로)."""
    spec = NAMED_METRICS.get(metric)
    if spec is None:
        return {"error": "unknown_metric", "message": f"알 수 없는 지표: {metric!r}"}
    # 축 형태가 어긋나도 예외 대신 error dict 로 나가야 한다(I5) — 중첩 배열·field 없는 dict
    # 같은 흔한 오형식에서 set 조립이 터지면 에이전트 루프가 죽는다.
    dims = dims if isinstance(dims, list) else []
    dim_names = set()
    for dim in dims:
        name = dim.get("field") if isinstance(dim, dict) else dim
        if isinstance(name, str):
            dim_names.add(name)
    missing = [d for d in spec.get("require_dims", []) if d not in dim_names]
    if missing:
        return {"error": "missing_required_dims", "message":
                f"{metric} 지표는 {missing} 축이 있어야 의미가 성립합니다",
                "hint": f"dims 에 {missing} 를 포함해 다시 호출하세요."}
    return run_query(spec["source"], dims=dims,
                     measures=[_metric_measure(metric, spec)],
                     filters=[spec["filter"]] if spec.get("filter") else [], limit=limit)


# ── 검색·역해결 (열거 전용 표면을 '찾을 수 있는' 표면으로) ──
# 112개 소스를 나열만 할 수 있으면 LLM 은 소스 선택에서 헤맨다. 외부 의존성 없이(SHARE §2)
# 스냅샷 문자열만으로 인덱스를 만들고, 레지스트리 갱신 시각으로 캐시를 무효화한다.
_SEARCH_CACHE: dict[str, Any] = {"generated_at": None, "docs": []}


def _tokens(text: str) -> list[str]:
    """영문/숫자 토큰 + 한글 2-gram — 형태소 분석기 없이 한국어 부분일치를 잡는다."""
    lowered = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", lowered)
    hangul = re.findall(r"[가-힣]+", lowered)
    grams = [chunk[i:i + 2] for chunk in hangul for i in range(max(1, len(chunk) - 1))]
    return words + hangul + grams


def _search_docs() -> list[dict]:
    """소스별 검색 문서(이름·라벨·설명·필드). 스냅샷이 바뀌면 자동 재구축."""
    generated_at = ontology.registry.generated_at
    if _SEARCH_CACHE["generated_at"] == generated_at and _SEARCH_CACHE["docs"]:
        return _SEARCH_CACHE["docs"]
    docs = []
    for source in ontology.registry.sources():
        field_text = " ".join(
            f"{f['name']} {f.get('label', '')}" for f in source["fields"])
        haystack = " ".join([
            source["name"], source.get("label", ""), source.get("description", ""),
            source.get("domain", ""), field_text,
        ])
        docs.append({"source": source["name"], "domain": source["domain"],
                     "label": source["label"], "tokens": set(_tokens(haystack)),
                     "text": haystack.lower()})
    _SEARCH_CACHE.update(generated_at=generated_at, docs=docs)
    return docs


def search_ontology(query: str, k: int = 8, domain: str | None = None) -> dict:
    """자연어로 소스를 찾는다 — 이름·라벨·설명·필드명을 토큰/부분일치로 점수화."""
    if not isinstance(query, str) or not query.strip():
        return {"error": "bad_arguments", "message": "query 가 필요합니다"}
    try:  # 모델이 문자열/실수로 k 를 줘도 예외 대신 기본값으로 흡수한다(I5)
        top_k = int(k)
    except (TypeError, ValueError):
        top_k = 8
    top_k = max(1, min(top_k, 50))
    needles = set(_tokens(query))
    raw = query.lower().strip()
    hits = []
    for doc in _search_docs():
        source = ontology.registry.get(doc["source"])
        if source is None or not _source_allowed(source):
            continue
        if domain and doc["domain"] != domain:
            continue
        score = len(needles & doc["tokens"])
        if raw and raw in doc["text"]:
            score += 5          # 원문 그대로 등장하면 강한 신호
        if score:
            hits.append({"source": doc["source"], "label": doc["label"],
                         "domain": doc["domain"], "score": score})
    hits.sort(key=lambda h: (-h["score"], h["source"]))
    return {"count": len(hits), "results": hits[:top_k]}


def resolve_label(text: str, field: str | None = None, source: str | None = None) -> dict:
    """한글 표기 → 코드 (value_labels 역인덱스). 예: '강남구' → gu_code 11680.

    온톨로지는 코드로 집계하므로 사람 말(한글 지명·업종명)을 필터에 쓰려면 코드가 필요하다.
    동명이지역은 후보를 모두 돌려준다 — 임의로 하나를 고르면 조용히 틀린 지역을 세게 된다.
    """
    if not isinstance(text, str) or not text.strip():
        return {"error": "bad_arguments", "message": "text 가 필요합니다"}
    needle = text.strip().lower()
    names = [source] if source else [s["name"] for s in ontology.registry.sources()]
    seen: set[tuple] = set()
    candidates = []
    for name in names:
        resolved = _resolve(name)
        if resolved is None:
            continue
        for field_name, mapping in ontology.registry.value_labels_for(name).items():
            if field and field_name != field:
                continue
            for code, label in mapping.items():
                low = str(label).lower()
                if needle == low or needle in low:
                    # 중복 판정은 (필드, 코드)로 한다 — 소스마다 표기 이형(신사동 / 신사동·강남구)이
                    # 있어 라벨까지 키에 넣으면 같은 코드가 여러 후보로 부풀어 ambiguous 를 오염시킨다.
                    key = (field_name, str(code))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append({"field": field_name, "code": str(code),
                                       "label": str(label), "source": name,
                                       "exact": needle == low})
    candidates.sort(key=lambda c: (not c["exact"], c["field"], c["code"]))
    return {"count": len(candidates), "candidates": candidates[:50],
            "ambiguous": len({(c["field"], c["code"]) for c in candidates}) > 1}


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
        "agg": {"type": "string", "enum": sorted(querybuilder.AGGS) + ["ratio"],
                "description": ("집계. 필드의 allowed_aggs 안에서만. 비가산(additive=false)은 sum 금지. "
                                "'ratio' 는 가중 비율 — field 대신 num·den 을 준다")},
        "num": {"type": "string", "description": "ratio 분자(가산 measure). 예: survivors"},
        "den": {"type": "string", "description": "ratio 분모(가산 measure). 예: cohort_n"},
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
                    "limit": {"type": "integer", "minimum": 1, "maximum": querybuilder.MAX_LIMIT},
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
                    "limit": {"type": "integer", "minimum": 1, "maximum": querybuilder.MAX_LIMIT},
                },
                "required": ["metric"],
            },
        },
        {
            "name": "plan_query",
            "description": ("run_query 와 같은 스펙을 받아 실행 없이 검증하고 렌더된 SQL 만 반환한다"
                            "(dry-run). spec_error 를 한 턴에서 자가수정할 때 쓴다."),
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
                    "limit": {"type": "integer", "minimum": 1, "maximum": querybuilder.MAX_LIMIT},
                },
                "required": ["source"],
            },
        },
        {
            "name": "ontology_manifest",
            "description": ("온톨로지 요약(역할 어휘·role 개념·geo part-of·도표 슬롯 계약·집계·예산)을 "
                            "한 번에 반환한다. 세션 시작 시 그라운딩 프리앰블로 쓴다."),
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        },
        {
            "name": "search_ontology",
            "description": ("자연어로 소스를 찾는다(이름·라벨·설명·필드명 검색). 112개 소스를 "
                            "전부 나열하는 대신 이걸로 좁힌 뒤 describe_source 를 부른다."),
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "예: '자치구 개폐업', '지하철 혼잡'"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "domain": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "resolve_label",
            "description": ("한글 표기를 코드로 되돌린다(예: '강남구' → gu_code 11680). 집계·필터는 "
                            "코드로 하므로 사람 말을 필터 값으로 쓰기 전에 이걸 부른다. "
                            "동명이지역은 후보를 모두 돌려주므로 ambiguous 를 확인한다."),
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "field": {"type": "string", "description": "특정 필드로 한정(선택)"},
                    "source": {"type": "string", "description": "특정 소스로 한정(선택)"},
                },
                "required": ["text"],
            },
        },
    ]


# 도구 이름 → 실행 함수 (MCP 서버·수동 루프가 공유하는 디스패치 정본)
TOOL_DISPATCH = {
    "list_sources": list_sources,
    "describe_source": describe_source,
    "plan_query": plan_query,
    "run_query": run_query,
    "list_metrics": list_metrics,
    "run_metric": run_metric,
    "search_ontology": search_ontology,
    "resolve_label": resolve_label,
    # "ontology_manifest" 는 파일 하단 정의라 정의 직후 아래에서 등록한다.
}


def call_tool(name: str, arguments: dict | None) -> dict:
    """도구 이름+인자 → 결과 dict. 알 수 없는 도구·잘못된 인자는 error 로(루프가 죽지 않게).

    스키마에 없는 키(force 등 숨은 인자)는 조용히 버리고 남은 인자로만 호출한다 — 인자 주입 차단.
    """
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": "unknown_tool", "message": f"알 수 없는 도구: {name!r}"}
    args = arguments or {}
    if not isinstance(args, dict):
        return {"error": "bad_arguments", "message": "arguments 는 객체(dict)여야 합니다"}
    accepted = set(inspect.signature(fn).parameters)
    clean = {k: v for k, v in args.items() if k in accepted}
    try:
        return fn(**clean)
    except TypeError as exc:
        return {"error": "bad_arguments", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — 도구 내부 예외도 결과로(루프가 죽지 않게)
        return {"error": "tool_error", "message": f"{type(exc).__name__}: {exc}"}


# ── 계약 버전 (자동 파생 표면을 '버전 있는 계약'으로) ──
# 도구 표면은 스냅샷에서 자동 파생되므로 상류가 바뀌면 조용히 계약이 변한다.
# semver 는 손으로 올리고, hash 는 실제 표면에서 계산해 CI 가 드리프트를 잡는다.
CONTRACT_VERSION = "1.1.0"


def contract_hash() -> str:
    """도구 계약(도구 이름·입력 스키마·역할 어휘·도표 슬롯)의 결정적 해시."""
    import hashlib

    meta = ontology.registry.meta()
    payload = json.dumps({
        "tools": [{"name": t["name"], "input_schema": t["input_schema"]}
                  for t in sorted(tool_schemas(), key=lambda t: t["name"])],
        "roles": sorted(set(ROLE_CONCEPT) | set(GEO_PARENT)),
        "charts": {name: [s["name"] for s in spec["slots"]]
                   for name, spec in sorted(meta["chart_types"].items())},
        "aggregations": sorted(querybuilder.AGGS) + ["ratio"],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── 온톨로지 매니페스트 (기계 판독형 요약 — 스키마 압축기 겸 보강 노출) ──
def ontology_manifest() -> dict:
    """온톨로지 전체를 프롬프트 예산에 맞게 압축한 기계 판독형 요약.

    역할 어휘(+얕은 상위어)·geo part-of 관계·도표 슬롯 계약·도메인·식별/표기 규칙을 한 번에 준다.
    큰 온톨로지를 매 소스 나열 없이 AI/MCP 호스트에 접지시키는 용도(스키마 요약기).
    """
    meta = ontology.registry.meta()
    return {
        "generated_at": meta["generated_at"],
        "contract_version": CONTRACT_VERSION,
        "contract_hash": contract_hash(),
        "provenance": ontology.registry.provenance,
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


# 파일 하단 정의라 여기서 dispatch 에 등록한다(정의 후 바인딩).
TOOL_DISPATCH["ontology_manifest"] = ontology_manifest

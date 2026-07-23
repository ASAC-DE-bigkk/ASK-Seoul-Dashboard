"""차트 스튜디오 API 응답/요청 계약 (Pydantic) — 본체 models.py 와 같은 사상.

입력 표준 필터(app/inputguard.py) 집행 지점: SQL 로 갈 수 있는 식별자(source/field/alias)는
IDENT 패턴, id 는 SAFE_SEGMENT, 사람이 읽는 제목/이름은 SAFE_TEXT(제어문자 금지).
값(value)은 여기서 형만 받고 querybuilder 가 LITERAL 규칙(제어문자 거부+이스케이프)로 조립한다.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (
    BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator,
)

from app.inputguard import assert_safe_text

IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
SEGMENT_PATTERN = r"^[A-Za-z0-9_-]+$"
ALIAS_PATTERN = r"^[A-Za-z0-9_]+$"


class FieldInfo(BaseModel):
    name: str
    type: str
    role: str
    label: str
    desc: str = ""
    granularity: Optional[str] = None
    preferred_agg: Optional[str] = None
    additive: Optional[bool] = None
    allowed_aggs: Optional[list[str]] = None
    cumulative_safe: bool = False
    recommendation_priority: int = 40
    chartable: bool = True
    allowed_filter_ops: list[str] = Field(default_factory=list)
    # 식별↔표시 동반 필드(ontology.companion_pairs) — 표시 필드는 id_field(코드)로
    # 승격 집계되고, 식별 필드 값은 label_field/value_labels 로 한글 표기된다.
    id_field: Optional[str] = None
    label_field: Optional[str] = None
    # 실측 통계(extract approx_distinct·min/max) — 저카디널리티 groupby 개방과
    # 구간화 기본 폭 제안의 근거. groupable=True 면 measure 여도 category 축 슬롯 허용.
    distinct_count: Optional[int] = None
    min: Optional[float] = None
    max: Optional[float] = None
    groupable: bool = False


class SourceSummary(BaseModel):
    name: str
    domain: str
    label: str
    description: str = ""
    row_count: int
    supports: list[str]


class SourceDetail(SourceSummary):
    relation: str
    date_range: Optional[dict] = None
    fields: list[FieldInfo]
    default_chart: Optional[dict] = None
    # 다중 백엔드 — 소스가 사는 DB(연결 이름·방언·테이블/뷰). 기본은 trino(기존 gold)
    datasource: str = "trino"
    backend: str = "trino"
    object_type: str = "table"


class SourceAvailability(BaseModel):
    source: str
    fields: dict[str, int]
    mode: str
    elapsed_ms: int = 0


class SourcesResponse(BaseModel):
    generated_at: str
    source_count: int
    sources: list[SourceSummary]


class MeasureSpec(BaseModel):
    field: Optional[str] = Field(default=None, max_length=120, pattern=IDENT_PATTERN)
    agg: str = Field(default="sum", max_length=30, pattern=r"^[a-z_]+$")
    alias: Optional[str] = Field(default=None, max_length=120, pattern=ALIAS_PATTERN)


class FilterSpec(BaseModel):
    field: str = Field(min_length=1, max_length=120, pattern=IDENT_PATTERN)
    op: str = Field(default="eq", max_length=20, pattern=r"^[a-z_]+$")
    value: Any = None  # LITERAL — querybuilder._lit 이 제어문자 거부+이스케이프로 조립


class FilterGroup(BaseModel):
    """OR/AND 그룹 — 안은 leaf 전용(2단 계약: 그룹 안 그룹 금지 = 깊이 폭탄 구조 차단).

    extra=forbid: {field, op, logic} 혼합 dict 는 판별자('logic' 존재)가 그룹으로
    보낸 뒤 잔여 field/op 키가 여기서 422 — 모호 통과가 구조적으로 불가능하다.
    """
    model_config = ConfigDict(extra="forbid")
    logic: Literal["and", "or"]
    filters: list[FilterSpec] = Field(min_length=1, max_length=20)


def _filter_kind(value: Any) -> str:
    """판별자 — 'logic' 키 존재가 유일 기준(querybuilder._is_group·프론트 isGroup 과 동일).

    스마트 union 백트래킹(그 자체가 DoS 표면)을 차단하고 leaf/group 경로를 결정적으로 만든다.
    """
    if isinstance(value, dict):
        return "group" if "logic" in value else "leaf"
    return "group" if isinstance(value, FilterGroup) else "leaf"


FilterNode = Annotated[
    Union[Annotated[FilterGroup, Tag("group")], Annotated[FilterSpec, Tag("leaf")]],
    Discriminator(_filter_kind),
]

MAX_FILTER_LEAVES = 50  # 트리 전체 leaf 총량 — querybuilder 와 이중검증(심층방어)


def _bound_filter_leaves(nodes: list) -> list:
    """총 leaf 예산 — 최상위 max_length 는 '노드 수'만 세므로 그룹 증폭을 여기서 막는다."""
    total = sum(len(n.filters) if isinstance(n, FilterGroup) else 1 for n in nodes)
    if total > MAX_FILTER_LEAVES:
        raise ValueError(f"필터 조건은 총 {MAX_FILTER_LEAVES}개 이하여야 합니다")
    return nodes


class HavingSpec(BaseModel):
    """집계 결과 조건(HAVING) — 집계식은 SELECT 와 동일 화이트리스트로 재조립된다."""
    field: Optional[str] = Field(default=None, max_length=120, pattern=IDENT_PATTERN)
    agg: str = Field(default="count", max_length=30, pattern=r"^[a-z_]+$")
    op: str = Field(default="gte", max_length=20, pattern=r"^[a-z_]+$")
    value: Any = None  # 숫자 또는 between [최소, 최대] — querybuilder._numeric 검증


class OrderSpec(BaseModel):
    field: str = Field(min_length=1, max_length=120, pattern=ALIAS_PATTERN)
    dir: str = Field(default="asc", max_length=4, pattern=r"^(asc|desc)$")


class BinDim(BaseModel):
    """구간 축 차원 — 숫자 측정값을 bin_width 폭의 구간 시작값으로 그룹핑(히스토그램형)."""
    field: str = Field(min_length=1, max_length=120, pattern=IDENT_PATTERN)
    bin_width: float = Field(gt=0)


class QueryRequest(BaseModel):
    source: str = Field(min_length=1, max_length=120, pattern=IDENT_PATTERN)
    dims: list[str | BinDim] = Field(default_factory=list, max_length=10)

    @field_validator("dims")
    @classmethod
    def _dims_ident(cls, dims: list) -> list:
        import re
        for d in dims:
            if isinstance(d, str) and not re.fullmatch(IDENT_PATTERN, d):
                raise ValueError(f"dims 식별자 형식이 올바르지 않습니다: {d[:40]!r}")
        return dims
    measures: list[MeasureSpec] = Field(default_factory=list, min_length=1, max_length=20)
    # 필터 트리(2단) — 원소는 leaf 또는 {logic, filters:[leaf...]} 그룹. 최상위 결합은
    # filters_logic(기본 and — 종전 평면 배열과 하위호환). 총 leaf 예산은 별도 검증.
    filters: list[FilterNode] = Field(default_factory=list, max_length=50)
    filters_logic: Literal["and", "or"] = "and"
    having: list[HavingSpec] = Field(default_factory=list, max_length=10)
    order_by: list[OrderSpec] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=1000, ge=1, le=5000)
    force: bool = False  # 신선 캐시 무시하고 라이브 재질의 ('다시 조회')

    @field_validator("filters")
    @classmethod
    def _filters_budget(cls, nodes: list) -> list:
        return _bound_filter_leaves(nodes)


class QueryResponse(BaseModel):
    source: str
    columns: list[str]
    rows: list[list]
    row_count: int
    mode: str  # live | cache | stale
    sql: str
    elapsed_ms: int = 0
    cached_at: Optional[float] = None


class ChartConfig(BaseModel):
    """저장되는 차트 1개 — bindings 는 슬롯명→필드명 (온톨로지 바인딩)."""
    id: str = Field(min_length=1, max_length=80, pattern=SEGMENT_PATTERN)
    title: str = Field(default="", max_length=120)
    type: str = Field(min_length=1, max_length=30, pattern=r"^[a-z_]+$")
    source: str = Field(min_length=1, max_length=120, pattern=IDENT_PATTERN)
    bindings: dict[str, str] = Field(default_factory=dict)
    agg: str = Field(default="sum", max_length=30, pattern=r"^[a-z_]+$")
    filters: list[FilterNode] = Field(default_factory=list, max_length=50)
    filters_logic: Literal["and", "or"] = "and"   # 최상위 결합 — v1 UI 미노출(계약 선점)
    having: list[HavingSpec] = Field(default_factory=list, max_length=10)
    options: dict[str, Any] = Field(default_factory=dict)
    grid: dict[str, int] = Field(default_factory=dict)  # {x, y, w, h}
    # 구간 폭(슬롯명 → 폭) — binnable 슬롯에 measure 를 바인딩할 때 필수(히스토그램 축)
    bins: dict[str, float] = Field(default_factory=dict)

    @field_validator("filters")
    @classmethod
    def _filters_budget(cls, nodes: list) -> list:
        return _bound_filter_leaves(nodes)

    @field_validator("title")
    @classmethod
    def _title_safe(cls, value: str) -> str:
        return assert_safe_text(value, field="title", max_length=120)

    @field_validator("bindings")
    @classmethod
    def _bindings_ident(cls, bindings: dict[str, str]) -> dict[str, str]:
        import re
        for slot, name in bindings.items():
            if not re.fullmatch(r"^[a-z_]{1,30}$", slot) or not re.fullmatch(IDENT_PATTERN, name or ""):
                raise ValueError(f"bindings 형식이 올바르지 않습니다: {slot[:20]!r}")
        return bindings


class PageSummary(BaseModel):
    id: str
    name: str
    chart_count: int


class PageDetail(BaseModel):
    id: str
    name: str
    charts: list[ChartConfig]


class PageCreate(BaseModel):
    name: str = Field(default="새 레이아웃", min_length=1, max_length=80)
    template: Optional[str] = None  # 미래 확장용

    @field_validator("name")
    @classmethod
    def _name_safe(cls, value: str) -> str:
        return assert_safe_text(value, field="name", max_length=80)


class PagePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    charts: Optional[list[ChartConfig]] = Field(default=None, max_length=50)

    @field_validator("name")
    @classmethod
    def _name_safe(cls, value: Optional[str]) -> Optional[str]:
        return value if value is None else assert_safe_text(value, field="name", max_length=80)


class ReorderRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("ids")
    @classmethod
    def _ids_safe(cls, ids: list[str]) -> list[str]:
        from app.inputguard import is_safe_segment
        for item in ids:
            if not is_safe_segment(item):
                raise ValueError(f"id 형식이 올바르지 않습니다: {str(item)[:20]!r}")
        return ids

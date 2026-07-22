"""차트 스튜디오 API 응답/요청 계약 (Pydantic) — 본체 models.py 와 같은 사상."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


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
    field: Optional[str] = Field(default=None, max_length=120)
    agg: str = Field(default="sum", max_length=30)
    alias: Optional[str] = Field(default=None, max_length=120)


class FilterSpec(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    op: str = Field(default="eq", max_length=20)
    value: Any = None


class OrderSpec(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    dir: str = Field(default="asc", max_length=4)


class QueryRequest(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    dims: list[str] = Field(default_factory=list, max_length=10)
    measures: list[MeasureSpec] = Field(default_factory=list, min_length=1, max_length=20)
    filters: list[FilterSpec] = Field(default_factory=list, max_length=50)
    order_by: list[OrderSpec] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=1000, ge=1, le=5000)
    force: bool = False  # 신선 캐시 무시하고 라이브 재질의 ('다시 조회')


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
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=120)
    type: str = Field(min_length=1, max_length=30)
    source: str = Field(min_length=1, max_length=120)
    bindings: dict[str, str] = Field(default_factory=dict)
    agg: str = Field(default="sum", max_length=30)
    filters: list[FilterSpec] = Field(default_factory=list, max_length=50)
    options: dict[str, Any] = Field(default_factory=dict)
    grid: dict[str, int] = Field(default_factory=dict)  # {x, y, w, h}


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


class PagePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    charts: Optional[list[ChartConfig]] = Field(default=None, max_length=50)


class ReorderRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)

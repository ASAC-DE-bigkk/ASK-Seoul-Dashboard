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


class SourcesResponse(BaseModel):
    generated_at: str
    source_count: int
    sources: list[SourceSummary]


class MeasureSpec(BaseModel):
    field: Optional[str] = None
    agg: str = "sum"
    alias: Optional[str] = None


class FilterSpec(BaseModel):
    field: str
    op: str = "eq"
    value: Any = None


class OrderSpec(BaseModel):
    field: str
    dir: str = "asc"


class QueryRequest(BaseModel):
    source: str
    dims: list[str] = []
    measures: list[MeasureSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    order_by: list[OrderSpec] = Field(default_factory=list)
    limit: int = 1000
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
    id: str
    title: str = ""
    type: str
    source: str
    bindings: dict[str, str] = {}
    agg: str = "sum"
    filters: list[FilterSpec] = Field(default_factory=list)
    options: dict[str, Any] = {}
    grid: dict[str, int] = {}  # {x, y, w, h}


class PageSummary(BaseModel):
    id: str
    name: str
    chart_count: int


class PageDetail(BaseModel):
    id: str
    name: str
    charts: list[ChartConfig]


class PageCreate(BaseModel):
    name: str = "새 레이아웃"
    template: Optional[str] = None  # 미래 확장용


class PagePatch(BaseModel):
    name: Optional[str] = None
    charts: Optional[list[ChartConfig]] = None


class ReorderRequest(BaseModel):
    ids: list[str]

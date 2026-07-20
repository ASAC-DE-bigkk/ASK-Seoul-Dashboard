"""API 응답 계약 (Pydantic) — dbt contract 가 테이블에 하는 일을 응답에 한다.

response_model 로 선언된 스키마를 어기면 서버가 에러를 내고,
같은 선언이 그대로 OpenAPI(/docs) 문서가 된다.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    name: str
    type: str
    description: str = ""


class DateRange(BaseModel):
    column: str
    min: str | None = None
    max: str | None = None


class QualityEntry(BaseModel):
    table: str
    distribution: dict[str, int]


class TableSummary(BaseModel):
    name: str
    domain: str = "culture"
    external: bool = True  # 외부 공개 대상 여부(#269). false=내부/팀 전용(예: SLO 운영 지표)
    relation: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    contract_enforced: bool
    materialized: str = ""
    row_count: int
    column_count: int
    date_range: DateRange | None = None
    quality_source_count: int


class TableDetail(TableSummary):
    columns: list[ColumnInfo]
    quality: list[QualityEntry]
    lineage: dict[str, list[str]]
    sample: list[dict]


class CatalogResponse(BaseModel):
    generated_at: str
    domain: str
    domains: dict[str, int] = Field(default_factory=dict)
    table_count: int
    tables: list[TableSummary]


class CatalogSnapshotResponse(BaseModel):
    generated_at: str
    domain: str
    domains: dict[str, int] = Field(default_factory=dict)
    table_count: int
    tables: list[TableDetail]


class SchemaResponse(BaseModel):
    name: str
    columns: list[ColumnInfo]


class QualityResponse(BaseModel):
    name: str
    quality: list[QualityEntry]


class SampleResponse(BaseModel):
    name: str
    rows: list[dict]

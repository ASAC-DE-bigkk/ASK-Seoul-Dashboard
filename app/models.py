"""API 응답 계약 (Pydantic) — dbt contract 가 테이블에 하는 일을 응답에 한다.

response_model 로 선언된 스키마를 어기면 서버가 에러를 내고,
같은 선언이 그대로 OpenAPI(/docs) 문서가 된다.
"""
from __future__ import annotations

from pydantic import BaseModel


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
    relation: str
    description: str = ""
    tags: list[str] = []
    contract_enforced: bool
    materialized: str = ""
    serving_tier: str | None = None   # 도메인이 dbt config.meta 로 선언한 D1 서빙 tier
    refresh: str | None = None        # 도메인이 config.meta.refresh 로 선언한 갱신주기(데이터 그레인)
    tests: list[str] = []             # 모델에 걸린 dbt 테스트 게이트 (정의 기준)
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
    domains: dict[str, int] = {}
    table_count: int
    tables: list[TableSummary]


class SchemaResponse(BaseModel):
    name: str
    columns: list[ColumnInfo]


class QualityResponse(BaseModel):
    name: str
    quality: list[QualityEntry]
    tests: list[str] = []


class SampleResponse(BaseModel):
    name: str
    rows: list[dict]

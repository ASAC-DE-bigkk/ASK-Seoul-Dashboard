"""query_gold 도구 스펙 → (SQL, params) 조립 — 화이트리스트 강제.

LLM 출력은 신뢰할 수 없는 입력이다. 테이블/컬럼/별칭은 정규식 + 실측 화이트리스트를
통과해야 SQL 식별자가 되고, 값은 전부 파라미터 바인딩(?)으로만 전달된다.
serving Worker(/data/{table})의 계약(등호 필터 + 시간축 + limit)과 동형이며,
제약된 집계(group_by + count/sum/avg/min/max)를 추가한 형태다.
"""
from __future__ import annotations

import re
from typing import Any

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")

OPS = ("eq", "neq", "in", "gt", "gte", "lt", "lte", "contains", "is_null", "not_null")
AGGS = ("count", "count_distinct", "sum", "avg", "min", "max")
MAX_FILTERS = 20
MAX_IN_ITEMS = 50
MAX_VALUE_LEN = 500
MAX_SELECT = 30

# 모델에게 제시하는 도구 입력 계약(JSON Schema). queryspec 검증과 짝을 이룬다.
TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string", "description": "조회할 테이블 이름(카탈로그에 있는 것만)"},
        "columns": {
            "type": "array", "items": {"type": "string"},
            "description": "가져올 컬럼(생략 시 전체). 집계 시에는 group_by 만 사용",
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "op": {"type": "string", "enum": list(OPS)},
                    "value": {"description": "eq/neq/비교/contains 는 스칼라, in 은 배열, is_null/not_null 은 생략"},
                },
                "required": ["column", "op"],
            },
            "description": "AND 로 결합되는 필터 목록",
        },
        "time_from": {"type": "string", "description": "시간축 하한(포함) — 시간축이 있는 테이블만"},
        "time_to": {"type": "string", "description": "시간축 상한(포함)"},
        "group_by": {"type": "array", "items": {"type": "string"}},
        "aggs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fn": {"type": "string", "enum": list(AGGS)},
                    "column": {"type": "string", "description": "count 는 생략 가능"},
                    "alias": {"type": "string"},
                },
                "required": ["fn"],
            },
        },
        "order_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "description": "컬럼명 또는 집계 alias"},
                    "dir": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["by"],
            },
        },
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["table"],
}


class ChatSpecError(ValueError):
    """스펙 위반 — 메시지는 모델/사용자가 고칠 수 있게 한글로 쓴다."""


def _ident(name: Any, kind: str, allowed: set[str] | None = None) -> str:
    if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
        raise ChatSpecError(f"{kind} 이름이 올바르지 않습니다: {name!r}")
    if allowed is not None and name not in allowed:
        raise ChatSpecError(f"{kind} '{name}' 은(는) 이 테이블에 없습니다.")
    return f'"{name}"'


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        # SQLite 에는 bool 리터럴이 없고, None 비교는 is_null/not_null 로만 허용한다.
        if isinstance(value, bool):
            return 1 if value else 0
        raise ChatSpecError("필터 값이 비어 있습니다 — is_null/not_null 을 쓰세요.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ChatSpecError("필터 숫자는 유한한 값이어야 합니다.")
        return value
    if isinstance(value, str):
        if len(value) > MAX_VALUE_LEN:
            raise ChatSpecError(f"필터 값이 너무 깁니다(최대 {MAX_VALUE_LEN}자).")
        if any(ord(ch) < 0x20 for ch in value):
            raise ChatSpecError("필터 값에 제어문자는 쓸 수 없습니다.")
        return value
    raise ChatSpecError("필터 값은 문자열·숫자만 허용됩니다.")


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def build(spec: dict, *, columns: list[str], time_axis: str | None,
          max_limit: int) -> tuple[str, list, int]:
    """검증 통과 시 (sql, params, effective_limit) 반환, 위반 시 ChatSpecError.

    SQL 은 effective_limit+1 행을 조회한다 — 실행기가 effective_limit 로 잘라
    truncated(초과분 존재 여부)를 판정할 수 있게 한다.
    """
    if not isinstance(spec, dict):
        raise ChatSpecError("query_gold 입력은 객체여야 합니다.")
    allowed = set(columns)
    table_sql = _ident(spec.get("table"), "테이블")

    group_by = spec.get("group_by") or []
    aggs = spec.get("aggs") or []
    if not isinstance(group_by, list) or not isinstance(aggs, list):
        raise ChatSpecError("group_by/aggs 는 배열이어야 합니다.")
    if group_by and not aggs:
        raise ChatSpecError("group_by 를 쓰려면 aggs 를 함께 지정하세요.")

    select_parts: list[str] = []
    order_targets: set[str] = set()

    if aggs:
        if len(aggs) > 10 or len(group_by) > 5:
            raise ChatSpecError("집계는 최대 10개, group_by 는 최대 5개입니다.")
        for name in group_by:
            select_parts.append(_ident(name, "group_by 컬럼", allowed))
            order_targets.add(name)
        used_aliases: set[str] = set()
        for agg in aggs:
            if not isinstance(agg, dict):
                raise ChatSpecError("aggs 항목은 객체여야 합니다.")
            fn = agg.get("fn")
            if fn not in AGGS:
                raise ChatSpecError(f"허용되지 않은 집계입니다: {fn!r}")
            column = agg.get("column")
            if fn == "count" and column in (None, "", "*"):
                expr = "COUNT(*)"
            elif fn == "count_distinct":
                expr = f"COUNT(DISTINCT {_ident(column, '집계 컬럼', allowed)})"
            else:
                if column in (None, ""):
                    raise ChatSpecError(f"{fn} 집계에는 column 이 필요합니다.")
                expr = f"{fn.upper()}({_ident(column, '집계 컬럼', allowed)})"
            alias = agg.get("alias") or (f"{fn}_{column}" if column else fn)
            if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
                raise ChatSpecError(f"집계 alias 가 올바르지 않습니다: {alias!r}")
            if alias in used_aliases or alias in allowed:
                raise ChatSpecError(f"집계 alias 가 중복됩니다: {alias!r}")
            used_aliases.add(alias)
            order_targets.add(alias)
            select_parts.append(f'{expr} AS "{alias}"')
    else:
        wanted = spec.get("columns") or []
        if not isinstance(wanted, list):
            raise ChatSpecError("columns 는 배열이어야 합니다.")
        if len(wanted) > MAX_SELECT:
            raise ChatSpecError(f"columns 는 최대 {MAX_SELECT}개입니다.")
        if wanted:
            for name in wanted:
                select_parts.append(_ident(name, "컬럼", allowed))
                order_targets.add(name)
        else:
            select_parts.append("*")
            order_targets.update(allowed)

    where_parts: list[str] = []
    params: list = []
    filters = spec.get("filters") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise ChatSpecError(f"filters 는 배열이며 최대 {MAX_FILTERS}개입니다.")
    for item in filters:
        if not isinstance(item, dict):
            raise ChatSpecError("filters 항목은 객체여야 합니다.")
        column_sql = _ident(item.get("column"), "필터 컬럼", allowed)
        op = item.get("op")
        value = item.get("value")
        if op == "eq":
            where_parts.append(f"{column_sql} = ?")
            params.append(_scalar(value))
        elif op == "neq":
            where_parts.append(f"{column_sql} <> ?")
            params.append(_scalar(value))
        elif op in ("gt", "gte", "lt", "lte"):
            sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            where_parts.append(f"{column_sql} {sym} ?")
            params.append(_scalar(value))
        elif op == "in":
            if not isinstance(value, list) or not value:
                raise ChatSpecError("in 필터의 value 는 비어있지 않은 배열이어야 합니다.")
            if len(value) > MAX_IN_ITEMS:
                raise ChatSpecError(f"in 목록은 최대 {MAX_IN_ITEMS}개입니다.")
            placeholders = ", ".join("?" for _ in value)
            where_parts.append(f"{column_sql} IN ({placeholders})")
            params.extend(_scalar(v) for v in value)
        elif op == "contains":
            scalar = _scalar(value)
            if not isinstance(scalar, str):
                raise ChatSpecError("contains 필터의 value 는 문자열이어야 합니다.")
            where_parts.append(f"{column_sql} LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(scalar))
        elif op == "is_null":
            where_parts.append(f"{column_sql} IS NULL")
        elif op == "not_null":
            where_parts.append(f"{column_sql} IS NOT NULL")
        else:
            raise ChatSpecError(f"허용되지 않은 필터 연산입니다: {op!r}")

    for bound, sym in (("time_from", ">="), ("time_to", "<=")):
        raw = spec.get(bound)
        if raw in (None, ""):
            continue
        if not time_axis:
            raise ChatSpecError("이 테이블은 시간축이 없어 time_from/time_to 를 지원하지 않습니다.")
        scalar = _scalar(raw)
        if not isinstance(scalar, str):
            raise ChatSpecError(f"{bound} 는 ISO 문자열이어야 합니다.")
        where_parts.append(f"{_ident(time_axis, '시간축', allowed)} {sym} ?")
        params.append(scalar)

    order_sql: list[str] = []
    order_by = spec.get("order_by") or []
    if not isinstance(order_by, list) or len(order_by) > 5:
        raise ChatSpecError("order_by 는 배열이며 최대 5개입니다.")
    for item in order_by:
        if not isinstance(item, dict):
            raise ChatSpecError("order_by 항목은 객체여야 합니다.")
        by = item.get("by")
        direction = (item.get("dir") or "asc").lower()
        if direction not in ("asc", "desc"):
            raise ChatSpecError(f"정렬 방향이 올바르지 않습니다: {item.get('dir')!r}")
        order_sql.append(f"{_ident(by, '정렬 대상', order_targets)} {direction.upper()}")

    limit_raw = spec.get("limit")
    if limit_raw is None:
        limit = min(50, max_limit)
    elif isinstance(limit_raw, int) and not isinstance(limit_raw, bool) and limit_raw >= 1:
        limit = min(limit_raw, max_limit)
    else:
        raise ChatSpecError("limit 은 1 이상의 정수여야 합니다.")

    sql = f"SELECT {', '.join(select_parts)} FROM {table_sql}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if aggs and group_by:
        sql += " GROUP BY " + ", ".join(_ident(n, "group_by 컬럼", allowed) for n in group_by)
    if order_sql:
        sql += " ORDER BY " + ", ".join(order_sql)
    # +1 을 조회해 실행기가 초과분(truncated)을 감지한다.
    sql += f" LIMIT {limit + 1}"
    return sql, params, limit

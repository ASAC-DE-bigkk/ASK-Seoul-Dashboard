"""안전한 SQL 조립 — 식별자는 전부 레지스트리 화이트리스트, 값은 이스케이프.

동적 SQL 이지만 사용자 입력이 식별자로 들어갈 길은 없다:
  - source/필드명: 온톨로지 레지스트리에 실재하는 이름만 통과 (그 외 400)
  - 집계/연산자: 고정 화이트리스트
  - 리터럴 값: 문자열은 '' 이스케이프 + 제어문자 거부, 숫자는 형 검증
"""
from __future__ import annotations

import math
import re
from typing import Any

AGGS = {"sum", "avg", "min", "max", "count", "count_distinct"}
OPS = {"eq", "neq", "gte", "lte", "in", "not_in", "like", "between"}
NUMERIC_FILTER_ROLES = {"measure", "sequence", "ordinal"}
MAX_LIMIT = 5000


class SpecError(ValueError):
    """스펙이 온톨로지/화이트리스트에 안 맞음 — 400 으로 변환된다."""


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ident(name: str, fields: dict[str, dict]) -> str:
    if name not in fields:
        raise SpecError(f"'{name}' 은(는) 이 소스에 없는 필드입니다")
    return _quote_ident(name)


def _relation(value: str) -> str:
    parts = value.split(".")
    if not 1 <= len(parts) <= 3 or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
        for part in parts
    ):
        raise SpecError("소스 relation 형식이 안전하지 않습니다")
    return ".".join(_quote_ident(part) for part in parts)


def _lit(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise SpecError("필터 숫자는 유한한 값이어야 합니다")
        return repr(value)
    s = str(value)
    if any(ord(ch) < 0x20 for ch in s):
        raise SpecError("필터 값에 제어문자는 쓸 수 없습니다")
    return "'" + s.replace("'", "''") + "'"


def _dim_expr(field: dict) -> str:
    """차원 표현식 — timestamp/date 는 JSON 안전하게 varchar 로 낸다."""
    quoted = _quote_ident(field["name"])
    base = field.get("type", "").split("(")[0].lower()
    if base.startswith("timestamp") or base == "date":
        return f"cast({quoted} as varchar)"
    return quoted


def _measure_expr(field_name: str | None, agg: str, fields: dict[str, dict]) -> str:
    if agg not in AGGS:
        raise SpecError(f"허용되지 않는 집계입니다: {agg}")
    if agg == "count" and not field_name:
        return "cast(count(*) as double)"
    if not field_name:
        raise SpecError(f"{agg} 집계에는 필드가 필요합니다")
    quoted = _ident(field_name, fields)
    field = fields[field_name]
    allowed = field.get("allowed_aggs")
    if allowed is None and field.get("role") != "measure":
        allowed = ["count", "count_distinct"]
    if allowed is not None and agg not in allowed:
        raise SpecError(f"'{field_name}' 필드에는 {agg} 집계를 사용할 수 없습니다")
    if agg == "count_distinct":
        return f"cast(count(distinct {quoted}) as double)"
    return f"cast({agg}({quoted}) as double)"


def _str_lit(value: Any) -> str:
    """값을 문자열 리터럴로 — 컬럼 쪽을 varchar 로 캐스팅해 비교하므로 타입 드리프트에 안전."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    return _lit(str(value))


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def allowed_filter_ops(field: dict) -> list[str]:
    """필드 role별 허용 연산자. SourceDetail로 전달해 UI와 서버가 같은 계약을 쓴다."""
    role = field.get("role")
    if role in NUMERIC_FILTER_ROLES or role == "time":
        return ["eq", "neq", "gte", "lte", "between", "in", "not_in"]
    if role == "category":
        return ["eq", "neq", "like", "in", "not_in"]
    return ["eq", "neq", "in", "not_in"]


def _numeric(value: Any) -> int | float:
    """JSON 숫자와 유한한 숫자 문자열을 같은 double 비교 값으로 정규화한다."""
    if isinstance(value, bool) or _is_blank(value):
        raise SpecError("숫자 필터에는 유한한 숫자가 필요합니다")
    if _is_num(value):
        number = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise SpecError("숫자 필터에는 유한한 숫자가 필요합니다") from exc
    else:
        raise SpecError("숫자 필터에는 유한한 숫자가 필요합니다")
    if isinstance(number, float) and not math.isfinite(number):
        raise SpecError("숫자 필터에는 유한한 숫자가 필요합니다")
    return number


def _condition(f: dict, fields: dict[str, dict]) -> str:
    """비교는 타입 유연하게 조립한다 — 물리 테이블 타입이 스냅샷과 달라져도(온톨로지
    드리프트: 예. varchar 연도 → integer) 필터가 깨지지 않도록 정규화한다.
    숫자 값 → try_cast(col as double) 비교 (varchar 캐스팅은 double 을 '5.0E-1' 식
    과학표기로 내 동등 비교가 항상 거짓이 된다), 문자열 값 → cast(col as varchar) 비교."""
    op = f.get("op", "eq")
    if op not in OPS:
        raise SpecError(f"허용되지 않는 연산자입니다: {op}")
    field_name = f.get("field")
    col = _ident(field_name, fields)
    field = fields[field_name]
    if op not in allowed_filter_ops(field):
        raise SpecError(f"{field_name} 필드에는 {op} 연산자를 사용할 수 없습니다")
    numeric_field = field.get("role") in NUMERIC_FILTER_ROLES
    v = f.get("value")
    s_col = f"cast({col} as varchar)"
    n_col = f"try_cast({col} as double)"

    if op in ("eq", "neq"):
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        sign = "=" if op == "eq" else "<>"
        if numeric_field:
            return f"{n_col} {sign} {_lit(_numeric(v))}"
        if _is_num(v):
            return f"{n_col} {sign} {_lit(v)}"
        return f"{s_col} {sign} {_str_lit(v)}"
    if op == "like":
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        return f"{s_col} like {_str_lit(v)}"
    if op in ("gte", "lte"):
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        sign = ">=" if op == "gte" else "<="
        if numeric_field:
            return f"{n_col} {sign} {_lit(_numeric(v))}"
        if _is_num(v):
            return f"{n_col} {sign} {_lit(v)}"
        return f"{s_col} {sign} {_str_lit(v)}"
    if op == "between":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise SpecError("between 값은 [최소, 최대] 형식이어야 합니다")
        if any(_is_blank(item) for item in v):
            raise SpecError("between 값은 비워둘 수 없습니다")
        if numeric_field:
            numeric = [_numeric(item) for item in v]
            return f"{n_col} between {_lit(numeric[0])} and {_lit(numeric[1])}"
        if all(_is_num(x) for x in v):
            return f"{n_col} between {_lit(v[0])} and {_lit(v[1])}"
        return f"{s_col} between {_str_lit(v[0])} and {_str_lit(v[1])}"
    # in / not_in
    if not isinstance(v, (list, tuple)) or not v:
        raise SpecError("in/not_in 값은 비어있지 않은 배열이어야 합니다")
    if any(_is_blank(item) for item in v):
        raise SpecError("in/not_in 값은 비워둘 수 없습니다")
    neg = "not " if op == "not_in" else ""
    if numeric_field:
        vals = ", ".join(_lit(_numeric(item)) for item in v)
        return f"{n_col} {neg}in ({vals})"
    if all(_is_num(x) for x in v):
        vals = ", ".join(_lit(x) for x in v)
        return f"{n_col} {neg}in ({vals})"
    vals = ", ".join(_str_lit(x) for x in v)
    return f"{s_col} {neg}in ({vals})"


def validate_filter(filter_spec: dict, fields: dict[str, dict]) -> None:
    """SQL 실행 없이 필터 필드·연산자·값 모양을 동일 규칙으로 검증한다."""
    _condition(filter_spec, fields)


def build(source: dict, spec: dict) -> str:
    """spec = {dims: [name], measures: [{field?, agg, alias?}], filters, order_by, limit}"""
    fields = {f["name"]: f for f in source["fields"]}

    dims = spec.get("dims") or []
    measures = spec.get("measures") or []
    if not measures:
        raise SpecError("measures 가 최소 1개 필요합니다")
    if len(dims) != len(set(dims)):
        raise SpecError("차원 필드는 서로 달라야 합니다")

    select_parts: list[str] = []
    aliases: list[str] = []
    for d in dims:
        f = fields.get(d)
        if f is None:
            raise SpecError(f"'{d}' 은(는) 이 소스에 없는 필드입니다")
        select_parts.append(f"{_dim_expr(f)} as {_quote_ident(d)}")
        aliases.append(d)
    for m in measures:
        agg = m.get("agg", "sum")
        field_name = m.get("field")
        alias = m.get("alias") or (f"{agg}_{field_name}" if field_name else "count")
        if not alias.replace("_", "").isalnum():
            raise SpecError(f"alias 형식이 잘못됐습니다: {alias}")
        if alias in aliases:
            alias = f"{alias}_{len(aliases)}"
        select_parts.append(
            f"{_measure_expr(field_name, agg, fields)} as {_quote_ident(alias)}"
        )
        aliases.append(alias)

    sql = f"select {', '.join(select_parts)} from {_relation(source['relation'])}"

    conds = [_condition(f, fields) for f in (spec.get("filters") or [])]
    if conds:
        sql += " where " + " and ".join(conds)

    if dims:
        sql += " group by " + ", ".join(str(i + 1) for i in range(len(dims)))

    order_by = spec.get("order_by") or []
    if order_by:
        parts = []
        for o in order_by:
            key = o.get("field")
            if key not in aliases:
                raise SpecError(f"order_by 는 선택된 필드만 가능합니다: {key}")
            direction = "desc" if o.get("dir", "asc") == "desc" else "asc"
            parts.append(f"{_quote_ident(key)} {direction}")
        sql += " order by " + ", ".join(parts)

    limit = min(int(spec.get("limit") or 1000), MAX_LIMIT)
    sql += f" limit {limit}"
    return sql

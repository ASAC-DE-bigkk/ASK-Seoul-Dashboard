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
OPS = {
    # 비교(엄격/포함) · 범위 · 집합
    "eq", "neq", "gt", "gte", "lt", "lte", "between", "not_between", "in", "not_in",
    # 문자열 — like/not_like 는 사용자가 %·_ 를 직접 쓰는 고급 패턴,
    # contains/starts_with/ends_with 는 서버가 이스케이프해 조립하는 리터럴 부분일치
    "like", "not_like", "contains", "starts_with", "ends_with",
    # NULL 판정(값 없음) · 상대 시간 창(time granularity 연동)
    "is_null", "not_null", "last_n",
}
HAVING_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "between"}  # 집계 결과 비교 전용
GROUP_LOGICS = {"and", "or"}
NUMERIC_FILTER_ROLES = {"measure", "sequence", "ordinal"}
# 고정폭 코드·식별자 — 숫자로 와도 문자열 비교로 고정(선행 0·정밀도·체계 확장 안전)
CODE_STRICT_ROLES = {"geo_gu_code", "geo_dong_code", "geo_legal_code", "id"}
NO_VALUE_OPS = {"is_null", "not_null"}
MAX_LIMIT = 5000
MAX_FILTER_LEAVES = 50    # 트리 전체 leaf 총량 상한(그룹 증폭 방어 — models 와 이중검증)
MAX_GROUP_CONDITIONS = 20  # 그룹당 leaf 상한
LIKE_ESCAPE = "\\"        # ESCAPE 문자는 서버 상수 — 절대 사용자 입력이 아님


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


def _bin_width(raw: Any) -> float:
    """구간 폭 검증 — 유한한 양수만. (0/음수/무한은 floor 나눗셈 의미가 무너진다.)"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SpecError("구간 폭(bin)은 숫자여야 합니다")
    width = float(raw)
    if not math.isfinite(width) or width <= 0:
        raise SpecError("구간 폭(bin)은 유한한 양수여야 합니다")
    return width


def _binned_dim_expr(field: dict, width: float) -> str:
    """숫자 측정값의 구간 축 — floor(값/폭)*폭 = 구간 시작값. 별칭은 필드명 유지
    (소비자·정렬·피벗이 일반 dim 과 동일하게 동작). null 값은 구간 null 로 남는다."""
    quoted = _quote_ident(field["name"])
    lit = repr(width)
    return f"cast(floor(try_cast({quoted} as double) / {lit}) * {lit} as double)"


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
    """필드 role별 허용 연산자. SourceDetail로 전달해 UI와 서버가 같은 계약을 쓴다.

    확장 규칙(2026-07-23): NULL 판정은 전 role, 엄격 부등호·범위 밖은 숫자·시간,
    부분일치(contains 계열)는 이름/범주 문자열, last_n 은 granularity 있는 time 전용,
    고정폭 코드·id 는 동등/집합만(대소·패턴은 코드 체계 오용이라 차단).
    """
    role = field.get("role")
    if role in CODE_STRICT_ROLES:
        return ["eq", "neq", "in", "not_in", "is_null", "not_null"]
    if role in NUMERIC_FILTER_ROLES:
        return ["eq", "neq", "gt", "gte", "lt", "lte", "between", "not_between",
                "in", "not_in", "is_null", "not_null"]
    if role == "time":
        ops = ["eq", "neq", "gt", "gte", "lt", "lte", "between", "not_between",
               "in", "not_in", "is_null", "not_null"]
        if field.get("granularity") in ("year", "month", "date", "datetime"):
            ops.append("last_n")
        return ops
    if role in ("category", "geo_gu", "geo_dong", "geo_sido", "geo_legal_dong", "geo_country"):
        return ["eq", "neq", "in", "not_in", "contains", "starts_with", "ends_with",
                "like", "not_like", "is_null", "not_null"]
    return ["eq", "neq", "in", "not_in", "is_null", "not_null"]


def _like_pattern(value: Any, *, prefix: str, suffix: str) -> str:
    """contains/starts_with/ends_with 의 리터럴 부분일치 패턴 — 서버가 이스케이프해 조립한다.

    순서가 결정적: 백슬래시 먼저(\\ → \\\\), 그다음 % → \\%, _ → \\_ — 순서가 바뀌면
    앞 치환이 만든 백슬래시가 이중화되어 패턴이 깨진다. ESCAPE 문자는 LIKE_ESCAPE 상수.
    """
    if _is_blank(value):
        raise SpecError("필터 값은 비워둘 수 없습니다")
    s = str(value)
    if any(ord(ch) < 0x20 for ch in s):
        raise SpecError("필터 값에 제어문자는 쓸 수 없습니다")
    escaped = (s.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
                .replace("%", LIKE_ESCAPE + "%")
                .replace("_", LIKE_ESCAPE + "_"))
    return _str_lit(f"{prefix}{escaped}{suffix}") + f" escape '{LIKE_ESCAPE}'"


def _code_str(value: Any) -> str:
    """고정폭 코드/식별자 값의 문자열 정규화 — 숫자로 와도 '1168051000' 형태로 비교."""
    if isinstance(value, bool):
        raise SpecError("코드 값 형식이 올바르지 않습니다")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SpecError("코드 값 형식이 올바르지 않습니다")
        return str(int(value))
    return str(value)


def _last_n_condition(field: dict, s_col: str, n_col: str, value: Any) -> str:
    """최근 N(일/개월/년) — time granularity 연동. 컷오프는 **빌드 시점 리터럴**로 해석:
    SQL 이 재현 가능한 산출물이 되고 캐시 키가 자정에 자연 회전한다(오늘 포함 N단위)."""
    from datetime import date, timedelta

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise SpecError("최근 N 조건의 값은 1~3650 정수여야 합니다")
    gran = field.get("granularity")
    today = date.today()
    if gran == "year":
        return f"{n_col} >= {float(today.year - (value - 1))!r}"
    if gran == "month":
        months = today.year * 12 + (today.month - 1) - (value - 1)
        year, month = divmod(months, 12)
        return f"{s_col} >= {_str_lit(f'{year:04d}-{month + 1:02d}')}"
    if gran in ("date", "datetime"):
        cutoff = today - timedelta(days=value - 1)
        return f"{s_col} >= {_str_lit(cutoff.isoformat())}"
    raise SpecError("이 필드는 최근 N 조건을 지원하지 않습니다")


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
    과학표기로 내 동등 비교가 항상 거짓이 된다), 문자열 값 → cast(col as varchar) 비교.

    주의(기존 8종 연산자): 출력 SQL 을 바꾸지 않는다 — 캐시 키가 SQL 원문 해시라
    렌더링이 1바이트라도 바뀌면 기존 캐시 전량이 콜드스타트된다.
    """
    op = f.get("op", "eq")
    if op not in OPS:
        raise SpecError(f"허용되지 않는 연산자입니다: {op}")
    field_name = f.get("field")
    col = _ident(field_name, fields)
    field = fields[field_name]
    if op not in allowed_filter_ops(field):
        raise SpecError(f"{field_name} 필드에는 {op} 연산자를 사용할 수 없습니다")
    numeric_field = field.get("role") in NUMERIC_FILTER_ROLES
    code_strict = field.get("role") in CODE_STRICT_ROLES
    v = f.get("value")
    s_col = f"cast({col} as varchar)"
    n_col = f"try_cast({col} as double)"

    # NULL 판정은 캐스팅 이전의 **원본 컬럼**을 본다 — try_cast(col as double) is null 로
    # 조립하면 '비숫자 문자열'까지 매칭되어 의미가 '물리적 null'에서 왜곡된다. 값 불허.
    if op in NO_VALUE_OPS:
        if not _is_blank(v):
            raise SpecError("값 없음/값 있음 조건에는 값을 쓸 수 없습니다")
        return f"{col} is null" if op == "is_null" else f"{col} is not null"
    if op == "last_n":
        return _last_n_condition(field, s_col, n_col, v)

    if op in ("eq", "neq"):
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        sign = "=" if op == "eq" else "<>"
        if code_strict:
            return f"{s_col} {sign} {_str_lit(_code_str(v))}"
        if numeric_field:
            return f"{n_col} {sign} {_lit(_numeric(v))}"
        if _is_num(v):
            return f"{n_col} {sign} {_lit(v)}"
        return f"{s_col} {sign} {_str_lit(v)}"
    if op in ("like", "not_like"):
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        neg = "not " if op == "not_like" else ""
        return f"{s_col} {neg}like {_str_lit(v)}"
    if op in ("contains", "starts_with", "ends_with"):
        wrap = {"contains": ("%", "%"), "starts_with": ("", "%"), "ends_with": ("%", "")}[op]
        return f"{s_col} like {_like_pattern(v, prefix=wrap[0], suffix=wrap[1])}"
    if op in ("gt", "gte", "lt", "lte"):
        if _is_blank(v):
            raise SpecError("필터 값은 비워둘 수 없습니다")
        sign = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        if numeric_field:
            return f"{n_col} {sign} {_lit(_numeric(v))}"
        if _is_num(v):
            return f"{n_col} {sign} {_lit(v)}"
        return f"{s_col} {sign} {_str_lit(v)}"
    if op in ("between", "not_between"):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise SpecError("between 값은 [최소, 최대] 형식이어야 합니다")
        if any(_is_blank(item) for item in v):
            raise SpecError("between 값은 비워둘 수 없습니다")
        if numeric_field:
            numeric = [_numeric(item) for item in v]
            core = f"{n_col} between {_lit(numeric[0])} and {_lit(numeric[1])}"
        elif all(_is_num(x) for x in v):
            core = f"{n_col} between {_lit(v[0])} and {_lit(v[1])}"
        else:
            core = f"{s_col} between {_str_lit(v[0])} and {_str_lit(v[1])}"
        return f"not ({core})" if op == "not_between" else core
    # in / not_in
    if not isinstance(v, (list, tuple)) or not v:
        raise SpecError("in/not_in 값은 비어있지 않은 배열이어야 합니다")
    if any(_is_blank(item) for item in v):
        raise SpecError("in/not_in 값은 비워둘 수 없습니다")
    neg = "not " if op == "not_in" else ""
    if code_strict:
        vals = ", ".join(_str_lit(_code_str(item)) for item in v)
        return f"{s_col} {neg}in ({vals})"
    if numeric_field:
        vals = ", ".join(_lit(_numeric(item)) for item in v)
        return f"{n_col} {neg}in ({vals})"
    if all(_is_num(x) for x in v):
        vals = ", ".join(_lit(x) for x in v)
        return f"{n_col} {neg}in ({vals})"
    vals = ", ".join(_str_lit(x) for x in v)
    return f"{s_col} {neg}in ({vals})"


def _is_group(node: Any) -> bool:
    """그룹 판별 — 'logic' 키 존재가 유일 기준(프론트·pydantic 판별자와 동일 한 문장)."""
    return isinstance(node, dict) and "logic" in node


def _render_filters(nodes: list, fields: dict[str, dict], logic: str = "and") -> str:
    """필터 트리(2단: 최상위 leaf|그룹, 그룹 안은 leaf 전용) → WHERE 본문.

    캐시 호환 불변식: 평면 leaf 배열의 출력은 종전 ' and '.join 과 byte-동일해야 한다 —
    leaf 는 괄호 없이, **그룹만 괄호**로 감싼다(단일 비교식 + 괄호 그룹의 AND/OR 결합은
    우선순위 모호성이 없다). 빈 그룹·초과 그룹은 SpecError.
    """
    if logic not in GROUP_LOGICS:
        raise SpecError(f"허용되지 않는 결합 논리입니다: {logic}")
    leaves = 0
    parts: list[str] = []
    for node in nodes:
        if _is_group(node):
            inner_logic = node.get("logic")
            if inner_logic not in GROUP_LOGICS:
                raise SpecError(f"허용되지 않는 그룹 논리입니다: {inner_logic}")
            children = node.get("filters")
            if not isinstance(children, list) or not children:
                raise SpecError("그룹은 비어있을 수 없습니다 — 조건을 추가하거나 그룹을 삭제하세요")
            if len(children) > MAX_GROUP_CONDITIONS:
                raise SpecError(f"그룹당 조건은 {MAX_GROUP_CONDITIONS}개 이하여야 합니다")
            if any(_is_group(child) for child in children):
                raise SpecError("그룹 안에 그룹은 넣을 수 없습니다(2단 계약)")
            rendered = [_condition(child, fields) for child in children]
            leaves += len(children)
            parts.append(rendered[0] if len(rendered) == 1
                         else "(" + f" {inner_logic} ".join(rendered) + ")")
        else:
            parts.append(_condition(node, fields))
            leaves += 1
        if leaves > MAX_FILTER_LEAVES:
            raise SpecError(f"필터 조건은 총 {MAX_FILTER_LEAVES}개 이하여야 합니다")
    return f" {logic} ".join(parts)


def _having_condition(h: dict, fields: dict[str, dict]) -> str:
    """집계 결과 조건(HAVING) — 집계식은 SELECT 와 **동일한 _measure_expr 화이트리스트**를
    재사용한다(별도 조립 경로를 만들면 SELECT 에서 막힌 금지 집계를 HAVING 으로 우회 가능).
    Trino 는 HAVING 에서 SELECT 별칭 참조가 불가하므로 집계식 원문을 재조립한다."""
    op = h.get("op", "gte")
    if op not in HAVING_OPS:
        raise SpecError(f"집계 조건에 허용되지 않는 연산자입니다: {op}")
    agg = h.get("agg", "count")
    expr = _measure_expr(h.get("field"), agg, fields)
    v = h.get("value")
    if op == "between":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise SpecError("집계 between 값은 [최소, 최대] 형식이어야 합니다")
        lo, hi = _numeric(v[0]), _numeric(v[1])
        return f"{expr} between {_lit(lo)} and {_lit(hi)}"
    sign = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return f"{expr} {sign} {_lit(_numeric(v))}"


def validate_filter(filter_spec: dict, fields: dict[str, dict]) -> None:
    """SQL 실행 없이 필터 leaf 의 필드·연산자·값 모양을 동일 규칙으로 검증한다."""
    _condition(filter_spec, fields)


def validate_filter_tree(nodes: list, fields: dict[str, dict]) -> None:
    """SQL 실행 없이 필터 트리(그룹 포함) 전체를 조회 경로와 같은 워커로 검증한다 —
    저장(레이아웃)과 조회(query)가 서로 다른 walker 를 가지면 계약이 갈라진다."""
    _render_filters(nodes, fields)


def validate_having(having: list, fields: dict[str, dict]) -> None:
    """SQL 실행 없이 집계 조건을 조회 경로와 같은 규칙으로 검증한다."""
    for h in having:
        _having_condition(h, fields)


def build(source: dict, spec: dict) -> str:
    """spec = {dims: [name | {field, bin_width}], measures: [{field?, agg, alias?}], filters, order_by, limit}

    dim 이 {field, bin_width} 형태면 숫자 측정값을 구간(히스토그램) 축으로 그룹핑한다 —
    별칭은 필드명 그대로라 소비자는 일반 dim 과 동일하게 읽는다.
    """
    fields = {f["name"]: f for f in source["fields"]}

    dims = spec.get("dims") or []
    measures = spec.get("measures") or []
    if not measures:
        raise SpecError("measures 가 최소 1개 필요합니다")
    dim_names = [d["field"] if isinstance(d, dict) else d for d in dims]
    if any(not isinstance(n, str) or not n for n in dim_names):
        raise SpecError("차원 형식이 올바르지 않습니다")
    if len(dim_names) != len(set(dim_names)):
        raise SpecError("차원 필드는 서로 달라야 합니다")

    select_parts: list[str] = []
    aliases: list[str] = []
    for d in dims:
        name = d["field"] if isinstance(d, dict) else d
        f = fields.get(name)
        if f is None:
            raise SpecError(f"'{name}' 은(는) 이 소스에 없는 필드입니다")
        if isinstance(d, dict):
            expr = _binned_dim_expr(f, _bin_width(d.get("bin_width")))
        else:
            expr = _dim_expr(f)
        select_parts.append(f"{expr} as {_quote_ident(name)}")
        aliases.append(name)
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

    filters = spec.get("filters") or []
    if filters:
        where = _render_filters(filters, fields, spec.get("filters_logic") or "and")
        if where:
            sql += " where " + where

    if dims:
        sql += " group by " + ", ".join(str(i + 1) for i in range(len(dims)))

    having = spec.get("having") or []
    if having:
        if not dims:
            raise SpecError("집계 조건(having)은 차원(축)이 있는 질의에서만 쓸 수 있습니다")
        sql += " having " + " and ".join(_having_condition(h, fields) for h in having)

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

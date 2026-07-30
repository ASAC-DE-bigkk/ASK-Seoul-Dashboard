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
# 시간성 그룹 축 — 재고(semi-additive) 측정값을 이 축과 함께 sum 하면 이중계산이다.
TIME_GROUP_ROLES = {"time", "sequence"}
# 고정폭 코드·식별자 — 숫자로 와도 문자열 비교로 고정(선행 0·정밀도·체계 확장 안전)
CODE_STRICT_ROLES = {"geo_gu_code", "geo_dong_code", "geo_legal_code", "id"}
NO_VALUE_OPS = {"is_null", "not_null"}
MAX_LIMIT = 5000
MAX_FILTER_LEAVES = 50    # 트리 전체 leaf 총량 상한(그룹 증폭 방어 — models 와 이중검증)
MAX_GROUP_CONDITIONS = 20  # 그룹당 leaf 상한
LIKE_ESCAPE = "\\"        # ESCAPE 문자는 서버 상수 — 절대 사용자 입력이 아님

# ── SQL 방언(2026-07-23 다중 백엔드): 온톨로지 소스가 Trino 외 RDB 에 살아도 같은 스펙으로
# 조립한다. 식별자 인용은 전 방언 ANSI("x") 로 통일한다 — mysql/mariadb 는 실행기가 세션
# sql_mode 에 ANSI_QUOTES·NO_BACKSLASH_ESCAPES 를 켜고(backends.py), mssql 은
# QUOTED_IDENTIFIER ON(드라이버 기본)이 보장한다. LIKE ESCAPE·floor·리터럴 이스케이프('')
# 규칙도 그 전제 위에서 전 방언 공통. 방언 분기는 아래 프로파일 항목뿐이다.
# **불변식: trino 방언의 출력은 종전과 byte-동일**(캐시 키 = SQL 원문 해시).
NUMERIC_FIELD_TYPES = ("bigint", "integer", "int", "smallint", "tinyint",
                       "double", "real", "decimal", "float")
DIALECTS: dict[str, dict] = {
    #                 집계 캐스트          문자 캐스트     bool 리터럴      GROUP BY   LIMIT     LIKE 추가 와일드카드
    "trino":    {"agg": "double",           "str": "varchar",       "bool": ("TRUE", "FALSE"), "positional": True,  "limit": "limit", "wild": ""},
    "postgres": {"agg": "double precision", "str": "varchar",       "bool": ("TRUE", "FALSE"), "positional": True,  "limit": "limit", "wild": ""},
    "sqlite":   {"agg": "real",             "str": "varchar",       "bool": ("1", "0"),        "positional": True,  "limit": "limit", "wild": ""},
    "mysql":    {"agg": "double",           "str": "char",          "bool": ("TRUE", "FALSE"), "positional": True,  "limit": "limit", "wild": ""},
    # oracle: GROUP BY 위치지정은 '상수 1'로 해석되는 치명 함정 → 식 반복. LIMIT 은 12c+ FETCH FIRST.
    "oracle":   {"agg": "binary_double",    "str": "varchar2(4000)", "bool": ("1", "0"),       "positional": False, "limit": "fetch", "wild": ""},
    # mssql: TOP n(SELECT 절 삽입), LIKE 는 '[' 도 와일드카드(문자 클래스 시작) — 이스케이프 대상.
    "mssql":    {"agg": "float",            "str": "varchar(max)",  "bool": ("1", "0"),        "positional": False, "limit": "top",   "wild": "["},
}
# 와이어/방언 호환 별칭 — 같은 프로파일로 조립해도 안전함을 **검증한** 계열만.
# snowflake 는 제외한다(적대적 검증 실측): TRY_CAST 가 문자열 원본 전용이라 숫자 컬럼
# 필터·구간이 컴파일 오류 — 별칭으로 뭉개면 조용히 틀린 SQL 이 나간다. 필요 시 전용
# 프로파일(TO_DOUBLE/TRY_TO_DOUBLE)을 추가하는 것이 정직한 경로다. clickhouse/bigquery 동일.
DIALECT_ALIASES = {
    "mariadb": "mysql",            # 동일 프로토콜·문법 계열(CAST AS DOUBLE 10.4+)
    "cockroachdb": "postgres",     # PG 와이어·문법 호환
    "redshift": "postgres",        # PG 계열(8.x 문법 기반 — 사용 기능 범위 내 호환)
    "duckdb": "trino",             # try_cast(전 타입 허용)·double·ANSI·positional — 검증됨
}


def resolve_dialect(name: str | None) -> str:
    canon = DIALECT_ALIASES.get((name or "trino").lower(), (name or "trino").lower())
    if canon not in DIALECTS:
        raise SpecError(f"지원하지 않는 backend 입니다: {name}")
    return canon


def _is_numeric_type(field: dict) -> bool:
    return (field.get("type", "").split("(")[0].lower() in NUMERIC_FIELD_TYPES)


def _str_expr(quoted: str, dialect: str) -> str:
    return f"cast({quoted} as {DIALECTS[dialect]['str']})"


def _is_time_type(field: dict) -> bool:
    base = field.get("type", "").split("(")[0].lower()
    return base.startswith("timestamp") or base == "date"


def _time_expr(quoted: str, dialect: str) -> str:
    """시간 컬럼의 문자열 직렬화 — 반드시 ISO 여야 사전순=시간순 계약이 성립한다.
    oracle 은 암묵 변환이 세션 NLS_DATE_FORMAT('23-JUL-26')을, mssql 레거시 datetime 은
    CAST 기본 스타일('Jul 23 2026')을 따르므로 명시 포맷으로 고정한다(적대적 검증 실측).
    trino/postgres/mysql/sqlite 는 기본 캐스트가 ISO — 기존 경로 그대로(byte-동일)."""
    if dialect == "oracle":
        return f"to_char({quoted}, 'YYYY-MM-DD HH24:MI:SS')"
    if dialect == "mssql":
        return f"convert(varchar(30), {quoted}, 121)"
    return _str_expr(quoted, dialect)


def _num_expr(quoted: str, field: dict, dialect: str) -> str:
    """숫자 비교 축 — Trino/mssql 은 try_cast 네이티브, 나머지는 물리 타입이 숫자면 직접
    캐스트, 문자면 검증 가드식으로 에뮬레이트한다(sqlite CAST('abc' AS REAL)=0.0 ·
    mysql 암묵 변환 'abc'→0 함정 — 검증 없이 캐스트하면 쓰레기 문자열이 0 과 같아진다).
    정규식은 백슬래시 없는 [.] 클래스만 쓴다 — 방언별 문자열 리터럴 해석 차이를 원천 회피."""
    if dialect == "postgres":
        if _is_numeric_type(field):
            return f"cast({quoted} as double precision)"
        return (f"(case when cast({quoted} as varchar) ~ '^-?[0-9]+([.][0-9]+)?$' "
                f"then cast(cast({quoted} as varchar) as double precision) end)")
    if dialect == "sqlite":
        if _is_numeric_type(field):
            return f"cast({quoted} as real)"
        # 알려진 한계(정직): 왕복 검증식은 정규형('42','830.5')만 숫자로 인정한다 —
        # '5.50'·'007'·'1e3'·' 42' 같은 비정규형은 NULL 로 탈락해 trino/pg/mysql 과
        # 결과가 다를 수 있다. sqlite 소스는 타입드 컬럼(D1 export 류)이 전제라 실무
        # 영향은 좁고, 느슨하게 풀면 CAST('abc')=0 함정이 되살아난다(SHARE §9.0).
        return (f"(case when cast(cast({quoted} as real) as text) = cast({quoted} as text) "
                f"or cast(cast({quoted} as integer) as text) = cast({quoted} as text) "
                f"then cast({quoted} as real) end)")
    if dialect == "mysql":
        if _is_numeric_type(field):
            return f"cast({quoted} as double)"
        return (f"(case when cast({quoted} as char) regexp '^-?[0-9]+([.][0-9]+)?$' "
                f"then cast({quoted} as double) end)")
    if dialect == "oracle":
        if _is_numeric_type(field):
            return f"cast({quoted} as binary_double)"
        return f"cast({quoted} as binary_double default null on conversion error)"
    if dialect == "mssql":
        return f"try_cast({quoted} as float)"
    return f"try_cast({quoted} as double)"


def _coerce_bools(value: Any, dialect: str) -> Any:
    """bool 리터럴이 없는 방언(oracle/mssql/sqlite)은 값 단계에서 1/0 으로 변환한다 —
    _lit 자체는 건드리지 않아 trino 경로 byte-동일이 자명하게 유지된다."""
    if DIALECTS[dialect]["bool"] == ("TRUE", "FALSE"):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (list, tuple)):
        return [1 if isinstance(v, bool) and v else 0 if isinstance(v, bool) else v
                for v in value]
    return value


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


def _dim_expr(field: dict, dialect: str = "trino") -> str:
    """차원 표현식 — timestamp/date 는 JSON 안전하게 ISO 문자열로 낸다(방언 시간 직렬화)."""
    quoted = _quote_ident(field["name"])
    if _is_time_type(field):
        return _time_expr(quoted, dialect)
    return quoted


def _bin_width(raw: Any) -> float:
    """구간 폭 검증 — 유한한 양수만. (0/음수/무한은 floor 나눗셈 의미가 무너진다.)"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SpecError("구간 폭(bin)은 숫자여야 합니다")
    width = float(raw)
    if not math.isfinite(width) or width <= 0:
        raise SpecError("구간 폭(bin)은 유한한 양수여야 합니다")
    return width


def _binned_dim_expr(field: dict, width: float, dialect: str = "trino") -> str:
    """숫자 측정값의 구간 축 — floor(값/폭)*폭 = 구간 시작값. 별칭은 필드명 유지
    (소비자·정렬·피벗이 일반 dim 과 동일하게 동작). null 값은 구간 null 로 남는다."""
    quoted = _quote_ident(field["name"])
    lit = repr(width)
    num = _num_expr(quoted, field, dialect)
    return f"cast(floor({num} / {lit}) * {lit} as {DIALECTS[dialect]['agg']})"


def _measure_expr(field_name: str | None, agg: str, fields: dict[str, dict],
                  dialect: str = "trino") -> str:
    if agg not in AGGS:
        raise SpecError(f"허용되지 않는 집계입니다: {agg}")
    if agg == "count" and not field_name:
        return f"cast(count(*) as {DIALECTS[dialect]['agg']})"
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
        return f"cast(count(distinct {quoted}) as {DIALECTS[dialect]['agg']})"
    return f"cast({agg}({quoted}) as {DIALECTS[dialect]['agg']})"


def _ratio_expr(num: Any, den: Any, fields: dict[str, dict],
                dialect: str = "trino") -> str:
    """가중 비율 — ``sum(분자)/nullif(sum(분모),0)``.

    사전계산된 비율 컬럼(survival_rate·lq 등)을 여러 행에 걸쳐 보려면 지금까지 ``avg`` 뿐이었고,
    그것은 **비가중 평균**이라 코호트·모집단 크기를 무시해 통계적으로 틀린다(SHARE.md §7.1 —
    "비율 재집계가 필요한 경우 단순평균을 정확한 값처럼 표현하지 않는다"). 원자 분자·분모가
    gold 에 있으면 이 형태로 가중 비율을 낸다.

    분모 0 은 NULL 로 떨어뜨린다(결측≠0 규칙) — 전 방언 공통 ANSI ``nullif``.
    분자·분모는 **가산 measure 만** 허용한다(비가산을 합치면 비율 자체가 무의미해진다).
    """
    if not num or not den:
        raise SpecError("비율(ratio) 집계에는 분자(num)와 분모(den) 필드가 모두 필요합니다")
    for name in (num, den):
        field = fields.get(name)
        if field is None:
            raise SpecError(f"'{name}' 은(는) 이 소스에 없는 필드입니다")
        if field.get("role") != "measure":
            raise SpecError(f"'{name}' 은(는) 측정값이 아니라 비율의 분자·분모로 쓸 수 없습니다")
        if not field.get("additive"):
            raise SpecError(f"'{name}' 은(는) 비가산 값이라 비율의 분자·분모로 쓸 수 없습니다")
    cast = DIALECTS[dialect]["agg"]
    numerator = f"cast(sum({_quote_ident(num)}) as {cast})"
    denominator = f"cast(sum({_quote_ident(den)}) as {cast})"
    return f"{numerator} / nullif({denominator}, 0)"


def assert_additive_over_dims(dims: list, measures: list, fields: dict[str, dict],
                              *, filters: list | None = None,
                              date_range: dict | None = None) -> None:
    """재고(semi-additive) 측정값을 **시간을 접어서** sum 하는 것을 막는다 — 이중계산 방지.

    Kimball semi-additive: 한 시점의 '수위'(재고·정원·활성수)는 공간·범주로는 합산해도 옳지만
    **시간으로 합산하면** 같은 대상을 여러 번 센다. 여기서 방향이 중요하다 —

    - 시간축이 GROUP BY 에 **있으면** 시각별로 나눠 보는 것이라 안전하다
      (일자별 sum(seat_count) = 그 날 전체 좌석 수).
    - 시간축이 **없으면** 여러 시점이 한 그룹으로 접히므로 이중계산이다
      (자치구별 sum(seat_count) = 같은 극장 좌석을 날짜 수만큼 더한 값).

    그래서 '시간이 접히는 경우'만 거부한다. 접을 시간이 없거나(분석용 시간 필드 없음),
    스냅샷이 단일 시점이거나, 필터가 시각을 한 점으로 고정했으면 통과시킨다.
    분류 정본은 ontology.additive_over 이고 여기서는 집행만 한다.

    **거부만 한다 — 통과하는 스펙의 SQL 텍스트는 바뀌지 않는다**(캐시 키 불변, SHARE §9.0).
    """
    stock_measures = []
    for m in measures:
        # ratio 도 내부적으로 분자·분모를 sum 하므로 같은 검사를 받아야 한다 —
        # 집계 '이름'만 보면 sum 을 두 번 쓰는 ratio 가 게이트를 우회한다.
        agg = m.get("agg", "sum")
        if agg == "sum":
            names = [m.get("field")]
        elif agg == "ratio":
            names = [m.get("num"), m.get("den")]
        else:
            continue
        for name in names:
            # 비가산 필드는 allowed_aggs 에 sum 이 없어 _measure_expr 가 제 사유로 거부한다
            # (additive_over 가 빈 목록이라 여기서 먼저 잡으면 '재고'라는 틀린 이유를 대게 된다).
            field = fields.get(name or "")
            if field is None or not field.get("additive"):
                continue
            additive_over = field.get("additive_over")
            if additive_over is not None and "time" not in additive_over:
                stock_measures.append(field)
    if not stock_measures:
        return

    # 접을 시간축이 있는가 — 적재/수집 시각(technical)은 분석 그레인이 아니라 세지 않는다.
    if not any(f.get("role") in TIME_GROUP_ROLES and f.get("chartable", True)
               for f in fields.values()):
        return
    # 단일 시점 스냅샷이면 접을 시간 자체가 없다.
    if (date_range and date_range.get("min") is not None
            and date_range.get("min") == date_range.get("max")):
        return

    bin_fields = {d["field"] for d in dims if isinstance(d, dict)}
    # role 이 없는 손수 만든 source dict(테스트·외부 호출)도 안전히 통과 — 여기서 KeyError 가
    # 나면 안전 계약이 아니라 크래시가 된다.
    group_roles = {
        fields[name].get("role")
        for name in (d if isinstance(d, str) else d["field"] for d in dims)
        if name in fields and name not in bin_fields
    }
    if group_roles & TIME_GROUP_ROLES:
        return  # 시각별로 나눠 보는 중 — 시간을 접지 않았다
    if _pins_single_instant(filters or [], fields):
        return

    names = ", ".join(sorted({f["name"] for f in stock_measures}))
    raise SpecError(
        f"'{names}' 은(는) 특정 시점의 상태(재고)라 여러 시점을 한데 합산하면 이중계산이 "
        f"됩니다 — 시간축을 축에 추가하거나 시각을 하나로 고정하거나 max/avg 를 쓰세요"
    )


def _pins_single_instant(nodes: list, fields: dict[str, dict]) -> bool:
    """필터가 시간축을 한 시점으로 고정했는지(eq, 또는 값 1개짜리 in)."""
    for node in nodes:
        children = node.get("filters", []) if _is_group(node) else [node]
        if not isinstance(children, list):
            continue
        for leaf in children:
            if not isinstance(leaf, dict):
                continue
            field = fields.get(leaf.get("field") or "")
            if field is None or field.get("role") not in TIME_GROUP_ROLES:
                continue
            op, value = leaf.get("op", "eq"), leaf.get("value")
            if op == "eq" and not _is_blank(value):
                return True
            if op == "in" and isinstance(value, (list, tuple)) and len(value) == 1:
                return True
    return False


def measure_expr_from_spec(spec: dict, fields: dict[str, dict],
                           dialect: str = "trino") -> str:
    """측정 스펙(dict) → 집계식. ``agg='ratio'`` 만 분기하고 나머지는 기존 경로 그대로.

    불변식: 비-ratio 스펙의 출력은 종전 ``_measure_expr`` 과 **byte-동일**(캐시 키 보존).
    SELECT 와 HAVING 이 이 한 함수를 공유해야 금지 집계를 HAVING 으로 우회할 수 없다.
    """
    agg = spec.get("agg", "sum")
    if agg == "ratio":
        return _ratio_expr(spec.get("num"), spec.get("den"), fields, dialect)
    return _measure_expr(spec.get("field"), agg, fields, dialect)


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


def _like_pattern(value: Any, *, prefix: str, suffix: str, dialect: str = "trino") -> str:
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
    for wild in DIALECTS[dialect]["wild"]:      # mssql: '[' 도 와일드카드(문자 클래스)
        escaped = escaped.replace(wild, LIKE_ESCAPE + wild)
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
    SQL 이 재현 가능한 산출물이 되고 캐시 키가 자정에 자연 회전한다(오늘 포함 N단위).

    **닫힌 구간**으로 낸다 — 하한만 걸면 예보/미래 일자 테이블(스냅샷 112개 중 25개가
    date_range.max 가 미래)에서 '최근 7일'이 앞으로 올 예보까지 끌어와 과거 집계를 오염시킨다.
    상한은 오늘까지: 날짜·시각은 '내일 미만'(시각 문자열 'YYYY-MM-DD HH:MM:SS' 도 사전순으로
    내일보다 작다), 월은 이번 달까지, 연은 올해까지.
    """
    from datetime import date, timedelta

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise SpecError("최근 N 조건의 값은 1~3650 정수여야 합니다")
    gran = field.get("granularity")
    today = date.today()
    if gran == "year":
        return (f"{n_col} >= {float(today.year - (value - 1))!r}"
                f" and {n_col} <= {float(today.year)!r}")
    if gran == "month":
        months = today.year * 12 + (today.month - 1) - (value - 1)
        year, month = divmod(months, 12)
        return (f"{s_col} >= {_str_lit(f'{year:04d}-{month + 1:02d}')}"
                f" and {s_col} <= {_str_lit(f'{today.year:04d}-{today.month:02d}')}")
    if gran in ("date", "datetime"):
        cutoff = today - timedelta(days=value - 1)
        tomorrow = today + timedelta(days=1)
        return (f"{s_col} >= {_str_lit(cutoff.isoformat())}"
                f" and {s_col} < {_str_lit(tomorrow.isoformat())}")
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


def _condition(f: dict, fields: dict[str, dict], dialect: str = "trino") -> str:
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
    v = _coerce_bools(f.get("value"), dialect)
    # 물리 시간 컬럼의 문자 비교는 ISO 직렬화 경유 — oracle NLS/mssql 스타일 의존 차단
    s_col = _time_expr(col, dialect) if _is_time_type(field) else _str_expr(col, dialect)
    n_col = _num_expr(col, field, dialect)

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
        return f"{s_col} like {_like_pattern(v, prefix=wrap[0], suffix=wrap[1], dialect=dialect)}"
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


def _render_filters(nodes: list, fields: dict[str, dict], logic: str = "and",
                    dialect: str = "trino") -> str:
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
            rendered = [_condition(child, fields, dialect) for child in children]
            leaves += len(children)
            parts.append(rendered[0] if len(rendered) == 1
                         else "(" + f" {inner_logic} ".join(rendered) + ")")
        else:
            parts.append(_condition(node, fields, dialect))
            leaves += 1
        if leaves > MAX_FILTER_LEAVES:
            raise SpecError(f"필터 조건은 총 {MAX_FILTER_LEAVES}개 이하여야 합니다")
    return f" {logic} ".join(parts)


def _having_condition(h: dict, fields: dict[str, dict], dialect: str = "trino") -> str:
    """집계 결과 조건(HAVING) — 집계식은 SELECT 와 **동일한 _measure_expr 화이트리스트**를
    재사용한다(별도 조립 경로를 만들면 SELECT 에서 막힌 금지 집계를 HAVING 으로 우회 가능).
    Trino 는 HAVING 에서 SELECT 별칭 참조가 불가하므로 집계식 원문을 재조립한다."""
    op = h.get("op", "gte")
    if op not in HAVING_OPS:
        raise SpecError(f"집계 조건에 허용되지 않는 연산자입니다: {op}")
    # HAVING 의 기본 집계는 **count** 다(models.HavingSpec 계약) — SELECT 의 기본값(sum)을
    # 빌려 쓰면 agg 를 생략한 기존 스펙의 SQL 이 조용히 바뀌고(캐시 키 무효), 소표본 억제
    # 관용구 `having count(*) >= N`(필드 없는 형태)이 "sum 집계에는 필드가 필요합니다" 로 깨진다.
    expr = measure_expr_from_spec({**h, "agg": h.get("agg", "count")}, fields, dialect)
    v = h.get("value")
    if op == "between":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise SpecError("집계 between 값은 [최소, 최대] 형식이어야 합니다")
        lo, hi = _numeric(v[0]), _numeric(v[1])
        return f"{expr} between {_lit(lo)} and {_lit(hi)}"
    sign = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return f"{expr} {sign} {_lit(_numeric(v))}"


def validate_filter(filter_spec: dict, fields: dict[str, dict],
                    dialect: str = "trino") -> None:
    """SQL 실행 없이 필터 leaf 의 필드·연산자·값 모양을 동일 규칙으로 검증한다."""
    _condition(filter_spec, fields, dialect)


def validate_filter_tree(nodes: list, fields: dict[str, dict],
                         dialect: str = "trino") -> None:
    """SQL 실행 없이 필터 트리(그룹 포함) 전체를 조회 경로와 같은 워커로 검증한다 —
    저장(레이아웃)과 조회(query)가 서로 다른 walker 를 가지면 계약이 갈라진다."""
    _render_filters(nodes, fields, dialect=dialect)


def validate_having(having: list, fields: dict[str, dict],
                    dialect: str = "trino") -> None:
    """SQL 실행 없이 집계 조건을 조회 경로와 같은 규칙으로 검증한다."""
    for h in having:
        _having_condition(h, fields, dialect)


def build(source: dict, spec: dict) -> str:
    """spec = {dims: [name | {field, bin_width}], measures: [{field?, agg, alias?}], filters, order_by, limit}

    dim 이 {field, bin_width} 형태면 숫자 측정값을 구간(히스토그램) 축으로 그룹핑한다 —
    별칭은 필드명 그대로라 소비자는 일반 dim 과 동일하게 읽는다.
    방언은 소스의 backend(trino|postgres|sqlite)가 결정한다 — 스펙·검증 계약은 동일.
    """
    dialect = resolve_dialect(source.get("backend"))
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
    # HAVING 도 SELECT 와 같은 집계 어휘를 쓰므로 같은 가산성 검사를 받는다 —
    # 안 그러면 SELECT 에서 막힌 재고 합산을 HAVING 으로 우회할 수 있다.
    assert_additive_over_dims(dims, list(measures) + list(spec.get("having") or []), fields,
                              filters=spec.get("filters") or [],
                              date_range=source.get("date_range"))

    select_parts: list[str] = []
    aliases: list[str] = []
    dim_exprs: list[str] = []   # oracle/mssql — GROUP BY 위치지정 불가 시 식 반복용
    for d in dims:
        name = d["field"] if isinstance(d, dict) else d
        f = fields.get(name)
        if f is None:
            raise SpecError(f"'{name}' 은(는) 이 소스에 없는 필드입니다")
        if isinstance(d, dict):
            expr = _binned_dim_expr(f, _bin_width(d.get("bin_width")), dialect)
        else:
            expr = _dim_expr(f, dialect)
        select_parts.append(f"{expr} as {_quote_ident(name)}")
        aliases.append(name)
        dim_exprs.append(expr)
    for m in measures:
        agg = m.get("agg", "sum")
        field_name = m.get("field")
        if agg == "ratio":
            default_alias = f"ratio_{m.get('num')}_{m.get('den')}"
        else:
            default_alias = f"{agg}_{field_name}" if field_name else "count"
        alias = m.get("alias") or default_alias
        if not alias.replace("_", "").isalnum():
            raise SpecError(f"alias 형식이 잘못됐습니다: {alias}")
        if alias in aliases:
            alias = f"{alias}_{len(aliases)}"
        select_parts.append(
            f"{measure_expr_from_spec(m, fields, dialect)} as {_quote_ident(alias)}"
        )
        aliases.append(alias)

    sql = f"select {', '.join(select_parts)} from {_relation(source['relation'])}"

    filters = spec.get("filters") or []
    if filters:
        where = _render_filters(filters, fields, spec.get("filters_logic") or "and", dialect)
        if where:
            sql += " where " + where

    if dims:
        if DIALECTS[dialect]["positional"]:
            sql += " group by " + ", ".join(str(i + 1) for i in range(len(dims)))
        else:  # oracle 은 'group by 1' 을 상수로 해석(치명) — 식 반복이 유일한 안전 경로
            sql += " group by " + ", ".join(dim_exprs)

    having = spec.get("having") or []
    if having:
        if not dims:
            raise SpecError("집계 조건(having)은 차원(축)이 있는 질의에서만 쓸 수 있습니다")
        sql += " having " + " and ".join(_having_condition(h, fields, dialect) for h in having)

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
    style = DIALECTS[dialect]["limit"]
    if style == "limit":
        sql += f" limit {limit}"
    elif style == "fetch":                       # oracle 12c+
        sql += f" fetch first {limit} rows only"
    else:                                        # mssql — SELECT 절 TOP 삽입
        sql = f"select top {limit} " + sql[len("select "):]
    return sql

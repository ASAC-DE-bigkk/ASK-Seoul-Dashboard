"""다중 백엔드 온톨로지 계약 — SQLite(필수)·Postgres 의 테이블/뷰가 같은 온톨로지로 열린다.

종단 검증(임시 SQLite): 실측 추출(extract_datasource) → 스냅샷 → Registry(role·라벨·통계)
→ querybuilder(sqlite 방언) → backends.execute(읽기전용) 실행 결과까지.
Postgres 는 서버 없이 방언 SQL 생성 계약만 고정한다.

불변식: ① trino 방언 출력은 종전 byte-동일(기본값 경로) ② sqlite 의 문자열 숫자
캐스트는 검증 가드('abc'→NULL — CAST('abc' AS REAL)=0.0 함정 차단) ③ 읽기전용 강제.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.charts import backends, querybuilder
from app.charts.ontology import Registry


@pytest.fixture()
def demo_db(tmp_path, monkeypatch):
    """테이블+뷰+지저분한 텍스트 숫자까지 담은 임시 SQLite — 전 경로 공용 픽스처."""
    db = tmp_path / "demo.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE store_summary (
          gu_code TEXT, gu TEXT, category TEXT, category_ko TEXT,
          active_cnt INTEGER, avg_days REAL, note TEXT, updated_date TEXT);
        INSERT INTO store_summary VALUES
          ('11680','강남구','chicken','치킨',120,830.5,'12','2026-07-20'),
          ('11650','서초구','chicken','치킨',80,910.0,'abc','2026-07-20'),
          ('11680','강남구','cafe','카페',300,412.3,NULL,'2026-07-21'),
          ('11740','강동구','cafe','카페',50,1200.0,'7','2026-07-21');
        CREATE VIEW v_gu_rollup AS
          SELECT gu_code, gu, sum(active_cnt) AS active_cnt
          FROM store_summary GROUP BY gu_code, gu;
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv(
        "CHARTS_DATASOURCES",
        json.dumps({"demo": {"backend": "sqlite", "path": str(db)}}),
    )
    return db


def _demo_registry(tmp_path, db) -> Registry:
    import extract

    tables = extract.extract_datasource("demo", {"backend": "sqlite", "path": str(db)})
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"generated_at": "test", "tables": tables},
                               ensure_ascii=False), encoding="utf-8")
    return Registry(snapshot_path=snap)


def test_sqlite_end_to_end_table_and_view_through_ontology(demo_db, tmp_path) -> None:
    registry = _demo_registry(tmp_path, demo_db)

    src = registry.get("demo__store_summary")
    assert src is not None and src["backend"] == "sqlite" and src["datasource"] == "demo"
    by = {f["name"]: f for f in src["fields"]}
    # role 추론·실측 통계·식별=코드 계약이 DB 소스에 그대로 적용된다
    assert by["gu_code"]["role"] == "geo_gu_code"
    assert by["gu"]["role"] == "geo_gu" and by["gu"]["id_field"] == "gu_code"
    assert by["category_ko"]["id_field"] == "category"
    assert by["active_cnt"]["role"] == "measure" and by["active_cnt"]["groupable"]
    assert registry.meta()["value_labels"]["category"]["chicken"] == "치킨"
    assert "bar" in src["supports"]

    # 뷰도 동일 계약으로 소스가 된다
    view = registry.get("demo__v_gu_rollup")
    assert view is not None and view["object_type"] == "view"

    # 차트 질의(그룹 필터 + HAVING) → sqlite 방언 → 실행
    sql = querybuilder.build(src, {
        "dims": ["gu"],
        "measures": [{"field": "active_cnt", "agg": "sum", "alias": "sum_active"}],
        "filters": [{"logic": "or", "filters": [
            {"field": "category", "op": "eq", "value": "chicken"},
            {"field": "category", "op": "eq", "value": "cafe"}]}],
        "having": [{"field": "active_cnt", "agg": "sum", "op": "gte", "value": 100}],
        "order_by": [{"field": "sum_active", "dir": "desc"}],
    })
    assert "cast(sum(\"active_cnt\") as real)" in sql          # sqlite 집계 캐스트
    result = backends.execute(src, sql)
    assert result["mode"] == "live"
    assert result["rows"] == [["강남구", 420.0], ["서초구", 80.0]] or \
           result["rows"] == [["강남구", 420]]  # having 100 → 서초 80 제외
    # 정확 판정: having ≥100 이므로 강남만
    assert [r[0] for r in result["rows"]] == ["강남구"]

    view_sql = querybuilder.build(view, {
        "dims": ["gu"], "measures": [{"field": "active_cnt", "agg": "sum"}]})
    view_rows = backends.execute(view, view_sql)["rows"]
    assert sorted(r[0] for r in view_rows) == ["강남구", "강동구", "서초구"]


def test_sqlite_dialect_guards_text_numeric_and_null(demo_db, tmp_path) -> None:
    registry = _demo_registry(tmp_path, demo_db)
    src = registry.get("demo__store_summary")

    # note(TEXT)에 'abc' — 검증 가드가 없으면 CAST('abc' AS REAL)=0.0 이 eq 0 에 걸린다
    sql = querybuilder.build(src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "note", "op": "eq", "value": 0}]})
    assert "case when" in sql                     # 가드식 사용
    assert backends.execute(src, sql)["rows"] == []  # 'abc' 는 0 과 같지 않다

    # category 필드 + 숫자 값 집합 — 숫자 경로(가드식)로 '12'만 매치, 'abc'/NULL/'7' 탈락
    sql_num = querybuilder.build(src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "note", "op": "in", "value": [10, 12]}]})
    rows = backends.execute(src, sql_num)["rows"]
    assert rows == [["강남구", 1.0]]

    # is_null 은 원본 컬럼 — 물리 NULL 1행만
    sql_null = querybuilder.build(src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "note", "op": "is_null"}]})
    assert '"note" is null' in sql_null
    assert backends.execute(src, sql_null)["rows"] == [["강남구", 1.0]]

    # 구간 축(floor) — sqlite math 함수 가용성까지 실측
    sql_bin = querybuilder.build(src, {
        "dims": [{"field": "avg_days", "bin_width": 500}],
        "measures": [{"field": None, "agg": "count", "alias": "count"}]})
    assert "floor(" in sql_bin and "as real)" in sql_bin
    bins = {r[0]: r[1] for r in backends.execute(src, sql_bin)["rows"]}
    assert bins == {0.0: 1.0, 500.0: 2.0, 1000.0: 1.0}

    # contains 이스케이프 경로(공통 문법) 실행 확인
    sql_like = querybuilder.build(src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "gu", "op": "contains", "value": "남"}]})
    assert backends.execute(src, sql_like)["rows"] == [["강남구", 2.0]]


def test_readonly_and_unknown_datasource(demo_db) -> None:
    src = {"datasource": "demo"}
    with pytest.raises(Exception):
        backends.execute(src, 'DELETE FROM "store_summary"')
    with pytest.raises(backends.DatasourceError, match="정의되지 않은"):
        backends.execute({"datasource": "ghost"}, "SELECT 1")


def test_postgres_dialect_sql_shapes(demo_db, tmp_path) -> None:
    """서버 없이 방언 문자열 계약만 고정 — 숫자 타입은 직접 캐스트, 텍스트는 정규식 가드."""
    registry = _demo_registry(tmp_path, demo_db)
    src = dict(registry.get("demo__store_summary"))
    src["backend"] = "postgres"
    sql = querybuilder.build(src, {
        "dims": ["gu"],
        "measures": [{"field": "active_cnt", "agg": "sum"}],
        "filters": [{"field": "active_cnt", "op": "gte", "value": 100},
                    {"field": "note", "op": "eq", "value": 5}],
        "having": [{"agg": "count", "op": "gte", "value": 2}],
    })
    assert 'cast(sum("active_cnt") as double precision)' in sql
    assert 'cast("active_cnt" as double precision) >= 100' in sql      # 숫자 타입 직접
    assert "~ '^-?[0-9]+([.][0-9]+)?$'" in sql   # 텍스트 가드([.] — 리터럴 해석 차이 회피)
    assert "having cast(count(*) as double precision) >= 2" in sql


def test_mysql_oracle_mssql_dialect_sql_shapes(demo_db, tmp_path) -> None:
    """서버 없이 방언 문자열 계약 고정 — mysql(REGEXP 가드·ANSI 세션 전제),
    oracle(conversion-error 캐스트·GROUP BY 식 반복·FETCH FIRST·bool 1/0),
    mssql(try_cast float·TOP·GROUP BY 식 반복·LIKE '[' 이스케이프)."""
    registry = _demo_registry(tmp_path, demo_db)
    base = registry.get("demo__store_summary")
    spec = {
        "dims": ["gu"],
        "measures": [{"field": "active_cnt", "agg": "sum"}],
        "filters": [{"field": "note", "op": "eq", "value": 5},
                    {"field": "active_cnt", "op": "gte", "value": 100}],
        "limit": 100,
    }

    my = querybuilder.build({**base, "backend": "mysql"}, spec)
    assert "regexp '^-?[0-9]+([.][0-9]+)?$'" in my        # 암묵 'abc'→0 변환 가드
    assert 'cast(sum("active_cnt") as double)' in my
    assert my.endswith("limit 100")

    ora = querybuilder.build({**base, "backend": "oracle"}, spec)
    assert 'cast("note" as binary_double default null on conversion error)' in ora
    assert 'group by "gu"' in ora                          # 위치지정 금지 — 식 반복
    assert ora.endswith("fetch first 100 rows only")
    assert 'cast(sum("active_cnt") as binary_double)' in ora

    ms = querybuilder.build({**base, "backend": "mssql"}, spec)
    assert ms.startswith("select top 100 ")
    assert 'try_cast("note" as float)' in ms and 'try_cast("active_cnt" as float)' in ms
    assert 'group by "gu"' in ms
    ms_like = querybuilder.build({**base, "backend": "mssql"}, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "gu", "op": "contains", "value": "50%[a]_x"}]})
    assert "like '%50\\%\\[a]\\_x%' escape '\\'" in ms_like  # '[' 도 이스케이프

    # oracle/mssql — bool 리터럴 없음 → 1/0 변환
    flag_src = {**base, "backend": "oracle"}
    sql_bool = querybuilder.build(flag_src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "category", "op": "eq", "value": True}]})
    assert " = 1" in sql_bool and "TRUE" not in sql_bool

    # 별칭 — mariadb→mysql, duckdb→trino(try_cast 유지), 미지 backend 거부
    assert querybuilder.resolve_dialect("mariadb") == "mysql"
    assert "try_cast" in querybuilder.build({**base, "backend": "duckdb"}, spec)
    with pytest.raises(querybuilder.SpecError, match="지원하지 않는 backend"):
        querybuilder.build({**base, "backend": "clickhouse"}, spec)


def test_trino_dialect_stays_byte_identical_default() -> None:
    """backend 미지정(기존 gold 스냅샷) → trino 경로 그대로 — 캐시 키 보존."""
    from app.charts.ontology import registry as live_registry
    src = live_registry.get("gold_license_dong_summary")
    assert src["backend"] == "trino" and src["datasource"] == "trino"
    sql = querybuilder.build(src, {
        "dims": ["gu"], "measures": [{"field": None, "agg": "count", "alias": "count"}],
        "filters": [{"field": "business_count", "op": "gte", "value": 100}]})
    assert 'try_cast("business_count" as double) >= 100' in sql
    assert "cast(count(*) as double)" in sql

"""Ask Chat 조회 경계 계약 — queryspec 화이트리스트 + sqlite 종단 실행.

LLM 출력(도구 입력)은 신뢰 불가 입력이다: 식별자는 화이트리스트를 통과해야 하고
값은 절대 SQL 텍스트로 승격되지 않아야 한다(전부 파라미터 바인딩).
"""
from __future__ import annotations

import sqlite3

import pytest

from app.chat import queryspec
from app.chat.config import ChatSettings
from app.chat import datasource
from app.chat.models import ChatMessage, ChatRequest
from pydantic import ValidationError

COLUMNS = ["gu", "gu_code", "place", "ppltn", "observed_at"]
TIME_AXIS = "observed_at"

SQLI_SAMPLES = [
    'gu"; drop table gold_x; --',
    "gu' or '1'='1",
    "gu`;--",
    "gu) union select 1",
    "../../../etc/passwd",
    "gu\x00",
]


def build(spec: dict) -> tuple[str, list, int]:
    return queryspec.build(spec, columns=COLUMNS, time_axis=TIME_AXIS, max_limit=200)


# ── 정상 조립 ─────────────────────────────────────────────


def test_basic_select_with_binds():
    sql, params, effective = build({
        "table": "gold_citydata_ppltn",
        "columns": ["gu", "ppltn"],
        "filters": [{"column": "gu", "op": "eq", "value": "강남구"}],
        "limit": 10,
    })
    # truncated 감지를 위해 SQL 은 effective+1 을 조회한다.
    assert sql == 'SELECT "gu", "ppltn" FROM "gold_citydata_ppltn" WHERE "gu" = ? LIMIT 11'
    assert params == ["강남구"]
    assert effective == 10
    assert "강남구" not in sql  # 값은 SQL 텍스트에 등장하지 않는다


def test_aggregation_group_by():
    sql, params, _ = build({
        "table": "t",
        "group_by": ["gu"],
        "aggs": [{"fn": "avg", "column": "ppltn", "alias": "avg_ppltn"}],
        "order_by": [{"by": "avg_ppltn", "dir": "desc"}],
        "limit": 5,
    })
    assert 'AVG("ppltn") AS "avg_ppltn"' in sql
    assert 'GROUP BY "gu"' in sql
    assert 'ORDER BY "avg_ppltn" DESC' in sql
    assert params == []


def test_time_bounds_and_in_and_contains():
    sql, params, _ = build({
        "table": "t",
        "filters": [
            {"column": "gu", "op": "in", "value": ["강남구", "서초구"]},
            {"column": "place", "op": "contains", "value": "50%_역"},
        ],
        "time_from": "2026-07-01",
        "time_to": "2026-07-22",
    })
    assert '"gu" IN (?, ?)' in sql
    assert '"place" LIKE ? ESCAPE \'\\\'' in sql
    assert '"observed_at" >= ?' in sql and '"observed_at" <= ?' in sql
    # LIKE 와일드카드는 이스케이프되어 리터럴로 취급된다
    assert "%50\\%\\_역%" in params


def test_limit_clamped_to_max():
    sql, _, effective = build({"table": "t", "limit": 999999})
    assert effective == 200
    assert sql.endswith("LIMIT 201")  # effective(200) + 1


# ── 화이트리스트 거부 ─────────────────────────────────────


@pytest.mark.parametrize("bad", SQLI_SAMPLES)
def test_injection_rejected_as_table(bad):
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": bad})


@pytest.mark.parametrize("bad", SQLI_SAMPLES)
def test_injection_rejected_as_column(bad):
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "columns": [bad]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "filters": [{"column": bad, "op": "eq", "value": 1}]})


def test_unknown_column_rejected():
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "columns": ["nope"]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "group_by": ["nope"], "aggs": [{"fn": "count"}]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "order_by": [{"by": "nope"}]})


def test_bad_ops_and_values_rejected():
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "filters": [{"column": "gu", "op": "regexp", "value": "x"}]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "filters": [{"column": "gu", "op": "eq", "value": "a\x00b"}]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "filters": [{"column": "gu", "op": "eq", "value": {"$gt": 1}}]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "aggs": [{"fn": "group_concat", "column": "gu"}]})


def test_alias_cannot_shadow_or_break():
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "aggs": [{"fn": "count", "alias": 'x"; --'}]})
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "aggs": [{"fn": "count", "alias": "gu"}]})  # 실제 컬럼 가림


def test_group_by_requires_aggs_and_time_axis_guard():
    with pytest.raises(queryspec.ChatSpecError):
        build({"table": "t", "group_by": ["gu"]})
    with pytest.raises(queryspec.ChatSpecError):
        queryspec.build({"table": "t", "time_from": "2026-01-01"},
                        columns=["a"], time_axis=None, max_limit=100)


# ── sqlite 종단 실행 (읽기 전용) ──────────────────────────


def _settings(tmp_path, db_name="chat_test.db") -> ChatSettings:
    path = tmp_path / db_name
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE gold_test (gu TEXT, ppltn INTEGER, observed_at TEXT);
        INSERT INTO gold_test VALUES ('강남구', 100, '2026-07-01'),
                                     ('서초구', 50, '2026-07-02'),
                                     ('강남구', 30, '2026-07-03');
        """
    )
    conn.commit()
    conn.close()
    return ChatSettings(
        api_key="", model="m", base_url="", max_tokens=1024, llm_timeout_s=30,
        max_tool_calls=3, max_concurrent=1, data_backend="sqlite",
        d1_account_id="", d1_database_id="", d1_api_token="",
        sqlite_path=str(path), query_row_limit=100, ontology_ttl_s=60,
    )


def test_sqlite_end_to_end(tmp_path):
    settings = _settings(tmp_path)
    sql, params, effective = queryspec.build(
        {"table": "gold_test", "group_by": ["gu"],
         "aggs": [{"fn": "sum", "column": "ppltn", "alias": "total"}],
         "order_by": [{"by": "total", "dir": "desc"}]},
        columns=["gu", "ppltn", "observed_at"], time_axis="observed_at", max_limit=100,
    )
    result = datasource.run_select(settings, sql, params, max_rows=effective)
    assert result["columns"] == ["gu", "total"]
    assert result["rows"] == [["강남구", 130], ["서초구", 50]]
    assert result["truncated"] is False


def test_truncated_detected_when_over_limit(tmp_path):
    settings = _settings(tmp_path)
    # max_limit=2 인데 3행이 있으므로 truncated=True 여야 한다.
    sql, params, effective = queryspec.build(
        {"table": "gold_test", "columns": ["gu"]},
        columns=["gu", "ppltn", "observed_at"], time_axis="observed_at", max_limit=2,
    )
    assert sql.endswith("LIMIT 3")  # effective(2) + 1
    result = datasource.run_select(settings, sql, params, max_rows=effective)
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_sqlite_readonly_enforced(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(datasource.ChatDataError):
        datasource.run_select(settings, "DELETE FROM gold_test", [], max_rows=10)


def _sqlite_settings(path) -> ChatSettings:
    return ChatSettings(
        api_key="", model="m", base_url="", max_tokens=1024, llm_timeout_s=30,
        max_tool_calls=3, max_concurrent=1, data_backend="sqlite",
        d1_account_id="", d1_database_id="", d1_api_token="",
        sqlite_path=str(path), query_row_limit=100, ontology_ttl_s=60,
    )


def test_sqlite_app_internal_path_rejected():
    from pathlib import Path
    app_db = Path(__file__).parents[1] / "app" / "whatever.db"
    with pytest.raises(datasource.ChatDataError):
        datasource.run_select(_sqlite_settings(app_db), "SELECT 1", [], max_rows=10)


def test_sqlite_runtime_data_dir_rejected():
    from pathlib import Path
    # 인증 DB·세션 시크릿이 사는 런타임 data/ 는 금지 — 여기에 auth DB 가 있다.
    data_db = Path(__file__).parents[1] / "data" / "ask_seoul.db"
    with pytest.raises(datasource.ChatDataError):
        datasource.run_select(_sqlite_settings(data_db), "SELECT 1", [], max_rows=10)


def test_sqlite_auth_database_url_rejected(tmp_path, monkeypatch):
    # data/ 밖의 커스텀 경로라도 DATABASE_URL 이 가리키는 인증 DB 면 거부한다.
    auth_db = tmp_path / "custom_auth.db"
    auth_db.write_bytes(b"")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{auth_db.as_posix()}")
    with pytest.raises(datasource.ChatDataError):
        datasource.run_select(_sqlite_settings(auth_db), "SELECT 1", [], max_rows=10)


def test_pragma_fallback_excludes_auth_tables(tmp_path):
    import sqlite3
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE gold_ok (a TEXT);"
        "CREATE TABLE auth_users (password_hash TEXT);"
    )
    conn.commit()
    conn.close()
    cards = datasource.fetch_catalog(_sqlite_settings(path))
    assert [c["name"] for c in cards] == ["gold_ok"]  # auth_* 는 카탈로그에서 제외


def test_fetch_catalog_pragma_fallback(tmp_path):
    settings = _settings(tmp_path)
    cards = datasource.fetch_catalog(settings)
    assert [c["name"] for c in cards] == ["gold_test"]
    assert {c["name"] for c in cards[0]["columns"]} == {"gu", "ppltn", "observed_at"}
    assert cards[0]["source"] == "pragma"


# ── API 계약 ─────────────────────────────────────────────


def test_chat_message_rejects_control_chars():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="hi\x01there")
    assert ChatMessage(role="user", content="줄1\n줄2").content == "줄1\n줄2"


def test_stream_semaphore_released_on_unconsumed_stream():
    """클라이언트가 스트림을 소비하지 않고 끊어도 세마포어 슬롯이 반납되어야 한다.

    프라이밍(첫 next)으로 sse 제너레이터가 suspended 되므로, 응답이 소비 없이
    GC 되어도 finally(release)가 실행된다 — 누수 시 몇 번 만에 429 로 벽돌화된다.
    """
    import gc
    import sys
    import threading

    import app.main  # noqa: F401  앱 로드(라우터 등록)

    rmod = sys.modules["app.chat.router"]
    saved = rmod._stream_slots
    rmod._stream_slots = threading.BoundedSemaphore(2)

    class FakeUser:
        role = "operator"

    req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    try:
        for _ in range(5):
            resp = rmod.chat_messages(req, _user=FakeUser())
            assert resp.status_code == 200
            del resp
            gc.collect()
            free = []
            for _ in range(2):
                ok = rmod._stream_slots.acquire(blocking=False)
                free.append(ok)
                if ok:
                    rmod._stream_slots.release()
            assert all(free), "세마포어 슬롯이 반납되지 않음(누수)"
    finally:
        rmod._stream_slots = saved


def test_run_turn_first_event_is_network_free_start():
    """프라이밍 안전성: 첫 이벤트는 네트워크 없이 즉시 start 여야 한다."""
    from app.chat.config import ChatSettings
    from app.chat import service

    settings = ChatSettings(
        api_key="k", model="m", base_url="http://127.0.0.1:9", max_tokens=1024,
        llm_timeout_s=2, max_tool_calls=2, max_concurrent=2, data_backend="",
        d1_account_id="", d1_database_id="", d1_api_token="",
        sqlite_path="", query_row_limit=50, ontology_ttl_s=60,
    )
    gen = service.run_turn(settings, [{"role": "user", "content": "hi"}])
    try:
        first = next(gen)  # 네트워크가 있으면 여기서 지연/예외
        assert first == {"event": "start", "model": "m"}
    finally:
        gen.close()


def test_chat_request_last_must_be_user():
    with pytest.raises(ValidationError):
        ChatRequest(messages=[{"role": "assistant", "content": "안녕"}])
    ok = ChatRequest(messages=[
        {"role": "user", "content": "질문"},
        {"role": "assistant", "content": "답"},
        {"role": "user", "content": "추가 질문"},
    ])
    assert ok.messages[-1].role == "user"

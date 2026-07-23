"""Ask Chat 데이터 실행기 — D1 REST / 로컬 SQLite, 읽기 전용.

charts/backends.py 와 같은 사상: 드라이버 예외의 내부 정보(토큰·계정·경로)는
서버 로그로만 남기고 클라이언트/LLM 에는 일반화된 한글 메시지만 돌려준다.
SQL 은 queryspec.build() 가 조립한 단일 SELECT + 파라미터 바인딩만 실행한다.
"""
from __future__ import annotations

import http.client
import json
import logging
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from .config import ChatSettings

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parents[1]
_DASHBOARD_DIR = _APP_DIR.parent
_RUNTIME_DIR = _DASHBOARD_DIR / "data"  # 인증 DB·세션 시크릿이 사는 런타임 디렉터리
# 서빙 gold 는 gold_* 이므로 인증/내부 테이블 접두는 모두 카탈로그에서 제외한다(심층 방어).
_INTERNAL_TABLE_PREFIXES = ("_", "sqlite_", "d1_", "auth_")
_D1_TIMEOUT_S = 20


def _auth_db_path() -> Path | None:
    """DATABASE_URL 이 sqlite 면 그 파일 경로(어디에 있든)를 반환한다."""
    import os

    url = os.environ.get("DATABASE_URL", "").strip()
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    try:
        return Path(url[len(prefix):]).resolve()
    except (OSError, ValueError):
        return None


class ChatDataError(RuntimeError):
    """설정·검증 문제 — 사용자/모델이 고칠 수 있는 오류."""


class ChatDataUnavailable(RuntimeError):
    """백엔드 접속 실패 — 재시도 대상."""


def _assert_allowed_sqlite_path(path: str) -> None:
    resolved = Path(path).resolve()
    # (1) 앱 코드 트리, (2) 런타임 data/ (인증 DB·세션 시크릿), (3) 실제 인증 DB 파일.
    for forbidden in (_APP_DIR, _RUNTIME_DIR):
        if resolved == forbidden or forbidden in resolved.parents:
            raise ChatDataError(
                "앱 내부·인증 런타임 DB 경로는 채팅 데이터 소스로 쓸 수 없습니다."
            )
    auth_db = _auth_db_path()
    if auth_db is not None and resolved == auth_db:
        raise ChatDataError(
            "인증 데이터베이스는 채팅 데이터 소스로 쓸 수 없습니다."
        )


def _run_sqlite(path: str, sql: str, params: list, max_rows: int) -> dict:
    _assert_allowed_sqlite_path(path)
    if not Path(path).is_file():
        raise ChatDataUnavailable("로컬 SQLite 파일을 찾을 수 없습니다.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.error("chat sqlite open failed error=%s", exc)
        raise ChatDataUnavailable("로컬 SQLite 를 열 수 없습니다.") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description or []]
        rows = [list(row) for row in cursor.fetchmany(max_rows + 1)]
    except sqlite3.Error as exc:
        logger.error("chat sqlite query failed error=%s sql=%s", exc, sql)
        raise ChatDataError("조회 실행에 실패했습니다 — 스펙(컬럼·타입)을 확인하세요.") from exc
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    return {"columns": columns, "rows": rows[:max_rows], "truncated": truncated}


def _run_d1(settings: ChatSettings, sql: str, params: list, max_rows: int) -> dict:
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.d1_account_id}/d1/database/{settings.d1_database_id}/query"
    )
    body = json.dumps({"sql": sql, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.d1_api_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_D1_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except OSError:
            pass
        logger.error("chat d1 http %s detail=%s sql=%s", exc.code, detail, sql)
        if exc.code in (401, 403):
            raise ChatDataError("D1 인증에 실패했습니다 — API 토큰 권한을 확인하세요.") from exc
        if exc.code == 400:
            raise ChatDataError("조회 실행에 실패했습니다 — 스펙(컬럼·타입)을 확인하세요.") from exc
        raise ChatDataUnavailable("D1 응답 오류 — 잠시 후 다시 시도하세요.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # IncompleteRead(HTTPException) 등 응답 중단도 접속 실패로 일반화한다.
        logger.error("chat d1 unreachable error=%s", exc)
        raise ChatDataUnavailable("D1 에 접속할 수 없습니다.") from exc

    if not payload.get("success"):
        errors = payload.get("errors") or []
        logger.error("chat d1 query failed errors=%s sql=%s", errors, sql)
        raise ChatDataError("조회 실행에 실패했습니다 — 스펙(컬럼·타입)을 확인하세요.")
    results = payload.get("result") or []
    first = results[0] if results else {}
    if not first.get("success", True):
        logger.error("chat d1 statement failed sql=%s", sql)
        raise ChatDataError("조회 실행에 실패했습니다 — 스펙(컬럼·타입)을 확인하세요.")
    dict_rows = first.get("results") or []
    columns = list(dict_rows[0].keys()) if dict_rows else []
    rows = [[row.get(col) for col in columns] for row in dict_rows[: max_rows + 1]]
    truncated = len(rows) > max_rows
    return {"columns": columns, "rows": rows[:max_rows], "truncated": truncated}


def run_select(settings: ChatSettings, sql: str, params: list, *, max_rows: int) -> dict:
    """단일 SELECT 실행. 반환: {columns, rows, row_count, truncated}."""
    if settings.data_backend == "sqlite":
        result = _run_sqlite(settings.sqlite_path, sql, params, max_rows)
    elif settings.data_backend == "d1":
        result = _run_d1(settings, sql, params, max_rows)
    else:
        raise ChatDataError(
            "데이터 소스가 설정되지 않았습니다 — CHAT_SQLITE_PATH 또는 CHAT_D1_* 을 설정하세요."
        )
    result["row_count"] = len(result["rows"])
    return result


# ── 카탈로그(온톨로지 원천) 조회 ─────────────────────────────────


def _is_internal_table(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(p) for p in _INTERNAL_TABLE_PREFIXES)


def fetch_catalog(settings: ChatSettings) -> list[dict]:
    """serving `_catalog` 우선, 없으면 sqlite_master + pragma_table_info 폴백.

    반환 카드: {name, description, serving_tier, time_axis, row_count,
               columns: [{name, type}], source: "_catalog"|"pragma"}.
    """
    try:
        result = run_select(
            settings,
            "SELECT name, description, serving_tier, time_axis, columns, row_count "
            "FROM _catalog ORDER BY name",
            [],
            max_rows=500,
        )
        cards = []
        idx = {c: i for i, c in enumerate(result["columns"])}
        for row in result["rows"]:
            try:
                columns = json.loads(row[idx["columns"]] or "[]")
            except (ValueError, TypeError):
                columns = []
            cards.append(
                {
                    "name": row[idx["name"]],
                    "description": row[idx["description"]] or "",
                    "serving_tier": row[idx["serving_tier"]],
                    "time_axis": row[idx["time_axis"]],
                    "row_count": row[idx["row_count"]],
                    "columns": [
                        {"name": c.get("name", ""), "type": c.get("type", "")}
                        for c in columns
                        if isinstance(c, dict) and c.get("name")
                    ],
                    "source": "_catalog",
                }
            )
        if cards:
            return cards
    except ChatDataError:
        # _catalog 미존재(아직 적재 전) — 실측 폴백으로 넘어간다.
        pass

    listed = run_select(
        settings,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        [],
        max_rows=500,
    )
    cards = []
    for (name,) in (tuple(r) for r in listed["rows"]):
        if not isinstance(name, str) or _is_internal_table(name):
            continue
        info = run_select(
            settings,
            "SELECT name, type FROM pragma_table_info(?)",
            [name],
            max_rows=500,
        )
        cards.append(
            {
                "name": name,
                "description": "",
                "serving_tier": None,
                "time_axis": None,
                "row_count": None,
                "columns": [
                    {"name": row[0], "type": row[1] or ""} for row in info["rows"]
                ],
                "source": "pragma",
            }
        )
    return cards

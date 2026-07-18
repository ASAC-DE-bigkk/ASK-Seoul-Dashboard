"""Trino 실행기 — 표준 라이브러리(urllib)만으로 REST 프로토콜을 따라간다.

카탈로그 API 본체는 "스냅샷 서빙" 사상이지만, 차트 스튜디오는 gold 를 직접 집계해야 하므로
이 모듈만 라이브 질의를 갖는다. 대신:
  - 결과는 디스크 캐시(TTL)로 박제해 같은 질의는 Trino 를 다시 두드리지 않고,
  - Trino 가 내려가 있으면 stale 캐시로 응답해 화면이 죽지 않게 한다(응답에 mode 표기).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TRINO_URL = os.environ.get("CHARTS_TRINO_URL", "http://127.0.0.1:30586").rstrip("/")
TRINO_USER = os.environ.get("CHARTS_TRINO_USER", "charts-studio")
CACHE_DIR = Path(__file__).parent / "data" / "cache"
FRESH_TTL_S = int(os.environ.get("CHARTS_CACHE_TTL", "600"))
MAX_ROWS = 5000

_lock = threading.Lock()


class TrinoUnavailable(RuntimeError):
    """접속 불가(기동 안 됨/타임아웃) — stale 캐시 폴백 대상."""


class QueryFailed(RuntimeError):
    """SQL 자체가 거부됨 — 폴백하지 않고 그대로 알린다."""


class _Busy503(RuntimeError):
    """Trino 프로토콜상 503 = '같은 URI 로 잠시 후 재시도' 신호 (실패 아님)."""


def _fetch(url: str, body: bytes | None = None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url, data=body, method="POST" if body is not None else "GET",
        headers={"X-Trino-User": TRINO_USER},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx — 프로토콜 레벨 거부
        if exc.code == 503:
            raise _Busy503(url) from exc
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise QueryFailed(f"trino http {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TrinoUnavailable(f"trino unreachable: {exc}") from exc


def _cancel(next_uri: str) -> None:
    """포기 시 실행 중 질의를 Trino 에 남기지 않는다 (best effort)."""
    try:
        req = urllib.request.Request(next_uri, method="DELETE",
                                     headers={"X-Trino-User": TRINO_USER})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:  # noqa: BLE001 — 취소 실패는 치명적이지 않다
        pass


def _fetch_retry_busy(url: str, body: bytes | None, deadline: float) -> dict:
    """503(busy) 은 프로토콜상 재시도 신호 — 데드라인 안에서 재시도한다."""
    while True:
        try:
            return _fetch(url, body=body)
        except _Busy503:
            if time.monotonic() > deadline:
                raise TrinoUnavailable("trino busy (503) — 데드라인 초과") from None
            time.sleep(0.1)


def _run(sql: str, max_rows: int = MAX_ROWS) -> tuple[list[str], list[list]]:
    """POST /v1/statement 후 nextUri 를 끝까지 따라가며 rows 수집."""
    deadline = time.monotonic() + 120
    payload = _fetch_retry_busy(f"{TRINO_URL}/v1/statement", sql.encode("utf-8"), deadline)
    columns: list[str] = []
    rows: list[list] = []
    while True:
        if payload.get("error"):
            msg = payload["error"].get("message", "unknown trino error")
            raise QueryFailed(msg[:300])
        if not columns and payload.get("columns"):
            columns = [c["name"] for c in payload["columns"]]
        for row in payload.get("data", []) or []:
            if len(rows) < max_rows:
                rows.append(row)
        next_uri = payload.get("nextUri")
        if not next_uri:
            return columns, rows
        if time.monotonic() > deadline:
            _cancel(next_uri)
            raise TrinoUnavailable("trino query timed out (120s)")
        try:
            payload = _fetch_retry_busy(next_uri, None, deadline)
        except TrinoUnavailable:
            _cancel(next_uri)
            raise


def _cache_path(sql: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(sql.encode("utf-8")).hexdigest()[:24] + ".json")


def _cache_read(sql: str) -> dict | None:
    p = _cache_path(sql)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cache_write(sql: str, columns: list[str], rows: list[list]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"cached_at": time.time(), "columns": columns, "rows": rows}
    tmp = _cache_path(sql).with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_cache_path(sql))


def execute(sql: str, max_rows: int = MAX_ROWS, force: bool = False) -> dict:
    """질의 실행. 반환: {columns, rows, mode(live|cache|stale), cached_at?, elapsed_ms}.
    force=True 는 신선 캐시를 건너뛰고 라이브 재질의('다시 조회' 버튼 경로)."""
    cached = _cache_read(sql)
    if not force and cached and time.time() - cached["cached_at"] < FRESH_TTL_S:
        return {"columns": cached["columns"], "rows": cached["rows"],
                "mode": "cache", "cached_at": cached["cached_at"], "elapsed_ms": 0}
    started = time.monotonic()
    try:
        columns, rows = _run(sql, max_rows)
    except TrinoUnavailable:
        if cached:  # 죽은 Trino 보다 낡은 데이터가 낫다 — mode 로 낡음을 정직하게 표기
            return {"columns": cached["columns"], "rows": cached["rows"],
                    "mode": "stale", "cached_at": cached["cached_at"], "elapsed_ms": 0}
        raise
    _cache_write(sql, columns, rows)
    return {"columns": columns, "rows": rows, "mode": "live",
            "elapsed_ms": round((time.monotonic() - started) * 1000)}

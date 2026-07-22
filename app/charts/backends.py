"""다중 백엔드 실행 디스패처 — 온톨로지 소스를 Trino 외 DB(SQLite·Postgres)에서도 조회한다.

사상: 온톨로지(Registry)는 스냅샷만 읽는 백엔드 중립 설계다. 소스가 어느 DB 에 사는지는
스냅샷 테이블의 ``datasource``(연결 이름)·``backend``(방언 키)가 말해주고, 이 모듈이
이름→실행기로 라우팅한다. SQL 은 querybuilder 가 방언에 맞춰 조립한 **화이트리스트 산출물**
이므로 여기서는 실행·타임아웃·읽기전용 강제만 책임진다.

연결 정의(env — 코드에 자격증명 금지):
    CHARTS_DATASOURCES='{"serving": {"backend": "sqlite", "path": "/data/serving.db"},
                         "rds":     {"backend": "postgres", "dsn_env": "CHARTS_RDS_DSN"}}'
  - 이름: ^[a-z_][a-z0-9_]*$ (스냅샷·레지스트리 키 접두로 쓰인다: <이름>__<테이블>)
  - sqlite.path: 서버 관리자가 지정한 파일 경로만(사용자 입력 경로 금지). 읽기전용
    (URI mode=ro + PRAGMA query_only)으로만 연다.
  - postgres.dsn_env: DSN 을 담은 **환경변수 이름**(간접 참조 — 값이 설정/로그에 남지 않게).
    세션은 default_transaction_read_only=on + statement_timeout 으로 강제한다.

trino 는 기존 trino.py(캐시·stale 폴백 포함)를 그대로 쓴다. sqlite/postgres 는 라이브
조회만 한다(mode='live') — 로컬/RDS 는 Trino 처럼 죽거나 느린 프로파일이 아니고,
stale 캐시 복잡도를 얹을 근거가 없다. 필요해지면 trino.py 캐시를 일반화한다.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import trino

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_STMT_TIMEOUT_MS = int(os.environ.get("CHARTS_DB_STATEMENT_TIMEOUT_MS", "20000"))


class DatasourceError(RuntimeError):
    """연결 정의/드라이버 문제 — 502 로 변환된다(문제는 서버 구성이지 사용자 입력이 아님)."""


# 지원 backend — querybuilder 방언 프로파일·별칭과 동일 어휘(단일 정본은 querybuilder)
from .querybuilder import DIALECT_ALIASES, DIALECTS  # noqa: E402

SUPPORTED_BACKENDS = (set(DIALECTS) - {"trino"}) | set(DIALECT_ALIASES)


def datasources() -> dict[str, dict]:
    """env 의 연결 정의(트러스티드 구성). 잘못된 항목은 기동 시점이 아니라 사용 시점에
    명확한 오류로 드러낸다(카탈로그 서빙 자체를 막지 않기 위해)."""
    raw = os.environ.get("CHARTS_DATASOURCES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasourceError(f"CHARTS_DATASOURCES JSON 형식 오류: {exc}") from exc
    out: dict[str, dict] = {}
    for name, conf in (parsed or {}).items():
        if not _NAME_RE.fullmatch(str(name)):
            raise DatasourceError(f"datasource 이름 형식 오류: {name!r}")
        if not isinstance(conf, dict) or conf.get("backend") not in SUPPORTED_BACKENDS:
            raise DatasourceError(
                f"datasource {name}: backend 는 {sorted(SUPPORTED_BACKENDS)} 중 하나여야 합니다")
        out[str(name)] = conf
    return out


def canonical_backend(backend: str) -> str:
    return DIALECT_ALIASES.get(backend, backend)


def _execute_sqlite(path: str, sql: str, max_rows: int) -> tuple[list[str], list[list]]:
    import sqlite3

    if not path or not os.path.isfile(path):
        raise DatasourceError(f"sqlite 파일이 없습니다: {path}")
    # 읽기전용 2중 강제: URI mode=ro(파일 계층) + query_only(세션 계층)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(max_rows)]
        return columns, rows
    finally:
        conn.close()


def _pg_connect(dsn: str):
    """psycopg(3) 우선, psycopg2 폴백 — 둘 다 없으면 구성 오류로 명확히 알린다."""
    options = f"-c default_transaction_read_only=on -c statement_timeout={_STMT_TIMEOUT_MS}"
    try:
        import psycopg
        return psycopg.connect(dsn, options=options)
    except ImportError:
        pass
    try:
        import psycopg2
        return psycopg2.connect(dsn, options=options)
    except ImportError as exc:
        raise DatasourceError(
            "postgres 드라이버(psycopg 또는 psycopg2)가 설치되어 있지 않습니다") from exc


def _dsn_from(conf: dict, backend: str) -> str:
    dsn_env = conf.get("dsn_env", "")
    dsn = os.environ.get(dsn_env, "").strip() if dsn_env else ""
    if not dsn:
        raise DatasourceError(f"{backend} DSN env 미설정: {dsn_env or '(dsn_env 없음)'}")
    return dsn


def _mysql_connect(conf: dict):
    """mysql/mariadb — 접속 정보는 dsn_env 의 JSON({host,port,user,password,database}).
    세션에 ANSI_QUOTES(\"x\" 식별자)·NO_BACKSLASH_ESCAPES(리터럴 ANSI 화)를 강제해
    querybuilder 의 ANSI 조립이 그대로 유효하게 만든다 + 읽기전용·실행시간 상한."""
    params = json.loads(_dsn_from(conf, "mysql"))
    try:
        import pymysql
        conn = pymysql.connect(**params)
    except ImportError:
        try:
            import mysql.connector
            conn = mysql.connector.connect(**params)
        except ImportError as exc:
            raise DatasourceError(
                "mysql 드라이버(pymysql 또는 mysql-connector-python)가 없습니다") from exc
    cur = conn.cursor()
    cur.execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',ANSI_QUOTES,NO_BACKSLASH_ESCAPES')")
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    for stmt in (f"SET SESSION max_execution_time = {_STMT_TIMEOUT_MS}",           # mysql 5.7+
                 f"SET SESSION max_statement_time = {_STMT_TIMEOUT_MS / 1000}"):   # mariadb
        try:
            cur.execute(stmt)
            break
        except Exception:  # noqa: BLE001 — 두 계열 중 하나만 지원돼도 충분
            continue
    cur.close()
    return conn


def _oracle_connect(conf: dict):
    try:
        import oracledb
    except ImportError as exc:
        raise DatasourceError("oracle 드라이버(oracledb)가 없습니다") from exc
    conn = oracledb.connect(_dsn_from(conf, "oracle"))
    conn.call_timeout = _STMT_TIMEOUT_MS
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ ONLY")
    cur.close()
    return conn


def _mssql_connect(conf: dict):
    """mssql — QUOTED_IDENTIFIER ON(드라이버 기본)이 ANSI \"x\" 를 보장한다.
    세션 읽기전용 개념이 없어(가용성 복제 전용) **계정 권한을 SELECT 로 제한**하는 것이
    운영 계약이다 — 연결 정의 문서에 명시. LOCK_TIMEOUT 으로 잠금 대기만 상한."""
    dsn = _dsn_from(conf, "mssql")
    try:
        import pyodbc
        conn = pyodbc.connect(dsn, timeout=max(1, _STMT_TIMEOUT_MS // 1000))
    except ImportError:
        try:
            import pymssql
            conn = pymssql.connect(**json.loads(dsn))
        except ImportError as exc:
            raise DatasourceError("mssql 드라이버(pyodbc 또는 pymssql)가 없습니다") from exc
    cur = conn.cursor()
    cur.execute(f"SET LOCK_TIMEOUT {_STMT_TIMEOUT_MS}")
    cur.close()
    return conn


def _execute_dbapi(conn, sql: str, max_rows: int) -> tuple[list[str], list[list]]:
    try:
        cur = conn.cursor()
        cur.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(max_rows)]
        cur.close()
        return columns, rows
    finally:
        conn.close()


def _execute_postgres(conf: dict, sql: str, max_rows: int) -> tuple[list[str], list[list]]:
    return _execute_dbapi(_pg_connect(_dsn_from(conf, "postgres")), sql, max_rows)


def execute(source: dict, sql: str, max_rows: int = trino.MAX_ROWS,
            force: bool = False) -> dict:
    """소스의 datasource 로 라우팅 실행. 반환 계약은 trino.execute 와 동일
    {columns, rows, mode, elapsed_ms} — 소비자(router/프론트)는 백엔드를 모른다."""
    name = source.get("datasource") or "trino"
    if name == "trino":
        return trino.execute(sql, max_rows=max_rows, force=force)
    conf = datasources().get(name)
    if conf is None:
        raise DatasourceError(f"정의되지 않은 datasource 입니다: {name}")
    started = time.monotonic()
    backend = canonical_backend(conf["backend"])
    try:
        if backend == "sqlite":
            columns, rows = _execute_sqlite(str(conf.get("path", "")), sql, max_rows)
        elif backend == "postgres":
            columns, rows = _execute_postgres(conf, sql, max_rows)
        elif backend == "mysql":
            columns, rows = _execute_dbapi(_mysql_connect(conf), sql, max_rows)
        elif backend == "oracle":
            columns, rows = _execute_dbapi(_oracle_connect(conf), sql, max_rows)
        elif backend == "mssql":
            columns, rows = _execute_dbapi(_mssql_connect(conf), sql, max_rows)
        else:
            raise DatasourceError(f"실행기 미구현 backend: {backend}")
    except DatasourceError:
        raise
    except Exception as exc:  # noqa: BLE001 — DB 드라이버 예외를 조회 실패 계약으로 통일(502)
        raise trino.QueryFailed(f"datasource {name}: {str(exc)[:200]}") from exc
    return {"columns": columns, "rows": rows, "mode": "live",
            "elapsed_ms": round((time.monotonic() - started) * 1000)}

"""Ask Chat 설정 — env 지연 평가.

import 시점에 필수값을 강제하거나 네트워크에 닿지 않는다(앱 부팅 안전).
D1 접속값은 CHAT_D1_* 를 우선 읽고, 없으면 상위 sample/.env 에서 쓰는 이름
(CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN / D1_UUID)을 폴백으로 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return max(low, min(high, value))


@dataclass(frozen=True)
class ChatSettings:
    # LLM
    api_key: str
    model: str
    base_url: str
    max_tokens: int
    llm_timeout_s: int
    max_tool_calls: int
    max_concurrent: int
    # 데이터
    data_backend: str  # "d1" | "sqlite" | "" (미설정)
    d1_account_id: str
    d1_database_id: str
    d1_api_token: str
    sqlite_path: str
    query_row_limit: int
    ontology_ttl_s: int

    @property
    def llm_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def data_configured(self) -> bool:
        return self.data_backend in ("d1", "sqlite")


def load_chat_settings() -> ChatSettings:
    sqlite_path = _env("CHAT_SQLITE_PATH")
    d1_account = _env("CHAT_D1_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")
    d1_database = _env("CHAT_D1_DATABASE_ID", "D1_UUID")
    d1_token = _env("CHAT_D1_API_TOKEN", "CLOUDFLARE_API_TOKEN")

    backend = _env("CHAT_DATA_BACKEND").lower()
    if backend not in ("d1", "sqlite"):
        # 명시가 없으면 로컬 파일 > D1 순으로 자동 선택한다(로컬 분석 우선).
        if sqlite_path:
            backend = "sqlite"
        elif d1_account and d1_database and d1_token:
            backend = "d1"
        else:
            backend = ""
    if backend == "sqlite" and not sqlite_path:
        backend = ""
    if backend == "d1" and not (d1_account and d1_database and d1_token):
        backend = ""

    return ChatSettings(
        api_key=_env("ANTHROPIC_API_KEY"),
        model=_env("CHAT_LLM_MODEL", default="claude-opus-4-8"),
        base_url=_env("CHAT_LLM_BASE_URL", default="https://api.anthropic.com").rstrip("/"),
        max_tokens=_int_env("CHAT_LLM_MAX_TOKENS", 16000, low=1024, high=64000),
        llm_timeout_s=_int_env("CHAT_LLM_TIMEOUT_S", 180, low=10, high=600),
        max_tool_calls=_int_env("CHAT_MAX_TOOL_CALLS", 6, low=1, high=20),
        max_concurrent=_int_env("CHAT_MAX_CONCURRENT", 2, low=1, high=16),
        data_backend=backend,
        d1_account_id=d1_account,
        d1_database_id=d1_database,
        d1_api_token=d1_token,
        sqlite_path=sqlite_path,
        query_row_limit=_int_env("CHAT_QUERY_ROW_LIMIT", 200, low=10, high=1000),
        ontology_ttl_s=_int_env("CHAT_ONTOLOGY_TTL", 300, low=10, high=86400),
    )

"""Ask Chat 턴 오케스트레이션 — 도구 루프 + UI 이벤트 생성.

run_turn() 은 UI 로 보낼 이벤트(dict)를 순서대로 yield 한다:
  start → (thinking | delta | tool | tool_result)* → done | error
조회 근거(스펙·SQL·행 수)는 done 이벤트의 queries 로 함께 내려간다.
"""
from __future__ import annotations

import json
import logging
from typing import Iterator

from . import datasource, ontology, queryspec
from .config import ChatSettings
from .llm import BlockAccumulator, LLMError, stream_events

logger = logging.getLogger(__name__)

TOOL_NAME = "query_gold"
MAX_CELL_CHARS = 300


def _tools() -> list[dict]:
    return [
        {
            "name": TOOL_NAME,
            "description": (
                "서빙 카탈로그의 gold 테이블을 한정된 스펙으로 조회한다. "
                "자유 SQL 불가 — 테이블·컬럼은 카탈로그에 있는 것만, 값 비교는 필터로만. "
                "집계 질문은 group_by + aggs 로 DB에서 집계한다."
            ),
            "input_schema": queryspec.TOOL_INPUT_SCHEMA,
        }
    ]


def _clip_cell(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    return value


def _execute_tool(settings: ChatSettings, cards_by_name: dict[str, dict],
                  spec: dict) -> tuple[str, dict]:
    """도구 실행. 반환: (tool_result 본문 문자열, 근거 레코드)."""
    table = spec.get("table") if isinstance(spec, dict) else None
    record: dict = {"table": table, "spec": spec, "ok": False}
    card = cards_by_name.get(table) if isinstance(table, str) else None
    if card is None:
        raise queryspec.ChatSpecError(
            f"테이블 '{table}' 은(는) 서빙 카탈로그에 없습니다."
        )
    sql, params, effective_limit = queryspec.build(
        spec,
        columns=[c["name"] for c in card["columns"]],
        time_axis=card.get("time_axis"),
        max_limit=settings.query_row_limit,
    )
    record["sql"] = sql
    # build 가 effective_limit+1 을 조회하므로, effective_limit 로 잘라 truncated 판정.
    result = datasource.run_select(settings, sql, params, max_rows=effective_limit)
    record.update(ok=True, row_count=result["row_count"], truncated=result["truncated"])
    body = json.dumps(
        {
            "columns": result["columns"],
            "rows": [[_clip_cell(v) for v in row] for row in result["rows"]],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
        },
        ensure_ascii=False,
        default=str,
    )
    return body, record


def run_turn(settings: ChatSettings, history: list[dict]) -> Iterator[dict]:
    # 첫 이벤트는 네트워크 없이 즉시 낸다 — 라우터가 제너레이터를 프라임(첫 next)해
    # finally(세마포어 반납)가 항상 실행되도록 보장하기 위함(끊김 시 슬롯 누수 방지).
    yield {"event": "start", "model": settings.model}

    if not settings.llm_configured:
        yield {
            "event": "error",
            "title": "llm not configured",
            "detail": "ANTHROPIC_API_KEY 가 설정되지 않았습니다 — docs/chat-design.md §2 참조.",
        }
        return

    # 이력 정규화(네트워크 없음). Messages API 는 첫 메시지가 user 여야 한다 —
    # 클라이언트 이력 절단으로 assistant 가 앞에 오면 잘라낸다.
    api_messages: list[dict] = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if (m.get("content") or "").strip()
    ]
    while api_messages and api_messages[0]["role"] != "user":
        api_messages.pop(0)
    if not api_messages:
        yield {"event": "error", "title": "empty request",
               "detail": "보낼 메시지가 없습니다."}
        return

    data_note = ""
    try:
        cards = ontology.get_cards(settings) if settings.data_configured else []
        if not settings.data_configured:
            data_note = "(주의: 데이터 소스가 설정되지 않아 조회가 불가능하다. 그 사실을 안내하라.)"
    except (datasource.ChatDataError, datasource.ChatDataUnavailable) as exc:
        cards = []
        data_note = f"(주의: 데이터 소스 오류로 카탈로그를 읽지 못했다 — {exc})"
    cards_by_name = ontology.card_lookup(cards)
    system_text = ontology.build_system_prompt(cards)
    if data_note:
        system_text += f"\n\n{data_note}"

    payload_base = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "thinking": {"type": "adaptive"},
        "system": [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ],
        "tools": _tools(),
    }

    queries: list[dict] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    tool_calls_used = 0
    stop_reason: str | None = None

    # 도구 루프 — 상한 초과 시에도 tool_result 짝은 반드시 맞춰 준다.
    for _round in range(settings.max_tool_calls + 2):
        acc = BlockAccumulator()
        try:
            for event in stream_events(settings, {**payload_base, "messages": api_messages}):
                acc.feed(event)
                etype = event.get("type")
                if etype == "content_block_start":
                    if (event.get("content_block") or {}).get("type") == "thinking":
                        yield {"event": "thinking"}
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield {"event": "delta", "text": delta["text"]}
        except LLMError as exc:
            yield {"event": "error", "title": "llm error", "detail": str(exc)}
            return

        for key in usage_total:
            usage_total[key] += int(acc.usage.get(key) or 0)
        stop_reason = acc.stop_reason
        assistant_content = acc.assistant_content()
        if not assistant_content:
            break
        if stop_reason != "tool_use":
            break

        api_messages.append({"role": "assistant", "content": assistant_content})
        tool_results: list[dict] = []
        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue
            tool_use_id = block.get("id", "")
            spec = block.get("input") or {}
            over_budget = tool_calls_used >= settings.max_tool_calls
            wrong_tool = block.get("name") != TOOL_NAME
            yield {
                "event": "tool",
                "name": block.get("name"),
                "table": spec.get("table") if isinstance(spec, dict) else None,
                "spec": spec,
            }
            if over_budget or wrong_tool:
                error_text = (
                    "도구 호출 상한을 초과했습니다 — 지금까지의 결과만으로 답하세요."
                    if over_budget
                    else f"알 수 없는 도구입니다: {block.get('name')!r}"
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": error_text, "is_error": True}
                )
                yield {"event": "tool_result", "ok": False, "error": error_text}
                continue
            tool_calls_used += 1
            try:
                body, record = _execute_tool(settings, cards_by_name, spec)
                queries.append(record)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": body}
                )
                yield {
                    "event": "tool_result",
                    "ok": True,
                    "table": record.get("table"),
                    "row_count": record.get("row_count"),
                    "truncated": record.get("truncated"),
                }
            except queryspec.ChatSpecError as exc:
                queries.append({"spec": spec, "ok": False, "error": str(exc)})
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": f"스펙 오류: {exc}", "is_error": True}
                )
                yield {"event": "tool_result", "ok": False, "error": str(exc)}
            except (datasource.ChatDataError, datasource.ChatDataUnavailable) as exc:
                queries.append({"spec": spec, "ok": False, "error": str(exc)})
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": f"조회 실패: {exc}", "is_error": True}
                )
                yield {"event": "tool_result", "ok": False, "error": str(exc)}
        if not tool_results:
            break
        api_messages.append({"role": "user", "content": tool_results})

    yield {
        "event": "done",
        "stop_reason": stop_reason,
        "usage": usage_total,
        "queries": queries,
    }

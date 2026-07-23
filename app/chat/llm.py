"""Claude Messages API 스트리밍 클라이언트 — 표준 라이브러리(urllib)만 사용.

trino.py 와 같은 사상으로 REST 프로토콜을 직접 따라간다. anthropic SDK 를 쓰지 않는
이유는 docs/chat-design.md D2 참조(저장소 관례 + 실행 환경의 네이티브 DLL 차단).

- stream_events(): SSE 이벤트(dict) 제너레이터. 오류는 LLMError 로 승격.
- BlockAccumulator: 스트림 이벤트를 assistant content 블록 목록으로 복원한다.
  (thinking 블록·signature 포함 — 도구 루프에서 원형 그대로 되돌려 보내야 한다.)
"""
from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import Iterator

from .config import ChatSettings

logger = logging.getLogger(__name__)

API_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error_message(status: int, body: str) -> str:
    detail = ""
    try:
        parsed = json.loads(body)
        detail = ((parsed.get("error") or {}).get("message") or "")[:300]
    except (ValueError, TypeError):
        pass
    if status == 401:
        return "LLM API 키가 유효하지 않습니다 — ANTHROPIC_API_KEY 를 확인하세요."
    if status == 400:
        return f"LLM 요청이 거부되었습니다: {detail or '요청 형식 오류'}"
    if status == 429:
        return "LLM 요청 한도를 초과했습니다 — 잠시 후 다시 시도하세요."
    if status == 529:
        return "LLM 서비스가 혼잡합니다 — 잠시 후 다시 시도하세요."
    return f"LLM 호출에 실패했습니다 (HTTP {status})."


def stream_events(settings: ChatSettings, payload: dict) -> Iterator[dict]:
    """POST /v1/messages (stream=true) → SSE data 이벤트를 dict 로 yield."""
    body = json.dumps({**payload, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{settings.base_url}/v1/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": settings.api_key,
            "anthropic-version": API_VERSION,
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=settings.llm_timeout_s)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except OSError:
            pass
        logger.error("chat llm http %s body=%s", exc.code, raw[:500])
        raise LLMError(exc.code, _error_message(exc.code, raw)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        logger.error("chat llm unreachable error=%s", exc)
        raise LLMError(503, "LLM 서비스에 접속할 수 없습니다.") from exc

    try:
        data_lines: list[str] = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line == "" and data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if event.get("type") == "error":
                    message = ((event.get("error") or {}).get("message") or "")[:300]
                    logger.error("chat llm stream error=%s", message)
                    raise LLMError(502, f"LLM 스트림 오류: {message or '알 수 없음'}")
                yield event
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # IncompleteRead(HTTPException) 등 청크 전송 중단도 LLMError 로 승격한다.
        logger.error("chat llm stream aborted error=%s", exc)
        raise LLMError(504, "LLM 스트림이 중단되었습니다 — 다시 시도하세요.") from exc
    finally:
        resp.close()


class BlockAccumulator:
    """스트림 이벤트 → assistant content 블록 복원(원형 보존)."""

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.stop_reason: str | None = None
        self.usage: dict = {}
        self._partial_json: dict[int, list[str]] = {}

    def feed(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            self.usage = (event.get("message") or {}).get("usage") or {}
        elif etype == "content_block_start":
            index = event.get("index", len(self.blocks))
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                self._partial_json[index] = []
                block.setdefault("input", {})
            while len(self.blocks) <= index:
                self.blocks.append({})
            self.blocks[index] = block
        elif etype == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta") or {}
            if index >= len(self.blocks):
                return
            block = self.blocks[index]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + (delta.get("text") or "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
            elif dtype == "input_json_delta":
                self._partial_json.setdefault(index, []).append(delta.get("partial_json") or "")
        elif etype == "content_block_stop":
            index = event.get("index", 0)
            if index in self._partial_json and index < len(self.blocks):
                raw = "".join(self._partial_json.pop(index))
                try:
                    self.blocks[index]["input"] = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    self.blocks[index]["input"] = {}
                    self.blocks[index]["_input_parse_error"] = True
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            for key, value in (event.get("usage") or {}).items():
                if value is not None:
                    self.usage[key] = value

    def assistant_content(self) -> list[dict]:
        """API 로 되돌려 보낼 content — 내부 표시용 키(_*)만 제거하고 원형 유지."""
        return [
            {k: v for k, v in block.items() if not k.startswith("_")}
            for block in self.blocks
            if block
        ]

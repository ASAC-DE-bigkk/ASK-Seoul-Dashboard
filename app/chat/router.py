"""Ask Chat API — /api/v1/chat/*.

- GET  /meta      : 설정·카탈로그 상태(미설정도 정직하게 보고 — 200)
- POST /messages  : SSE 스트리밍 턴(무상태 — 클라이언트가 전체 이력 전송)
인증은 페이지 키 'chat'(미들웨어 게이트 + require_page 이중), CSRF·레이트리밋·
바디 상한은 기존 AuthSecurityMiddleware 가 그대로 적용된다.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.dependencies import require_page
from ..auth.models import User
from . import datasource, ontology, service
from .config import load_chat_settings
from .models import ChatMetaResponse, ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

_require_chat = require_page("chat")
_stream_slots = threading.BoundedSemaphore(load_chat_settings().max_concurrent)


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


@router.get("/meta", response_model=ChatMetaResponse)
def chat_meta(_user: User = Depends(_require_chat)) -> dict:
    settings = load_chat_settings()
    tables: list[str] = []
    detail = ""
    if not settings.llm_configured:
        detail = "ANTHROPIC_API_KEY 가 설정되지 않았습니다."
    if settings.data_configured:
        try:
            tables = [c["name"] for c in ontology.get_cards(settings)]
        except (datasource.ChatDataError, datasource.ChatDataUnavailable) as exc:
            detail = (detail + " " if detail else "") + f"데이터 소스 오류: {exc}"
    else:
        detail = (detail + " " if detail else "") + (
            "데이터 소스가 설정되지 않았습니다 (CHAT_SQLITE_PATH 또는 CHAT_D1_*)."
        )
    return {
        "llm_configured": settings.llm_configured,
        "model": settings.model,
        "data_backend": settings.data_backend or None,
        "data_configured": settings.data_configured,
        "table_count": len(tables) if settings.data_configured else None,
        "tables": tables,
        "detail": detail.strip(),
    }


@router.post("/messages", response_model=None)
def chat_messages(
    req: ChatRequest,
    _user: User = Depends(_require_chat),
) -> StreamingResponse | JSONResponse:
    if not _stream_slots.acquire(blocking=False):
        return _problem(
            429, "chat busy",
            "동시 대화 상한에 도달했습니다 — 잠시 후 다시 시도하세요.",
        )
    settings = load_chat_settings()
    history = [{"role": m.role, "content": m.content} for m in req.messages]

    released = threading.Event()

    def release() -> None:
        if not released.is_set():
            released.set()
            _stream_slots.release()

    def sse() -> Iterator[bytes]:
        try:
            for event in service.run_turn(settings, history):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            release()

    generator = sse()
    try:
        # 제너레이터를 첫 프레임까지 프라임한다 — 이후 어떤 경로(정상 종료·조기 close·
        # GC)로 닫히든 finally 가 실행되어 세마포어가 반드시 반납된다. run_turn 의 첫
        # yield 는 네트워크가 없어 프라이밍 비용이 없다.
        first = next(generator)
    except StopIteration:
        first = None
    except Exception:
        release()
        raise

    def body() -> Iterator[bytes]:
        if first is not None:
            yield first
        yield from generator

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

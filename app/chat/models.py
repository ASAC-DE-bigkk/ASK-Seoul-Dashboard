"""Ask Chat API Pydantic 계약."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# 채팅 본문은 여러 줄을 허용하되, 개행·탭 외의 제어문자는 거부한다
# (inputguard SAFE_TEXT 의 다중행 변형 — 상세 규칙은 이 스키마가 정본).
_FORBIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_MESSAGE_CHARS = 8000


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if _FORBIDDEN_CONTROL.search(value):
            raise ValueError("메시지에 제어문자는 쓸 수 없습니다.")
        if not value.strip():
            raise ValueError("메시지가 비어 있습니다.")
        return value


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _last_is_user(self) -> "ChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("마지막 메시지는 user 여야 합니다.")
        return self


class ChatMetaResponse(BaseModel):
    llm_configured: bool
    model: str
    data_backend: Optional[str] = None  # "d1" | "sqlite" | None
    data_configured: bool
    table_count: Optional[int] = None
    tables: list[str] = Field(default_factory=list)
    detail: str = ""

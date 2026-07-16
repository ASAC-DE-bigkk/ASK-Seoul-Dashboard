"""Discord·Slack·Telegram 교체형 운영 알림.

호출자는 제목·본문·필드만 전달하고, 채널별 포맷/전송은 어댑터가 담당한다.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    severity: str = "info"
    fields: dict[str, Any] = field(default_factory=dict)
    target_roles: tuple[str, ...] = ("admin", "operator")

    def plain_text(self) -> str:
        lines = [f"[{self.severity.upper()}] {self.title}", self.body]
        lines.extend(f"- {key}: {value}" for key, value in self.fields.items())
        lines.append(f"- target_roles: {', '.join(self.target_roles)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    configured: bool
    delivered: bool
    target: str = ""
    error: str = ""


@dataclass(frozen=True)
class NotificationResult:
    deliveries: tuple[DeliveryResult, ...]

    @property
    def configured(self) -> bool:
        return any(item.configured for item in self.deliveries)

    @property
    def delivered(self) -> bool:
        return any(item.delivered for item in self.deliveries)

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "delivered": self.delivered,
            "deliveries": [
                {
                    "channel": item.channel,
                    "configured": item.configured,
                    "delivered": item.delivered,
                    "target": item.target,
                    "error": item.error,
                }
                for item in self.deliveries
            ],
        }


def _post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"content-type": "application/json", "user-agent": "ask-seoul-notifier/1"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")


class NotificationService:
    def __init__(self) -> None:
        self.discord_url = os.environ.get("NOTIFY_DISCORD_WEBHOOK_URL", "").strip()
        self.slack_url = os.environ.get("NOTIFY_SLACK_WEBHOOK_URL", "").strip()
        self.telegram_token = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chats = tuple(
            chat.strip()
            for chat in os.environ.get("NOTIFY_TELEGRAM_CHAT_IDS", "").split(",")
            if chat.strip()
        )

    @staticmethod
    def _error(channel: str, configured: bool, target: str, exc: Exception) -> DeliveryResult:
        # webhook URL/토큰/응답 본문을 오류에 포함하지 않는다.
        return DeliveryResult(
            channel=channel,
            configured=configured,
            delivered=False,
            target=target,
            error=f"{type(exc).__name__}: 전송 실패",
        )

    def send(self, message: NotificationMessage) -> NotificationResult:
        results: list[DeliveryResult] = []
        text = message.plain_text()

        if not self.discord_url:
            results.append(DeliveryResult("discord", False, False))
        else:
            try:
                _post_json(self.discord_url, {"content": text[:1900]})
                results.append(DeliveryResult("discord", True, True, "webhook"))
            except Exception as exc:
                results.append(self._error("discord", True, "webhook", exc))

        if not self.slack_url:
            results.append(DeliveryResult("slack", False, False))
        else:
            try:
                _post_json(self.slack_url, {"text": text})
                results.append(DeliveryResult("slack", True, True, "webhook"))
            except Exception as exc:
                results.append(self._error("slack", True, "webhook", exc))

        if not self.telegram_token or not self.telegram_chats:
            results.append(DeliveryResult("telegram", False, False))
        else:
            url = (
                "https://api.telegram.org/bot"
                + urllib.parse.quote(self.telegram_token, safe=":")
                + "/sendMessage"
            )
            for chat_id in self.telegram_chats:
                try:
                    _post_json(url, {"chat_id": chat_id, "text": text})
                    results.append(DeliveryResult("telegram", True, True, chat_id))
                except Exception as exc:
                    results.append(self._error("telegram", True, chat_id, exc))

        return NotificationResult(tuple(results))

"""Discord·Slack·Telegram 교체형 운영 알림.

호출자는 제목·본문·필드만 전달하고, 채널별 포맷/전송은 어댑터가 담당한다.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import re
from dataclasses import dataclass, field
from typing import Any


def _clean_line(value: Any, limit: int) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in text if ord(ch) >= 0x20)[:limit]


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    severity: str = "info"
    fields: dict[str, Any] = field(default_factory=dict)
    target_roles: tuple[str, ...] = ("admin", "operator")

    def plain_text(self) -> str:
        title = _clean_line(self.title, 200)
        severity = _clean_line(self.severity.upper(), 20)
        body = str(self.body).replace("\r\n", "\n").replace("\r", "\n")[:4000]
        lines = [f"[{severity}] {title}", body]
        lines.extend(
            f"- {_clean_line(key, 80)}: {_clean_line(value, 500)}"
            for key, value in list(self.fields.items())[:30]
        )
        roles = ", ".join(_clean_line(role, 30) for role in self.target_roles[:10])
        lines.append(f"- target_roles: {roles}")
        return "\n".join(lines)[:35_000]


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"content-type": "application/json", "user-agent": "ask-seoul-notifier/1"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=8) as response:
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

    @staticmethod
    def _telegram_target(chat_id: str) -> str:
        suffix = chat_id[-4:] if len(chat_id) >= 4 else chat_id
        return f"chat:***{suffix}"

    @staticmethod
    def _valid_webhook(url: str, channel: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return False
        if channel == "discord":
            return (
                parsed.hostname in {"discord.com", "discordapp.com"}
                and parsed.path.startswith("/api/webhooks/")
            )
        return (
            parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
            and parsed.path.startswith("/services/")
        )

    def configuration(self) -> NotificationResult:
        """자격증명을 노출하지 않고 채널 구성 여부와 안전한 대상 표지만 반환한다."""
        discord_valid = bool(self.discord_url) and self._valid_webhook(
            self.discord_url, "discord"
        )
        slack_valid = bool(self.slack_url) and self._valid_webhook(
            self.slack_url, "slack"
        )
        results = [
            DeliveryResult(
                "discord",
                discord_valid,
                False,
                "webhook" if discord_valid else "",
                "" if not self.discord_url or discord_valid else "ValueError: webhook URL 형식 오류",
            ),
            DeliveryResult(
                "slack",
                slack_valid,
                False,
                "webhook" if slack_valid else "",
                "" if not self.slack_url or slack_valid else "ValueError: webhook URL 형식 오류",
            ),
        ]
        telegram_token_valid = bool(
            re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", self.telegram_token)
        )
        if telegram_token_valid and self.telegram_chats:
            results.extend(
                DeliveryResult(
                    "telegram",
                    bool(re.fullmatch(r"-?\d+", chat_id)),
                    False,
                    self._telegram_target(chat_id)
                    if re.fullmatch(r"-?\d+", chat_id)
                    else "",
                    ""
                    if re.fullmatch(r"-?\d+", chat_id)
                    else "ValueError: Telegram chat ID 형식 오류",
                )
                for chat_id in self.telegram_chats
            )
        else:
            results.append(
                DeliveryResult(
                    "telegram",
                    False,
                    False,
                    error=(
                        "ValueError: Telegram token 형식 오류"
                        if self.telegram_token and not telegram_token_valid
                        else ""
                    ),
                )
            )
        return NotificationResult(tuple(results))

    def send(self, message: NotificationMessage) -> NotificationResult:
        results: list[DeliveryResult] = []
        text = message.plain_text()

        if not self.discord_url:
            results.append(DeliveryResult("discord", False, False))
        elif not self._valid_webhook(self.discord_url, "discord"):
            results.append(
                DeliveryResult(
                "discord",
                False,
                False,
                "",
                "ValueError: 허용되지 않은 webhook URL",
                )
            )
        else:
            try:
                _post_json(
                    self.discord_url,
                    {
                        "content": text[:1900],
                        "allowed_mentions": {"parse": []},
                    },
                )
                results.append(DeliveryResult("discord", True, True, "webhook"))
            except Exception as exc:
                results.append(self._error("discord", True, "webhook", exc))

        if not self.slack_url:
            results.append(DeliveryResult("slack", False, False))
        elif not self._valid_webhook(self.slack_url, "slack"):
            results.append(
                DeliveryResult(
                "slack",
                False,
                False,
                "",
                "ValueError: 허용되지 않은 webhook URL",
                )
            )
        else:
            try:
                _post_json(self.slack_url, {"text": text})
                results.append(DeliveryResult("slack", True, True, "webhook"))
            except Exception as exc:
                results.append(self._error("slack", True, "webhook", exc))

        if not self.telegram_token or not self.telegram_chats:
            results.append(DeliveryResult("telegram", False, False))
        elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", self.telegram_token):
            results.append(
                DeliveryResult(
                    "telegram",
                    False,
                    False,
                    "",
                    "ValueError: Telegram token 형식 오류",
                )
            )
        else:
            url = (
                "https://api.telegram.org/bot"
                + urllib.parse.quote(self.telegram_token, safe=":")
                + "/sendMessage"
            )
            for chat_id in self.telegram_chats:
                safe_target = self._telegram_target(chat_id)
                if not re.fullmatch(r"-?\d+", chat_id):
                    results.append(
                        DeliveryResult(
                            "telegram",
                            False,
                            False,
                            "",
                            "ValueError: Telegram chat ID 형식 오류",
                        )
                    )
                    continue
                try:
                    _post_json(url, {"chat_id": chat_id, "text": text[:4000]})
                    results.append(DeliveryResult("telegram", True, True, safe_target))
                except Exception as exc:
                    results.append(self._error("telegram", True, safe_target, exc))

        return NotificationResult(tuple(results))

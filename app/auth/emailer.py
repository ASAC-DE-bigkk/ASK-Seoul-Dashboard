"""SMTP 기반 이메일 인증·비밀번호 재설정 인터페이스."""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class EmailResult:
    configured: bool
    delivered: bool
    error: str = ""


class EmailSender:
    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "").strip()
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.username = os.environ.get("SMTP_USERNAME", "").strip()
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_email = os.environ.get("SMTP_FROM_EMAIL", "").strip()
        self.use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
        self.use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_email)

    def send(self, *, to_email: str, subject: str, text: str) -> EmailResult:
        if not self.configured:
            return EmailResult(configured=False, delivered=False)
        message = EmailMessage()
        message["From"] = self.from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text)
        try:
            smtp_cls = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
            with smtp_cls(self.host, self.port, timeout=10) as smtp:
                if self.use_tls and not self.use_ssl:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
            return EmailResult(configured=True, delivered=True)
        except Exception as exc:
            # 자격증명·서버 응답 원문을 API로 전달하지 않는다.
            return EmailResult(
                configured=True,
                delivered=False,
                error=f"{type(exc).__name__}: 이메일 전송 실패",
            )

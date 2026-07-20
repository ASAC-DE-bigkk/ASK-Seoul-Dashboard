"""SMTP 기반 이메일 인증·비밀번호 재설정 인터페이스."""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name}은 true 또는 false여야 합니다.")


def _smtp_port() -> int:
    raw = os.environ.get("SMTP_PORT", "587").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT는 정수여야 합니다.") from exc
    if not 1 <= port <= 65_535:
        raise RuntimeError("SMTP_PORT는 1~65535 범위여야 합니다.")
    return port


@dataclass(frozen=True)
class EmailResult:
    configured: bool
    delivered: bool
    error: str = ""


class EmailSender:
    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "").strip()
        self.port = _smtp_port()
        self.username = os.environ.get("SMTP_USERNAME", "").strip()
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_email = os.environ.get("SMTP_FROM_EMAIL", "").strip()
        self.use_tls = _env_bool("SMTP_USE_TLS", True)
        self.use_ssl = _env_bool("SMTP_USE_SSL", False)
        self.allow_plaintext = _env_bool("SMTP_ALLOW_PLAINTEXT", False)

        if self.use_tls and self.use_ssl:
            raise RuntimeError("SMTP_USE_TLS와 SMTP_USE_SSL은 동시에 true일 수 없습니다.")
        if bool(self.host) != bool(self.from_email):
            raise RuntimeError(
                "SMTP_HOST와 SMTP_FROM_EMAIL은 함께 설정하거나 함께 비워야 합니다."
            )
        if self.from_email:
            display_name, address = parseaddr(self.from_email)
            if (
                not address
                or address != self.from_email
                or display_name
                or "\r" in self.from_email
                or "\n" in self.from_email
            ):
                raise RuntimeError("SMTP_FROM_EMAIL은 단일 이메일 주소여야 합니다.")
        if (
            self.configured
            and os.environ.get("AUTH_ENV", "development").strip().lower()
            == "production"
            and not self.use_tls
            and not self.use_ssl
            and not self.allow_plaintext
        ):
            raise RuntimeError(
                "production SMTP는 TLS/SSL이 필요합니다. "
                "내부 보안 relay만 SMTP_ALLOW_PLAINTEXT=true로 명시적으로 허용하세요."
            )

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

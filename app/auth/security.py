"""비밀번호·세션·토큰·입력 보안 유틸."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import base64
import struct
import time
import unicodedata
from datetime import datetime
from email.utils import parseaddr
from typing import Iterable
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type


# OWASP Argon2id 권고치보다 높은 64 MiB/3회. 서버 자원에 맞춰 성능 측정 후 조정한다.
PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
# 존재하지 않는 계정도 동일한 검증 경로를 태우되 요청마다 새 Argon2 해시를 만들지는 않는다.
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("ask-seoul-dummy-login-password")

ROLE_RANK = {"guest": 10, "member": 20, "operator": 30, "admin": 40}
ROLE_LABELS = {
    "guest": "게스트",
    "member": "일반회원",
    "operator": "운영자",
    "admin": "최고관리자",
}
USER_STATUSES = {"pending", "active", "suspended", "rejected"}
ROLES = set(ROLE_RANK)

COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "qwerty123",
    "123456789012345",
    "adminadmin",
    "letmeinplease",
    "askseoul123456",
}

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
NICKNAME_RE = re.compile(r"^[0-9A-Za-z가-힣][0-9A-Za-z가-힣._-]{1,38}[0-9A-Za-z가-힣]$")


def normalize_email(value: str) -> str:
    _, address = parseaddr(value.strip())
    address = address.casefold()
    if not EMAIL_RE.fullmatch(address):
        raise ValueError("올바른 이메일 주소를 입력하세요.")
    return address


def normalize_nickname(value: str) -> str:
    nickname = unicodedata.normalize("NFKC", value).strip()
    if not NICKNAME_RE.fullmatch(nickname):
        raise ValueError("닉네임은 3~40자의 한글·영문·숫자·._- 조합이어야 합니다.")
    return nickname


def validate_password(password: str, *, email: str = "", nickname: str = "") -> None:
    """NIST 800-63B-4 기준: 길이와 blocklist 중심, 조합 규칙은 강제하지 않는다."""
    if len(password) < 15:
        raise ValueError("비밀번호는 15자 이상이어야 합니다.")
    if len(password) > 128:
        raise ValueError("비밀번호는 128자 이하여야 합니다.")
    folded = unicodedata.normalize("NFKC", password).casefold()
    if folded in COMMON_PASSWORDS:
        raise ValueError("널리 사용되는 비밀번호는 사용할 수 없습니다.")
    local = email.split("@", 1)[0].casefold() if email else ""
    if local and len(local) >= 4 and local in folded:
        raise ValueError("비밀번호에 이메일 아이디를 포함하지 마세요.")
    if nickname and len(nickname) >= 4 and nickname.casefold() in folded:
        raise ValueError("비밀번호에 닉네임을 포함하지 마세요.")


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, bool]:
    try:
        ok = PASSWORD_HASHER.verify(password_hash, password)
        return bool(ok), PASSWORD_HASHER.check_needs_rehash(password_hash)
    except (VerifyMismatchError, InvalidHashError):
        return False, False


def random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def token_digest(token: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def derive_totp_secret(master_key: str, public_id: str, seed_salt: str) -> str:
    material = hmac.new(
        master_key.encode("utf-8"),
        f"{public_id}:{seed_salt}".encode("utf-8"),
        hashlib.sha256,
    ).digest()[:20]
    return base64.b32encode(material).decode("ascii").rstrip("=")


def totp_code(secret: str, counter: int, digits: int = 6) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(
    secret: str,
    value: str,
    *,
    last_counter: int | None = None,
    now: int | None = None,
    window: int = 1,
) -> int | None:
    normalized = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"\d{6}", normalized):
        return None
    current = int((now if now is not None else time.time()) // 30)
    for counter in range(current - window, current + window + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp_code(secret, counter), normalized):
            return counter
    return None


def normalize_recovery_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def recovery_code() -> str:
    raw = secrets.token_hex(8).upper()
    return "-".join((raw[:4], raw[4:8], raw[8:12], raw[12:16]))


def stable_digest(value: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_public_id(public_id: str) -> str:
    if len(public_id) <= 4:
        return public_id
    return public_id[:4] + "*" * min(12, len(public_id) - 4)


def safe_next_path(value: str | None, allowed_prefixes: Iterable[str]) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 0x20 for char in value)
    ):
        return "/catalog"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/catalog"
    path = parsed.path
    if any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes):
        return value
    return "/catalog"


def parse_network(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError as exc:
        raise ValueError("IP 또는 CIDR 형식이 올바르지 않습니다.") from exc


def ip_in_networks(ip: str, networks: Iterable[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for raw in networks:
        try:
            if address in ipaddress.ip_network(raw, strict=False):
                return True
        except ValueError:
            continue
    return False


def role_can_manage(actor_role: str, target_role: str) -> bool:
    if actor_role == "admin":
        return True
    if actor_role == "operator":
        return ROLE_RANK.get(target_role, 999) < ROLE_RANK["operator"]
    return False


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"

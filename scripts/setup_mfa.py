#!/usr/bin/env python3
"""로그인 차단 없이 권한 계정의 최초 MFA를 안전하게 설정한다."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.models import User
from app.auth.security import (
    derive_totp_secret,
    normalize_email,
    random_token,
    verify_password,
)
from app.auth.service import AuthService, DomainError, initialize_database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", help="생략하면 대화형으로 입력")
    parser.add_argument(
        "--reset-existing",
        action="store_true",
        help="기존 MFA를 같은 트랜잭션에서 폐기하고 즉시 재등록하는 break-glass 절차",
    )
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "MFA seed와 복구 코드는 입출력 기록이 남지 않는 보호된 대화형 TTY에서만 "
            "처리할 수 있습니다."
        )
    email = normalize_email(args.email or input("Account email: ").strip())
    password = getpass.getpass("Current password: ")

    settings = load_settings()
    database = Database(
        settings.database_url,
        strict_file_permissions=settings.production,
    )
    initialize_database(database, settings)
    with database.session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            raise SystemExit("account not found")
        ok, _ = verify_password(user.password_hash, password)
        if not ok:
            raise SystemExit("invalid password")
        service = AuthService(db, settings)
        break_glass = user.mfa_enabled_at is not None
        if user.mfa_enabled_at is not None:
            if not args.reset_existing:
                raise SystemExit(
                    "MFA is already enabled. Use a recovery code in the profile, "
                    "or rerun with --reset-existing for audited break-glass re-enrollment."
                )
            confirmation = input(
                f"Type RESET {email} to revoke the current MFA and all sessions: "
            ).strip()
            if confirmation != f"RESET {email}":
                raise SystemExit("confirmation mismatch")
            setup_salt = random_token(24)
            secret = derive_totp_secret(
                settings.mfa_master_key, user.public_id, setup_salt
            )
            issuer = quote("ASK SEOUL", safe="")
            account = quote(user.email, safe="")
            setup = {
                "secret": secret,
                "otpauth_uri": (
                    f"otpauth://totp/{issuer}:{account}?secret={secret}"
                    f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
                ),
                "setup_salt": setup_salt,
            }
        else:
            try:
                setup = service.begin_mfa_setup(
                    user,
                    current_password=password,
                    current_code="",
                    ip_hash="cli",
                    user_agent_hash="setup-mfa-cli",
                )
            except DomainError as exc:
                raise SystemExit(exc.detail) from exc
        print("\nAuthenticator secret:")
        print(setup["secret"])
        print("\notpauth URI:")
        print(setup["otpauth_uri"])
        code = input("\nCurrent 6-digit authenticator code: ").strip()
        try:
            if break_glass:
                recovery_codes = service.force_reenroll_mfa(
                    user,
                    actor=user,
                    setup_salt=setup["setup_salt"],
                    code=code,
                    reason=(
                        "CLI break-glass MFA re-enrollment after identity verification"
                    ),
                    ip_hash="cli",
                )
            else:
                recovery_codes = service.confirm_mfa_setup(
                    user,
                    setup["setup_token"],
                    code,
                    ip_hash="cli",
                    user_agent_hash="setup-mfa-cli",
                )
        except DomainError as exc:
            raise SystemExit(exc.detail) from exc

    print("\nMFA enabled. Store these one-time recovery codes securely:")
    for value in recovery_codes:
        print(value)


if __name__ == "__main__":
    main()

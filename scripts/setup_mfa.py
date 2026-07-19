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
from sqlalchemy.exc import SQLAlchemyError

from app.auth.config import AuthSettings, load_settings
from app.auth.database import Database
from app.auth.models import User
from app.auth.security import (
    derive_totp_secret,
    normalize_email,
    random_token,
    verify_password,
)
from app.auth.service import AuthService, DomainError, initialize_database


MAX_CODE_ATTEMPTS = 5


def _confirm_setup(
    database: Database,
    settings: AuthSettings,
    *,
    user_id: int,
    setup: dict[str, str],
    break_glass: bool,
    expected_state_digest: str,
) -> list[str]:
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        code = getpass.getpass("\nCurrent 6-digit authenticator code: ").strip()
        try:
            with database.session() as db:
                user = db.get(User, user_id)
                if user is None:
                    raise DomainError(404, "user not found", "account not found")
                service = AuthService(db, settings)
                if break_glass:
                    if user.mfa_enabled_at is None:
                        raise DomainError(
                            409,
                            "mfa state changed",
                            "MFA 상태가 변경되었습니다. 새로 시작하세요.",
                        )
                    recovery_codes = service.force_reenroll_mfa(
                        user,
                        actor=user,
                        setup_salt=setup["setup_salt"],
                        code=code,
                        reason=(
                            "CLI break-glass MFA re-enrollment after identity verification"
                        ),
                        ip_hash="cli",
                        expected_state_digest=expected_state_digest,
                    )
                else:
                    if user.mfa_enabled_at is not None:
                        raise DomainError(
                            409,
                            "mfa state changed",
                            "MFA가 이미 설정되었습니다. 새로 시작하세요.",
                        )
                    recovery_codes = service.confirm_mfa_setup(
                        user,
                        setup["setup_token"],
                        code,
                        ip_hash="cli",
                        user_agent_hash="setup-mfa-cli",
                        expected_state_digest=expected_state_digest,
                    )
            return recovery_codes
        except DomainError as exc:
            retryable = exc.title in {"mfa failed", "mfa mismatch"}
            remaining = MAX_CODE_ATTEMPTS - attempt
            if not retryable or remaining == 0:
                raise SystemExit(
                    f"{exc.detail} 표시된 인증 앱 항목을 삭제하고 "
                    "스크립트를 다시 실행하세요."
                ) from exc
            print(
                f"{exc.detail} 새 코드를 다시 입력하세요. 남은 횟수: {remaining}",
                file=sys.stderr,
            )
        except SQLAlchemyError as exc:
            raise SystemExit(
                "MFA 데이터베이스 처리에 실패했습니다. 표시된 인증 앱 항목을 "
                "삭제하고 상태 확인 후 다시 실행하세요."
            ) from exc
    raise AssertionError("unreachable")


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
            account_email = user.email
            public_id = user.public_id
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
        user_id = user.id
        expected_state_digest = service.mfa_security_state_digest(user)

    if break_glass:
        confirmation = input(
            f"Type RESET {account_email} to revoke the current MFA and all sessions: "
        ).strip()
        if confirmation != f"RESET {account_email}":
            raise SystemExit("confirmation mismatch")
        setup_salt = random_token(24)
        secret = derive_totp_secret(
            settings.mfa_master_key, public_id, setup_salt
        )
        issuer = quote("ASK SEOUL", safe="")
        account = quote(account_email, safe="")
        setup = {
            "secret": secret,
            "otpauth_uri": (
                f"otpauth://totp/{issuer}:{account}?secret={secret}"
                f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
            ),
            "setup_salt": setup_salt,
        }

    print(
        "\n인증 앱에 수동 설정 키로 등록하세요 "
        "(시간 기반 TOTP, SHA-1, 6자리, 30초; QR 미지원)."
    )
    print("Seed와 URI를 채팅·티켓·브라우저 주소창에 붙여넣지 마세요.")
    print("\nAuthenticator secret:")
    print(setup["secret"])
    print("\notpauth URI:")
    print(setup["otpauth_uri"])
    try:
        recovery_codes = _confirm_setup(
            database,
            settings,
            user_id=user_id,
            setup=setup,
            break_glass=break_glass,
            expected_state_digest=expected_state_digest,
        )
    except (EOFError, KeyboardInterrupt) as exc:
        raise SystemExit(
            "\nMFA 설정을 취소했습니다. 표시된 인증 앱 항목을 삭제하고 다시 실행하세요."
        ) from exc

    print("\nMFA enabled. Store these one-time recovery codes securely:")
    for value in recovery_codes:
        print(value)


if __name__ == "__main__":
    main()

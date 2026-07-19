#!/usr/bin/env python3
"""최초 최고관리자를 안전하게 생성하거나 기존 계정을 승격한다."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.models import User, utcnow
from app.auth.security import hash_password, normalize_email, validate_password
from app.auth.service import AuthService, _unique_nickname, audit, initialize_database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", help="생략하면 대화형으로 입력")
    parser.add_argument(
        "--promote-existing",
        action="store_true",
        help="기존 계정의 비밀번호를 교체하고 최고관리자로 승격",
    )
    parser.add_argument(
        "--create-additional",
        action="store_true",
        help="사용자가 있는 DB에 별도 최고관리자 계정을 추가",
    )
    args = parser.parse_args()
    if args.promote_existing and args.create_additional:
        raise SystemExit(
            "--promote-existing와 --create-additional은 함께 사용할 수 없습니다."
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "관리자 자격증명은 입출력 기록이 남지 않는 보호된 대화형 TTY에서만 생성할 수 있습니다."
        )
    email = normalize_email(args.email or input("Admin email: ").strip())
    password = getpass.getpass("Admin password (15-128 chars): ")
    validate_password(password, email=email)

    settings = load_settings()
    database = Database(
        settings.database_url,
        strict_file_permissions=settings.production,
    )
    initialize_database(database, settings)
    with database.session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            existing_users = db.scalar(select(User.id).limit(1))
            if existing_users is not None:
                if not args.create_additional:
                    raise SystemExit(
                        "database already has users; 추가 최고관리자 생성이 의도된 "
                        "경우에만 --create-additional을 사용하세요."
                    )
                confirmation = input(
                    f"Type CREATE ADMIN {email} to add another admin: "
                ).strip()
                if confirmation != f"CREATE ADMIN {email}":
                    raise SystemExit("confirmation mismatch")
            user = User(
                email=email,
                password_hash=hash_password(password),
                nickname=_unique_nickname(db),
                role="admin",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            audit(db, "admin_cli_created", target=user)
            action = "created"
        else:
            if not args.promote_existing:
                raise SystemExit(
                    "account already exists; 승격·비밀번호 교체가 의도된 경우에만 "
                    "--promote-existing를 사용하세요."
                )
            confirmation = input(
                f"Type PROMOTE {email} to replace its password and grant admin: "
            ).strip()
            if confirmation != f"PROMOTE {email}":
                raise SystemExit("confirmation mismatch")
            user.password_hash = hash_password(password)
            user.password_changed_at = utcnow()
            user.role = "admin"
            user.status = "active"
            user.email_verified_at = user.email_verified_at or utcnow()
            user.approved_at = user.approved_at or utcnow()
            AuthService(db, settings).revoke_user_sessions(user.id)
            audit(
                db,
                "admin_cli_promoted",
                target=user,
                details={"source": "local_operator_cli"},
            )
            action = "promoted"
    print(f"{action} admin: {email}")
    if settings.require_mfa_for_privileged:
        print(f"next: {sys.executable} scripts/setup_mfa.py --email {email}")


if __name__ == "__main__":
    main()

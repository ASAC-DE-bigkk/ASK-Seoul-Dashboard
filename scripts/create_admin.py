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
    args = parser.parse_args()
    email = normalize_email(args.email or input("Admin email: ").strip())
    password = getpass.getpass("Admin password (15-128 chars): ")
    validate_password(password, email=email)

    settings = load_settings()
    database = Database(settings.database_url)
    initialize_database(database, settings)
    with database.session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
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
            user.password_hash = hash_password(password)
            user.password_changed_at = utcnow()
            user.role = "admin"
            user.status = "active"
            user.email_verified_at = user.email_verified_at or utcnow()
            user.approved_at = user.approved_at or utcnow()
            AuthService(db, settings).revoke_user_sessions(user.id)
            audit(db, "admin_cli_promoted", actor=user, target=user)
            action = "promoted"
    print(f"{action} admin: {email}")
    if settings.require_mfa_for_privileged:
        print(f"next: python3 scripts/setup_mfa.py --email {email}")


if __name__ == "__main__":
    main()

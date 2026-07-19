#!/usr/bin/env python3
"""인증 임시 데이터를 보수적으로 정리한다. 기본은 dry-run이다."""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, or_, select, update

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.models import (
    AccountToken,
    AuthSession,
    IpBlock,
    MfaChallenge,
    MfaRecoveryCode,
    NotificationDelivery,
    utcnow,
)
from app.auth.service import verify_database_schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="생략하면 건수만 출력")
    parser.add_argument("--token-days", type=int, default=7)
    parser.add_argument("--session-days", type=int, default=30)
    parser.add_argument("--recovery-days", type=int, default=30)
    parser.add_argument("--notification-days", type=int, default=90)
    args = parser.parse_args()

    settings = load_settings()
    database = Database(
        settings.database_url,
        enable_sqlite_wal=args.apply,
        strict_file_permissions=settings.production,
    )
    verify_database_schema(database)
    now = utcnow()
    token_cutoff = now - timedelta(days=max(1, args.token_days))
    session_cutoff = now - timedelta(days=max(1, args.session_days))
    recovery_cutoff = now - timedelta(days=max(1, args.recovery_days))
    notification_cutoff = now - timedelta(days=max(1, args.notification_days))

    with database.session() as db:
        filters = {
            "account_tokens": and_filter(
                AccountToken.expires_at < token_cutoff,
                or_(
                    AccountToken.consumed_at.is_(None),
                    AccountToken.consumed_at < token_cutoff,
                ),
            ),
            "mfa_challenges": MfaChallenge.expires_at < token_cutoff,
            "sessions": or_(
                AuthSession.expires_at < session_cutoff,
                and_filter(
                    AuthSession.revoked_at.is_not(None),
                    AuthSession.revoked_at < session_cutoff,
                ),
            ),
            "recovery_codes": and_filter(
                MfaRecoveryCode.used_at.is_not(None),
                MfaRecoveryCode.used_at < recovery_cutoff,
            ),
            "notification_deliveries": and_filter(
                NotificationDelivery.status.not_in(("queued", "processing")),
                NotificationDelivery.created_at < notification_cutoff,
            ),
        }
        models = {
            "account_tokens": AccountToken,
            "mfa_challenges": MfaChallenge,
            "sessions": AuthSession,
            "recovery_codes": MfaRecoveryCode,
            "notification_deliveries": NotificationDelivery,
        }
        counts = {
            name: db.scalar(select(func.count(model.id)).where(filters[name])) or 0
            for name, model in models.items()
        }
        expired_blocks = db.scalar(
            select(func.count(IpBlock.id)).where(
                IpBlock.active.is_(True),
                IpBlock.expires_at.is_not(None),
                IpBlock.expires_at <= now,
            )
        ) or 0
        if args.apply:
            for name, model in models.items():
                db.execute(delete(model).where(filters[name]))
            db.execute(
                update(IpBlock)
                .where(
                    IpBlock.active.is_(True),
                    IpBlock.expires_at.is_not(None),
                    IpBlock.expires_at <= now,
                )
                .values(active=False)
            )

    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode} account_tokens={counts['account_tokens']} "
        f"mfa_challenges={counts['mfa_challenges']} sessions={counts['sessions']} "
        f"recovery_codes={counts['recovery_codes']} "
        f"notification_deliveries={counts['notification_deliveries']} "
        f"expired_ip_blocks={expired_blocks}"
    )


def and_filter(*conditions):
    from sqlalchemy import and_

    return and_(*conditions)


if __name__ == "__main__":
    main()

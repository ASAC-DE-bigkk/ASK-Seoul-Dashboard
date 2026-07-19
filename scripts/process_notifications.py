#!/usr/bin/env python3
"""중단 후 남은 운영 알림 outbox를 복구하고 처리한다."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.service import (
    process_queued_notifications,
    recover_stale_notifications,
    verify_database_schema,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--stale-minutes", type=int, default=5)
    args = parser.parse_args()

    settings = load_settings()
    database = Database(
        settings.database_url,
        strict_file_permissions=settings.production,
    )
    verify_database_schema(database)
    recovered = recover_stale_notifications(
        database, older_than_minutes=max(1, args.stale_minutes)
    )
    processed = process_queued_notifications(database, limit=max(1, args.limit))
    print(f"recovered={recovered} processed={processed}")


if __name__ == "__main__":
    main()

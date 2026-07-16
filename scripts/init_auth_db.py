#!/usr/bin/env python3
"""DATABASE_URL에 인증/권한 스키마와 기본 정책을 초기화한다."""
from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.service import initialize_database


def main() -> None:
    settings = load_settings()
    database = Database(settings.database_url)
    initialize_database(database, settings)
    print(f"initialized auth schema: {database.engine.url.render_as_string(hide_password=True)}")


if __name__ == "__main__":
    main()

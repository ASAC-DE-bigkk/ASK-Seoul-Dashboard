import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.models import Base, SchemaVersion
from app.auth.service import initialize_database


def test_auth_schema_compiles_for_primary_rdb_dialects():
    dialects = (sqlite.dialect(), postgresql.dialect(), mysql.dialect())
    for dialect in dialects:
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            assert "CREATE TABLE" in ddl
            for index in table.indexes:
                index_ddl = str(CreateIndex(index).compile(dialect=dialect))
                assert "CREATE" in index_ddl and "INDEX" in index_ddl


def test_sqlite_v1_schema_migrates_to_current(tmp_path):
    path = tmp_path / "legacy-v1.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE auth_schema_version (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version INTEGER NOT NULL UNIQUE,
          applied_at DATETIME NOT NULL
        );
        INSERT INTO auth_schema_version(version, applied_at)
        VALUES (1, CURRENT_TIMESTAMP);

        CREATE TABLE auth_users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          public_id VARCHAR(36) NOT NULL UNIQUE,
          email VARCHAR(320) NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          nickname VARCHAR(40) NOT NULL UNIQUE,
          role VARCHAR(20) NOT NULL,
          status VARCHAR(20) NOT NULL,
          email_verified_at DATETIME,
          approved_at DATETIME,
          approved_by_id INTEGER,
          membership_ends_at DATETIME,
          failed_login_count INTEGER NOT NULL DEFAULT 0,
          locked_until DATETIME,
          last_login_at DATETIME,
          password_changed_at DATETIME NOT NULL,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        CREATE TABLE auth_payment_plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code VARCHAR(20) NOT NULL UNIQUE,
          label VARCHAR(50) NOT NULL,
          duration_days INTEGER NOT NULL,
          price_amount INTEGER NOT NULL,
          currency VARCHAR(3) NOT NULL,
          active BOOLEAN NOT NULL
        );
        CREATE TABLE auth_payment_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          public_id VARCHAR(36) NOT NULL UNIQUE,
          user_id INTEGER NOT NULL,
          plan_id INTEGER NOT NULL,
          status VARCHAR(20) NOT NULL,
          requested_at DATETIME NOT NULL,
          reviewed_at DATETIME,
          reviewed_by_id INTEGER,
          review_note VARCHAR(500) NOT NULL,
          notification_result JSON NOT NULL
        );
        CREATE TABLE auth_notification_deliveries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          payment_request_id INTEGER,
          channel VARCHAR(30) NOT NULL,
          target VARCHAR(120) NOT NULL,
          status VARCHAR(20) NOT NULL,
          error VARCHAR(300) NOT NULL,
          created_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    initialize_database(database, settings)

    inspector = inspect(database.engine)
    user_columns = {item["name"] for item in inspector.get_columns("auth_users")}
    payment_columns = {
        item["name"] for item in inspector.get_columns("auth_payment_requests")
    }
    delivery_columns = {
        item["name"]
        for item in inspector.get_columns("auth_notification_deliveries")
    }
    session_columns = {
        item["name"] for item in inspector.get_columns("auth_sessions")
    }
    assert {
        "mfa_enabled_at",
        "mfa_seed_salt",
        "mfa_last_counter",
        "terms_accepted_at",
        "terms_version",
    } <= user_columns
    assert "pending_key" in payment_columns
    assert {"attempts", "last_attempt_at"} <= delivery_columns
    assert "remembered" in session_columns
    assert "auth_control" in inspector.get_table_names()
    assert "uq_auth_payment_pending_key" in {
        item["name"] for item in inspector.get_indexes("auth_payment_requests")
    }
    assert {
        "ix_auth_notification_created",
        "ix_auth_notification_status_attempt",
        "ix_auth_notification_request_status",
    } <= {
        item["name"]
        for item in inspector.get_indexes("auth_notification_deliveries")
    }
    with database.session() as db:
        assert db.scalar(
            select(SchemaVersion.version).order_by(SchemaVersion.version.desc())
        ) == 6


def test_unversioned_auth_schema_fails_closed_but_unrelated_tables_are_allowed(
    tmp_path,
):
    settings = replace(
        load_settings(),
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )

    unrelated_path = tmp_path / "shared.db"
    connection = sqlite3.connect(unrelated_path)
    connection.execute("CREATE TABLE unrelated_data (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    unrelated_db = Database(f"sqlite:///{unrelated_path}")
    initialize_database(
        unrelated_db,
        replace(settings, database_url=f"sqlite:///{unrelated_path}"),
    )
    assert "auth_schema_version" in inspect(unrelated_db.engine).get_table_names()

    legacy_path = tmp_path / "unversioned-auth.db"
    connection = sqlite3.connect(legacy_path)
    connection.execute("CREATE TABLE auth_users (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    legacy_db = Database(f"sqlite:///{legacy_path}")
    with pytest.raises(RuntimeError, match="버전 정보"):
        initialize_database(
            legacy_db,
            replace(settings, database_url=f"sqlite:///{legacy_path}"),
        )

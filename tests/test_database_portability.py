import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.auth.config import load_settings
from app.auth.database import Database
from app.auth.models import (
    AccountToken,
    AuthControl,
    AuthSession,
    Base,
    IpBlock,
    MfaChallenge,
    MfaRecoveryCode,
    NotificationDelivery,
    PaymentPlan,
    PaymentRequest,
    SchemaVersion,
    User,
    utcnow,
)
from app.auth.service import (
    initialize_database,
    prepare_database_for_app,
    verify_database_schema,
)


PROJECT_ROOT = Path(__file__).parents[1]


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_sqlite_runtime_files_are_private(tmp_path):
    path = tmp_path / "private-auth" / "auth.db"
    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    initialize_database(database, settings)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_schema_verification_is_read_only_and_maintenance_does_not_reinitialize(
    tmp_path,
):
    path = tmp_path / "maintenance.db"
    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    initialize_database(database, settings)
    now = utcnow()
    with database.session() as db:
        user = User(
            email="cleanup@example.com",
            password_hash="test-only",
            nickname="cleanup-test-user",
            role="member",
            status="active",
        )
        db.add(user)
        db.flush()
        plan = db.scalar(select(PaymentPlan).where(PaymentPlan.code == "daily"))
        payment = PaymentRequest(
            user_id=user.id,
            plan_id=plan.id,
            status="approved",
            pending_key=None,
        )
        db.add(payment)
        db.flush()
        db.add_all(
            [
                AccountToken(
                    user_id=user.id,
                    purpose="verify_email",
                    token_hash="a" * 64,
                    expires_at=now - timedelta(days=8),
                ),
                AccountToken(
                    user_id=user.id,
                    purpose="verify_email",
                    token_hash="b" * 64,
                    expires_at=now + timedelta(days=1),
                ),
                MfaChallenge(
                    user_id=user.id,
                    token_hash="c" * 64,
                    purpose="login",
                    expires_at=now - timedelta(days=8),
                ),
                MfaChallenge(
                    user_id=user.id,
                    token_hash="d" * 64,
                    purpose="login",
                    expires_at=now + timedelta(minutes=5),
                ),
                AuthSession(
                    user_id=user.id,
                    token_hash="e" * 64,
                    csrf_hash="f" * 64,
                    expires_at=now - timedelta(days=31),
                ),
                AuthSession(
                    user_id=user.id,
                    token_hash="1" * 64,
                    csrf_hash="2" * 64,
                    expires_at=now + timedelta(hours=1),
                ),
                MfaRecoveryCode(
                    user_id=user.id,
                    code_hash="3" * 64,
                    used_at=now - timedelta(days=31),
                ),
                MfaRecoveryCode(
                    user_id=user.id,
                    code_hash="4" * 64,
                    used_at=None,
                ),
                NotificationDelivery(
                    payment_request_id=payment.id,
                    channel="slack",
                    status="delivered",
                    created_at=now - timedelta(days=91),
                ),
                NotificationDelivery(
                    payment_request_id=payment.id,
                    channel="slack",
                    status="delivered",
                    created_at=now,
                ),
                IpBlock(
                    network="198.51.100.1/32",
                    reason="expired test",
                    active=True,
                    expires_at=now - timedelta(minutes=1),
                ),
                IpBlock(
                    network="198.51.100.2/32",
                    reason="active test",
                    active=True,
                    expires_at=now + timedelta(days=1),
                ),
            ]
        )

    tracked_models = (
        AccountToken,
        MfaChallenge,
        AuthSession,
        MfaRecoveryCode,
        NotificationDelivery,
        IpBlock,
    )

    def row_snapshot(db_instance):
        with db_instance.engine.connect() as connection:
            return {
                model.__tablename__: [
                    tuple(row)
                    for row in connection.execute(
                        select(model.__table__).order_by(model.id)
                    ).all()
                ]
                for model in tracked_models
            }

    with database.engine.connect() as connection:
        control_before = connection.execute(
            select(AuthControl.revision, AuthControl.updated_at).where(
                AuthControl.id == 1
            )
        ).one()
    rows_before = row_snapshot(database)
    verify_database_schema(database)
    database.engine.dispose()
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    raw.close()

    verify_database_schema(
        Database(f"sqlite:///{path}", enable_sqlite_wal=False)
    )
    env = os.environ.copy()
    env.update(
        {
            "AUTH_ENV": "test",
            "DATABASE_URL": f"sqlite:///{path}",
            "AUTH_SESSION_PEPPER": "maintenance-test-session-pepper-long-value",
            "AUTH_MFA_MASTER_KEY": "maintenance-test-mfa-master-key-long-value",
            "AUTH_BOOTSTRAP_ADMIN_EMAIL": "",
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD": "",
        }
    )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "cleanup_auth.py")],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    after_dry_run = Database(
        f"sqlite:///{path}", enable_sqlite_wal=False
    )
    with after_dry_run.engine.connect() as connection:
        control_after = connection.execute(
            select(AuthControl.revision, AuthControl.updated_at).where(
                AuthControl.id == 1
            )
        ).one()
        assert connection.exec_driver_sql(
            "PRAGMA journal_mode"
        ).scalar_one() == "delete"
    assert control_after == control_before
    assert row_snapshot(after_dry_run) == rows_before

    applied = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "cleanup_auth.py"),
            "--apply",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    with Database(f"sqlite:///{path}").engine.connect() as connection:
        assert connection.scalar(select(AccountToken.id).where(AccountToken.token_hash == "a" * 64)) is None
        assert connection.scalar(select(AccountToken.id).where(AccountToken.token_hash == "b" * 64)) is not None
        assert connection.scalar(select(MfaChallenge.id).where(MfaChallenge.token_hash == "c" * 64)) is None
        assert connection.scalar(select(MfaChallenge.id).where(MfaChallenge.token_hash == "d" * 64)) is not None
        assert connection.scalar(select(AuthSession.id).where(AuthSession.token_hash == "e" * 64)) is None
        assert connection.scalar(select(AuthSession.id).where(AuthSession.token_hash == "1" * 64)) is not None
        assert connection.scalar(select(MfaRecoveryCode.id).where(MfaRecoveryCode.code_hash == "3" * 64)) is None
        assert connection.scalar(select(MfaRecoveryCode.id).where(MfaRecoveryCode.code_hash == "4" * 64)) is not None
        blocks = {
            row.network: row.active
            for row in connection.execute(
                select(IpBlock.network, IpBlock.active)
            )
        }
        assert blocks == {
            "198.51.100.1/32": False,
            "198.51.100.2/32": True,
        }


def test_bootstrap_cannot_add_an_admin_to_nonempty_database(tmp_path):
    path = tmp_path / "bootstrap.db"
    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="first-admin@example.com",
        bootstrap_admin_password="First-Harbor-Secure-Password-2026!",
    )
    database = Database(settings.database_url)
    initialize_database(database, settings)

    second = replace(
        settings,
        bootstrap_admin_email="second-admin@example.com",
        bootstrap_admin_password="Second-Harbor-Secure-Password-2026!",
    )
    with pytest.raises(RuntimeError, match="신규 DB"):
        initialize_database(database, second)
    with database.engine.connect() as connection:
        assert connection.scalar(select(User.id).where(User.role == "admin")) is not None
        assert (
            connection.scalar(
                select(User.id).where(User.email == "second-admin@example.com")
            )
            is None
        )


@pytest.mark.parametrize("version", (None, 6, 7))
def test_invalid_existing_schema_fails_before_creating_tables(tmp_path, version):
    path = tmp_path / f"invalid-version-{version}.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE auth_schema_version (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version INTEGER NOT NULL UNIQUE,
          applied_at DATETIME NOT NULL
        )
        """
    )
    if version is not None:
        connection.execute(
            "INSERT INTO auth_schema_version(version, applied_at) "
            "VALUES (?, CURRENT_TIMESTAMP)",
            (version,),
        )
    connection.commit()
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    connection.close()

    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    with pytest.raises(RuntimeError):
        initialize_database(database, settings)
    after = set(inspect(database.engine).get_table_names())
    assert after == before - {"sqlite_sequence"}


def test_deep_schema_verifier_detects_missing_index(tmp_path):
    path = tmp_path / "missing-index.db"
    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    initialize_database(database, settings)
    verify_database_schema(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX ix_auth_notification_status_attempt"
        )
    with pytest.raises(RuntimeError, match="indexes=.*ix_auth_notification_status_attempt"):
        verify_database_schema(database)


def test_production_app_requires_preinitialized_schema(tmp_path):
    path = tmp_path / "production-start.db"
    settings = replace(
        load_settings(),
        env="production",
        database_url=f"sqlite:///{path}",
        bootstrap_admin_email="",
        bootstrap_admin_password="",
    )
    database = Database(settings.database_url)
    with pytest.raises(RuntimeError, match="초기화"):
        prepare_database_for_app(database, settings)
    assert inspect(database.engine).get_table_names() == []

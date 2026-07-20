"""SQLAlchemy 기반 멀티 RDB 연결과 세션 관리."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import stat
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


logger = logging.getLogger(__name__)


class Database:
    def __init__(
        self,
        url: str,
        *,
        enable_sqlite_wal: bool = True,
        strict_file_permissions: bool = False,
    ):
        self.url = url
        self._sqlite_path: Path | None = None
        self._enable_sqlite_wal = enable_sqlite_wal
        self._strict_file_permissions = strict_file_permissions
        connect_args: dict = {}
        parsed_url = make_url(url)
        if parsed_url.get_backend_name() == "sqlite":
            connect_args["check_same_thread"] = False
            db_path = parsed_url.database
            if db_path and db_path != ":memory:" and not db_path.startswith("file:"):
                path = Path(db_path).expanduser().resolve()
                parent_created = not path.parent.exists()
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if parent_created:
                    self._chmod(path.parent, 0o700)
                default_runtime = Path(__file__).resolve().parents[2] / "data"
                if path.parent == default_runtime.resolve():
                    self._chmod(path.parent, 0o700)
                self._sqlite_path = path
        self.engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
        if parsed_url.get_backend_name() == "sqlite":
            event.listen(self.engine, "connect", self._sqlite_pragmas)
        self.Session = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    def _sqlite_pragmas(self, dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if self._enable_sqlite_wal:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        if self._sqlite_path is not None:
            for path in (
                self._sqlite_path,
                Path(f"{self._sqlite_path}-wal"),
                Path(f"{self._sqlite_path}-shm"),
            ):
                if path.exists():
                    self._chmod(path, 0o600)

    def _chmod(self, path: Path, mode: int) -> None:
        if os.name == "nt":
            return
        try:
            path.chmod(mode)
            actual = stat.S_IMODE(path.stat().st_mode)
            if actual != mode:
                raise OSError(
                    f"requested mode {oct(mode)}, actual mode {oct(actual)}"
                )
        except OSError as exc:
            if self._strict_file_permissions:
                raise RuntimeError(
                    f"SQLite 인증 저장소 권한을 {oct(mode)}로 제한할 수 없습니다: {path}"
                ) from exc
            logger.warning(
                "SQLite auth storage permission hardening failed path=%s mode=%s",
                path,
                oct(mode),
            )

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

"""Engine/session management and Alembic-based initialization."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
log = logging.getLogger(__name__)


def retry_on_conflict[**P, R](fn: Callable[P, R], attempts: int = 3) -> Callable[P, R]:
    """Retry an idempotent write when a concurrent writer inserted the same unique row.

    The decorated methods check for existing rows before inserting, so re-running them
    after a unique-constraint conflict skips what the other writer already committed.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except sa_exc.IntegrityError:
                if attempt + 1 >= attempts:
                    raise
                log.info("write conflict; retrying", extra={"fn": fn.__qualname__, "attempt": attempt + 1})
        raise AssertionError("unreachable")

    return wrapper


def make_engine(database_url: str) -> Engine:
    kwargs: dict = {"future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
            Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            if ":memory:" not in database_url:
                cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cur.close()

    return engine


def alembic_config(database_url: str):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade_database(database_url: str) -> None:
    """Apply all Alembic migrations (idempotent)."""
    from alembic import command

    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database_url), "head")


class Database:
    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine = make_engine(database_url)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Test helper: create tables directly from metadata (no Alembic)."""
        from finance_agent.storage.models import Base

        Base.metadata.create_all(self.engine)

    def migrate(self) -> None:
        upgrade_database(self.url)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

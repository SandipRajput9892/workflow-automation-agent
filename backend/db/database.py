"""SQLAlchemy engine and session management (SQLite by default)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        is_sqlite = url.startswith("sqlite")
        if is_sqlite and ":memory:" not in url:
            Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        # Workflows run in worker threads, so SQLite connections must be shareable across threads.
        self.engine: Engine = create_engine(url, connect_args={"check_same_thread": False} if is_sqlite else {})
        if is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from backend.db import models  # noqa: F401  (registers the tables)

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session that commits on success and rolls back on error."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer thread
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with request.app.state.db.session() as session:
        yield session

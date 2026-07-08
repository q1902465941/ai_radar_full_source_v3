from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings


def _default_database_url() -> str:
    return f"sqlite:///{settings.db_path}"


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or _default_database_url()
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False, "timeout": 30.0} if is_sqlite else {}
    built = create_engine(url, connect_args=connect_args, future=True)
    if is_sqlite:
        _configure_sqlite_engine(built)
    return built


def _configure_sqlite_engine(target: Engine) -> None:
    @event.listens_for(target, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db(bind: Engine | None = None) -> None:
    from backend.app.db.models import Base

    target = bind or engine
    try:
        Base.metadata.create_all(target)
    except OperationalError as exc:
        if not _is_concurrent_create_all_exists(exc):
            raise
        Base.metadata.create_all(target, checkfirst=True)


def _is_concurrent_create_all_exists(exc: OperationalError) -> bool:
    parts = [str(exc)]
    if getattr(exc, "orig", None) is not None:
        parts.append(str(exc.orig))
    if getattr(exc, "statement", None):
        parts.append(str(exc.statement))
    message = " ".join(parts).lower()
    return "already exists" in message and "create table" in message


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    session.expire_on_commit = False
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

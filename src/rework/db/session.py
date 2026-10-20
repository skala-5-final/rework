from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from rework.config import get_settings
from rework.db.models import Base, Event


@lru_cache
def get_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def _factory():
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Session:
    s = _factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    """FastAPI 의존성."""
    with session_scope() as s:
        yield s


def init_db() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)


def record_event(job_id: str, run_id: str | None, type_: str, stage: str = "", message: str = "", data: dict | None = None) -> None:
    with session_scope() as s:
        s.add(Event(job_id=job_id, run_id=run_id, type=type_, stage=stage, message=message, data=data or {}))

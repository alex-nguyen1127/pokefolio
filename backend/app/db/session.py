"""Shared synchronous engine and session factory."""

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# Resolve from this file so launching from backend/ still finds the root .env.
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Initialize lazily so basic health stays available without a database."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")

    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use postgresql+psycopg")

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"connect_timeout": 5},
        hide_parameters=True,
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Provide a request session and close it on success or failure."""
    with get_session_factory()() as session:
        yield session

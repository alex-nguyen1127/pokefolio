"""Alembic environment for the synchronous PostgreSQL database."""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from app.db.base import Base
from app.models import Card, Transaction, User  # noqa: F401 -- register with Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Match the application's root .env loading and environment precedence.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise ValueError("DATABASE_URL is required")
url = make_url(database_url)
if url.drivername != "postgresql+psycopg":
    raise ValueError("DATABASE_URL must use postgresql+psycopg")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without connecting to PostgreSQL."""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Use synchronous psycopg for migrations and autogenerate."""
    engine = create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 5},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

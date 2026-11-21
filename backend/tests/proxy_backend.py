"""Test-only live backend for scripts/test_api_proxy.py; never use in deployment."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import session as database
from app.db.base import Base
from app.main import app
from app.models import Card

# The driver owns a fresh temporary directory and an initially nonexistent file.
# No PostgreSQL factory is ever permitted in this fixture, even by accident.
def forbid_development_database():
    raise RuntimeError("Live proxy tests must use their isolated database")


database.get_session_factory = forbid_development_database
path = Path(os.environ["POKEFOLIO_PROXY_TEST_DATABASE"])
if path.exists() or not path.parent.name.startswith("pokefolio-proxy-"):
    raise RuntimeError("Refusing a preexisting or non-test database")
engine = create_engine(f"sqlite:///{path}", hide_parameters=True)
Base.metadata.create_all(engine)
with Session(engine) as session:
    session.add(Card(id="proxy-test-card", name="Proxy test", set_name="Test", card_number="1"))
    session.commit()


def isolated_session():
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        yield session


app.dependency_overrides[database.get_session] = isolated_session

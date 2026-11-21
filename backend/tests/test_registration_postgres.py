"""Opt-in concurrent registration test in a disposable test-only schema."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.auth import RegisterRequest
from app.services.auth import EmailAlreadyRegisteredError, register_user


@unittest.skipUnless(os.environ.get("POKEFOLIO_TEST_DATABASE_URL"),
                     "Set POKEFOLIO_TEST_DATABASE_URL for isolated PostgreSQL testing")
class PostgreSQLRegistrationTests(unittest.TestCase):
    def test_concurrent_registrations_have_one_winner(self):
        url = make_url(os.environ["POKEFOLIO_TEST_DATABASE_URL"])
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.database, "pokefolio_test",
                         "Refusing any database other than pokefolio_test")
        schema = "registration_" + uuid4().hex
        admin = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")), "pokefolio_test")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        def cleanup_schema():
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

        self.addCleanup(cleanup_schema)
        engine = create_engine(url, hide_parameters=True, connect_args={
            "connect_timeout": 5,
            "options": f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=8000",
        })
        self.addCleanup(engine.dispose)
        User.__table__.create(engine)
        ready = Barrier(2)

        def synchronize_inserts(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith("INSERT INTO USERS"):
                ready.wait(timeout=10)

        event.listen(engine, "before_cursor_execute", synchronize_inserts)
        data = RegisterRequest(email="Concurrent@Example.com", password="synthetic test password")

        def register():
            with Session(engine) as session:
                try:
                    user = register_user(session, data)
                    return ("created", user.email)
                except EmailAlreadyRegisteredError:
                    self.assertFalse(session.in_transaction())
                    return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(result[0] for result in results), ["conflict", "created"])
        with Session(engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(User)), 1)
            self.assertEqual(session.scalar(select(User.email)), "concurrent@example.com")

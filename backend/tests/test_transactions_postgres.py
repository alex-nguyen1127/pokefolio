"""Opt-in PostgreSQL lock test. Never use the development database."""

import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Card, Transaction, User
from app.schemas.transaction import TransactionCreate
from app.services.portfolio import get_user_holdings
from app.services.transactions import InsufficientHoldingsError, create_transaction


@unittest.skipUnless(os.environ.get("POKEFOLIO_TEST_DATABASE_URL"),
                     "Set POKEFOLIO_TEST_DATABASE_URL for isolated PostgreSQL testing")
class PostgreSQLConcurrencyTests(unittest.TestCase):
    def test_concurrent_sells_cannot_oversell(self):
        url = make_url(os.environ["POKEFOLIO_TEST_DATABASE_URL"])
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.database, "pokefolio_test",
                         "Refusing any database other than pokefolio_test")
        schema = "concurrency_" + uuid4().hex
        admin = create_engine(url, hide_parameters=True,
                              connect_args={"connect_timeout": 5})
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")),
                             "pokefolio_test")
            self.assertEqual(connection.get_isolation_level(), "READ COMMITTED",
                             "This test requires the server's default READ COMMITTED")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        def remove_test_schema():
            # Only this run's randomly named schema; never drop a database.
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

        self.addCleanup(remove_test_schema)
        engine = create_engine(
            url, hide_parameters=True,
            connect_args={"connect_timeout": 5, "options":
                          f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=8000"},
        )
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        user_id, card_id = uuid4(), "lock-test-card"
        with Session(engine) as session:
            session.add_all([
                User(id=user_id, email="lock@example.test", password_hash="unused"),
                Card(id=card_id, name="Lock test", set_name="Test", card_number="1"),
            ])
            session.commit()
            create_transaction(session, user_id, TransactionCreate(
                card_id=card_id, type="BUY", quantity=2, price_per_unit=Decimal("1.00")
            ))
        with Session(engine) as session:
            self.assertEqual(get_user_holdings(session, user_id),
                             [{"card_id": card_id, "quantity_owned": 2}])

        locked, release, second_ready = Event(), Event(), Event()
        pids = {}

        def sell(label):
            with engine.connect() as connection:
                self.assertEqual(connection.get_isolation_level(), "READ COMMITTED")
                pids[label] = connection.scalar(text("SELECT pg_backend_pid()"))
                connection.commit()  # Service must receive an idle session.
                if label == "first":
                    def pause_after_lock(conn, cursor, statement, params, context, many):
                        if "FOR UPDATE" in statement.upper():
                            locked.set()
                            if not release.wait(15):
                                raise TimeoutError("Timed out waiting to release user lock")
                    event.listen(connection, "after_cursor_execute", pause_after_lock)
                else:
                    second_ready.set()
                with Session(bind=connection, expire_on_commit=False) as session:
                    try:
                        create_transaction(session, user_id, TransactionCreate(
                            card_id=card_id, type="SELL", quantity=2,
                            price_per_unit=Decimal("1.00"),
                        ))
                        return "saved"
                    except InsufficientHoldingsError:
                        return "insufficient"

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(sell, "first")
            try:
                self.assertTrue(locked.wait(5), "First SELL did not acquire user lock")
                second = executor.submit(sell, "second")
                self.assertTrue(second_ready.wait(5), "Second SELL did not connect")
                self.assertNotEqual(pids["first"], pids["second"])
                deadline = time.monotonic() + 5
                contended = False
                with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
                    while time.monotonic() < deadline:
                        blockers = monitor.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["second"]}
                        )
                        if pids["first"] in blockers:
                            contended = True
                            break
                        time.sleep(0.02)
                self.assertTrue(contended, "Second SELL never blocked on first SELL's lock")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=15), "saved")
            self.assertEqual(second.result(timeout=15), "insufficient")

        with Session(engine) as session:
            self.assertEqual(get_user_holdings(session, user_id), [])
            sell_count = session.scalar(select(func.count()).select_from(Transaction).where(
                Transaction.user_id == user_id, Transaction.card_id == card_id,
                Transaction.type == "SELL",
            ))
            self.assertEqual(sell_count, 1)

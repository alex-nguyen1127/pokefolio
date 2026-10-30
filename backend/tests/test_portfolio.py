"""Holdings aggregation tests using SQLite in memory, never PostgreSQL."""

import unittest
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import Card, Transaction, User
from app.services.portfolio import get_user_holdings


class PortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        # Create only isolated test tables; application schema is never touched.
        for model in (Card, User, Transaction):
            model.__table__.create(self.engine)
        self.session = Session(self.engine, autoflush=False)
        self.addCleanup(self.session.close)
        self.user_id = uuid4()
        self.other_user_id = uuid4()
        self.session.add_all(
            [
                User(id=self.user_id, email="first@example.test", password_hash="unused"),
                User(id=self.other_user_id, email="second@example.test", password_hash="unused"),
                Card(id="card-a", name="A", set_name="Test", card_number="1"),
                Card(id="card-b", name="B", set_name="Test", card_number="2"),
            ]
        )
        self.session.flush()

    def add_transaction(
        self, card_id: str, transaction_type: str, quantity: int,
        user_id: UUID | None = None,
    ) -> None:
        self.session.add(
            Transaction(
                user_id=user_id if user_id is not None else self.user_id,
                card_id=card_id,
                type=transaction_type,
                quantity=quantity,
                price_per_unit=Decimal("1.00"),
            )
        )
        # Match the application's autoflush=False session configuration too.
        self.session.flush()

    def test_multiple_buys_are_grouped_by_card(self) -> None:
        self.add_transaction("card-b", "BUY", 2)
        self.add_transaction("card-a", "BUY", 3)
        self.add_transaction("card-a", "BUY", 4)
        self.assertEqual(
            get_user_holdings(self.session, self.user_id),
            [
                {"card_id": "card-a", "quantity_owned": 7},
                {"card_id": "card-b", "quantity_owned": 2},
            ],
        )

    def test_sells_reduce_holdings(self) -> None:
        self.add_transaction("card-a", "BUY", 10)
        self.add_transaction("card-a", "SELL", 2)
        self.add_transaction("card-a", "SELL", 3)
        self.assertEqual(
            get_user_holdings(self.session, self.user_id),
            [{"card_id": "card-a", "quantity_owned": 5}],
        )

    def test_zero_and_negative_holdings_are_excluded(self) -> None:
        self.add_transaction("card-a", "BUY", 2)
        self.add_transaction("card-a", "SELL", 2)
        self.add_transaction("card-b", "SELL", 1)
        self.assertEqual(get_user_holdings(self.session, self.user_id), [])

    def test_users_are_isolated(self) -> None:
        self.add_transaction("card-a", "BUY", 3)
        self.add_transaction("card-a", "SELL", 1)
        self.add_transaction("card-a", "BUY", 100, self.other_user_id)
        self.add_transaction("card-b", "BUY", 5, self.other_user_id)
        self.assertEqual(
            get_user_holdings(self.session, self.user_id),
            [{"card_id": "card-a", "quantity_owned": 2}],
        )
        self.assertEqual(
            get_user_holdings(self.session, self.other_user_id),
            [
                {"card_id": "card-a", "quantity_owned": 100},
                {"card_id": "card-b", "quantity_owned": 5},
            ],
        )

    def test_user_without_transactions_has_no_holdings(self) -> None:
        self.add_transaction("card-a", "BUY", 1, self.other_user_id)
        self.assertEqual(get_user_holdings(self.session, self.user_id), [])


if __name__ == "__main__":
    unittest.main()

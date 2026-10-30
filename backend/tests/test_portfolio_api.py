"""API tests against isolated SQLite; never connect to PostgreSQL."""

import unittest
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Card, Transaction, User


class PortfolioApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.user_id, self.other_user_id = uuid4(), uuid4()
        with Session(self.engine) as session:
            session.add_all([
                User(id=self.user_id, email="first@example.test", password_hash="unused"),
                User(id=self.other_user_id, email="second@example.test", password_hash="unused"),
                Card(id="brs-154", name="Test", set_name="Test", card_number="154"),
            ])
            session.commit()

        def isolated_session():
            with Session(self.engine, autoflush=False) as session:
                yield session

        previous_overrides = app.dependency_overrides.copy()
        self.addCleanup(setattr, app, "dependency_overrides", previous_overrides)
        app.dependency_overrides[get_session] = isolated_session
        # Any accidental use of the real factory must fail before connecting.
        factory_guard = patch(
            "app.db.session.get_session_factory",
            side_effect=AssertionError("Development database must not be used"),
        )
        factory_guard.start()
        self.addCleanup(factory_guard.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_successful_retrieval_is_scoped_to_user(self) -> None:
        with Session(self.engine) as session:
            session.add_all([
                Transaction(user_id=self.user_id, card_id="brs-154", type="BUY",
                            quantity=3, price_per_unit=Decimal("1.00")),
                Transaction(user_id=self.user_id, card_id="brs-154", type="SELL",
                            quantity=1, price_per_unit=Decimal("1.00")),
                Transaction(user_id=self.other_user_id, card_id="brs-154", type="BUY",
                            quantity=100, price_per_unit=Decimal("1.00")),
            ])
            session.commit()
        response = self.client.get(f"/users/{self.user_id}/holdings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "holdings": [{"card_id": "brs-154", "quantity_owned": 2}]
        })

    def test_empty_holdings(self) -> None:
        response = self.client.get(f"/users/{self.user_id}/holdings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"holdings": []})

    def test_unknown_user(self) -> None:
        response = self.client.get(f"/users/{uuid4()}/holdings")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "User not found"})

    def test_malformed_uuid(self) -> None:
        response = self.client.get("/users/not-a-uuid/holdings")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["path", "user_id"])

    def test_health_remains_independent_of_database(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_database_health_success(self) -> None:
        with patch("app.api.routes.health.get_session_factory") as factory:
            factory.return_value = lambda: Session(self.engine)
            response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_database_health_failure(self) -> None:
        with patch("app.api.routes.health.get_session_factory", side_effect=ValueError):
            response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Database unavailable"})

    def post_transaction(self, user_id=None, **changes):
        payload = {"card_id": "brs-154", "type": "BUY", "quantity": 3,
                   "price_per_unit": "12.34"}
        payload.update(changes)
        return self.client.post(
            f"/users/{user_id or self.user_id}/transactions", json=payload
        )

    def transaction_count(self):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(Transaction))

    def test_buy_and_sell_update_holdings_and_return_saved_fields(self) -> None:
        from datetime import datetime
        from uuid import UUID

        buy = self.post_transaction()
        self.assertEqual(buy.status_code, 201, buy.text)
        body = buy.json()
        UUID(body["id"])
        self.assertEqual(body["user_id"], str(self.user_id))
        self.assertEqual(body["card_id"], "brs-154")
        self.assertEqual(body["type"], "BUY")
        self.assertEqual(body["quantity"], 3)
        self.assertEqual(Decimal(body["price_per_unit"]), Decimal("12.34"))
        for field in ("created_at", "transacted_at"):
            datetime.fromisoformat(body[field])
        with Session(self.engine) as session:
            saved = session.get(Transaction, UUID(body["id"]))
            self.assertEqual(saved.quantity, 3)
            self.assertEqual(saved.price_per_unit, Decimal("12.34"))
        sell = self.post_transaction(type="SELL", quantity=1, price_per_unit="0.00")
        self.assertEqual(sell.status_code, 201, sell.text)
        self.assertEqual(sell.json()["type"], "SELL")
        self.assertNotEqual(sell.json()["id"], body["id"])
        self.assertEqual(self.client.get(f"/users/{self.user_id}/holdings").json(), {
            "holdings": [{"card_id": "brs-154", "quantity_owned": 2}]
        })
        self.assertEqual(self.post_transaction(type="SELL", quantity=2).status_code, 201)
        self.assertEqual(self.client.get(f"/users/{self.user_id}/holdings").json(),
                         {"holdings": []})

    def test_oversell_rolls_back_without_changing_holdings(self) -> None:
        self.assertEqual(self.post_transaction().status_code, 201)
        response = self.post_transaction(type="SELL", quantity=4)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Insufficient holdings"})
        self.assertEqual(self.transaction_count(), 1)
        self.assertEqual(self.client.get(f"/users/{self.user_id}/holdings").json(), {
            "holdings": [{"card_id": "brs-154", "quantity_owned": 3}]
        })

    def test_other_users_holdings_cannot_be_sold(self) -> None:
        self.assertEqual(self.post_transaction(user_id=self.other_user_id).status_code, 201)
        self.assertEqual(self.post_transaction(type="SELL", quantity=1).status_code, 409)
        self.assertEqual(self.transaction_count(), 1)
        self.assertEqual(self.client.get(f"/users/{self.user_id}/holdings").json(),
                         {"holdings": []})

    def test_other_cards_holdings_cannot_be_sold(self) -> None:
        with Session(self.engine) as session:
            session.add(Card(id="other", name="Other", set_name="Test", card_number="2"))
            session.commit()
        self.assertEqual(self.post_transaction().status_code, 201)
        response = self.post_transaction(card_id="other", type="SELL", quantity=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.transaction_count(), 1)

    def test_missing_transaction_user_or_card(self) -> None:
        response = self.post_transaction(user_id=uuid4())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "User not found"})
        response = self.post_transaction(card_id="missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Card not found"})
        self.assertEqual(self.transaction_count(), 0)

    def test_invalid_transaction_data(self) -> None:
        cases = [
            {"quantity": 0}, {"quantity": -1}, {"quantity": 1.5},
            {"quantity": True}, {"quantity": 2_147_483_648},
            {"price_per_unit": "-0.01"}, {"price_per_unit": "1.001"},
            {"price_per_unit": "1.230"}, {"price_per_unit": "10000000000.00"},
            {"price_per_unit": "NaN"}, {"price_per_unit": "Infinity"},
            {"type": "buy"}, {"type": "TRANSFER"}, {"card_id": ""},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                response = self.post_transaction(**changes)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.post_transaction(user_id="bad-uuid").status_code, 422)
        self.assertEqual(self.transaction_count(), 0)

    def test_maximum_numeric_price_is_accepted(self) -> None:
        response = self.post_transaction(price_per_unit="9999999999.99")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(Decimal(response.json()["price_per_unit"]),
                         Decimal("9999999999.99"))

    def test_service_rolls_back_insert_failure_and_session_can_be_reused(self) -> None:
        from sqlalchemy.exc import IntegrityError

        from app.schemas.transaction import TransactionCreate
        from app.services.transactions import create_transaction

        data = TransactionCreate(card_id="brs-154", type="BUY", quantity=1,
                                 price_per_unit=Decimal("1.00"))
        with Session(self.engine) as session:
            def fail_insert(_mapper, _connection, _target):
                raise IntegrityError("simulated insert failure", {}, Exception())

            event.listen(Transaction, "before_insert", fail_insert)
            try:
                with self.assertRaises(IntegrityError):
                    create_transaction(session, self.user_id, data)
            finally:
                event.remove(Transaction, "before_insert", fail_insert)
            self.assertFalse(session.in_transaction())
            self.assertEqual(self.transaction_count(), 0)
            saved = create_transaction(session, self.user_id, data)
            self.assertEqual(saved.quantity, 1)
        self.assertEqual(self.transaction_count(), 1)

    def seed_history(self):
        from datetime import datetime, timedelta, timezone
        from uuid import UUID

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Expected descending order exercises each of the three sort keys.
        rows = [
            (1, base + timedelta(days=2), base),
            (4, base + timedelta(days=1), base + timedelta(hours=1)),
            (3, base + timedelta(days=1), base),
            (2, base + timedelta(days=1), base),
        ]
        with Session(self.engine) as session:
            for number, transacted_at, created_at in reversed(rows):
                session.add(Transaction(
                    id=UUID(int=number), user_id=self.user_id, card_id="brs-154",
                    type="SELL" if number == 4 else "BUY", quantity=1,
                    price_per_unit=Decimal("12.34"), transacted_at=transacted_at,
                    created_at=created_at,
                ))
            session.add(Transaction(
                user_id=self.other_user_id, card_id="brs-154", type="BUY",
                quantity=99, price_per_unit=Decimal("1.00"),
                transacted_at=base + timedelta(days=3),
            ))
            session.commit()
        return [str(UUID(int=number)) for number, _, _ in rows]

    def test_history_ordering_and_user_isolation(self) -> None:
        expected_ids = self.seed_history()
        response = self.client.get(f"/users/{self.user_id}/transactions")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["limit"], body["offset"], body["total"]), (20, 0, 4))
        self.assertEqual([item["id"] for item in body["transactions"]], expected_ids)
        for item in body["transactions"]:
            self.assertEqual(item["user_id"], str(self.user_id))
            self.assertEqual(item["card_id"], "brs-154")
            self.assertEqual(item["quantity"], 1)
            self.assertEqual(item["price_per_unit"], "12.34")
            self.assertIn("created_at", item)
            self.assertIn("transacted_at", item)
        self.assertEqual(body["transactions"][1]["type"], "SELL")
        other = self.client.get(f"/users/{self.other_user_id}/transactions").json()
        self.assertEqual(other["total"], 1)
        self.assertEqual(other["transactions"][0]["user_id"], str(self.other_user_id))

    def test_history_pagination(self) -> None:
        expected_ids = self.seed_history()
        for offset in (0, 2, 4, 10):
            with self.subTest(offset=offset):
                response = self.client.get(
                    f"/users/{self.user_id}/transactions", params={"limit": 2, "offset": offset}
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual((body["limit"], body["offset"], body["total"]), (2, offset, 4))
                self.assertEqual([item["id"] for item in body["transactions"]],
                                 expected_ids[offset:offset + 2])
        self.assertEqual(self.client.get(
            f"/users/{self.user_id}/transactions?limit=100"
        ).status_code, 200)

    def test_empty_history(self) -> None:
        response = self.client.get(f"/users/{self.user_id}/transactions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "transactions": [], "limit": 20, "offset": 0, "total": 0
        })

    def test_history_maximum_offset_is_accepted(self) -> None:
        self.seed_history()
        response = self.client.get(
            f"/users/{self.user_id}/transactions", params={"offset": 1_000_000}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "transactions": [], "limit": 20, "offset": 1_000_000, "total": 4
        })

    def test_history_oversized_offset_is_rejected(self) -> None:
        for offset in (1_000_001, 9_223_372_036_854_775_808):
            with self.subTest(offset=offset):
                response = self.client.get(
                    f"/users/{self.user_id}/transactions", params={"offset": offset}
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"][0]["loc"], ["query", "offset"])

    def test_invalid_history_pagination(self) -> None:
        for params in ({"limit": 0}, {"limit": -1}, {"limit": 101},
                       {"limit": "abc"}, {"limit": "1.5"},
                       {"offset": -1}, {"offset": "abc"}, {"offset": "1.5"}):
            with self.subTest(params=params):
                response = self.client.get(
                    f"/users/{self.user_id}/transactions", params=params
                )
                self.assertEqual(response.status_code, 422)

    def test_history_unknown_user_and_malformed_uuid(self) -> None:
        response = self.client.get(f"/users/{uuid4()}/transactions")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "User not found"})
        response = self.client.get("/users/not-a-uuid/transactions")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["path", "user_id"])

"""Registration tests against isolated SQLite, never development PostgreSQL."""

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from argon2.exceptions import HashingError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.security import verify_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from tests.rate_limit_helpers import isolate_auth_rate_limit
from app.models import Session as AuthSession, User
from app.schemas.auth import RegisterRequest
from app.services.auth import EmailAlreadyRegisteredError, register_user


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        isolate_auth_rate_limit(self, app)
        csrf_env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://localhost:3000"})
        csrf_env.start()
        self.addCleanup(csrf_env.stop)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool, hide_parameters=True,
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)

        def isolated_session():
            with Session(self.engine, autoflush=False) as session:
                yield session

        previous = app.dependency_overrides.copy()
        self.addCleanup(setattr, app, "dependency_overrides", previous)
        app.dependency_overrides[get_session] = isolated_session
        guard = patch("app.db.session.get_session_factory", side_effect=AssertionError(
            "Development database must not be used"
        ))
        guard.start()
        self.addCleanup(guard.stop)
        self.client = TestClient(app, headers={"Origin": "http://localhost:3000"}, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.password = "synthetic registration password 🐉"

    def post(self, **changes):
        payload = {"email": "trainer@example.com", "password": self.password}
        payload.update(changes)
        return self.client.post("/auth/register", json=payload)

    def count(self):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(User))

    def test_success_normalization_hashing_and_safe_response(self):
        response = self.post(email="  Trainer@EXAMPLE.COM \t")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(set(body), {"id", "email", "created_at"})
        self.assertEqual(body["email"], "trainer@example.com")
        user_id = UUID(body["id"])
        datetime.fromisoformat(body["created_at"])
        self.assertNotIn("set-cookie", response.headers)
        with Session(self.engine) as session:
            saved = session.get(User, user_id)
            self.assertTrue(saved.password_hash.startswith("$argon2id$"))
            self.assertTrue(verify_password(self.password, saved.password_hash))
            self.assertFalse(saved.password_hash in response.text)
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthSession)), 0)
        self.assertFalse(self.password in response.text)

    def test_duplicate_normalized_email_is_conflict(self):
        self.assertEqual(self.post().status_code, 201)
        response = self.post(email=" TRAINER@Example.com ", password="different synthetic password")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Email already registered"})
        self.assertEqual(self.count(), 1)
        with Session(self.engine) as session:
            self.assertTrue(verify_password(self.password, session.scalar(select(User)).password_hash))

    def test_invalid_inputs_are_422_without_echoing_values(self):
        cases = [
            {"email": "invalid"}, {"email": "a@@example.com"}, {"email": ""},
            {"email": "a b@example.com"}, {"email": None}, {"email": 42},
            {"email": "a" * 300 + "@example.com"},
            {"password": "x" * 11}, {"password": "x" * 129},
            {"password": None}, {"password": 42}, {"password": ""},
            {"password_hash": "synthetic forbidden value"},
        ]
        for changes in cases:
            with self.subTest():
                response = self.post(**changes)
                self.assertEqual(response.status_code, 422)
                for error in response.json()["detail"]:
                    self.assertEqual(set(error), {"loc", "type", "msg"})
                self.assertFalse(self.password in response.text)
        for payload in ({}, {"email": "trainer@example.com"}, {"password": self.password}, []):
            response = self.client.post("/auth/register", json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertFalse(self.password in response.text)
        response = self.client.post("/auth/register", content='{"password":',
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.count(), 0)

    def test_password_boundaries_and_whitespace_preserved(self):
        for index, password in enumerate(("x" * 12, "x" * 128, "  padded password  ")):
            response = self.post(email=f"boundary{index}@example.com", password=password)
            self.assertEqual(response.status_code, 201)
            with Session(self.engine) as session:
                saved = session.get(User, UUID(response.json()["id"]))
                self.assertTrue(verify_password(password, saved.password_hash))
                self.assertFalse(verify_password(password + "x", saved.password_hash))
                if password.startswith(" "):
                    self.assertFalse(verify_password(password.strip(), saved.password_hash))

    def test_duplicate_rolls_back_and_session_is_reusable(self):
        data = RegisterRequest(email="trainer@example.com", password=self.password)
        with Session(self.engine) as session:
            register_user(session, data)
            with self.assertRaises(EmailAlreadyRegisteredError):
                register_user(session, data)
            self.assertFalse(session.in_transaction())
            saved = register_user(session, RegisterRequest(
                email="another@example.com", password=self.password,
            ))
            self.assertEqual(saved.email, "another@example.com")
        self.assertEqual(self.count(), 2)

    def test_postgresql_unique_violation_maps_to_conflict(self):
        class UniqueViolation(Exception):
            sqlstate = "23505"
            diag = SimpleNamespace(constraint_name="users_email_key")

        with patch("app.services.auth.Session.flush", side_effect=IntegrityError(
            "synthetic statement", {}, UniqueViolation(), hide_parameters=True,
        )):
            response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Email already registered"})
        self.assertEqual(self.count(), 0)

    def test_unrelated_integrity_failure_is_not_reported_as_duplicate(self):
        def fail_insert(_mapper, _connection, _target):
            raise IntegrityError("synthetic statement", {}, Exception(), hide_parameters=True)

        event.listen(User, "before_insert", fail_insert)
        try:
            response = self.post()
        finally:
            event.remove(User, "before_insert", fail_insert)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, "Internal Server Error")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.post().status_code, 201)

    def test_hashing_failure_does_not_persist_user(self):
        with patch("app.services.auth.hash_password", side_effect=HashingError("synthetic failure")):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, "Internal Server Error")
        self.assertEqual(self.count(), 0)

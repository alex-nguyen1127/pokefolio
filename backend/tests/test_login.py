"""Login tests using only isolated SQLite and synthetic credentials."""

from datetime import timedelta, timezone
from hashlib import sha256
from http.cookies import SimpleCookie
import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import SESSION_COOKIE_NAME, SESSION_MAX_AGE
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from tests.rate_limit_helpers import isolate_auth_rate_limit
from app.models import Session as AuthSession, User
from app.schemas.auth import LoginRequest
from app.services.auth import login_user


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "synthetic login password 🐉"
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        isolate_auth_rate_limit(self, app)
        csrf_env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://localhost:3000"})
        csrf_env.start()
        self.addCleanup(csrf_env.stop)
        env = patch.dict(os.environ, {
            "POKEFOLIO_ENV": "production", "POKEFOLIO_SESSION_COOKIE_SECURE": "true",
        })
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False}, hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            user = User(email="trainer@example.com", password_hash=self.password_hash)
            session.add(user)
            session.commit()
            self.user_id = user.id

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
        self.client = TestClient(app, headers={"Origin": "http://localhost:3000"}, base_url="https://localhost", raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def post(self, **changes):
        data = {"email": "trainer@example.com", "password": self.password}
        data.update(changes)
        return self.client.post("/auth/login", json=data)

    def count(self):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(AuthSession))

    def test_valid_login_cookie_hash_expiration_and_response_safety(self):
        response = self.post(email=" Trainer@EXAMPLE.COM ")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"id", "email", "created_at"})
        self.assertEqual(UUID(body["id"]), self.user_id)
        self.assertEqual(body["email"], "trainer@example.com")
        cookie = SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"].lower(), "lax")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["max-age"], str(SESSION_MAX_AGE))
        self.assertFalse(cookie["domain"])
        self.assertGreaterEqual(len(cookie.value), 43)
        self.assertFalse(cookie.value in response.text)
        self.assertFalse(self.password in response.text)
        self.assertFalse(self.password_hash in response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with Session(self.engine) as session:
            saved = session.scalar(select(AuthSession))
            self.assertEqual(saved.user_id, self.user_id)
            self.assertTrue(saved.token_hash == sha256(cookie.value.encode("ascii")).hexdigest())
            self.assertEqual(len(saved.token_hash), 64)
            self.assertFalse(saved.token_hash in response.text)
            self.assertFalse(saved.token_hash == cookie.value)
            self.assertEqual(saved.expires_at - saved.created_at, timedelta(days=7))
            self.assertIsNone(saved.revoked_at)

    def test_invalid_credentials_are_identical_and_verify_once(self):
        from app.core.security import verify_password

        responses = []
        for changes in ({"email": "absent@example.com"}, {"password": "wrong synthetic password"}):
            with patch("app.services.auth.verify_password", wraps=verify_password) as verify:
                response = self.post(**changes)
                self.assertEqual(verify.call_count, 1)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("set-cookie", response.headers)
            responses.append(response.json())
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[0], {"detail": "Invalid email or password"})
        self.assertEqual(self.count(), 0)

    def test_invalid_data_is_422_without_password_echo(self):
        for changes in ({"email": "invalid"}, {"password": "short"}, {"password": "x" * 129},
                        {"password": None}, {"password": 42}, {"token": "forbidden"}):
            response = self.post(**changes)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("set-cookie", response.headers)
            self.assertFalse(self.password in response.text)
            self.assertTrue(all("input" not in item for item in response.json()["detail"]))
        self.assertEqual(self.count(), 0)

    def test_each_login_generates_a_fresh_token(self):
        first, second = self.post(), self.post()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.headers["set-cookie"] == second.headers["set-cookie"])
        self.assertEqual(self.count(), 2)

    def test_service_commits_and_uses_aware_times_and_secure_randomness(self):
        from secrets import token_urlsafe

        captured = []
        def capture(_mapper, _connection, target):
            captured.append((target.created_at, target.expires_at))

        event.listen(AuthSession, "before_insert", capture)
        try:
            with Session(self.engine) as session:
                with patch("app.services.auth.secrets.token_urlsafe", wraps=token_urlsafe) as generate:
                    user, token = login_user(session, LoginRequest(
                        email="trainer@example.com", password=self.password,
                    ))
                    generate.assert_called_once_with(32)
                self.assertFalse(session.in_transaction())
                self.assertEqual(user.id, self.user_id)
                self.assertFalse(token.get_secret_value() in repr(token))
        finally:
            event.remove(AuthSession, "before_insert", capture)
        self.assertEqual(self.count(), 1)
        self.assertEqual(captured[0][0].tzinfo, timezone.utc)
        self.assertEqual(captured[0][1] - captured[0][0], timedelta(days=7))

    def test_insert_and_commit_failures_roll_back_without_cookie(self):
        for method in ("flush", "commit"):
            # Session.begin() commits via SessionTransaction, not Session.commit().
            target = ("app.services.auth.Session.flush" if method == "flush"
                      else "sqlalchemy.orm.session.SessionTransaction._prepare_impl")
            with patch(target, side_effect=IntegrityError(
                "synthetic failure", {}, Exception(), hide_parameters=True,
            )):
                response = self.post()
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.text, "Internal Server Error")
            self.assertNotIn("set-cookie", response.headers)
            self.assertEqual(self.count(), 0)
        self.assertEqual(self.post().status_code, 200)

    def test_verification_operational_failure_is_not_a_success(self):
        with patch("app.services.auth.verify_password", side_effect=RuntimeError("synthetic failure")):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.count(), 0)

    def test_explicit_local_http_configuration(self):
        with patch.dict(os.environ, {"POKEFOLIO_ENV": "development",
                                     "POKEFOLIO_SESSION_COOKIE_SECURE": "false"}):
            with TestClient(app, headers={"Origin": "http://localhost:3000"}, base_url="http://localhost") as client:
                response = client.post("/auth/login", json={
                    "email": "trainer@example.com", "password": self.password,
                })
            self.assertEqual(response.status_code, 200)
            cookie = SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE_NAME]
            self.assertFalse(cookie["secure"])
            self.assertTrue(cookie["httponly"])

    def test_insecure_production_and_nonlocal_configs_rejected(self):
        for environment, hostname in (("production", "localhost"), ("development", "example.com")):
            with patch.dict(os.environ, {"POKEFOLIO_ENV": environment,
                                         "POKEFOLIO_SESSION_COOKIE_SECURE": "false"}):
                with TestClient(app, headers={"Origin": "http://localhost:3000"}, base_url=f"http://{hostname}") as client:
                    response = client.post("/auth/login", json={
                        "email": "trainer@example.com", "password": self.password,
                    })
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.count(), 0)

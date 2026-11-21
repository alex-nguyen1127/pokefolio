"""Logout tests using isolated SQLite and synthetic session material."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.cookies import SimpleCookie
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import SESSION_COOKIE_NAME
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from tests.rate_limit_helpers import isolate_auth_rate_limit
from app.models import Session as AuthSession, User
from app.services.auth import revoke_session


class LogoutTests(unittest.TestCase):
    def setUp(self):
        isolate_auth_rate_limit(self, app)
        csrf_env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://localhost:3000"})
        csrf_env.start()
        self.addCleanup(csrf_env.stop)
        env = patch.dict(os.environ, {"POKEFOLIO_ENV": "production",
                                     "POKEFOLIO_SESSION_COOKIE_SECURE": "true"})
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False}, hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.tokens = ["synthetic-device-one-session", "synthetic-device-two-session"]
        self.now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            user = User(email="logout@example.com", password_hash="unused")
            session.add(user)
            session.flush()
            self.user_id = user.id
            for token in self.tokens:
                session.add(AuthSession(user_id=user.id, token_hash=sha256(token.encode()).hexdigest(),
                                        created_at=self.now, expires_at=self.now + timedelta(days=7)))
            session.commit()

        def isolated_session():
            with Session(self.engine) as session:
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
        self.authenticate(self.tokens[0])

    def authenticate(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE_NAME, token, domain="localhost.local", path="/")

    def test_logout_commits_only_current_session_and_deletes_cookie(self):
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")
        cookie = SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE_NAME]
        self.assertFalse(cookie.value)
        self.assertEqual(cookie["max-age"], "0")
        self.assertTrue(bool(cookie["expires"]))
        self.assertEqual(cookie["path"], "/")
        self.assertFalse(cookie["domain"])
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "lax")
        self.assertFalse(any(token in response.headers["set-cookie"] for token in self.tokens))
        self.assertNotIn(SESSION_COOKIE_NAME, self.client.cookies)
        with Session(self.engine) as session:
            rows = session.scalars(select(AuthSession)).all()
            self.assertEqual(len(rows), 2)
            for row in rows:
                if row.token_hash == sha256(self.tokens[0].encode()).hexdigest():
                    self.assertIsNotNone(row.revoked_at)
                    self.assertGreaterEqual(row.revoked_at.replace(tzinfo=timezone.utc), self.now)
                else:
                    self.assertIsNone(row.revoked_at)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_revoked_token_replay_fails_on_me_portfolio_and_logout(self):
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        self.authenticate(self.tokens[0])
        responses = [
            self.client.get("/auth/me"),
            self.client.get(f"/users/{self.user_id}/holdings"),
            self.client.get(f"/users/{self.user_id}/transactions"),
            self.client.post(f"/users/{self.user_id}/transactions", json={
                "card_id": "unused", "type": "BUY", "quantity": 1, "price_per_unit": "1.00",
            }),
            self.client.post("/auth/logout"),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Not authenticated"})
            self.assertNotIn("set-cookie", response.headers)

    def test_other_device_remains_authenticated(self):
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        self.authenticate(self.tokens[1])
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(self.client.get(f"/users/{self.user_id}/holdings").status_code, 200)
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)

    def test_missing_invalid_expired_and_revoked_sessions_are_401(self):
        for state in ("missing", "unknown", "expired", "revoked"):
            self.client.cookies.clear()
            if state == "unknown":
                self.authenticate("synthetic-unknown-session")
            elif state != "missing":
                self.authenticate(self.tokens[0])
                with Session(self.engine) as session:
                    row = session.scalar(select(AuthSession).where(
                        AuthSession.token_hash == sha256(self.tokens[0].encode()).hexdigest()))
                    row.expires_at = self.now + timedelta(days=1)
                    row.revoked_at = None
                    if state == "expired":
                        row.expires_at = self.now - timedelta(seconds=1)
                    else:
                        row.revoked_at = self.now
                    session.commit()
            with patch("app.api.routes.auth.revoke_session") as revoke:
                response = self.client.post("/auth/logout")
                revoke.assert_not_called()
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Not authenticated"})
            self.assertNotIn("set-cookie", response.headers)

    def test_database_update_and_commit_failures_roll_back_without_cookie_deletion(self):
        def fail(*args):
            raise OperationalError("synthetic failure", {}, Exception(), hide_parameters=True)

        for target, event_name in ((AuthSession, "before_update"), (self.engine, "commit")):
            event.listen(target, event_name, fail)
            try:
                response = self.client.post("/auth/logout")
            finally:
                event.remove(target, event_name, fail)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.text, "Internal Server Error")
            self.assertNotIn("set-cookie", response.headers)
            with Session(self.engine) as session:
                self.assertTrue(all(row.revoked_at is None for row in session.scalars(select(AuthSession))))
            self.assertEqual(self.client.get("/auth/me").status_code, 200)

    def test_service_uses_aware_utc_and_reuses_session_after_rollback(self):
        with Session(self.engine) as session:
            row = session.scalar(select(AuthSession).where(
                AuthSession.token_hash == sha256(self.tokens[0].encode()).hexdigest()))
            with patch.object(session, "commit", side_effect=OperationalError(
                "synthetic failure", {}, Exception(), hide_parameters=True,
            )):
                with self.assertRaises(OperationalError):
                    revoke_session(session, row)
            self.assertFalse(session.in_transaction())
            revoke_session(session, row)
            self.assertFalse(session.in_transaction())
            # expire_on_commit=False keeps the original aware value observable.
        with Session(self.engine, expire_on_commit=False) as session:
            row = session.scalar(select(AuthSession).where(
                AuthSession.token_hash == sha256(self.tokens[1].encode()).hexdigest()))
            revoke_session(session, row)
            self.assertEqual(row.revoked_at.tzinfo, timezone.utc)

    def test_local_http_cookie_deletion(self):
        with patch.dict(os.environ, {"POKEFOLIO_ENV": "development",
                                     "POKEFOLIO_SESSION_COOKIE_SECURE": "false"}):
            with TestClient(app, headers={"Origin": "http://localhost:3000"}, base_url="http://localhost") as client:
                client.cookies.set(SESSION_COOKIE_NAME, self.tokens[0], domain="localhost.local", path="/")
                response = client.post("/auth/logout")
                self.assertEqual(response.status_code, 204)
                self.assertNotIn(SESSION_COOKIE_NAME, client.cookies)
                cookie = SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE_NAME]
                self.assertFalse(cookie["secure"])
                self.assertTrue(cookie["httponly"])
                self.assertEqual(cookie["path"], "/")
                self.assertFalse(cookie["domain"])
                self.assertEqual(cookie["max-age"], "0")

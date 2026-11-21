"""Current-user tests with isolated SQLite and synthetic session material."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.api.dependencies import get_current_user
from app.core.config import SESSION_COOKIE_NAME
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from tests.rate_limit_helpers import isolate_auth_rate_limit
from app.models import Session as AuthSession, User


class CurrentUserTests(unittest.TestCase):
    def setUp(self):
        isolate_auth_rate_limit(self, app)
        csrf_env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://localhost:3000"})
        csrf_env.start()
        self.addCleanup(csrf_env.stop)
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False}, hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        # Leave SQLite FK enforcement off to exercise legacy/orphaned sessions.
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)
        self.token = "synthetic-current-user-session"
        with Session(self.engine) as session:
            user = User(email="current@example.com", password_hash="unused synthetic hash")
            session.add(user)
            session.flush()
            self.user_id = user.id
            session.add(AuthSession(
                user_id=user.id, token_hash=sha256(self.token.encode()).hexdigest(),
                created_at=self.now, expires_at=self.now + timedelta(days=7),
            ))
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

    def me(self, token=None):
        self.client.cookies.clear()
        if token is not None:
            self.client.cookies.set(SESSION_COOKIE_NAME, token)
        return self.client.get("/auth/me")

    def assert_unauthorized(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Not authenticated"})
        self.assertNotIn("set-cookie", response.headers)
        self.assertFalse(self.token in response.text)

    def test_valid_session_returns_only_safe_fields_and_does_not_mutate(self):
        response = self.me(self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"id", "email", "created_at"})
        self.assertEqual(UUID(response.json()["id"]), self.user_id)
        self.assertEqual(response.json()["email"], "current@example.com")
        self.assertFalse(self.token in response.text)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with Session(self.engine) as session:
            saved = session.scalar(select(AuthSession))
            self.assertEqual(saved.expires_at.replace(tzinfo=timezone.utc), self.now + timedelta(days=7))
            self.assertIsNone(saved.revoked_at)
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthSession)), 1)

    def test_missing_empty_and_unknown_cookies(self):
        for token in (None, "", "unknown-synthetic-token"):
            with self.subTest():
                self.assert_unauthorized(self.me(token))

    def test_digest_is_not_accepted_as_token(self):
        self.assert_unauthorized(self.me(sha256(self.token.encode()).hexdigest()))

    def test_expired_and_exact_boundary_sessions(self):
        for expires in (self.now - timedelta(seconds=1), self.now):
            with Session(self.engine) as session:
                saved = session.scalar(select(AuthSession))
                saved.expires_at = expires
                session.commit()
            with patch("app.api.dependencies.datetime") as clock:
                clock.now.return_value = self.now
                self.assert_unauthorized(self.me(self.token))
                clock.now.assert_called_once_with(timezone.utc)

    def test_revoked_session(self):
        with Session(self.engine) as session:
            saved = session.scalar(select(AuthSession))
            saved.revoked_at = self.now
            session.commit()
        self.assert_unauthorized(self.me(self.token))

    def test_deleted_user(self):
        with Session(self.engine) as session:
            session.execute(delete(User).where(User.id == self.user_id))
            session.commit()
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthSession)), 1)
        self.assert_unauthorized(self.me(self.token))

    def test_dependency_returns_sqlalchemy_user_and_queries_only_digest(self):
        captured = []
        def capture(connection, cursor, statement, parameters, context, many):
            captured.append(parameters)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            request = Request({"type": "http", "headers": [
                (b"cookie", f"{SESSION_COOKIE_NAME}={self.token}".encode()),
            ]})
            with Session(self.engine) as session:
                user = get_current_user(request, session)
                self.assertIsInstance(user, User)
                self.assertEqual(user.id, self.user_id)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(captured), 1)
        self.assertTrue(sha256(self.token.encode()).hexdigest() in captured[0])
        self.assertFalse(self.token in captured[0])

    def test_dependency_can_be_reused_by_another_route(self):
        other = FastAPI()
        other.dependency_overrides[get_session] = app.dependency_overrides[get_session]

        @other.get("/probe")
        def probe(user: User = Depends(get_current_user)):
            return {"id": str(user.id)}

        with TestClient(other) as client:
            client.cookies.set(SESSION_COOKIE_NAME, self.token)
            response = client.get("/probe")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": str(self.user_id)})

    def test_login_cookie_works_with_me(self):
        with patch.dict("os.environ", {"POKEFOLIO_ENV": "production",
                                       "POKEFOLIO_SESSION_COOKIE_SECURE": "true"}):
            registration = self.client.post("/auth/register", json={
                "email": "new@example.com", "password": "synthetic integration password",
            })
            self.assertEqual(registration.status_code, 201)
            login = self.client.post("/auth/login", json={
                "email": "new@example.com", "password": "synthetic integration password",
            })
            self.assertEqual(login.status_code, 200)
            response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), registration.json())

    def test_database_failure_does_not_authenticate_or_expose_details(self):
        with patch("app.api.dependencies.Session.execute", side_effect=OperationalError(
            "synthetic query", {}, Exception(), hide_parameters=True,
        )):
            response = self.me(self.token)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, "Internal Server Error")
        self.assertNotIn("set-cookie", response.headers)


# This optional check uses only a fresh schema in the dedicated test database.
@unittest.skipUnless(os.environ.get("POKEFOLIO_TEST_DATABASE_URL"),
                     "Set POKEFOLIO_TEST_DATABASE_URL for isolated PostgreSQL testing")
class PostgreSQLCurrentUserTests(unittest.TestCase):
    def test_expiry_uses_instants_with_non_utc_connection_timezone(self):
        url = make_url(os.environ["POKEFOLIO_TEST_DATABASE_URL"])
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.database, "pokefolio_test",
                         "Refusing any database other than pokefolio_test")
        schema = "current_user_" + uuid4().hex
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
        AuthSession.__table__.create(engine)
        instant = datetime.now(timezone.utc)
        offset = timezone(timedelta(hours=5, minutes=45))
        token = "synthetic-postgres-current-session"
        request = Request({"type": "http", "headers": [
            (b"cookie", f"{SESSION_COOKIE_NAME}={token}".encode()),
        ]})
        with Session(engine) as session:
            session.execute(text("SET TIME ZONE 'Asia/Kathmandu'"))
            user = User(email="timezone@example.com", password_hash="unused")
            session.add(user)
            session.flush()
            saved = AuthSession(
                user_id=user.id, token_hash=sha256(token.encode()).hexdigest(),
                created_at=instant, expires_at=(instant + timedelta(hours=1)).astimezone(offset),
            )
            session.add(saved)
            session.commit()
            with patch("app.api.dependencies.datetime") as clock:
                clock.now.return_value = instant
                self.assertEqual(get_current_user(request, session).id, user.id)
                for expiry in (instant, instant - timedelta(seconds=1)):
                    saved.expires_at = expiry.astimezone(offset)
                    session.commit()
                    with self.assertRaises(HTTPException) as failure:
                        get_current_user(request, session)
                    self.assertEqual(failure.exception.status_code, 401)

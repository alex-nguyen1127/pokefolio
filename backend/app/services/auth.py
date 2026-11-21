"""User registration with database-enforced email uniqueness."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import SESSION_MAX_AGE
from app.core.security import hash_password, verify_password
from app.models import Session as AuthSession, User
from app.schemas.auth import LoginRequest, RegisterRequest


class EmailAlreadyRegisteredError(Exception):
    pass


def _is_email_conflict(error: IntegrityError) -> bool:
    original = error.orig
    if getattr(original, "sqlstate", None) == "23505":
        return getattr(getattr(original, "diag", None), "constraint_name", None) == "users_email_key"
    # Isolated SQLite tests use the same model constraint.
    return (
        getattr(original, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
        and str(original) == "UNIQUE constraint failed: users.email"
    )


def register_user(session: Session, data: RegisterRequest) -> User:
    """Insert atomically using an idle session; never pre-check email uniqueness.

    The unique constraint arbitrates concurrent inserts. Only its email violation
    becomes a conflict; unrelated integrity and operational failures propagate.
    """
    password_hash = hash_password(data.password.get_secret_value())
    try:
        with session.begin():
            user = User(email=str(data.email), password_hash=password_hash)
            session.add(user)
            session.flush()
            session.refresh(user)
            session.expunge(user)
    except IntegrityError as error:
        # The transaction context has rolled back before translating the error.
        if _is_email_conflict(error):
            raise EmailAlreadyRegisteredError from None
        raise
    return user


# Perform one real Argon2 verification even when the account is absent.
# Generated once per process, never logged or persisted.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


class InvalidCredentialsError(Exception):
    pass


def login_user(session: Session, data: LoginRequest) -> tuple[User, SecretStr]:
    """Verify credentials and commit a session before returning its token.

    Caller supplies an idle session. Transaction errors roll back and propagate;
    the raw token is kept out of ORM objects and masked in the return value.
    """
    with session.begin():
        user = session.scalar(select(User).where(User.email == str(data.email)))
        valid = verify_password(
            data.password.get_secret_value(),
            user.password_hash if user is not None else _DUMMY_PASSWORD_HASH,
        )
        if user is None or not valid:
            raise InvalidCredentialsError
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc)
        session.add(AuthSession(
            user_id=user.id,
            token_hash=sha256(token.encode("ascii")).hexdigest(),
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=SESSION_MAX_AGE),
        ))
        session.flush()
        session.expunge(user)
    return user, SecretStr(token)


def revoke_session(session: Session, auth_session: AuthSession) -> None:
    """Commit revocation of the validated session in the request transaction.

    Authentication has already opened this session's transaction. A failed
    flush or commit rolls it back; no cookie should be cleared until this returns.
    """
    try:
        auth_session.revoked_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise

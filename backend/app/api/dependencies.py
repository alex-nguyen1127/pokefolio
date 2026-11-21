"""Reusable request authentication dependencies."""

from datetime import datetime, timezone
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SESSION_COOKIE_NAME
from app.db.session import get_session
from app.models import Session as AuthSession, User


def _get_session_and_user(request: Request, session: Session) -> tuple[AuthSession, User]:
    """Resolve an active cookie session without modifying it.

    Compare expiry in SQL against an aware UTC instant. PostgreSQL timestamptz
    compares absolute instants regardless of the connection's timezone; no
    timezone information is stripped or reinterpreted in Python.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        token_hash = sha256(token.encode("utf-8")).hexdigest()
        result = session.execute(
            select(User, AuthSession)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == token_hash,
                AuthSession.expires_at > datetime.now(timezone.utc),
                AuthSession.revoked_at.is_(None),
            )
        ).one_or_none()
        if result is not None:
            user, auth_session = result
            return auth_session, user
    raise HTTPException(
        status_code=401, detail="Not authenticated", headers={"Cache-Control": "no-store"},
    )


def get_current_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> User:
    """Return the user from a validated active session."""
    return _get_session_and_user(request, session)[1]


def get_current_session(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> AuthSession:
    """Return the active session using the same validation as current-user lookup."""
    return _get_session_and_user(request, session)[0]

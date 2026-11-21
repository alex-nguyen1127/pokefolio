"""Registration API with password-safe validation responses."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_session, get_current_user
from app.models import Session as AuthSession, User
from app.core.config import SESSION_COOKIE_NAME, SESSION_MAX_AGE, session_cookie_secure
from app.db.session import get_session
from app.schemas.auth import LoginRequest, RegisterRequest, UserResponse
from app.services.auth import (
    EmailAlreadyRegisteredError, InvalidCredentialsError, login_user, register_user, revoke_session,
)


class AuthRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_validation_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                # FastAPI normally includes raw input, including passwords, in 422s.
                return JSONResponse(status_code=422, content={"detail": [
                    {key: item[key] for key in ("type", "loc", "msg")}
                    for item in error.errors()
                ]})

        return safe_validation_handler


router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    data: RegisterRequest,
    session: Annotated[Session, Depends(get_session)],
) -> UserResponse:
    try:
        user = register_user(session, data)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    return UserResponse.model_validate(user)


@router.post("/login", response_model=UserResponse)
def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> UserResponse:
    try:
        secure = session_cookie_secure(request.url.hostname)
    except ValueError:
        raise HTTPException(status_code=503, detail="Authentication unavailable") from None
    try:
        user, token = login_user(session, data)
    except InvalidCredentialsError:
        raise HTTPException(status_code=401, detail="Invalid email or password") from None
    result = UserResponse.model_validate(user)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token.get_secret_value(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/me", response_model=UserResponse)
def current_user(
    user: Annotated[User, Depends(get_current_user)],
    response: Response,
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    auth_session: Annotated[AuthSession, Depends(get_current_session)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    try:
        secure = session_cookie_secure(request.url.hostname)
    except ValueError:
        raise HTTPException(status_code=503, detail="Authentication unavailable") from None
    revoke_session(session, auth_session)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME, path="/", domain=None,
        secure=secure, httponly=True, samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response

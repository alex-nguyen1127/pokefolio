"""FastAPI application entry point."""

from fastapi import FastAPI

from app.core.csrf import CSRFMiddleware
from app.core.rate_limit import AuthRateLimiter, AuthRateLimitMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.api.routes.portfolio import router as portfolio_router

app = FastAPI(title="PokéFolio API")
app.state.auth_rate_limiter = AuthRateLimiter()
app.add_middleware(AuthRateLimitMiddleware)
# Starlette's last-added middleware runs first: reject CSRF before admission.
app.add_middleware(CSRFMiddleware)
app.include_router(health_router)
app.include_router(portfolio_router)
app.include_router(auth_router)

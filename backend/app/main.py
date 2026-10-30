"""FastAPI application entry point."""

from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.portfolio import router as portfolio_router

app = FastAPI(title="PokéFolio API")
app.include_router(health_router)
app.include_router(portfolio_router)

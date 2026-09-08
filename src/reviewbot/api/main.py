"""FastAPI application: webhook receiver plus dashboard read APIs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from reviewbot import __version__
from reviewbot.db.database import DATABASE_URL, get_db, init_db
from reviewbot.utils.config import settings

from . import routes, webhooks

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables on startup and log the effective configuration."""
    init_db()
    logger.info(
        "ReviewBot AI %s starting (env=%s, model=%s)",
        __version__,
        settings.app_env,
        settings.anthropic_model,
    )
    if not settings.github_webhook_secret:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set: webhook signatures cannot be verified"
        )
    if not settings.anthropic_auth_token:
        logger.warning("ANTHROPIC_AUTH_TOKEN is not set: reviews will fail")
    if not settings.github_token:
        logger.warning("GITHUB_TOKEN is not set: GitHub calls will fail")
    yield
    logger.info("ReviewBot AI shutting down")


app = FastAPI(
    title="ReviewBot AI",
    description="AI-powered code review for GitHub pull requests",
    version=__version__,
    lifespan=lifespan,
)

_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # Credentials cannot be combined with a wildcard origin; browsers reject it.
    allow_credentials="*" not in _origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(routes.router)


@app.get("/", tags=["meta"])
def root() -> Dict[str, Any]:
    """Service banner."""
    return {
        "name": "ReviewBot AI",
        "version": __version__,
        "status": "running",
        "docs": "/docs",
        "endpoints": {
            "webhook": "POST /webhook/github",
            "reviews": "GET /api/reviews",
            "metrics": "GET /api/metrics",
            "manual_review": "POST /api/review",
        },
    }


@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Liveness probe: reports database reachability and which secrets are present.

    Only booleans are returned - never the credential values themselves.
    """
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.error("Database health check failed: %s", exc)
        database = "error"

    return {
        "status": "healthy" if database == "ok" else "degraded",
        "version": __version__,
        "environment": settings.app_env,
        "database": database,
        "database_backend": DATABASE_URL.split("://", 1)[0],
        "model": settings.anthropic_model,
        "config": {
            "github_token": bool(settings.github_token),
            "webhook_secret": bool(settings.github_webhook_secret),
            "anthropic_auth_token": bool(settings.anthropic_auth_token),
        },
    }

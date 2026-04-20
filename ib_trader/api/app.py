"""FastAPI application factory.

The API server is a thin read layer + command submitter.
It has NO broker connection — all order execution goes through
the engine service via the pending_commands SQLite table.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import scoped_session

from ib_trader.api.deps import set_session_factory
from ib_trader.api.routes import commands, trades, orders, alerts, system, bots, templates, positions, logs, watchlist
from ib_trader.api import ws

logger = logging.getLogger(__name__)


def create_app(
    session_factory: scoped_session,
    cors_origins: list[str] | None = None,
    api_key: str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        session_factory: SQLAlchemy scoped session factory for DB access.
        cors_origins: Allowed CORS origins. Defaults to localhost dev servers.
        api_key: If set, enables Bearer token auth on all API endpoints.
    """
    if cors_origins is None:
        cors_origins = [
            "http://localhost:5173",   # Vite dev server
            "http://localhost:5174",
            "http://localhost:5175",
            "http://localhost:3000",
        ]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        set_session_factory(session_factory)

        # Connect to Redis for real-time data
        try:
            from ib_trader.config.loader import load_settings
            settings = load_settings("config/settings.yaml")
            redis_url = settings.get("redis_url", "redis://localhost:6379/0")
            from ib_trader.redis.client import get_redis
            redis = await get_redis(redis_url)
            from ib_trader.api.deps import set_redis
            set_redis(redis)
            logger.info('{"event": "API_REDIS_CONNECTED"}')
        except Exception as e:
            logger.warning('{"event": "API_REDIS_FAILED", "error": "%s"}', str(e))

        logger.info('{"event": "API_SERVER_STARTED"}')
        try:
            yield
        except asyncio.CancelledError:
            pass  # Graceful shutdown via Ctrl+C
        try:
            from ib_trader.redis.client import close_redis
            await close_redis()
        except Exception as e:
            logger.debug("redis close failed on shutdown", exc_info=e)
        logger.info('{"event": "API_SERVER_STOPPED"}')

    app = FastAPI(
        title="IB Trader API",
        description="REST API for the IB Trader platform. "
                    "Reads from SQLite, submits commands to the engine service.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Auth middleware (if API key configured)
    if api_key:
        from ib_trader.api.auth import APIKeyMiddleware
        app.add_middleware(APIKeyMiddleware, api_key=api_key)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(commands.router)
    app.include_router(trades.router)
    app.include_router(orders.router)
    app.include_router(alerts.router)
    app.include_router(system.router)
    app.include_router(positions.router)
    app.include_router(bots.router)
    app.include_router(templates.router)
    app.include_router(logs.router)
    app.include_router(watchlist.router)
    app.include_router(ws.router)

    return app

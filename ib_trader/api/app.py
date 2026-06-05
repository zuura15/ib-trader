"""FastAPI application factory.

The API server is a thin read layer + command submitter.
It has NO broker connection — all order execution goes through
the engine service via the pending_commands SQLite table.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import scoped_session

from ib_trader.api.deps import set_session_factory
from ib_trader.api.routes import commands, trades, orders, alerts, system, bots, bot_trades, templates, positions, logs, watchlist, instruments, history, sr, regime, debug, audit, console_pnl
from ib_trader.api import ws

logger = logging.getLogger(__name__)

# Project root = .../ib-trader. Resolved from this file's location so we
# don't depend on cwd. Used to locate the optional ``frontend/dist``
# bundle that ``make prod`` produces and ib-api serves directly.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "dist"


def _mount_frontend_dist(app: FastAPI) -> None:
    """Serve the prebuilt frontend bundle from this FastAPI app.

    Skips silently if ``frontend/dist`` is absent — that's the dev-mode
    path where Vite serves the bundle from :5173 and proxies /api → us.
    When the bundle IS present (``make prod`` workflow), we mount the
    hashed-assets directory and add an SPA catch-all so client-side
    routing works without a separate reverse proxy.

    Must be called AFTER all API + WS routers are registered so the
    catch-all only matches paths the API doesn't claim.
    """
    if not _FRONTEND_DIST.is_dir():
        logger.info(
            '{"event": "FRONTEND_DIST_ABSENT", "path": "%s", "detail":'
            ' "skipping static mount; dev mode (Vite) expected"}',
            _FRONTEND_DIST,
        )
        return
    assets_dir = _FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    index_html = _FRONTEND_DIST / "index.html"
    if not index_html.is_file():
        logger.warning(
            '{"event": "FRONTEND_DIST_INCOMPLETE", "path": "%s", "detail":'
            ' "no index.html, SPA catch-all not registered"}',
            _FRONTEND_DIST,
        )
        return

    # SPA catch-all: any path the API didn't match falls through to
    # here. First try the path as a literal file (favicon.ico,
    # robots.txt, manifest.json — anything Vite drops at the dist
    # root). If that doesn't resolve to a real file inside dist,
    # serve index.html so React Router (or our flexlayout SPA
    # equivalent) handles the route client-side.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catch_all(full_path: str) -> FileResponse:
        # API + WS surfaces are matched by their specific routers
        # earlier; if we get here with an /api/* (or /ws*) prefix, the
        # caller hit an UNREGISTERED API endpoint and expects JSON 404,
        # not an SPA HTML page. Returning index.html for these would
        # confuse JSON consumers (the api ↔ engine HTTP calls, browser
        # ``fetch`` checking response.status, etc.).
        if full_path.startswith(("api/", "ws")):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = _FRONTEND_DIST / full_path
        try:
            # Resolve and ensure the path stays inside dist —
            # cheap path-traversal guard. ``relative_to`` raises
            # ``ValueError`` if the resolved candidate escapes.
            resolved = candidate.resolve()
            resolved.relative_to(_FRONTEND_DIST.resolve())
            if resolved.is_file():
                return FileResponse(resolved)
        except (ValueError, OSError):
            pass
        return FileResponse(index_html)

    logger.info(
        '{"event": "FRONTEND_DIST_MOUNTED", "path": "%s"}', _FRONTEND_DIST,
    )


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

        # Populate the in-memory bot registry from config/bots/*.yaml.
        # Without this, registry_config.get_by_name() returns None for
        # every lookup — the WebSocket `subscribe_bot` handler silently
        # aborts on that, which breaks the Bots-pane SharesCell and
        # ForceSellButton live updates. The bot runner does its own
        # load(); the API server had been missing this call.
        try:
            from ib_trader.bots import registry_config
            defns = registry_config.load()
            logger.info(
                '{"event": "API_BOT_REGISTRY_LOADED", "count": %d}', len(defns),
            )
        except Exception as e:
            logger.warning(
                '{"event": "API_BOT_REGISTRY_LOAD_FAILED", "error": "%s"}', str(e),
            )

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
    app.include_router(bot_trades.router)
    app.include_router(audit.router)
    app.include_router(templates.router)
    app.include_router(logs.router)
    app.include_router(watchlist.router)
    app.include_router(instruments.router)
    app.include_router(history.router)
    app.include_router(sr.router)
    app.include_router(regime.router)
    app.include_router(debug.router)
    app.include_router(ws.router)
    app.include_router(console_pnl.router)

    # Must come AFTER all routers so the SPA catch-all only matches
    # paths that no API / WS route claimed. No-op when ``frontend/dist``
    # doesn't exist (dev mode — Vite serves the UI from :5173).
    _mount_frontend_dist(app)

    return app

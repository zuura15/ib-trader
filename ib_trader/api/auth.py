"""API key authentication middleware.

Single static API key stored in .env as API_SECRET_KEY.
If not set, authentication is disabled (development mode).
Header: Authorization: Bearer <key>

This is a simple single-user auth scheme appropriate for a
self-hosted trading platform. Not intended for multi-tenant use.
"""
import logging
import os

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validates Bearer token against API_SECRET_KEY from environment."""

    def __init__(self, app, api_key: str | None = None):
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # Skip auth if no key configured (development mode)
        if not self._api_key:
            return await call_next(request)

        # WebSocket: check token in query string (/ws?token=<key>)
        if request.url.path == "/ws":
            token = request.query_params.get("token", "")
            if token != self._api_key:
                raise HTTPException(status_code=403, detail="Invalid WebSocket token")
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Authorization header")

        token = auth[7:]  # Strip "Bearer "
        if token != self._api_key:
            raise HTTPException(status_code=403, detail="Invalid API key")

        return await call_next(request)

"""API access-key authentication for ib-api (single operator, single static key).

The key lives in ``.env`` as ``API_SECRET_KEY``. Two ways to present it:

- ``Authorization: Bearer <key>`` — scripts, curl, and the Burrow Android
  app (its tailnet proxy injects the header on every request, WebSocket
  upgrades included).
- An HttpOnly session cookie — browsers. ``POST /api/auth/login`` checks
  the key once and sets the cookie; the browser then attaches it to every
  ``fetch``, WebSocket and EventSource request without app code. The
  cookie holds an HMAC derived from the key, never the key itself, and is
  ``SameSite=Strict`` so other sites can't ride it (CSRF / cross-site
  WebSocket hijacking).

Protected: everything under ``/api/`` (except the three auth endpoints),
``/ws``, and the OpenAPI docs. The SPA shell and its static assets stay
public so an unauthenticated browser can render the login screen; the
bundle contains no secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from http.cookies import CookieError, SimpleCookie

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ibt_session"
MIN_KEY_LENGTH = 32

# Reachable without credentials. Everything else under the protected
# prefixes needs a valid bearer or session cookie.
PUBLIC_API_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
})
_PROTECTED_EXACT = frozenset({"/ws", "/docs", "/redoc", "/openapi.json", "/api"})
_PROTECTED_PREFIXES = ("/api/", "/ws/", "/docs/")

_SESSION_CONTEXT = b"ib-trader:browser-session:v1"


class APIKeyConfigError(ValueError):
    """API_SECRET_KEY is missing or too weak to serve on the network."""


def validate_api_key(key: str | None) -> str:
    """Return the stripped key, or raise if it is missing or too short."""
    key = (key or "").strip()
    if not key:
        raise APIKeyConfigError(
            "API_SECRET_KEY is not set in .env. ib-api will not serve without it. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if len(key) < MIN_KEY_LENGTH:
        raise APIKeyConfigError(
            f"API_SECRET_KEY must be at least {MIN_KEY_LENGTH} characters "
            f"(got {len(key)}). Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return key


def session_token(api_key: str) -> str:
    """Cookie value for a browser session: HMAC-SHA256 of a fixed context under the key."""
    return hmac.new(api_key.encode(), _SESSION_CONTEXT, hashlib.sha256).hexdigest()


def is_protected_path(path: str) -> bool:
    """True when ``path`` needs credentials."""
    if path in PUBLIC_API_PATHS:
        return False
    return path in _PROTECTED_EXACT or path.startswith(_PROTECTED_PREFIXES)


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers", ()):
        if k == name:
            return v.decode("latin-1")
    return ""


def credential_status(scope: Scope, api_key: str) -> str:
    """Classify the request's credentials: ``"ok"``, ``"missing"`` or ``"invalid"``."""
    auth = _header(scope, b"authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode(), api_key.encode()
        ):
            return "ok"
        return "invalid"
    raw_cookie = _header(scope, b"cookie")
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except CookieError:  # malformed Cookie header → treat as no cookie
            jar = SimpleCookie()
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None:
            if hmac.compare_digest(morsel.value.encode(), session_token(api_key).encode()):
                return "ok"
            return "invalid"
    return "missing"


def _client_host(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


class APIKeyMiddleware:
    """Pure-ASGI gate for HTTP and WebSocket scopes.

    Pure ASGI rather than ``BaseHTTPMiddleware``, which skips WebSocket
    scopes entirely, so ``/ws`` was never checked.
    """

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not is_protected_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        # CORS preflight carries no credentials by design.
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        status = credential_status(scope, self.api_key)
        if status == "ok":
            await self.app(scope, receive, send)
            return

        if status == "invalid":
            logger.warning(json.dumps({
                "event": "API_AUTH_REJECTED",
                "reason": "invalid_credentials",
                "path": scope["path"],
                "scope": scope["type"],
                "client": _client_host(scope),
            }))
        else:
            logger.debug(json.dumps({
                "event": "API_AUTH_REJECTED",
                "reason": "missing_credentials",
                "path": scope["path"],
                "scope": scope["type"],
                "client": _client_host(scope),
            }))

        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 4401})
            return
        body = b'{"detail":"Authentication required"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="ib-trader"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# --- /api/auth endpoints ---------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Body of POST /api/auth/login."""

    key: str


def _auth_settings(request: Request) -> tuple[str | None, int]:
    return (
        getattr(request.app.state, "api_key", None),
        getattr(request.app.state, "auth_cookie_max_age", 30 * 86400),
    )


@router.get("/session")
def get_session(request: Request) -> dict:
    """Report whether auth is enforced and whether this request is authenticated."""
    api_key, _ = _auth_settings(request)
    if not api_key:
        return {"auth_required": False, "authenticated": True}
    return {
        "auth_required": True,
        "authenticated": credential_status(request.scope, api_key) == "ok",
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    """Check the access key and set the browser session cookie."""
    api_key, max_age = _auth_settings(request)
    if not api_key:
        return {"authenticated": True}
    if not hmac.compare_digest(body.key.strip().encode(), api_key.encode()):
        logger.warning(json.dumps({
            "event": "API_AUTH_LOGIN_FAILED",
            "client": _client_host(request.scope),
        }))
        response.status_code = 401
        return {"authenticated": False, "detail": "Invalid access key"}
    response.set_cookie(
        SESSION_COOKIE,
        session_token(api_key),
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    logger.info(json.dumps({
        "event": "API_AUTH_LOGIN",
        "client": _client_host(request.scope),
    }))
    return {"authenticated": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    """Clear the browser session cookie."""
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return {"authenticated": False}

"""Tests for ib-api access-key auth (ib_trader/api/auth.py)."""
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from starlette.websockets import WebSocketDisconnect

import ib_trader.data.models  # noqa: F401  (register models)
from ib_trader.api import auth
from ib_trader.api import deps as api_deps
from ib_trader.api.app import create_app
from ib_trader.data.models import Base

KEY = "k" * 40


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return scoped_session(sessionmaker(bind=engine))


@pytest.fixture
def app(session_factory):
    return create_app(session_factory, api_key=KEY, auth_cookie_max_age=3600)


@pytest.fixture
def client(app, session_factory):
    api_deps._session_factory = session_factory
    with TestClient(app) as c:
        yield c
    api_deps._session_factory = None


BEARER = {"Authorization": f"Bearer {KEY}"}


class TestValidateApiKey:
    def test_missing_key_refused(self):
        with pytest.raises(auth.APIKeyConfigError, match="not set"):
            auth.validate_api_key(None)
        with pytest.raises(auth.APIKeyConfigError, match="not set"):
            auth.validate_api_key("   ")

    def test_short_key_refused(self):
        with pytest.raises(auth.APIKeyConfigError, match="at least 32"):
            auth.validate_api_key("short")

    def test_valid_key_stripped(self):
        assert auth.validate_api_key(f"  {KEY}\n") == KEY


class TestProtectedPaths:
    @pytest.mark.parametrize("path", [
        "/api/status", "/api/bots", "/api", "/ws", "/ws/x", "/docs", "/redoc",
        "/openapi.json",
    ])
    def test_protected(self, path):
        assert auth.is_protected_path(path)

    @pytest.mark.parametrize("path", [
        "/", "/index.html", "/assets/index-abc.js", "/favicon.ico",
        "/api/auth/login", "/api/auth/logout", "/api/auth/session",
    ])
    def test_public(self, path):
        assert not auth.is_protected_path(path)

    def test_every_registered_route_is_protected(self, app):
        """A new router outside /api or /ws must not become public by accident."""
        for route in app.routes:
            if isinstance(route, (APIRoute, APIWebSocketRoute)):
                if route.path in auth.PUBLIC_API_PATHS or route.path == "/{full_path:path}":
                    continue
                assert auth.is_protected_path(route.path), route.path


class TestHttpAuth:
    def test_no_credentials_401(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Authentication required"}
        assert resp.headers["www-authenticate"].startswith("Bearer")

    def test_wrong_bearer_401(self, client):
        resp = client.get("/api/status", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_non_bearer_scheme_401(self, client):
        resp = client.get("/api/status", headers={"Authorization": f"Basic {KEY}"})
        assert resp.status_code == 401

    def test_bearer_ok(self, client):
        assert client.get("/api/status", headers=BEARER).status_code == 200

    def test_query_token_not_accepted(self, client):
        """Tokens in URLs leak into logs and history; only header or cookie."""
        assert client.get(f"/api/status?token={KEY}").status_code == 401

    def test_docs_protected(self, client):
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/openapi.json", headers=BEARER).status_code == 200

    def test_cors_preflight_passes(self, client):
        resp = client.options("/api/status", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        })
        assert resp.status_code == 200


class TestBrowserSession:
    def test_session_reports_unauthenticated(self, client):
        assert client.get("/api/auth/session").json() == {
            "auth_required": True, "authenticated": False,
        }

    def test_session_reports_bearer(self, client):
        assert client.get("/api/auth/session", headers=BEARER).json()["authenticated"]

    def test_wrong_key_login_rejected(self, client):
        resp = client.post("/api/auth/login", json={"key": "wrong"})
        assert resp.status_code == 401
        assert auth.SESSION_COOKIE not in resp.cookies
        assert client.get("/api/status").status_code == 401

    def test_login_sets_httponly_strict_cookie(self, client):
        resp = client.post("/api/auth/login", json={"key": KEY})
        assert resp.status_code == 200
        set_cookie = resp.headers["set-cookie"]
        assert f"{auth.SESSION_COOKIE}=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie
        assert "Max-Age=3600" in set_cookie
        # The cookie carries a derived value, never the key itself.
        assert KEY not in set_cookie

    def test_cookie_authenticates_then_logout_clears(self, client):
        client.post("/api/auth/login", json={"key": KEY})
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/auth/session").json()["authenticated"]
        client.post("/api/auth/logout")
        assert client.get("/api/status").status_code == 401

    def test_forged_cookie_rejected(self, client):
        client.cookies.set(auth.SESSION_COOKIE, "0" * 64)
        assert client.get("/api/status").status_code == 401

    def test_session_token_tied_to_key(self):
        assert auth.session_token(KEY) != auth.session_token("j" * 40)
        assert auth.session_token(KEY) == auth.session_token(KEY)


class TestWebSocketAuth:
    def test_ws_without_credentials_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws"):
                pass
        assert exc.value.code == 4401

    def test_ws_with_bearer_header(self, client):
        with client.websocket_connect("/ws", headers=BEARER) as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_ws_with_session_cookie(self, client):
        client.post("/api/auth/login", json={"key": KEY})
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_ws_query_token_rejected(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws?token={KEY}"):
                pass


class TestAuthDisabled:
    """create_app(api_key=None) is the test-only path; ib-api never runs that way."""

    def test_open_when_no_key(self, session_factory):
        api_deps._session_factory = session_factory
        try:
            with TestClient(create_app(session_factory)) as c:
                assert c.get("/api/status").status_code == 200
                assert c.get("/api/auth/session").json() == {
                    "auth_required": False, "authenticated": True,
                }
        finally:
            api_deps._session_factory = None

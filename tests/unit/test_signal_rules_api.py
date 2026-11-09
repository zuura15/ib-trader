"""GET/PUT /api/signal-rules (GH #102) — opaque, Redis-backed config doc."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ib_trader.api.deps import get_redis
from ib_trader.api.routes import signal_rules


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value):
        self.kv[key] = value


@pytest.fixture
def client():
    redis = _FakeRedis()
    app = FastAPI()
    app.include_router(signal_rules.router)
    app.dependency_overrides[get_redis] = lambda: redis
    return TestClient(app)


def test_absent_config_is_null(client):
    assert client.get("/api/signal-rules").json() == {"config": None}


def test_put_then_get_round_trips(client):
    doc = {"version": 1, "rules": {"srBounce": {"enabled": False}}}
    assert client.put("/api/signal-rules", json={"config": doc}).status_code == 200
    assert client.get("/api/signal-rules").json() == {"config": doc}


def test_oversized_config_rejected(client):
    doc = {"blob": "x" * (70 * 1024)}
    assert client.put("/api/signal-rules", json={"config": doc}).status_code == 413


def test_no_redis_is_503():
    app = FastAPI()
    app.include_router(signal_rules.router)
    app.dependency_overrides[get_redis] = lambda: None
    assert TestClient(app).get("/api/signal-rules").status_code == 503

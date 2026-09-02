"""Contract tests for ``POST /engine/close``.

Regression for the ``close GCV6`` relay bug: the API route used to
re-parse close commands field-wise (``int(parts[1])``) and the engine
rebuilt the text from ``serial``/``strategy`` — the SYMBOL form (#96)
crashed with ``invalid literal for int()`` and limit prices were
silently dropped. The relay now passes ``raw`` verbatim; these tests
pin that contract plus the legacy field-wise fallback bots still use.
"""
from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from ib_trader.engine.internal_api import app, set_context


@pytest.fixture(autouse=True)
def _init_engine_context():
    set_context(object())
    yield
    set_context(None)


def _stub_execute():
    return patch(
        "ib_trader.engine.service.execute_single_command",
        new=AsyncMock(return_value={"output": "ok", "cmd_id": "c1"}),
    )


class TestEngineClose:
    def test_raw_passthrough_symbol_form(self):
        with _stub_execute() as ex:
            client = TestClient(app)
            r = client.post("/engine/close", json={"raw": "close GCV6"})
            assert r.status_code == 200
            assert ex.await_args.args[1] == "close GCV6"

    def test_raw_preserves_limit_price(self):
        with _stub_execute() as ex:
            client = TestClient(app)
            r = client.post(
                "/engine/close", json={"raw": "close GCV6 limit 4470.0"},
            )
            assert r.status_code == 200
            assert ex.await_args.args[1] == "close GCV6 limit 4470.0"

    def test_legacy_serial_fields_still_work(self):
        with _stub_execute() as ex:
            client = TestClient(app)
            r = client.post(
                "/engine/close", json={"serial": 4, "strategy": "mid"},
            )
            assert r.status_code == 200
            assert ex.await_args.args[1] == "close 4 mid"

    def test_neither_raw_nor_serial_is_422(self):
        with _stub_execute():
            client = TestClient(app)
            r = client.post("/engine/close", json={})
            assert r.status_code == 422

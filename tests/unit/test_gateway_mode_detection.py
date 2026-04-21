"""Unit tests for Gateway paper/live auto-detection (ADR 015).

Covers the helpers in ib_trader/engine/connect.py:
- probe_gateway: iterates candidates, classifies from managedAccounts prefix
- pick_account: resolves account_id from .env per detected mode
- pick_market_data_type: resolves market data type per detected mode
- load_candidates: parses settings.yaml entries
"""
from __future__ import annotations

import pytest

from ib_trader.engine import connect as C


class _FakeIB:
    """Stand-in for ib_async.IB — records connect attempts, returns accounts."""

    def __init__(self, outcomes_by_port: dict[int, list[str] | Exception]):
        self._outcomes = outcomes_by_port
        self._accounts: list[str] = []
        self.connected = False
        self.disconnected = False

    async def connectAsync(self, host, port, clientId, timeout):
        outcome = self._outcomes.get(port)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise ConnectionRefusedError(f"nothing on {port}")
        self._accounts = outcome
        self.connected = True

    def managedAccounts(self):
        return list(self._accounts)

    def disconnect(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def _patch_ib(monkeypatch):
    """Route every IB() constructor in connect.py to our fake."""
    # Use a mutable holder so tests can configure per-port outcomes.
    holder: dict[str, dict[int, list[str] | Exception]] = {"outcomes": {}}

    def _factory():
        return _FakeIB(holder["outcomes"])

    monkeypatch.setattr(C, "IB", _factory)
    return holder


class TestProbeGateway:
    async def test_live_first_wins(self, _patch_ib):
        _patch_ib["outcomes"] = {4001: ["U1234567"], 4002: ["DU7654321"]}
        res = await C.probe_gateway(
            "127.0.0.1", list(C.DEFAULT_CANDIDATES), client_id=1, timeout=0.1,
        )
        assert res.port == 4001
        assert res.mode == "live"
        assert res.accounts == ["U1234567"]

    async def test_falls_through_to_paper(self, _patch_ib):
        _patch_ib["outcomes"] = {4002: ["DU7654321"]}  # 4001 missing -> refused
        res = await C.probe_gateway(
            "127.0.0.1", list(C.DEFAULT_CANDIDATES), client_id=1, timeout=0.1,
        )
        assert res.port == 4002
        assert res.mode == "paper"

    async def test_no_gateway_raises(self, _patch_ib):
        _patch_ib["outcomes"] = {}
        with pytest.raises(RuntimeError, match="No IB Gateway or TWS"):
            await C.probe_gateway(
                "127.0.0.1", list(C.DEFAULT_CANDIDATES), client_id=1, timeout=0.1,
            )

    async def test_empty_managed_accounts_raises(self, _patch_ib):
        # Connect succeeds but Gateway returns zero managed accounts — this
        # means the Gateway UI is logged out. Surface it loudly.
        _patch_ib["outcomes"] = {4001: []}
        with pytest.raises(RuntimeError, match="no managed accounts"):
            await C.probe_gateway(
                "127.0.0.1", [C.Candidate(4001, "gateway-live")],
                client_id=1, timeout=0.1,
            )

    async def test_mixed_accounts_classified_paper(self, _patch_ib):
        # Any DU-prefixed account on the session → treat the whole session
        # as paper. Conservative choice per _classify() docstring.
        _patch_ib["outcomes"] = {4001: ["U1234567", "DU7654321"]}
        res = await C.probe_gateway(
            "127.0.0.1", [C.Candidate(4001, "gateway-live")],
            client_id=1, timeout=0.1,
        )
        assert res.mode == "paper"


class TestPickAccount:
    def test_paper_env_match(self):
        env = {"IB_ACCOUNT_ID_PAPER": "DU1", "IB_ACCOUNT_ID": "U1"}
        assert C.pick_account("paper", env, ["DU1"]) == "DU1"

    def test_live_env_match(self):
        env = {"IB_ACCOUNT_ID": "U1"}
        assert C.pick_account("live", env, ["U1"]) == "U1"

    def test_env_acct_not_on_gateway_raises(self):
        env = {"IB_ACCOUNT_ID_PAPER": "DU999"}
        with pytest.raises(SystemExit, match="not in the Gateway's managed accounts"):
            C.pick_account("paper", env, ["DU111"])

    def test_missing_env_falls_back_to_discovered(self):
        # No IB_ACCOUNT_ID_PAPER set — pick the first DU* from discovered.
        assert C.pick_account("paper", {}, ["DU1"]) == "DU1"

    def test_missing_env_no_match_raises(self):
        with pytest.raises(SystemExit, match="no matching account_id"):
            C.pick_account("live", {}, ["DU1"])  # discovered is paper only


class TestPickMarketDataType:
    def test_paper_default_is_delayed(self):
        assert C.pick_market_data_type("paper", {}, {}) == 3

    def test_live_default_is_realtime(self):
        assert C.pick_market_data_type("live", {}, {}) == 1

    def test_paper_env_override(self):
        assert C.pick_market_data_type("paper", {"IB_MARKET_DATA_TYPE_PAPER": "4"}, {}) == 4

    def test_live_env_override(self):
        assert C.pick_market_data_type("live", {"IB_MARKET_DATA_TYPE": "2"}, {}) == 2


class TestLoadCandidates:
    def test_defaults_when_unset(self):
        assert C.load_candidates({}) == list(C.DEFAULT_CANDIDATES)

    def test_int_list(self):
        got = C.load_candidates({"ib_port_candidates": [4002, 4001]})
        assert [c.port for c in got] == [4002, 4001]
        # Labels inferred from DEFAULT_CANDIDATES
        assert got[0].label == "gateway-paper"
        assert got[1].label == "gateway-live"

    def test_dict_list(self):
        got = C.load_candidates({
            "ib_port_candidates": [{"port": 9999, "label": "custom"}],
        })
        assert got == [C.Candidate(9999, "custom")]

    def test_invalid_entry_raises(self):
        with pytest.raises(ValueError):
            C.load_candidates({"ib_port_candidates": ["not-a-port"]})

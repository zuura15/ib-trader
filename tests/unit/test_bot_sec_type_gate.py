"""Bot sec-type gate (Epic 1 D8).

Validates:
- Manifest.permits_sec_type honours ``supported_sec_types`` tuple.
- None / unset defaults to STK+ETF (legacy behaviour).
- Sec-type strings are case-insensitive.
"""
from __future__ import annotations

import pytest

from ib_trader.bots.strategy import StrategyManifest, Subscription


def _manifest(sec_types=None):
    return StrategyManifest(
        name="test",
        subscriptions=[Subscription(type="bars", symbols=["X"])],
        supported_sec_types=sec_types,
    )


class TestDefaultLegacy:
    def test_unset_permits_stk(self):
        m = _manifest(None)
        assert m.permits_sec_type("STK") is True
        assert m.permits_sec_type("ETF") is True

    def test_unset_rejects_fut(self):
        m = _manifest(None)
        assert m.permits_sec_type("FUT") is False

    def test_unset_rejects_opt(self):
        m = _manifest(None)
        assert m.permits_sec_type("OPT") is False


class TestExplicit:
    def test_fut_only(self):
        m = _manifest(("FUT",))
        assert m.permits_sec_type("FUT") is True
        assert m.permits_sec_type("STK") is False

    def test_mixed_set(self):
        m = _manifest(("STK", "FUT"))
        assert m.permits_sec_type("STK") is True
        assert m.permits_sec_type("FUT") is True
        assert m.permits_sec_type("OPT") is False

    @pytest.mark.parametrize("input_case", ["fut", "Fut", "FUT", "FuT"])
    def test_case_insensitive(self, input_case: str):
        m = _manifest(("FUT",))
        assert m.permits_sec_type(input_case) is True

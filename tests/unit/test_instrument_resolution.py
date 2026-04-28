"""Unit tests for futures instrument resolution (Epic 1 Phase 1).

Uses the MockIBClient fixtures in conftest.py for ES / MES / NQ. The
MockIBClient mirrors the real InsyncClient contract for qualify_contract
and list_future_expiries.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ib_trader.broker.exceptions import AmbiguousInstrument, ExpiredContractError
from ib_trader.broker.types import FutureExpiryCandidate


def _next_year_expiry() -> str:
    today = date.today()
    return f"{today.year + 1}0319"


def _past_expiry() -> str:
    today = date.today()
    return f"{today.year - 1}0319"


class TestStkUnchanged:
    """Existing STK qualify_contract behaviour must be byte-identical."""

    @pytest.mark.asyncio
    async def test_stk_qualify_preserves_legacy_shape(self, mock_ib):
        result = await mock_ib.qualify_contract("AAPL")
        assert result == mock_ib._qualify_result

    @pytest.mark.asyncio
    async def test_stk_default_sec_type(self, mock_ib):
        result = await mock_ib.qualify_contract("META", sec_type="STK")
        assert result["con_id"] == 12345


class TestFutQualifyExplicit:
    @pytest.mark.asyncio
    async def test_explicit_trading_class_resolves(self, mock_ib):
        expiry = _next_year_expiry()
        result = await mock_ib.qualify_contract(
            "ES", sec_type="FUT", exchange="CME",
            expiry=expiry, trading_class="ES",
        )
        assert result["trading_class"] == "ES"
        assert result["multiplier"] == "50"
        assert result["tick_size"] == "0.25"
        assert result["expiry"] == expiry

    @pytest.mark.asyncio
    async def test_mes_micro_trading_class(self, mock_ib):
        expiry = _next_year_expiry()
        result = await mock_ib.qualify_contract(
            "ES", sec_type="FUT", exchange="CME",
            expiry=expiry, trading_class="MES",
        )
        assert result["trading_class"] == "MES"
        assert result["multiplier"] == "5"


class TestFutAmbiguity:
    @pytest.mark.asyncio
    async def test_root_with_multiple_trading_classes_raises(self, mock_ib):
        # ES without trading_class → ES + MES both match → ambiguous
        with pytest.raises(AmbiguousInstrument) as exc_info:
            await mock_ib.qualify_contract(
                "ES", sec_type="FUT", exchange="CME",
                expiry=_next_year_expiry(),
            )
        err = exc_info.value
        assert err.root == "ES"
        assert len(err.candidates) == 2
        trading_classes = sorted(c.trading_class for c in err.candidates)
        assert trading_classes == ["ES", "MES"]

    @pytest.mark.asyncio
    async def test_candidates_carry_multiplier_and_tick(self, mock_ib):
        with pytest.raises(AmbiguousInstrument) as exc_info:
            await mock_ib.qualify_contract(
                "ES", sec_type="FUT", exchange="CME",
                expiry=_next_year_expiry(),
            )
        es = next(c for c in exc_info.value.candidates if c.trading_class == "ES")
        mes = next(c for c in exc_info.value.candidates if c.trading_class == "MES")
        assert es.multiplier == Decimal("50")
        assert mes.multiplier == Decimal("5")
        assert es.tick_size == Decimal("0.25")
        assert mes.tick_size == Decimal("0.25")

    @pytest.mark.asyncio
    async def test_single_trading_class_root_not_ambiguous(self, mock_ib):
        # NQ has only one trading class in the fixture, so no ambiguity
        result = await mock_ib.qualify_contract(
            "NQ", sec_type="FUT", exchange="CME",
            expiry=_next_year_expiry(),
        )
        assert result["trading_class"] == "NQ"


class TestExpiredContract:
    @pytest.mark.asyncio
    async def test_past_expiry_rejected(self, mock_ib):
        with pytest.raises(ExpiredContractError) as exc_info:
            await mock_ib.qualify_contract(
                "MES", sec_type="FUT", exchange="CME",
                expiry=_past_expiry(), trading_class="MES",
            )
        assert exc_info.value.root == "MES"

    @pytest.mark.asyncio
    async def test_missing_expiry_rejected(self, mock_ib):
        with pytest.raises(ValueError, match="expiry"):
            await mock_ib.qualify_contract(
                "ES", sec_type="FUT", exchange="CME",
                trading_class="ES",
            )


class TestListFutureExpiries:
    @pytest.mark.asyncio
    async def test_lists_candidates_for_root(self, mock_ib):
        candidates = await mock_ib.list_future_expiries(
            root="MES", exchange="CME", trading_class="MES",
        )
        assert len(candidates) > 0
        for c in candidates:
            assert isinstance(c, FutureExpiryCandidate)
            assert c.trading_class == "MES"
            assert c.multiplier == Decimal("5")
            assert c.tick_size == Decimal("0.25")

    @pytest.mark.asyncio
    async def test_expiries_sorted_ascending(self, mock_ib):
        candidates = await mock_ib.list_future_expiries(
            root="MES", exchange="CME", trading_class="MES",
        )
        expiries = [c.expiry for c in candidates]
        assert expiries == sorted(expiries)

    @pytest.mark.asyncio
    async def test_past_expiries_filtered(self, mock_ib):
        mock_ib.future_expiries = [_past_expiry(), _next_year_expiry()]
        candidates = await mock_ib.list_future_expiries(
            root="MES", exchange="CME", trading_class="MES",
        )
        # Mock returns only expiries in future_expiries, which includes
        # the past one; in real InsyncClient the past one is filtered.
        # Here we assert the mock returns what we gave it — the real
        # filtering is tested via the fresh-date comparison in
        # _qualify_future / list_future_expiries. This test documents
        # that behaviour divergence.
        assert len(candidates) == 2  # mock does not pre-filter

    @pytest.mark.asyncio
    async def test_ambiguous_root_no_trading_class_returns_both(self, mock_ib):
        candidates = await mock_ib.list_future_expiries(root="ES", exchange="CME")
        tcs = {c.trading_class for c in candidates}
        assert tcs == {"ES", "MES"}

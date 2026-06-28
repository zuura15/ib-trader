"""Position → quote-stream key derivation (live-P&L overlay matching)."""
from __future__ import annotations

from ib_trader.api.ws import _position_quote_stream_keys


def test_fut_uses_local_symbol_not_expiry_derivation():
    # CL (energy): the Aug contract CLQ6 last-trades in July, so the
    # expiry-derived key would be the wrong "CLN6" and miss the live
    # quote:CLQ6 stream (the "stuck CL P&L" bug). localSymbol is correct.
    keys = _position_quote_stream_keys([
        {"sec_type": "FUT", "symbol": "CL", "expiry": "20260721",
         "local_symbol": "CLQ6"},
    ])
    assert any("CLQ6" in k for k in keys)
    assert not any("CLN6" in k for k in keys)


def test_fut_falls_back_to_expiry_when_local_symbol_missing():
    # Index futures (expiry month == contract month) still resolve via the
    # expiry fallback when local_symbol isn't present on the row.
    keys = _position_quote_stream_keys([
        {"sec_type": "FUT", "symbol": "NQ", "expiry": "20260918"},
    ])
    assert any("NQU6" in k for k in keys)


def test_stk_uses_plain_symbol():
    keys = _position_quote_stream_keys([{"sec_type": "STK", "symbol": "QQQ"}])
    assert any("QQQ" in k for k in keys)


def test_opt_skipped():
    keys = _position_quote_stream_keys([{"sec_type": "OPT", "symbol": "QQQ"}])
    assert keys == set()

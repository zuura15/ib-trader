"""LIFO futures P&L (GH #101): lot matching, seed back-solve, rollup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ib_trader.engine.lifo_pnl import Lot, compute_lifo, realize_lifo
from ib_trader.engine.pnl_rollup import compute_pnl_rollup
from ib_trader.engine.internal_api import with_lifo_cost

T0 = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=2)


def _ex(i, side, qty, price, *, comm="0", realized=None, sym="NQZ6", mult="20"):
    return {
        "local_symbol": sym, "sec_type": "FUT", "side": side,
        "shares": D(str(qty)), "price": D(str(price)),
        "commission": D(comm), "multiplier": mult,
        "realized_pnl": None if realized is None else D(str(realized)),
        "exec_id": f"e{i:03d}", "exec_time": T0 + timedelta(minutes=i),
    }


def _pos(qty, avg, sym="NQZ6", mult="20"):
    return {sym: {"quantity": str(qty), "avg_cost": str(avg), "multiplier": mult}}


# ---------------------------------------------------------------------------
# realize_lifo
# ---------------------------------------------------------------------------

def test_partial_sale_matches_newest_lot():
    lots = [Lot(D("1"), D("100")), Lot(D("1"), D("110"))]
    pnl = realize_lifo(lots, D("-1"), D("112"), D("0"), D("20"))
    assert pnl == D("40")                       # (112 - 110) x 20
    assert lots == [Lot(D("1"), D("100"))]      # old core keeps its cost


def test_flip_closes_then_opens_opposite_lot():
    lots = [Lot(D("2"), D("100"))]
    pnl = realize_lifo(lots, D("-3"), D("105"), D("0"), D("20"))
    assert pnl == D("200")
    assert lots == [Lot(D("-1"), D("105"))]


def test_pure_open_returns_none_and_folds_commission():
    lots: list[Lot] = []
    assert realize_lifo(lots, D("2"), D("100"), D("4"), D("20")) is None
    assert lots == [Lot(D("2"), D("100.1"))]    # 4 / (2 x 20) per point


def test_short_lot_realizes_positive_on_cover_lower():
    lots = [Lot(D("-1"), D("100"))]
    assert realize_lifo(lots, D("1"), D("98"), D("0"), D("20")) == D("40")
    assert lots == []


# ---------------------------------------------------------------------------
# compute_lifo
# ---------------------------------------------------------------------------

def test_intraday_add_and_trim_is_exact_lifo():
    """The discussion example: buy 100, add 110, sell 112."""
    execs = [_ex(1, "BOT", 1, 100), _ex(2, "BOT", 1, 110), _ex(3, "SLD", 1, 112)]
    res = compute_lifo(execs, _pos(1, 105))["NQZ6"]
    assert res.method == "lifo"
    assert res.realized_by_exec == {"e003": D("40")}
    assert res.open_qty == 1 and res.avg_cost == D("100")


def test_round_trip_total_matches_average_cost():
    """Realized + open is method-independent; with commissions too."""
    execs = [
        _ex(1, "BOT", 2, 100, comm="4"), _ex(2, "BOT", 1, 110, comm="2"),
        _ex(3, "SLD", 2, 112, comm="4"), _ex(4, "SLD", 1, 95, comm="2"),
    ]
    res = compute_lifo(execs, {})["NQZ6"]
    total = sum(res.realized_by_exec.values())
    # Cash: -(2x100 + 110) x 20 + (2x112 + 95) x 20 - 12 commissions
    assert total == D("180") - D("12")
    assert res.open_qty == 0


def test_seed_lot_back_solved_from_ib_avg_cost():
    """Held 2 @ 100 before the window; add 1 @ 110 → IB avg 103.333…;
    LIFO must recover the seed at 100 and sell the 110 add first."""
    ib_avg = (D("2") * 100 + 110) / 3
    execs = [_ex(1, "BOT", 1, 110), _ex(2, "SLD", 1, 113)]
    res = compute_lifo(execs, _pos(2, ib_avg))["NQZ6"]
    assert res.method == "lifo"
    assert res.realized_by_exec["e002"] == D("60")   # (113 - 110) x 20
    assert res.open_qty == 2
    assert abs(res.avg_cost - D("100")) < D("1e-9")


def test_seed_lot_back_solved_from_ib_realized_when_closed_out():
    """Held 1 @ 100 before the window, then sold flat: IB realized gives
    the seed cost even though no position is left to read avg from."""
    # IB avg before the sale = 105; IB realized = (108 - 105) x 2 x 20.
    execs = [_ex(1, "BOT", 1, 110), _ex(2, "SLD", 2, 108, realized="120")]
    res = compute_lifo(execs, {})["NQZ6"]
    assert res.method == "lifo"
    # LIFO: sell 110 lot at 108 (-40) + seed 100 at 108 (+160) = 120 total.
    assert res.realized_by_exec["e002"] == D("120")


def test_missing_ib_realized_falls_back_to_avg():
    execs = [_ex(1, "SLD", 1, 108)]   # seed long closed, report unpaired
    res = compute_lifo(execs, {})["NQZ6"]
    assert res.method == "avg" and res.reason == "ib_realized_missing"


def test_implausible_seed_falls_back_to_avg():
    execs = [_ex(1, "BOT", 1, 110)]
    res = compute_lifo(execs, _pos(2, "5000"))["NQZ6"]  # wrong-multiplier avg
    assert res.method == "avg" and res.reason == "seed_cost_implausible"


def test_unknown_multiplier_falls_back():
    execs = [_ex(1, "BOT", 1, 110, mult=None)]
    res = compute_lifo(execs, {})["NQZ6"]
    assert res.method == "avg" and res.reason == "unknown_multiplier"


def test_non_futures_ignored():
    ex = _ex(1, "BOT", 10, 100)
    ex["sec_type"] = "STK"
    assert compute_lifo([ex], {}) == {}


# ---------------------------------------------------------------------------
# Rollup + positions overlay
# ---------------------------------------------------------------------------

def test_rollup_uses_lifo_and_keeps_ib_figures():
    execs = [
        _ex(1, "BOT", 1, 100), _ex(2, "BOT", 1, 110),
        _ex(3, "SLD", 1, 112, realized="140"),   # IB avg: (112 - 105) x 20
    ]
    lifo = compute_lifo(execs, _pos(1, 105))
    out = compute_pnl_rollup(execs, NOW, session_start_hour=8, lifo=lifo)["NQZ6"]
    assert out["method"] == "lifo"
    assert out["pnl_24h"] == 40.0 and out["ib_pnl_24h"] == 140.0
    assert out["pnl_session"] == 40.0 and out["ib_pnl_session"] == 140.0


def test_rollup_without_lifo_is_ib_average_cost():
    execs = [_ex(1, "SLD", 1, 112, realized="140")]
    out = compute_pnl_rollup(execs, NOW, session_start_hour=8)["NQZ6"]
    assert out["method"] == "avg" and out["pnl_24h"] == 140.0


def test_positions_get_lifo_cost_only_when_qty_matches():
    execs = [_ex(1, "BOT", 1, 100), _ex(2, "BOT", 1, 110), _ex(3, "SLD", 1, 112)]
    lifo = compute_lifo(execs, _pos(1, 105))
    pos = [{"sec_type": "FUT", "local_symbol": "NQZ6", "quantity": "1", "avg_cost": "105"}]
    assert with_lifo_cost(pos, lifo)[0]["lifo_avg_cost"] == "100"
    assert "lifo_avg_cost" not in pos[0]            # cache not mutated
    stale = [{**pos[0], "quantity": "2"}]           # basis lags a new fill
    assert "lifo_avg_cost" not in with_lifo_cost(stale, lifo)[0]


# ---------------------------------------------------------------------------
# Console close readout
# ---------------------------------------------------------------------------

async def test_console_readout_uses_lifo_lots():
    from types import SimpleNamespace

    from ib_trader.data.models import LegType
    from ib_trader.engine.order import _OrderContext, _emit_console_close_pnl

    captured = []
    router = SimpleNamespace(emit=lambda msg, **_: captured.append(msg))
    ctx = SimpleNamespace(router=router, redis=None)
    order_ctx = _OrderContext(
        trade_id="00000000-0000-0000-0000-000000000001", trade_serial=1,
        symbol="NQZ6", side="SELL", order_type="MID",
        qty_requested=D("1"), leg_type=LegType.ENTRY, correlation_id="c",
        security_type="FUT", multiplier=D("20"),
        pre_position_qty=D("2"), pre_position_avg_cost=D("105"),
        pre_position_lifo_lots=[Lot(D("1"), D("100")), Lot(D("1"), D("110"))],
    )
    await _emit_console_close_pnl(ctx, order_ctx, D("1"), D("112"), D("0"))
    assert captured == ["  P&L (closed 1 NQZ6): +$40.00"]
    # Snapshot untouched — the lots belong to the order context.
    assert order_ctx.pre_position_lifo_lots[-1] == Lot(D("1"), D("110"))

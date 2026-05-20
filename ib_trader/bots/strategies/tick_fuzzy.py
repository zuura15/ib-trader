"""Tick-driven fuzzy-line strategy — exit-first implementation (ADR 019).

Scope of v1
-----------
ENTRY is operator-driven via the existing Force LONG / Force SHORT
buttons (bot config has ``manual_entry_only: true``). The strategy
does NOT auto-fire entries yet — the bar-evaluation hook is a no-op
beyond emitting a "WATCHING" / "HOLDING" audit row so the audit
feed reflects the bot is alive. Auto entry (the per-line watch FSM
described in ADR 019) is the next iteration.

EXIT is dollar-denominated, tick-evaluated, and configurable per bot.
On every ``QuoteUpdate`` while the bot is ``AWAITING_EXIT_TRIGGER``:

  1. Compute unrealized P&L in dollars:
       direction == LONG  → (last − entry) × qty × contract_multiplier
       direction == SHORT → (entry − last) × qty × contract_multiplier
  2. Update HWM = max(prior_hwm, pnl).
  3. If trail not active and HWM ≥ ``trail_activation_dollars``,
     activate trail. Initial trail_stop = HWM − ``trail_giveback_dollars``.
  4. If trail active, ratchet trail_stop UP only:
       trail_stop = max(prior_trail_stop, HWM − giveback).
  5. Fire exit when:
       trail INACTIVE and pnl ≤ −``initial_sl_dollars``  → exit_reason "hard_sl"
       trail ACTIVE   and pnl ≤ trail_stop               → exit_reason "trail_stop"
  6. Exit order is **walking mid** (existing engine behaviour); aggressive
     enough to fill quickly when the stop triggers.

The dollar thresholds are MINIMUMS — designed around MNQ (multiplier
$2/pt). Smaller-value contracts use the same dollar floors rather than
proportional scaling. Operator can override per bot via YAML.

State this strategy owns
------------------------
- ``hwm_pnl_dollars``       — high-water-mark of unrealised pnl, in $.
- ``trail_active``          — bool, set True once HWM crosses activation.
- ``trail_stop_pnl_dollars``— current trail-stop pnl threshold, in $.
- ``last_price``, ``unrealized_pnl`` — for the PositionStrip UI.
- ``exit_reason``, ``exit_detail`` — stamped at the moment we fire so
  the TRADE_CLOSED audit row carries the right tag.

State the runtime already owns
------------------------------
- ``entry_price``, ``qty``, ``entry_time``, ``position_direction`` —
  populated by ``runtime.on_entry_filled`` after a Force entry's fill
  comes back from IB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import (
    Action, BarCompleted, EmitAudit, LogEventType, LogSignal, OrderFilled,
    OrderRejected, PlaceOrder, QuoteUpdate, StrategyContext,
    StrategyManifest, Subscription, UpdateState,
)


logger = logging.getLogger(__name__)


# Default dollar thresholds — operator can override in YAML.
DEFAULT_INITIAL_SL_DOLLARS = Decimal("10")
DEFAULT_TRAIL_ACTIVATION_DOLLARS = Decimal("20")
DEFAULT_TRAIL_GIVEBACK_DOLLARS = Decimal("5")


def _dec(v: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Defensive Decimal coercion. Empty / None / non-numeric → default."""
    if v is None or v == "":
        return default
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return default


def _pnl_to_stop_price(
    *, entry_price: Decimal, qty: Decimal, mult: Decimal,
    pnl_dollars: Decimal, direction: str,
) -> Decimal:
    """Convert a P&L dollar threshold into the price at which the position
    crosses that threshold.

    LONG  : pnl = (price − entry) × qty × mult  → price = entry + pnl/(qty*mult)
    SHORT : pnl = (entry − price) × qty × mult  → price = entry − pnl/(qty*mult)

    For an initial SL of -$10 on a LONG with qty=1 and mult=2, the stop
    price = entry − 10/2 = entry − 5. The frontend's PositionStrip reads
    ``state.active_stop`` as a price (not a P&L), so this helper lives
    here to keep the dollar-denominated trail logic separable from the
    UI's price-based stop display.
    """
    denom = qty * mult
    if denom <= 0:
        return entry_price
    if direction == "LONG":
        return entry_price + pnl_dollars / denom
    return entry_price - pnl_dollars / denom


class TickFuzzyStrategy:
    """Tick-driven dollar-denominated SL + trail. See module docstring."""

    def __init__(self, config: dict) -> None:
        self.config = config
        symbol = config["symbol"]
        bar_seconds = int(config.get("bar_size_seconds", 180))
        lookback = int(config.get("lookback_bars", 60))
        self.bar_seconds = bar_seconds

        self.manifest = StrategyManifest(
            name="tick_fuzzy",
            subscriptions=[
                # Bars only for audit/context — entry is manual.
                Subscription("bars", [symbol],
                             {"bar_seconds": bar_seconds,
                              "lookback": lookback}),
            ],
            capabilities=["execution", "state_store"],
            state_schema={
                "trade_serial": "int|null",
                "entry_price": "decimal|null",
                "entry_time": "str|null",
                "entry_bar_time": "str|null",
                "qty": "decimal|null",
                "position_direction": "str|null",  # LONG|SHORT
                "hwm_pnl_dollars": "decimal",
                "trail_active": "bool",
                "trail_stop_pnl_dollars": "decimal",
                "last_price": "decimal|null",
                "unrealized_pnl": "decimal|null",
                "exit_reason": "str|null",
                "exit_detail": "str|null",
            },
            version="0.1",
            # v1 targets futures only (MNQ/MES/MGC dollar-floor design).
            supported_sec_types=("FUT",),
        )

    # ------------------------------------------------------------------
    # Tunable readers — read from config every call so a hot-edited
    # YAML can be re-read on next bar (we don't restart the bot).
    # ------------------------------------------------------------------

    def _initial_sl(self) -> Decimal:
        return _dec(
            self.config.get("initial_sl_dollars"),
            DEFAULT_INITIAL_SL_DOLLARS,
        )

    def _trail_activation(self) -> Decimal:
        return _dec(
            self.config.get("trail_activation_dollars"),
            DEFAULT_TRAIL_ACTIVATION_DOLLARS,
        )

    def _trail_giveback(self) -> Decimal:
        return _dec(
            self.config.get("trail_giveback_dollars"),
            DEFAULT_TRAIL_GIVEBACK_DOLLARS,
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks — runtime calls these.
    # ------------------------------------------------------------------

    async def on_start(self, ctx: StrategyContext) -> list[Action]:
        return [LogSignal(
            event_type=LogEventType.SIGNAL,
            message=(
                f"tick_fuzzy started: fsm={ctx.fsm_state.value} "
                f"SL=-${self._initial_sl()} "
                f"trail_activate=+${self._trail_activation()} "
                f"trail_giveback=${self._trail_giveback()}"
            ),
        )]

    async def on_stop(self, ctx: StrategyContext) -> list[Action]:
        return [LogSignal(
            event_type=LogEventType.SIGNAL,
            message="tick_fuzzy stopped",
        )]

    async def on_event(self, event: Any, ctx: StrategyContext) -> list[Action]:
        """Dispatch by event type. Most decision work happens on quotes."""
        if isinstance(event, QuoteUpdate):
            return self._on_quote(event, ctx)
        if isinstance(event, BarCompleted):
            return self._on_bar(event, ctx)
        if isinstance(event, OrderFilled):
            return self._on_fill(event, ctx)
        if isinstance(event, OrderRejected):
            return self._on_rejected(event, ctx)
        return []

    # ------------------------------------------------------------------
    # Bar handler — no entry logic in v1. Just an audit breadcrumb so
    # the operator can see the bot is evaluating.
    # ------------------------------------------------------------------

    def _on_bar(self, event: BarCompleted,
                ctx: StrategyContext) -> list[Action]:
        bar = event.bar
        close_price = _dec(bar.get("close"))
        bar_time = bar.get("timestamp_utc")
        in_position = ctx.fsm_state == BotState.AWAITING_EXIT_TRIGGER
        decision = "HOLDING" if in_position else "WATCHING"

        return [EmitAudit(
            event_type="BAR_EVAL",
            decision=decision,
            symbol=event.symbol,
            bar_close=close_price if close_price > 0 else None,
            payload={
                "in_position": in_position,
                "fsm_state": ctx.fsm_state.value,
                # When in position, surface the current dollar telemetry
                # so the audit row carries the live SL / trail status.
                "hwm_pnl_dollars": str(_dec(ctx.state.get("hwm_pnl_dollars"))),
                "trail_active": bool(ctx.state.get("trail_active")),
                "trail_stop_pnl_dollars": str(_dec(
                    ctx.state.get("trail_stop_pnl_dollars"))),
                "bar_time": bar_time,
            },
        )]

    # ------------------------------------------------------------------
    # Quote handler — tick-time SL + trail evaluation.
    # ------------------------------------------------------------------

    def _on_quote(self, event: QuoteUpdate,
                  ctx: StrategyContext) -> list[Action]:
        if ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
            return []

        entry_price = _dec(ctx.state.get("entry_price"))
        qty = _dec(ctx.state.get("qty"))
        if entry_price <= 0 or qty <= 0:
            return []

        last = event.mid
        if last <= 0:
            return []

        direction = str(
            ctx.state.get("position_direction") or "LONG"
        ).upper()
        mult = _dec(self.config.get("contract_multiplier"), Decimal("1"))

        if direction == "LONG":
            pnl = (last - entry_price) * qty * mult
        else:
            pnl = (entry_price - last) * qty * mult

        # HWM ratchets only up (never reset by drawdown).
        prior_hwm = _dec(ctx.state.get("hwm_pnl_dollars"))
        hwm = max(prior_hwm, pnl)

        trail_active = bool(ctx.state.get("trail_active"))
        prior_trail_stop = _dec(ctx.state.get("trail_stop_pnl_dollars"))

        trail_activation = self._trail_activation()
        trail_giveback = self._trail_giveback()
        initial_sl = self._initial_sl()

        # Activate trail when HWM crosses the activation threshold.
        if not trail_active and hwm >= trail_activation:
            trail_active = True
            prior_trail_stop = hwm - trail_giveback

        # Ratchet trail UP only (never loosen).
        if trail_active:
            new_trail_stop = hwm - trail_giveback
            trail_stop = max(prior_trail_stop, new_trail_stop)
        else:
            trail_stop = prior_trail_stop

        # Fire decision — exclusive: trail wins when active, else hard SL.
        sl_hit = (not trail_active) and pnl <= -initial_sl
        trail_hit = trail_active and pnl <= trail_stop

        # Active stop in PRICE for the PositionStrip UI. Equal to the
        # initial-SL price before trail activates; switches to the
        # trail-stop price once trail is on, and ratchets up with HWM.
        active_stop_pnl = trail_stop if trail_active else -initial_sl
        active_stop_price = _pnl_to_stop_price(
            entry_price=entry_price, qty=qty, mult=mult,
            pnl_dollars=active_stop_pnl, direction=direction,
        )

        state_patch: dict = {
            "last_price": str(last),
            "unrealized_pnl": str(pnl),
            "hwm_pnl_dollars": str(hwm),
            "trail_active": trail_active,
            "trail_stop_pnl_dollars": str(trail_stop),
            "active_stop": str(active_stop_price),
        }

        if sl_hit or trail_hit:
            return self._build_exit_actions(
                ctx=ctx,
                event=event,
                direction=direction,
                qty=qty,
                pnl=pnl,
                hwm=hwm,
                trail_stop=trail_stop,
                trail_active=trail_active,
                trigger="trail_stop" if trail_hit else "hard_sl",
                state_patch=state_patch,
            )

        # No fire — just persist the telemetry update.
        return [UpdateState(state_patch)]

    def _build_exit_actions(
        self, *, ctx: StrategyContext, event: QuoteUpdate,
        direction: str, qty: Decimal, pnl: Decimal, hwm: Decimal,
        trail_stop: Decimal, trail_active: bool, trigger: str,
        state_patch: dict,
    ) -> list[Action]:
        symbol = self.config["symbol"]
        close_side = "SELL" if direction == "LONG" else "BUY"
        order_strategy = self.config.get("exit_order_strategy", "mid")

        # Detail string — matches chart_signal's "TRAILING_STOP [dir]: …"
        # shape so the audit feed normalisation (``_normalize_exit_reason``)
        # picks up "trail_stop" / "hard_sl" tags cleanly.
        if trigger == "trail_stop":
            detail = (
                f"TRAILING_STOP [{direction.lower()}]: pnl=${pnl:+.2f} "
                f"<= trail_stop ${trail_stop:+.2f} "
                f"(hwm=${hwm:+.2f}, giveback=${self._trail_giveback()}) "
                f"[trail_stop]"
            )
        else:
            detail = (
                f"TRAILING_STOP [{direction.lower()}]: pnl=${pnl:+.2f} "
                f"<= initial_sl ${-self._initial_sl():+.2f} "
                f"(hwm=${hwm:+.2f}, trail_inactive) "
                f"[hard_sl]"
            )

        # Persist the exit_reason BEFORE clearing position fields so
        # the runtime's record_trade_closed reads the right tag.
        state_patch_with_exit = {
            **state_patch,
            "exit_reason": trigger,
            "exit_detail": detail,
        }

        logger.info(
            '{"event": "TICK_FUZZY_EXIT_FIRE", "trigger": "%s", '
            '"direction": "%s", "pnl": "%s", "hwm": "%s", '
            '"trail_stop": "%s", "trail_active": %s}',
            trigger, direction, pnl, hwm, trail_stop, str(trail_active).lower(),
        )

        return [
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=detail,
                payload={
                    "exit_type": "TRAILING_STOP",
                    "direction": direction.lower(),
                    "trigger": trigger,
                    "pnl_dollars": str(pnl),
                    "hwm_dollars": str(hwm),
                    "trail_stop_dollars": str(trail_stop),
                    "trail_active": trail_active,
                },
            ),
            UpdateState(state_patch_with_exit),
            PlaceOrder(
                symbol=symbol,
                side=close_side,
                qty=qty,
                order_type=order_strategy,
                origin="exit",
            ),
        ]

    # ------------------------------------------------------------------
    # Fill handler — reset HWM / trail state on entry fill so the
    # next position starts clean even when the bot was force-entered.
    # ------------------------------------------------------------------

    def _on_fill(self, event: OrderFilled,
                 ctx: StrategyContext) -> list[Action]:
        pos = ctx.fsm_state
        direction = str(
            ctx.state.get("position_direction") or "LONG"
        ).upper()
        is_entry_leg = pos == BotState.AWAITING_EXIT_TRIGGER and (
            (direction == "LONG" and event.side == "BUY")
            or (direction == "SHORT" and event.side == "SELL")
        )
        if not is_entry_leg:
            return []

        # Fresh round — reset HWM/trail. Entry-side state (entry_price,
        # qty, position_direction, entry_time) is already populated by
        # ``runtime.on_entry_filled``; we only own the dollar-trail
        # bookkeeping.
        #
        # Seed ``active_stop`` (PRICE, not dollars) from the initial SL
        # so the chart-bot PositionStrip shows a concrete stop level the
        # instant the fill lands. Without this the stop reads "—" until
        # the first quote tick fires the trail-recompute, which is
        # operator-confusing ("is the SL even armed?"). active_stop is
        # the PRICE at which pnl = -initial_sl_dollars.
        entry_price = _dec(event.fill_price)
        qty = _dec(event.qty)
        mult = _dec(self.config.get("contract_multiplier"), Decimal("1"))
        initial_sl_price = _pnl_to_stop_price(
            entry_price=entry_price, qty=qty, mult=mult,
            pnl_dollars=-self._initial_sl(), direction=direction,
        )

        logger.info(
            '{"event": "TICK_FUZZY_ENTRY_FILLED", "direction": "%s", '
            '"entry_price": "%s", "qty": "%s", '
            '"initial_sl_price": "%s"}',
            direction, event.fill_price, event.qty, initial_sl_price,
        )
        return [
            UpdateState({
                "hwm_pnl_dollars": "0",
                "trail_active": False,
                "trail_stop_pnl_dollars": "0",
                "unrealized_pnl": "0",
                # active_stop is the operator-facing PRICE the bot would
                # exit at if pnl hit the current threshold. Starts as
                # the initial-SL price; the quote handler ratchets this
                # upward as the trail moves.
                "active_stop": str(initial_sl_price),
                "exit_reason": None,
                "exit_detail": None,
            }),
            LogSignal(
                event_type=LogEventType.SIGNAL,
                message=(
                    f"ENTRY filled {direction} @ ${event.fill_price} "
                    f"qty={event.qty} — armed: SL=-${self._initial_sl()} "
                    f"(stop @ ${initial_sl_price}), "
                    f"trail_activate=+${self._trail_activation()}, "
                    f"trail_giveback=${self._trail_giveback()}"
                ),
            ),
        ]

    def _on_rejected(self, event: OrderRejected,
                     ctx: StrategyContext) -> list[Action]:
        return [LogSignal(
            event_type=LogEventType.ERROR,
            message=f"order rejected: {event.reason}",
        )]

    # ------------------------------------------------------------------
    # Force-exit hook used by /force-quit / /force-sell endpoints.
    # Mid order for the full qty. Mirrors chart_signal's interface.
    # ------------------------------------------------------------------

    def build_exit_actions(
        self, ctx: StrategyContext, exit_type, detail: str,
    ) -> list[Action]:
        symbol = self.config["symbol"]
        qty = ctx.state.get("qty", 1)
        order_strategy = self.config.get("exit_order_strategy", "mid")
        direction = str(
            ctx.state.get("position_direction") or "LONG"
        ).upper()
        close_side = "SELL" if direction == "LONG" else "BUY"
        actions: list[Action] = [
            UpdateState({
                "exit_reason": "force_quit",
                "exit_detail": f"force-quit: {detail}",
            }),
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"FORCE_EXIT [{direction.lower()}]: {detail}",
                payload={"exit_type": "FORCE_EXIT",
                         "direction": direction.lower()},
            ),
            PlaceOrder(
                symbol=symbol, side=close_side,
                qty=Decimal(str(qty)),
                order_type=order_strategy,
                origin="exit",
            ),
        ]
        return actions

    # ------------------------------------------------------------------
    # Health hook — runtime calls this on a heartbeat. Always healthy
    # for v1 (the strategy has no external dependencies to monitor).
    # ------------------------------------------------------------------

    async def health_check(self, ctx: StrategyContext) -> dict:
        return {"ok": True, "details": "tick_fuzzy v0.1"}

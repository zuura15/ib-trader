"""Engine-side reconciler for position state management.

Runs in the engine process. Queries IB for open orders and positions,
parses orderRef tags, and updates Redis state keys. Three modes:

1. Startup recovery — rebuild state from IB after engine restart
2. Disconnect recovery — catch up after IB Gateway reconnection
3. Sanity heartbeat — periodic check (every 30-60s) comparing Redis vs IB

The reconciler is the SAFETY NET, not the primary update path. The primary
path is push-based: IB callbacks → Redis streams + keys immediately.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.engine.order_ref import decode as decode_order_ref
from ib_trader.redis.streams import StreamWriter, StreamNames
from ib_trader.redis.state import StateStore, StateKeys

logger = logging.getLogger(__name__)

# Position states
FLAT = "FLAT"
ENTERING = "ENTERING"
OPEN = "OPEN"
EXITING = "EXITING"


class Reconciler:
    """Engine-side reconciler that keeps Redis state in sync with IB.

    Args:
        ib: IBClientBase instance (the engine's sole IB connection).
        redis: Async Redis client.
        sanity_interval: Seconds between sanity checks (default 60).
    """

    def __init__(self, ib, redis, sanity_interval: int = 60) -> None:
        self._ib = ib
        self._redis = redis
        self._state = StateStore(redis)
        self._sanity_interval = sanity_interval

    async def startup_reconcile(self) -> None:
        """Rebuild Redis state from IB on engine startup.

        Queries IB for all open orders and current positions, parses
        orderRef tags, and SETs Redis position keys. Also XADDs initial
        state entries to streams so consumers can catch up.
        """
        logger.info('{"event": "RECONCILER_STARTUP_BEGIN"}')

        # Query IB for current state
        open_orders = await self._ib.get_open_orders()
        ib_positions = await self._get_ib_positions()

        # Build maps from orderRef
        our_orders = {}  # {(bot_ref, symbol): order_info}
        for order in open_orders:
            ref_info = decode_order_ref(order.get("order_ref", "") or "")
            if ref_info:
                key = (ref_info.bot_ref, ref_info.symbol)
                our_orders[key] = {
                    "ib_order_id": order["ib_order_id"],
                    "side": ref_info.side,
                    "status": order["status"],
                    "serial": ref_info.serial,
                }

        our_positions = {}  # {symbol: position_info}
        for pos in ib_positions:
            if pos["qty"] != 0:
                our_positions[pos["symbol"]] = pos

        # Reconcile: for each known bot position key in Redis, check against IB
        # Also discover any IB state that Redis doesn't know about
        reconciled = 0

        # Process orders we know about via orderRef
        for (bot_ref, symbol), order_info in our_orders.items():
            pos_key = StateKeys.position(bot_ref, symbol)
            current = await self._state.get(pos_key)
            has_position = symbol in our_positions

            new_state = self._determine_state(
                current_state=current.get("state") if current else None,
                has_order=True,
                order_side=order_info["side"],
                has_position=has_position,
            )

            # The bot's pos:* key tracks the bot's own position from its fills.
            # Don't pollute it with IB's total position (which includes manual
            # positions). Preserve existing qty/avg_price if we have them;
            # otherwise leave at 0 — fill callbacks will populate them.
            pos_data = {
                "state": new_state,
                "qty": current.get("qty", "0") if current else "0",
                "avg_price": current.get("avg_price", "0") if current else "0",
                "serial": order_info["serial"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if current and current.get("entry_price"):
                pos_data["entry_price"] = current["entry_price"]
                pos_data["entry_time"] = current.get("entry_time")

            await self._state.set(pos_key, pos_data)
            reconciled += 1

        # Positions without matching orderRef tags are manual/external —
        # silently skip them. The reconciler only manages bot-tagged positions.

        logger.info(
            '{"event": "RECONCILER_STARTUP_COMPLETE", "orders": %d, "positions": %d, "reconciled": %d}',
            len(our_orders), len(our_positions), reconciled,
        )

    async def sanity_check(self) -> None:
        """Compare Redis state keys against IB and fix disagreements.

        This is a lightweight paranoia check, not the primary update path.
        Logs WARNING on any discrepancy and fixes silently.
        """
        try:
            ib_positions = await self._get_ib_positions()
            # Only compare STK positions — option contracts share the
            # underlying symbol and cause false drift alerts until we
            # re-key bot state by con_id.
            ib_position_map = {
                p["symbol"]: p for p in ib_positions
                if p["qty"] != 0 and p.get("sec_type", "STK") == "STK"
            }

            # Iterate bot state keys (bot:<uuid>) via the YAML registry
            from ib_trader.bots import registry_config
            for defn in registry_config.all_definitions():
                key = f"bot:{defn.id}"
                current = await self._state.get(key)
                if not current:
                    continue
                bot_ref = defn.config.get("ref_id", defn.name)
                symbol = current.get("symbol") or defn.config.get("symbol", "")
                state = current.get("state") or current.get("position_state", FLAT)

                # Skip reconciliation for bots that aren't running.
                # Stale strat:* keys from prior sessions are just garbage —
                # silently flush them instead of logging a scary WARNING.
                from ib_trader.bots import registry_config
                defn = registry_config.get_by_name(bot_ref)
                if defn:
                    from ib_trader.bots.fsm import FSM, BotState
                    fsm = FSM(defn.id, self._redis)
                    fsm_state = await fsm.current_state()
                    if fsm_state in (BotState.OFF, BotState.ERRORED):
                        if state != FLAT:
                            # Silently clear stale state
                            current["state"] = FLAT
                            current["position_state"] = FLAT
                            current["qty"] = "0"
                            await self._state.set(key, current)
                            logger.info(
                                '{"event": "STALE_STATE_CLEARED", "key": "%s", '
                                '"old_state": "%s", "reason": "bot_fsm_is_off"}',
                                key, state,
                            )
                        continue

                has_position = symbol in ib_position_map

                # Observer-only: log drift + publish a RECONCILED event to
                # the bot's fill stream. The bot applies the reconciliation
                # rule on its own state key — reconciler no longer writes
                # there directly.
                if state == OPEN and not has_position:
                    logger.warning(
                        '{"event": "RECONCILER_DRIFT", "key": "%s", "local": "OPEN", "ib": "NO_POSITION"}',
                        key,
                    )
                    writer = StreamWriter(self._redis, StreamNames.order_updates(), maxlen=5000)
                    await writer.add({
                        "type": "RECONCILED",
                        "symbol": symbol,
                        "prev_state": OPEN,
                        "new_state": FLAT,
                        "reason": "position_closed_externally",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })

                elif state == FLAT and has_position:
                    ib_pos = ib_position_map[symbol]
                    logger.warning(
                        '{"event": "RECONCILER_DRIFT", "key": "%s", "local": "FLAT", '
                        '"ib": "HAS_POSITION", "qty": "%s"}',
                        key, ib_pos["qty"],
                    )
                    writer = StreamWriter(self._redis, StreamNames.order_updates(), maxlen=5000)
                    await writer.add({
                        "type": "RECONCILED",
                        "symbol": symbol,
                        "prev_state": FLAT,
                        "new_state": OPEN,
                        "reason": "position_found_in_ib",
                        "qty": str(ib_pos["qty"]),
                        "avg_price": str(ib_pos.get("avg_price", 0)),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })

        except Exception:
            logger.exception('{"event": "RECONCILER_SANITY_ERROR"}')

    async def run_sanity_loop(self) -> None:
        """Run the sanity check periodically as a background task."""
        while True:
            await asyncio.sleep(self._sanity_interval)
            await self.sanity_check()

    def _determine_state(
        self,
        current_state: str | None,
        has_order: bool,
        order_side: str | None,
        has_position: bool,
    ) -> str:
        """Apply the state transition table.

        Returns the new position state based on IB's view.
        """
        if not current_state:
            # No prior state — derive from IB
            if has_position:
                return OPEN
            if has_order:
                return ENTERING if order_side == "B" else EXITING
            return FLAT

        if current_state == ENTERING:
            if has_order and not has_position:
                return ENTERING  # Wait
            if not has_order and has_position:
                return OPEN  # Filled
            if not has_order and not has_position:
                return FLAT  # Cancelled/rejected
            return ENTERING

        if current_state == OPEN:
            if has_position:
                return OPEN
            return FLAT  # Closed externally

        if current_state == EXITING:
            if has_order and has_position:
                return EXITING  # Wait
            if not has_order and not has_position:
                return FLAT  # Exit complete
            if not has_order and has_position:
                return OPEN  # Exit cancelled
            return EXITING

        if current_state == FLAT:
            if has_order:
                # Repair: FLAT but IB has our order — update to match IB
                new_state = ENTERING if order_side == "B" else EXITING
                logger.warning(
                    '{"event": "RECONCILER_FLAT_REPAIRED", "side": "%s", "new_state": "%s"}',
                    order_side, new_state,
                )
                return new_state
            if has_position:
                # Repair: FLAT but IB has a position — must be OPEN
                logger.warning('{"event": "RECONCILER_FLAT_HAS_POSITION"}')
                return OPEN
            return FLAT

        return current_state

    async def _get_ib_positions(self) -> list[dict]:
        """Get current IB positions via the raw ib_async API.

        Returns list of dicts with keys: symbol, qty, avg_price, con_id.
        """
        if not hasattr(self._ib, '_ib'):
            return []

        ib_obj = self._ib._ib
        try:
            await asyncio.wait_for(ib_obj.reqPositionsAsync(), timeout=10)
        except asyncio.TimeoutError:
            logger.debug('{"event": "RECONCILER_POSITIONS_TIMEOUT"}')
            return []
        except Exception:
            logger.warning('{"event": "RECONCILER_POSITIONS_FAILED"}')
            return []

        positions = []
        for p in ib_obj.positions():
            positions.append({
                "symbol": p.contract.symbol,
                "qty": Decimal(str(p.position)),
                "avg_price": Decimal(str(p.avgCost)),
                "con_id": p.contract.conId,
            })
        return positions

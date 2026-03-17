"""Example mean reversion bot.

A simple strategy that monitors a symbol's price and places orders
when the price deviates from a rolling average. Demonstrates the
BotBase API: on_tick, place_order, wait_for_command, update_signal.

This bot:
  1. Tracks a rolling window of last N mid prices
  2. When price drops below (mean - threshold), places a BUY
  3. When price rises above (mean + threshold), places a SELL to close
  4. Writes signals and actions to bot_events for audit

Config (via config_json in bots table):
  {
    "symbol": "SPY",
    "window_size": 20,
    "threshold_pct": 0.5,
    "qty": 100,
    "broker": "ib",
    "tick_interval_seconds": 10
  }
"""
import json
import logging
from collections import deque
from decimal import Decimal

from ib_trader.bots.base import BotBase
from ib_trader.bots.registry import register_strategy

logger = logging.getLogger(__name__)


class MeanRevertBot(BotBase):
    """Simple mean reversion strategy.

    Buys when price is below mean - threshold, sells when above mean + threshold.
    Tracks position via open trades in SQLite.
    """

    def __init__(self, bot_id, config, session_factory):
        super().__init__(bot_id, config, session_factory)
        self.symbol = config.get("symbol", "SPY")
        self.window_size = config.get("window_size", 20)
        self.threshold_pct = Decimal(str(config.get("threshold_pct", 0.5)))
        self.qty = config.get("qty", 100)
        self.broker = config.get("broker", "ib")
        self.prices: deque[Decimal] = deque(maxlen=self.window_size)
        self._has_position = False

    async def on_startup(self, open_positions: list) -> None:
        """Check if we have an existing position from a previous run."""
        for pos in open_positions:
            if pos.symbol == self.symbol and pos.status.value == "OPEN":
                self._has_position = True
                self.log_event("STARTUP", message=f"Found existing position: {self.symbol}",
                               payload={"serial": pos.serial_number})
                break

    async def on_tick(self) -> None:
        """Check price vs mean, decide whether to trade."""
        # In a real implementation, we'd read market data from a data feed.
        # For this example, we simulate by reading the last trade price
        # from the pending_commands output or a market data service.
        #
        # Since bots don't have broker connections, they would either:
        # 1. Submit a "quote SYMBOL" command to get a price
        # 2. Read from a market_data table populated by the engine
        # 3. Use an external data API (e.g., Alpaca data API)
        #
        # For now, we demonstrate the bot lifecycle without live data.

        self.update_heartbeat()

        # Simulate price tracking (in production, read from market data)
        # This is a placeholder showing the bot framework API
        if len(self.prices) < self.window_size:
            self.update_signal(
                f"Collecting prices: {len(self.prices)}/{self.window_size}"
            )
            return

        mean = sum(self.prices) / len(self.prices)
        last = self.prices[-1]
        threshold = mean * self.threshold_pct / Decimal("100")

        if last < mean - threshold and not self._has_position:
            # Price below mean - threshold: BUY signal
            signal = f"BUY signal: {self.symbol} @ {last} (mean={mean:.2f}, threshold={threshold:.2f})"
            self.update_signal(signal)
            self.log_event("SIGNAL", message=signal,
                           payload={"type": "BUY", "price": str(last), "mean": str(mean)})

            # Place order through the engine
            cmd_text = f"buy {self.symbol} {self.qty} mid"
            cmd_id = await self.place_order(cmd_text, broker=self.broker)
            self.update_action(f"Placed BUY {self.qty} {self.symbol}")

            # Wait for command completion
            result = await self.wait_for_command(cmd_id, timeout=30)
            if result and result["status"] == "SUCCESS":
                self._has_position = True
                self.log_event("ACTION", message=f"BUY {self.qty} {self.symbol} placed",
                               payload={"command_id": cmd_id})
            else:
                error = result["error"] if result else "timeout"
                self.log_event("ERROR", message=f"BUY failed: {error}")

        elif last > mean + threshold and self._has_position:
            # Price above mean + threshold: CLOSE signal
            signal = f"CLOSE signal: {self.symbol} @ {last} (mean={mean:.2f})"
            self.update_signal(signal)
            self.log_event("SIGNAL", message=signal,
                           payload={"type": "CLOSE", "price": str(last), "mean": str(mean)})

            # Find and close the position
            positions = self.get_open_positions()
            for pos in positions:
                if pos.symbol == self.symbol:
                    cmd_text = f"close {pos.serial_number} mid"
                    cmd_id = await self.place_order(cmd_text, broker=self.broker)
                    self.update_action(f"Closing {self.symbol} (serial {pos.serial_number})")

                    result = await self.wait_for_command(cmd_id, timeout=30)
                    if result and result["status"] == "SUCCESS":
                        self._has_position = False
                        self.log_event("ACTION", message=f"Closed {self.symbol}",
                                       payload={"serial": pos.serial_number})
                    break
        else:
            self.update_signal(
                f"Watching {self.symbol}: last={last}, mean={mean:.2f}, "
                f"band=[{mean - threshold:.2f}, {mean + threshold:.2f}]"
            )


# Register this strategy so the bot runner can find it
register_strategy("mean_revert", MeanRevertBot)

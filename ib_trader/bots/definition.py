"""Immutable bot identity + config, loaded from a YAML file on disk.

Replaces the `bots` SQLAlchemy table as the source of truth for bot
identity. Runtime state (status, last heartbeat, last action, kill
switch, error message) lives in Redis — see `ib_trader/redis/state.py`.
Audit history stays in SQLite `bot_events`.

Rule: a `BotDefinition` is frozen at load time. Any field that the
runtime reads repeatedly (especially `ref_id` and `symbol`, which key
fill streams and position state) MUST NOT change under a running bot.
The reload endpoint is additive / next-start only and will refuse to
hot-swap a running bot (see `api/routes/bots.py` POST /api/bots/reload).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BotDefinition:
    """One bot's on-disk definition.

    Attributes are the minimum identity + configuration the runner needs
    to instantiate a strategy. Nothing here is mutable state; status,
    heartbeats, etc. live in Redis.
    """

    id: str                           # stable UUID (string form)
    name: str                         # unique human-readable name
    strategy: str                     # key in the strategy registry
    broker: str = "ib"
    tick_interval_seconds: int = 10

    # When True, `ManualEntryMiddleware` drops PlaceOrder(side="BUY",
    # origin="strategy") actions. Exits and manual overrides still flow.
    # Used for test bots where auto-entry would be dangerous on a live
    # account but we still want the full exit / trailing-stop machinery.
    manual_entry_only: bool = False

    # Opaque strategy config dict loaded from the bot YAML's `config:`
    # block. Typically contains `strategy_config` (path to a strategy
    # YAML), `symbol`, `qty`, `max_orders`, etc. Shape is strategy-specific
    # and is not interpreted here.
    config: dict[str, Any] = field(default_factory=dict)

    # Optional free-form symbol list for the UI; the strategy's own
    # symbol(s) come from `config`.
    symbols: tuple[str, ...] = ()

    # Where this definition was loaded from. Useful for logs and for the
    # reload endpoint to tell which file produced which bot.
    source_path: str = ""

    def __post_init__(self) -> None:
        # Belt-and-suspenders: strings only in name/id (YAML can produce
        # ints if the author forgets to quote a numeric id).
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "name", str(self.name))
        # Lock symbols to a tuple so nobody mutates the list we hand out.
        if not isinstance(self.symbols, tuple):
            object.__setattr__(self, "symbols", tuple(self.symbols))

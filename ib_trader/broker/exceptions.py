"""Exceptions raised by broker-facing resolution / qualification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class InstrumentResolutionError(Exception):
    """Base class for resolve_instrument / qualify_contract failures."""


@dataclass
class AmbiguousInstrument(InstrumentResolutionError):
    """Multiple IB contracts matched the qualify request.

    Raised when the caller omits ``trading_class`` (or any other
    disambiguator) and the broker returns more than one candidate.
    The caller (CLI / API / UI) surfaces the ``candidates`` list so the
    user can pick explicitly.
    """

    root: str
    candidates: Sequence["FutureExpiryCandidate"]

    def __str__(self) -> str:
        # Show each candidate as ``tradingClass@exchange`` so the user
        # can pick the right disambiguator. When trading classes diverge
        # (ES vs MES) the hint is --trading-class; when they don't but
        # exchanges do (GC@COMEX vs GC@QBALGO) the hint is --exchange.
        pairs = sorted({
            f"{c.trading_class}@{c.exchange or '?'}" for c in self.candidates
        })
        tcs = {c.trading_class for c in self.candidates}
        exs = {c.exchange or "" for c in self.candidates}
        if len(tcs) > 1:
            hint = "specify --trading-class"
        elif len(exs) > 1:
            hint = "specify --exchange"
        else:
            hint = "specify --trading-class or --exchange"
        return (
            f"ambiguous {self.root}: {len(self.candidates)} candidates "
            f"[{', '.join(pairs)}]; {hint}"
        )


class ExpiredContractError(InstrumentResolutionError):
    """The requested contract has already passed its last-trade date."""

    def __init__(self, root: str, expiry: str):
        self.root = root
        self.expiry = expiry
        super().__init__(f"{root} expiry {expiry} is in the past")


# Imported here to avoid a circular import from broker.types.
from ib_trader.broker.types import FutureExpiryCandidate

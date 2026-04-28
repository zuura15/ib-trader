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
        tcs = sorted({c.trading_class for c in self.candidates})
        return (
            f"ambiguous {self.root}: {len(self.candidates)} candidates "
            f"across trading classes {tcs}; specify trading_class explicitly"
        )


class ExpiredContractError(InstrumentResolutionError):
    """The requested contract has already passed its last-trade date."""

    def __init__(self, root: str, expiry: str):
        self.root = root
        self.expiry = expiry
        super().__init__(f"{root} expiry {expiry} is in the past")


# Imported here to avoid a circular import from broker.types.
from ib_trader.broker.types import FutureExpiryCandidate

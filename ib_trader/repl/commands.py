"""REPL command parsing and dataclasses.

Commands are parsed with shlex.split() — not argparse (argparse calls sys.exit() on errors).
On any parse or validation error: returns None and prints a clear error.

All parse functions accept an optional ``router`` argument.  When provided,
error messages are routed through the OutputRouter instead of print().  When
None (the default), print() is used for backward compatibility with tests and
non-TUI usage.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING

from ib_trader.repl.output_router import OutputPane, OutputSeverity

if TYPE_CHECKING:
    from ib_trader.repl.output_router import OutputRouter


class Strategy(StrEnum):
    """Valid order placement strategies."""
    MID = "mid"
    MARKET = "market"
    BID = "bid"
    ASK = "ask"
    LIMIT = "limit"


@dataclass
class BuyCommand:
    """Parsed 'buy' command."""
    symbol: str
    qty: Decimal | None
    dollars: Decimal | None
    strategy: Strategy
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    stop_loss: Decimal | None
    limit_price: Decimal | None = None


@dataclass
class SellCommand:
    """Parsed 'sell' command."""
    symbol: str
    qty: Decimal | None
    dollars: Decimal | None
    strategy: Strategy
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    stop_loss: Decimal | None
    limit_price: Decimal | None = None


@dataclass
class CloseCommand:
    """Parsed 'close' command."""
    serial: int
    strategy: Strategy               # default "mid"
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    limit_price: Decimal | None = None


@dataclass
class ModifyCommand:
    """Parsed 'modify' command. STUB — no other fields."""
    serial: int


def _emit_error(message: str, router: "OutputRouter | None") -> None:
    """Emit a parse error via router when available, otherwise print."""
    if router is not None:
        router.emit(message, pane=OutputPane.COMMAND, severity=OutputSeverity.ERROR)
    else:
        print(message)


def _parse_decimal(value: str, name: str) -> Decimal:
    """Parse a string as Decimal, raising ValueError on failure."""
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"'{name}' must be a number, got: {value!r}")


def parse_buy_sell(
    tokens: list[str], router: "OutputRouter | None" = None
) -> "BuyCommand | SellCommand | None":
    """Parse a buy or sell command from tokenized input.

    Grammar:
        buy/sell SYMBOL QTY STRATEGY [PROFIT] [--take-profit-price N]
                                               [--stop-loss N] [--dollars N]

    Args:
        tokens: List including the verb ('buy' or 'sell') as tokens[0].
        router: OutputRouter for error output.  Falls back to print() when None.

    Returns:
        BuyCommand or SellCommand, or None on parse error (error already emitted).
    """
    verb = tokens[0].lower()
    args = tokens[1:]

    qty = None
    dollars = None
    strategy = None
    profit_amount = None
    take_profit_price = None
    stop_loss = None

    positional = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--dollars":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --dollars requires a value", router)
                return None
            try:
                dollars = _parse_decimal(args[i], "--dollars")
            except ValueError as e:
                _emit_error(f"\u2717 Error: {e}", router)
                return None
        elif tok == "--take-profit-price":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --take-profit-price requires a value", router)
                return None
            try:
                take_profit_price = _parse_decimal(args[i], "--take-profit-price")
            except ValueError as e:
                _emit_error(f"\u2717 Error: {e}", router)
                return None
        elif tok == "--stop-loss":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --stop-loss requires a value", router)
                return None
            try:
                stop_loss = _parse_decimal(args[i], "--stop-loss")
            except ValueError as e:
                _emit_error(f"\u2717 Error: {e}", router)
                return None
        elif tok.startswith("--"):
            _emit_error(f"\u2717 Error: unknown option {tok!r}", router)
            return None
        else:
            positional.append(tok)
        i += 1

    # Positional: SYMBOL QTY STRATEGY [PROFIT]
    if len(positional) < 3:
        _emit_error(f"\u2717 Error: usage: {verb} SYMBOL QTY STRATEGY [PROFIT]", router)
        return None

    symbol = positional[0].upper()

    if dollars is None:
        try:
            qty = _parse_decimal(positional[1], "QTY")
            if qty <= 0:
                _emit_error("\u2717 Error: QTY must be a positive number", router)
                return None
        except ValueError as e:
            _emit_error(f"\u2717 Error: {e}", router)
            return None

    try:
        strategy = Strategy(positional[2].lower())
    except ValueError:
        valid = ", ".join(f"'{s}'" for s in Strategy)
        _emit_error(f"\u2717 Error: STRATEGY must be one of {valid}, got {positional[2]!r}", router)
        return None

    # For 'limit' strategy, the next positional is the required limit price
    limit_price = None
    next_pos = 3  # index of next positional to consume

    if strategy == Strategy.LIMIT:
        if len(positional) < 4:
            _emit_error(
                f"\u2717 Error: 'limit' strategy requires a price: {verb} SYMBOL QTY limit PRICE",
                router,
            )
            return None
        try:
            limit_price = _parse_decimal(positional[3], "LIMIT_PRICE")
            if limit_price <= 0:
                _emit_error("\u2717 Error: LIMIT_PRICE must be a positive number", router)
                return None
        except ValueError as e:
            _emit_error(f"\u2717 Error: {e}", router)
            return None
        next_pos = 4

    if len(positional) > next_pos:
        try:
            profit_amount = _parse_decimal(positional[next_pos], "PROFIT")
            if profit_amount <= 0:
                _emit_error("\u2717 Error: PROFIT must be a positive number", router)
                return None
        except ValueError as e:
            _emit_error(f"\u2717 Error: {e}", router)
            return None

    if verb == "buy":
        return BuyCommand(
            symbol=symbol,
            qty=qty,
            dollars=dollars,
            strategy=strategy,
            profit_amount=profit_amount,
            take_profit_price=take_profit_price,
            stop_loss=stop_loss,
            limit_price=limit_price,
        )
    else:
        return SellCommand(
            symbol=symbol,
            qty=qty,
            dollars=dollars,
            strategy=strategy,
            profit_amount=profit_amount,
            take_profit_price=take_profit_price,
            stop_loss=stop_loss,
            limit_price=limit_price,
        )


def parse_close(
    tokens: list[str], router: "OutputRouter | None" = None
) -> "CloseCommand | None":
    """Parse a close command from tokenized input.

    Grammar:
        close SERIAL [STRATEGY] [--take-profit-price N]

    Args:
        tokens: List including 'close' as tokens[0].
        router: OutputRouter for error output.  Falls back to print() when None.

    Returns:
        CloseCommand or None on parse error.
    """
    args = tokens[1:]
    take_profit_price = None
    profit_amount = None
    strategy = Strategy.MID

    positional = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--take-profit-price":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --take-profit-price requires a value", router)
                return None
            try:
                take_profit_price = _parse_decimal(args[i], "--take-profit-price")
            except ValueError as e:
                _emit_error(f"\u2717 Error: {e}", router)
                return None
        elif tok.startswith("--"):
            _emit_error(f"\u2717 Error: unknown option {tok!r}", router)
            return None
        else:
            positional.append(tok)
        i += 1

    if not positional:
        _emit_error("\u2717 Error: usage: close SERIAL [STRATEGY] [PROFIT]", router)
        return None

    try:
        serial = int(positional[0])
    except ValueError:
        _emit_error(f"\u2717 Error: SERIAL must be an integer, got {positional[0]!r}", router)
        return None

    if len(positional) >= 2:
        try:
            strategy = Strategy(positional[1].lower())
        except ValueError:
            valid = ", ".join(f"'{s}'" for s in Strategy)
            _emit_error(f"\u2717 Error: STRATEGY must be one of {valid}, got {positional[1]!r}", router)
            return None

    # For 'limit' strategy, the next positional is the required limit price
    limit_price = None
    next_pos = 2

    if strategy == Strategy.LIMIT:
        if len(positional) < 3:
            _emit_error(
                "\u2717 Error: 'limit' strategy requires a price: close SERIAL limit PRICE",
                router,
            )
            return None
        try:
            limit_price = _parse_decimal(positional[2], "LIMIT_PRICE")
            if limit_price <= 0:
                _emit_error("\u2717 Error: LIMIT_PRICE must be a positive number", router)
                return None
        except ValueError as e:
            _emit_error(f"\u2717 Error: {e}", router)
            return None
        next_pos = 3

    if len(positional) > next_pos:
        try:
            profit_amount = _parse_decimal(positional[next_pos], "PROFIT")
        except ValueError as e:
            _emit_error(f"\u2717 Error: {e}", router)
            return None

    return CloseCommand(
        serial=serial,
        strategy=strategy,
        profit_amount=profit_amount,
        take_profit_price=take_profit_price,
        limit_price=limit_price,
    )


def parse_modify(
    tokens: list[str], router: "OutputRouter | None" = None
) -> "ModifyCommand | None":
    """Parse a modify command. STUB — only serial number is parsed.

    Args:
        tokens: List including 'modify' as tokens[0].
        router: OutputRouter for error output.  Falls back to print() when None.

    Returns:
        ModifyCommand or None on parse error.
    """
    args = tokens[1:]
    if not args:
        _emit_error("\u2717 Error: usage: modify SERIAL", router)
        return None
    try:
        serial = int(args[0])
    except ValueError:
        _emit_error(f"\u2717 Error: SERIAL must be an integer, got {args[0]!r}", router)
        return None
    return ModifyCommand(serial=serial)


def parse_command(
    line: str, router: "OutputRouter | None" = None
) -> "BuyCommand | SellCommand | CloseCommand | ModifyCommand | str | None":
    """Parse a raw command line from the REPL prompt.

    Args:
        line: Raw command string from the user.
        router: OutputRouter for error output.  Falls back to print() when None.

    Returns:
        Parsed command dataclass, a string for built-in commands ('exit', 'orders', etc.),
        or None if the line is empty or parsing failed (error already emitted).
    """
    line = line.strip()
    if not line:
        return None

    try:
        tokens = shlex.split(line)
    except ValueError as e:
        _emit_error(f"\u2717 Error: {e}", router)
        return None

    if not tokens:
        return None

    verb = tokens[0].lower()

    if verb in ("exit", "quit"):
        return "exit"
    if verb in ("orders", "stats", "status", "refresh", "help"):
        return verb

    if verb in ("buy", "sell"):
        return parse_buy_sell(tokens, router=router)

    if verb == "close":
        return parse_close(tokens, router=router)

    if verb == "modify":
        return parse_modify(tokens, router=router)

    _emit_error(f"\u2717 Error: unknown command {verb!r}. Type 'help' for available commands.", router)
    return None

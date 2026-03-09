"""REPL command parsing and dataclasses.

Commands are parsed with shlex.split() — not argparse (argparse calls sys.exit() on errors).
On any parse or validation error: returns None and prints a clear error.
"""
import shlex
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class Strategy(StrEnum):
    """Valid order placement strategies."""
    MID = "mid"
    MARKET = "market"
    BID = "bid"
    ASK = "ask"


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


@dataclass
class CloseCommand:
    """Parsed 'close' command."""
    serial: int
    strategy: Strategy               # default "mid"
    profit_amount: Decimal | None
    take_profit_price: Decimal | None


@dataclass
class ModifyCommand:
    """Parsed 'modify' command. STUB — no other fields."""
    serial: int


def _parse_decimal(value: str, name: str) -> Decimal:
    """Parse a string as Decimal, raising ValueError on failure."""
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"'{name}' must be a number, got: {value!r}")


def parse_buy_sell(tokens: list[str]) -> "BuyCommand | SellCommand | None":
    """Parse a buy or sell command from tokenized input.

    Grammar:
        buy/sell SYMBOL QTY STRATEGY [PROFIT] [--take-profit-price N]
                                               [--stop-loss N] [--dollars N]

    Args:
        tokens: List including the verb ('buy' or 'sell') as tokens[0].

    Returns:
        BuyCommand or SellCommand, or None on parse error (error printed to stdout).
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
                print("\u2717 Error: --dollars requires a value")
                return None
            try:
                dollars = _parse_decimal(args[i], "--dollars")
            except ValueError as e:
                print(f"\u2717 Error: {e}")
                return None
        elif tok == "--take-profit-price":
            i += 1
            if i >= len(args):
                print("\u2717 Error: --take-profit-price requires a value")
                return None
            try:
                take_profit_price = _parse_decimal(args[i], "--take-profit-price")
            except ValueError as e:
                print(f"\u2717 Error: {e}")
                return None
        elif tok == "--stop-loss":
            i += 1
            if i >= len(args):
                print("\u2717 Error: --stop-loss requires a value")
                return None
            try:
                stop_loss = _parse_decimal(args[i], "--stop-loss")
            except ValueError as e:
                print(f"\u2717 Error: {e}")
                return None
        elif tok.startswith("--"):
            print(f"\u2717 Error: unknown option {tok!r}")
            return None
        else:
            positional.append(tok)
        i += 1

    # Positional: SYMBOL QTY STRATEGY [PROFIT]
    if len(positional) < 3:
        print(f"\u2717 Error: usage: {verb} SYMBOL QTY STRATEGY [PROFIT]")
        return None

    symbol = positional[0].upper()

    if dollars is None:
        try:
            qty = _parse_decimal(positional[1], "QTY")
            if qty <= 0:
                print("\u2717 Error: QTY must be a positive number")
                return None
        except ValueError as e:
            print(f"\u2717 Error: {e}")
            return None

    try:
        strategy = Strategy(positional[2].lower())
    except ValueError:
        valid = ", ".join(f"'{s}'" for s in Strategy)
        print(f"\u2717 Error: STRATEGY must be one of {valid}, got {positional[2]!r}")
        return None

    if len(positional) >= 4:
        try:
            profit_amount = _parse_decimal(positional[3], "PROFIT")
            if profit_amount <= 0:
                print("\u2717 Error: PROFIT must be a positive number")
                return None
        except ValueError as e:
            print(f"\u2717 Error: {e}")
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
        )


def parse_close(tokens: list[str]) -> "CloseCommand | None":
    """Parse a close command from tokenized input.

    Grammar:
        close SERIAL [STRATEGY] [--take-profit-price N]

    Args:
        tokens: List including 'close' as tokens[0].

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
                print("\u2717 Error: --take-profit-price requires a value")
                return None
            try:
                take_profit_price = _parse_decimal(args[i], "--take-profit-price")
            except ValueError as e:
                print(f"\u2717 Error: {e}")
                return None
        elif tok.startswith("--"):
            print(f"\u2717 Error: unknown option {tok!r}")
            return None
        else:
            positional.append(tok)
        i += 1

    if not positional:
        print("\u2717 Error: usage: close SERIAL [STRATEGY] [PROFIT]")
        return None

    try:
        serial = int(positional[0])
    except ValueError:
        print(f"\u2717 Error: SERIAL must be an integer, got {positional[0]!r}")
        return None

    if len(positional) >= 2:
        try:
            strategy = Strategy(positional[1].lower())
        except ValueError:
            valid = ", ".join(f"'{s}'" for s in Strategy)
            print(f"\u2717 Error: STRATEGY must be one of {valid}, got {positional[1]!r}")
            return None

    if len(positional) >= 3:
        try:
            profit_amount = _parse_decimal(positional[2], "PROFIT")
        except ValueError as e:
            print(f"\u2717 Error: {e}")
            return None

    return CloseCommand(
        serial=serial,
        strategy=strategy,
        profit_amount=profit_amount,
        take_profit_price=take_profit_price,
    )


def parse_modify(tokens: list[str]) -> "ModifyCommand | None":
    """Parse a modify command. STUB — only serial number is parsed.

    Args:
        tokens: List including 'modify' as tokens[0].

    Returns:
        ModifyCommand or None on parse error.
    """
    args = tokens[1:]
    if not args:
        print("\u2717 Error: usage: modify SERIAL")
        return None
    try:
        serial = int(args[0])
    except ValueError:
        print(f"\u2717 Error: SERIAL must be an integer, got {args[0]!r}")
        return None
    return ModifyCommand(serial=serial)


def parse_command(line: str) -> "BuyCommand | SellCommand | CloseCommand | ModifyCommand | str | None":
    """Parse a raw command line from the REPL prompt.

    Args:
        line: Raw command string from the user.

    Returns:
        Parsed command dataclass, a string for built-in commands ('exit', 'orders', etc.),
        or None if the line is empty or parsing failed (error already printed).
    """
    line = line.strip()
    if not line:
        return None

    try:
        tokens = shlex.split(line)
    except ValueError as e:
        print(f"\u2717 Error: {e}")
        return None

    if not tokens:
        return None

    verb = tokens[0].lower()

    if verb in ("exit", "quit"):
        return "exit"
    if verb in ("orders", "stats", "status", "refresh", "help"):
        return verb

    if verb in ("buy", "sell"):
        return parse_buy_sell(tokens)

    if verb == "close":
        return parse_close(tokens)

    if verb == "modify":
        return parse_modify(tokens)

    print(f"\u2717 Error: unknown command {verb!r}. Type 'help' for available commands.")
    return None

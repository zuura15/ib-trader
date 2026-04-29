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
from ib_trader.utils.symbol import parse_month_code

if TYPE_CHECKING:
    from ib_trader.repl.output_router import OutputRouter


# Known futures roots recognised by the REPL shorthand. If the first
# positional token matches one of these AND the second token is a
# month-code (``Z26``, ``H27``...), we parse as FUT and emit explicit
# security_type/expiry/trading_class fields on the command. No downstream
# code infers sec-type from token shape — the parser is the only place
# that converts shorthand into the explicit wire format (Epic 1 D2).
_FUTURES_ROOTS: frozenset[str] = frozenset({
    "ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K",
    "CL", "MCL", "GC", "MGC", "SI", "SIL", "HG", "MHG",
    "ZN", "ZB", "ZF", "ZT",
    "6E", "6J", "6B", "6A", "6C",
})


def _is_futures_local_symbol(symbol: str) -> bool:
    """True if ``symbol`` is an IB-paste futures localSymbol (e.g.
    ``ESZ6`` / ``MESM6`` / ``GCM26``). Detection is "starts with a
    known futures root, followed by a parseable month-code". Returns
    False for stocks even if a stock ticker happens to share a prefix
    with a futures root, because the trailing characters won't parse
    as a month code.

    Why only known roots? It's the IB-paste form the system already
    emits (see ``utils.symbol.format_ib_paste_symbol``). Restricting
    to ``_FUTURES_ROOTS`` keeps ``GLD`` (an ETF that starts with ``G``)
    out of the futures path.
    """
    if not symbol:
        return False
    s = symbol.upper()
    # Iterate longest roots first so MES is preferred over MES-prefixed
    # ETFs (none today, but future-proofs the check).
    for root in sorted(_FUTURES_ROOTS, key=len, reverse=True):
        if s.startswith(root) and len(s) > len(root):
            try:
                parse_month_code(s[len(root):])
                return True
            except ValueError:
                continue
    return False


class Strategy(StrEnum):
    """Valid order placement strategies.

    This enum is the single source of truth for the string values
    accepted by the engine's ``/engine/orders`` API, the REPL command
    parser, and the bot middleware. Downstream code should derive
    validation sets and user-facing descriptions from here rather than
    re-listing the values.
    """
    MID = "mid"
    MARKET = "market"
    BID = "bid"
    ASK = "ask"
    LIMIT = "limit"
    # Session-aware aggressive-mid execution. RTH: reprice fast toward
    # the far side for a fixed duration, then cross to MKT for any
    # residual. ETH/overnight: reprice fast toward the far side but cap
    # at a slippage floor; raise CATASTROPHIC alert if the cap is hit.
    # See docs/design/execution-algos.md for the full spec.
    SMART_MARKET = "smart_market"


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
    bot_ref: str | None = None  # Bot reference for orderRef tagging
    # Epic 1 additions — explicit sec-type fields produced by the parser.
    # Never inferred downstream; CLI shorthand writes them here.
    security_type: str = "STK"
    expiry: str | None = None          # YYYYMM (CLI) or YYYYMMDD (IB-normalized)
    trading_class: str | None = None
    exchange: str | None = None
    # Trailing stop (FUT only). Exactly one of ``trail_percent`` or
    # ``trail_amount`` is set when ``--trail`` is supplied. Both None
    # means the user didn't ask for a trail; the order flow leaves it
    # alone. STK is rejected at parse time because IB-simulated STK
    # trails are RTH-only and unreliable overnight.
    trail_percent: Decimal | None = None
    trail_amount: Decimal | None = None


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
    bot_ref: str | None = None  # Bot reference for orderRef tagging
    security_type: str = "STK"
    expiry: str | None = None
    trading_class: str | None = None
    exchange: str | None = None
    trail_percent: Decimal | None = None
    trail_amount: Decimal | None = None


@dataclass
class CloseCommand:
    """Parsed 'close' command."""
    serial: int
    strategy: Strategy               # default "mid"
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    limit_price: Decimal | None = None
    bot_ref: str | None = None  # Bot reference for orderRef tagging


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
    """Parse a user-typed string as Decimal, raising ValueError on failure.

    Accepts an optional leading ``$`` so REPL users can type prices as
    ``$28.45`` or ``28.45`` interchangeably.  Currency is implicit (USD); we
    do not infer locale.  Comma thousands-separators are not accepted — IB
    quotes are dot-decimal and we keep input unambiguous.
    """
    raw = value.strip()
    stripped = raw[1:] if raw.startswith("$") else raw
    try:
        return Decimal(stripped)
    except InvalidOperation as e:
        raise ValueError(f"'{name}' must be a number, got: {value!r}") from e


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
    trail_percent: Decimal | None = None
    trail_amount: Decimal | None = None

    positional = []
    explicit_sec_type: str | None = None
    explicit_expiry: str | None = None
    explicit_trading_class: str | None = None
    explicit_exchange: str | None = None
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
        elif tok == "--profit":
            # Same semantic as the trailing positional PROFIT (dollars
            # of P&L to take). Flag form keeps parity with --stop-loss
            # and matches the cmd_text the engine internal API emits.
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --profit requires a value", router)
                return None
            try:
                profit_amount = _parse_decimal(args[i], "--profit")
            except ValueError as e:
                _emit_error(f"\u2717 Error: {e}", router)
                return None
        elif tok == "--trail":
            # IB-server-managed trailing stop placed after entry fills.
            # Accepts either a percent (``0.5%``) or a fixed offset in
            # instrument points (``2.0``). Percent \u2192 trailingPercent on
            # the IB Order; fixed \u2192 auxPrice. Mutually exclusive (one
            # is None on the command).
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --trail requires a value (e.g. 0.5% or 2.0)", router)
                return None
            raw = args[i].strip()
            try:
                if raw.endswith("%"):
                    pct = _parse_decimal(raw[:-1], "--trail")
                    if pct <= 0 or pct >= 50:
                        _emit_error(
                            "\u2717 Error: --trail percent must be in (0, 50)", router,
                        )
                        return None
                    trail_percent = pct
                else:
                    amt = _parse_decimal(raw, "--trail")
                    if amt <= 0:
                        _emit_error(
                            "\u2717 Error: --trail amount must be positive", router,
                        )
                        return None
                    trail_amount = amt
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
        elif tok in ("--sec-type", "--security-type"):
            i += 1
            if i >= len(args):
                _emit_error(f"\u2717 Error: {tok} requires a value", router)
                return None
            explicit_sec_type = args[i].upper()
        elif tok == "--expiry":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --expiry requires a value", router)
                return None
            explicit_expiry = args[i]
        elif tok == "--trading-class":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --trading-class requires a value", router)
                return None
            explicit_trading_class = args[i]
        elif tok == "--exchange":
            i += 1
            if i >= len(args):
                _emit_error("\u2717 Error: --exchange requires a value", router)
                return None
            explicit_exchange = args[i]
        elif tok.startswith("--"):
            _emit_error(f"\u2717 Error: unknown option {tok!r}", router)
            return None
        else:
            positional.append(tok)
        i += 1

    # Symbol is always a SINGLE token. For futures the user types the
    # IB-paste localSymbol form (``ESZ6``, ``MESM6``, ``GCM6``); we
    # don't split it. The engine qualifies futures by localSymbol so
    # the symbol stays identical across IB, our UI, and the CLI.
    security_type = "STK"
    expiry_yyyymm: str | None = None

    if len(positional) < 3:
        _emit_error(
            f"\u2717 Error: usage: {verb} SYMBOL QTY STRATEGY [PROFIT]",
            router,
        )
        return None

    symbol = positional[0].upper()
    if _is_futures_local_symbol(symbol):
        security_type = "FUT"

    # Explicit --sec-type etc. override the auto-detection above. This
    # is the path the internal HTTP API uses when composing a command
    # from an OrderRequest that already carries explicit fields.
    if explicit_sec_type is not None:
        security_type = explicit_sec_type
    if explicit_expiry is not None:
        expiry_yyyymm = explicit_expiry

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

    exchange = explicit_exchange or ("CME" if security_type == "FUT" else None)
    trading_class = explicit_trading_class

    # Gate --trail to FUT only. IB-simulated trailing stops on STK run
    # RTH-only and stall overnight, which makes them a foot-gun on a
    # 24h-trading mindset. Reject loudly so the user doesn't think the
    # protection is in place when it isn't.
    if (trail_percent is not None or trail_amount is not None) and security_type != "FUT":
        _emit_error(
            "✗ Error: --trail is FUT-only (IB-simulated STK trailing stops "
            "only run during RTH and aren't reliable overnight)",
            router,
        )
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
            security_type=security_type,
            expiry=expiry_yyyymm,
            trading_class=trading_class,
            exchange=exchange,
            trail_percent=trail_percent,
            trail_amount=trail_amount,
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
            security_type=security_type,
            expiry=expiry_yyyymm,
            trading_class=trading_class,
            exchange=exchange,
            trail_percent=trail_percent,
            trail_amount=trail_amount,
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

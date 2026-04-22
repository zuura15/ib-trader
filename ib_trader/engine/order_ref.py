"""IB orderRef encoding and decoding.

Every order placed through our system is tagged with a self-contained,
human-readable orderRef string that survives the entire IB order lifecycle.

Format: IBT:{bot_ref}:{symbol}:{side}:{serial}
Example: IBT:saw-rsi:QQQ:B:42

The reconciler parses these to identify which bot owns which IB order.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "IBT"
_SEPARATOR = ":"
_MAX_LENGTH = 128  # IB orderRef limit


@dataclass(frozen=True)
class OrderRefInfo:
    """Parsed orderRef fields."""

    bot_ref: str
    symbol: str
    side: str  # "B" (buy/entry) or "S" (sell/exit)
    serial: int

    @property
    def is_manual(self) -> bool:
        """True if this order was placed manually (not by a bot)."""
        return self.bot_ref == "manual"


def encode(bot_ref: str, symbol: str, side: str, serial: int) -> str:
    """Encode an orderRef string for tagging an IB order.

    Args:
        bot_ref: Bot reference ID from strategy config (e.g., "saw-rsi").
                 Use "manual" for orders placed via REPL/API.
        symbol: Order symbol (e.g., "QQQ").
        side: "B" for buy/entry, "S" for sell/exit.
        serial: Trade serial number (0-999).

    Returns:
        Encoded orderRef string (e.g., "IBT:saw-rsi:QQQ:B:42").

    Raises:
        ValueError: If the encoded string exceeds IB's 128-char limit or
                    if any field contains the separator character.
    """
    if _SEPARATOR in bot_ref:
        raise ValueError(f"bot_ref must not contain '{_SEPARATOR}': {bot_ref!r}")
    if _SEPARATOR in symbol:
        raise ValueError(f"symbol must not contain '{_SEPARATOR}': {symbol!r}")
    if side not in ("B", "S"):
        raise ValueError(f"side must be 'B' or 'S', got: {side!r}")

    ref = _SEPARATOR.join([_PREFIX, bot_ref, symbol, side, str(serial)])

    if len(ref) > _MAX_LENGTH:
        raise ValueError(
            f"orderRef exceeds {_MAX_LENGTH} chars ({len(ref)}): {ref!r}"
        )
    return ref


def decode(ref: str) -> Optional[OrderRefInfo]:
    """Decode an orderRef string back to its fields.

    Args:
        ref: Raw orderRef string from IB (e.g., "IBT:saw-rsi:QQQ:B:42").

    Returns:
        OrderRefInfo if this is one of our orderRefs, None if it's not
        (e.g., an order placed directly through TWS with no tag).
    """
    if not ref or not ref.startswith(_PREFIX + _SEPARATOR):
        return None

    parts = ref.split(_SEPARATOR)
    if len(parts) != 5:
        logger.warning(
            '{"event": "ORDERREF_PARSE_FAILED", "ref": "%s", "reason": "expected 5 parts, got %d"}',
            ref, len(parts),
        )
        return None

    _, bot_ref, symbol, side, serial_str = parts

    if side not in ("B", "S"):
        logger.warning(
            '{"event": "ORDERREF_PARSE_FAILED", "ref": "%s", "reason": "invalid side"}',
            ref,
        )
        return None

    try:
        serial = int(serial_str)
    except ValueError:
        logger.warning(
            '{"event": "ORDERREF_PARSE_FAILED", "ref": "%s", "reason": "non-integer serial"}',
            ref,
        )
        return None

    return OrderRefInfo(bot_ref=bot_ref, symbol=symbol, side=side, serial=serial)

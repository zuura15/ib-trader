"""Monkey-patch ib_async 2.1.0 to support includeOvernight (server version 189).

ib_async 2.1.0 advertises MaxClientVersion=178.  IB Gateway 10.26+ supports
the ``includeOvernight`` order flag (server version 189), which lets SMART-routed
orders participate in the overnight session without explicit OVERNIGHT exchange
routing — avoiding the precautionary setting rejections that block direct
OVERNIGHT exchange routing on many Gateway configurations.

This module patches three things:

1. ``Client.MaxClientVersion`` → 189 so the server negotiates up.
2. ``Client.placeOrder`` — appends encoder fields for versions 183–189.
3. ``Decoder.contractDetails`` — consumes the ``lastTradeDate`` field added
   mid-stream at server version 182.  Without this patch, every field after
   ``lastTradeDateOrContractMonth`` is shifted by one, causing ``conId`` to
   resolve as None and cascading parse failures.

   Other decoder methods (openOrder, completedOrder) append new fields after
   the v170 block.  ib_async's ``*fields`` destructuring safely captures them
   — no patch needed.

Call ``apply()`` once before connecting to IB.
"""
import logging

from ib_async.client import Client
from ib_async.decoder import Decoder

logger = logging.getLogger(__name__)

_PATCHED = False


def apply() -> None:
    """Apply the includeOvernight monkey-patch to ib_async.  Idempotent."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # --- 1. Bump negotiated version ---
    Client.MaxClientVersion = 189
    logger.info('{"event": "IB_ASYNC_PATCHED", "MaxClientVersion": 189}')

    # --- 2. Patch placeOrder encoder ---
    _orig_placeOrder = Client.placeOrder

    def _patched_placeOrder(self, orderId, contract, order):
        """Wrap placeOrder to append v183–v189 fields before send().

        The original placeOrder builds a fields list and calls self.send().
        We temporarily replace self.send with a wrapper that appends the
        extra fields required by server versions 183–189 before forwarding.
        """
        _orig_send = self.send
        version = self.serverVersion()

        def _send_with_extras(*fields):
            fields = list(fields)
            if version >= 183:
                fields.append(getattr(order, 'customerAccount', ''))
            if version >= 184:
                fields.append(getattr(order, 'professionalCustomer', False))
            if 187 <= version < 190:
                # Transient RFQ fields — present in 187–189, removed in 190.
                fields.append('')
                fields.append(2147483647)
            if version >= 189:
                fields.append(getattr(order, 'includeOvernight', False))
            _orig_send(*fields)

        self.send = _send_with_extras
        try:
            _orig_placeOrder(self, orderId, contract, order)
        finally:
            self.send = _orig_send

    Client.placeOrder = _patched_placeOrder

    # --- 3. Patch contractDetails decoder ---
    #
    # Server version 182 (MIN_SERVER_VER_LAST_TRADE_DATE) inserts a new
    # ``lastTradeDate`` string field between ``lastTradeDateOrContractMonth``
    # and ``strike`` in the contractDetails message.  The original decoder
    # unpacks these positionally in a tuple, so the extra field shifts every
    # subsequent value by one — ``strike`` gets the date string, ``conId``
    # ends up as None, etc.
    #
    # Fix: replace the initial tuple unpack with a version-aware pop sequence.
    _orig_contractDetails = Decoder.contractDetails

    def _patched_contractDetails(self, fields):
        """Decode contractDetails with v182 lastTradeDate support."""
        try:
            _do_patched_contractDetails(self, list(fields))
        except Exception:
            # If our patched decoder fails, the original is guaranteed to
            # mis-parse at v182+, but at least let it try so the future
            # resolves (with possibly wrong data) rather than hanging forever.
            logger.exception("Patched contractDetails decoder failed, falling back")
            _orig_contractDetails(self, fields)

    def _do_patched_contractDetails(self, fields):
        """Inner implementation — separated so errors propagate cleanly."""
        from ib_async.contract import ContractDetails, Contract, TagValue

        cd = ContractDetails()
        cd.contract = c = Contract()
        if self.serverVersion < 164:
            fields.pop(0)

        # Pop the fields that come before the v182 insertion point.
        _ = fields.pop(0)           # message version marker
        reqId = fields.pop(0)
        c.symbol = fields.pop(0)
        c.secType = fields.pop(0)
        lastTimes = fields.pop(0)   # lastTradeDateOrContractMonth

        # v182: extra lastTradeDate field inserted here.
        if self.serverVersion >= 182:
            c.lastTradeDate = fields.pop(0)

        c.strike = fields.pop(0)
        c.right = fields.pop(0)
        c.exchange = fields.pop(0)
        c.currency = fields.pop(0)
        c.localSymbol = fields.pop(0)
        cd.marketName = fields.pop(0)
        c.tradingClass = fields.pop(0)
        c.conId = fields.pop(0)
        cd.minTick = fields.pop(0)

        if self.serverVersion < 164:
            fields.pop(0)  # obsolete mdSizeMultiplier

        (
            c.multiplier,
            cd.orderTypes,
            cd.validExchanges,
            cd.priceMagnifier,
            cd.underConId,
            cd.longName,
            c.primaryExchange,
            cd.contractMonth,
            cd.industry,
            cd.category,
            cd.subcategory,
            cd.timeZoneId,
            cd.tradingHours,
            cd.liquidHours,
            cd.evRule,
            cd.evMultiplier,
            numSecIds,
            *fields,
        ) = fields

        numSecIds = int(numSecIds)
        if numSecIds > 0:
            cd.secIdList = []
            for _ in range(numSecIds):
                tag, value, *fields = fields
                cd.secIdList += [TagValue(tag, value)]

        (
            cd.aggGroup,
            cd.underSymbol,
            cd.underSecType,
            cd.marketRuleIds,
            cd.realExpirationDate,
            cd.stockType,
            *fields,
        ) = fields

        if self.serverVersion == 163:
            cd.suggestedSizeIncrement, *fields = fields

        if self.serverVersion >= 164:
            (
                cd.minSize,
                cd.sizeIncrement,
                cd.suggestedSizeIncrement,
                *fields,
            ) = fields

        # v179: FUND_DATA_FIELDS — conditional on secType == "FUND".
        # We don't trade funds, but consume the fields to stay in sync.
        if self.serverVersion >= 179 and c.secType == "FUND":
            for _ in range(17):
                if fields:
                    fields.pop(0)

        # v186: INELIGIBILITY_REASONS — variable length at the end.
        if self.serverVersion >= 186 and fields:
            try:
                count = int(fields.pop(0))
                for _ in range(count):
                    if len(fields) >= 2:
                        fields.pop(0)  # id
                        fields.pop(0)  # description
            except (ValueError, IndexError):
                pass

        # Parse lastTimes the same way as the original decoder.
        times = lastTimes.split("-" if "-" in lastTimes else None)
        if len(times) > 0:
            c.lastTradeDateOrContractMonth = times[0]
        if len(times) > 1:
            cd.lastTradeTime = times[1]
        if len(times) > 2:
            cd.timeZoneId = times[2]

        cd.longName = cd.longName.encode().decode("unicode-escape")
        self.parse(cd)
        self.parse(c)
        reqId = int(reqId)
        self.wrapper.contractDetails(reqId, cd)

    Decoder.contractDetails = _patched_contractDetails

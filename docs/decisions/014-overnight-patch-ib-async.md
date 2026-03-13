# ADR-014: Monkey-Patching ib_async for Overnight Trading (Server Version 189)

**Date:** 2026-03-13
**Status:** Accepted

## Context

IB Gateway 10.26+ supports the `includeOvernight` order flag (server version 189),
which lets SMART-routed orders participate in the overnight session (Blue Ocean ATS /
IBEOS, 8 PM – 3:50 AM ET) without explicit OVERNIGHT exchange routing.

Direct OVERNIGHT exchange routing (`exchange="OVERNIGHT"`) is blocked by Gateway
precautionary settings on many configurations (error 10329 + 201). The TWS socket API
does not support `tif=OND` (error 10052) — that TIF is Client Portal / Web API only.

`ib_async` 2.1.0 advertises `MaxClientVersion=178` and has no knowledge of fields
added in server versions 179–189.

## Decision

Monkey-patch ib_async 2.1.0 at startup via `ib/overnight_patch.py` to support
`includeOvernight`. The patch is applied once before any IB connection is established
and is idempotent.

### Three patches applied:

1. **`Client.MaxClientVersion` → 189** so the server negotiates up to version 189.

2. **`Client.placeOrder` encoder** — appends fields required by server versions 183–189:
   - v183: `customerAccount` (empty string)
   - v184: `professionalCustomer` (False)
   - v187–189: RFQ fields (empty string + max int, transient — removed in v190)
   - v189: `includeOvernight` (True/False)

3. **`Decoder.contractDetails` decoder** — server version 182 inserts a new
   `lastTradeDate` field **mid-stream** between `lastTradeDateOrContractMonth` and
   `strike`. The original decoder unpacks positionally in a tuple, so the extra field
   shifts every subsequent value (strike gets a date string, conId becomes None, etc.).
   The patch replaces the initial tuple unpack with a field-by-field pop sequence that
   consumes `lastTradeDate` when `serverVersion >= 182`.

   A fallback wrapper catches exceptions in the patched decoder and falls back to the
   original decoder so that `reqContractDetailsAsync` futures resolve (with possibly
   wrong data) rather than hanging forever.

### Overnight order placement rules:

- `order.includeOvernight = True` with `exchange=SMART`, `tif=DAY`, `outsideRth=True`
- Applied in `insync_client.py` for limit orders, market orders, and amendments
- Only activated when `is_overnight_session()` returns True (8 PM – 3:50 AM ET)
- During RTH / pre-market / after-hours, orders use `tif=GTC` with no `includeOvernight`

### Overnight venue limitations:

- **Market orders are not supported** on Blue Ocean ATS. The engine auto-converts
  market orders to aggressive limit orders (at the ask for BUY, at the bid for SELL)
  during the overnight session.
- **reqMktData snapshot mode returns zeros** for some symbols during overnight when
  the data farm has no warm quote. Streaming mode (`snapshot=False`) with cancel is
  used instead. A historical midpoint fallback with synthetic spread provides a safety
  net when streaming also returns no data.

## Consequences

- Bumping `MaxClientVersion` to 189 means the server sends v179–189 fields in **all**
  sessions, not just overnight. If any decoder method other than `contractDetails`
  receives unexpected fields without `*fields` destructuring, it could mis-parse.
  In practice, ib_async's other decoders (openOrder, completedOrder) use `*fields`
  at the end of their unpack, which safely captures extra trailing fields.
- The `contractDetails` decoder patch is the most fragile component. It manually
  replicates the original field layout. If ib_async updates its decoder layout in a
  future version, the patch must be updated to match.
- All overnight logic is session-aware and has no effect during daytime trading.

## Removal Criteria

Remove `overnight_patch.py` when ib_async ships a version that natively supports
server version 189+ and the `includeOvernight` order attribute. Update this ADR
to "Superseded" at that time.

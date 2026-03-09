# ADR-006: Options and Futures Readiness

**Date:** 2026-03-08
**Status:** Accepted

## Decision

The data model and engine are designed to accommodate options and futures without assuming that only stocks and ETFs exist. The `SecurityType` enum includes `OPT` and `FUT`. The `Order` model includes `expiry`, `strike`, and `right` fields. No trading logic for these types is implemented in v1.

## Reasoning

Adding options or futures support to a system that assumed equities-only would require schema migrations, model changes, pricing function rewrites, and contract qualification changes. By including the fields and enum values from the start — even as nullable/unused — the path to supporting them is adding logic, not restructuring the entire data model.

## Consequences

- `Order.security_type` is an enum with `STK`, `ETF`, `OPT`, `FUT` values. Only `STK` and `ETF` have trading logic in v1.
- `Order.expiry`, `Order.strike`, `Order.right` are nullable columns present in the schema from migration 0001.
- `symbols.yaml` whitelist currently contains only equity symbols but is structurally extendable.
- Pricing functions in `engine/pricing.py` are pure functions with no security-type assumptions baked in.
- Any code that would break if options existed is a bug. No such code should be written.

## Future Considerations

Options support requires: option symbology in the whitelist, strike/expiry parsing in command grammar, contract qualification changes in `insync_client.py`, and pricing logic for premium-based profit targets. All of these are additive changes — no existing code needs restructuring.

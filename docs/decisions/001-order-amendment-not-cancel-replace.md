# ADR-001: Order Amendment Instead of Cancel-Replace

**Date:** 2026-03-08
**Status:** Accepted

## Decision

When repricing an open order, amend the existing IB order in place using `ib_insync`'s `modifyOrder()` rather than canceling the order and placing a new one.

## Reasoning

Cancel-replace generates two IB order IDs per reprice step, cluttering the IB order book and mobile app view. A position being actively repriced over 10 steps would produce 10 distinct order entries visible to the user in TWS. Amendment keeps a single IB order ID per entry leg regardless of how many reprice steps occur. This keeps the IB order book clean and makes manual inspection straightforward.

## Consequences

- Each order has exactly one `ib_order_id` written to SQLite on initial placement, never updated.
- The reprice loop logs each amendment as a `RepriceEvent` row with step number, prices, and confirmation flag.
- If IB rejects an amendment for a specific order type, fall back to cancel-replace and log a warning — this handles edge cases without breaking the general rule.
- The `Order` model has a single `ib_order_id` field, not a list.

## Future Considerations

If IB changes its amendment API or if options/futures legs require cancel-replace semantics, the fallback path is already in place. The `IBClientBase.amend_order()` abstraction allows swapping behavior per security type without touching engine code.

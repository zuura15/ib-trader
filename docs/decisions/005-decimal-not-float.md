# ADR-005: Decimal for All Monetary Values

**Date:** 2026-03-08
**Status:** Accepted

## Decision

All monetary values — prices, P&L, commissions, profit targets — are represented as Python `Decimal` objects throughout the codebase. `float` is never used for monetary values.

## Reasoning

IEEE 754 floating-point arithmetic produces rounding errors that compound unpredictably in financial calculations. `0.1 + 0.2 != 0.3` in Python float arithmetic. A system calculating profit targets, slippage in basis points, and commission totals across hundreds of orders would accumulate errors that are invisible during testing but visible in production reconciliation. `Decimal` arithmetic is exact for decimal fractions, which is what financial data is.

## Consequences

- All SQLAlchemy columns holding monetary values use `Numeric(18, 8)` — mapped to `Decimal` by SQLAlchemy.
- IB API returns floats. All IB float values are converted via `Decimal(str(value))` — never `Decimal(float_value)` directly. The `str()` conversion preserves the decimal representation without introducing float imprecision.
- All pricing functions in `engine/pricing.py` accept and return `Decimal`.
- `CLAUDE.md` enforces this rule on all future contributors.

## Future Considerations

If the system integrates with external APIs that return string prices (e.g., FIX protocol), `Decimal(str_value)` is already the correct pattern. No change needed. If performance profiling shows `Decimal` arithmetic is a bottleneck on hot paths, integer cents can be used internally with a thin conversion layer at API boundaries.

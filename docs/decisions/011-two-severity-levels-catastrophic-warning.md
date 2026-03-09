# ADR-011: Two Alert Severity Levels — CATASTROPHIC and WARNING

**Date:** 2026-03-08
**Status:** Accepted

## Decision

The alert system has exactly two severity levels: `CATASTROPHIC` and `WARNING`. `CATASTROPHIC` halts all daemon background activity and requires human confirmation to resume. `WARNING` logs and displays in amber but never halts anything.

## Reasoning

More granularity (CRITICAL, ERROR, WARNING, INFO) creates ambiguity about what requires human action. In a trading system, the question is binary: does this require a human to look at it right now and do something, or can it wait? `CATASTROPHIC` is "yes, stop and fix this before trading continues." `WARNING` is "note this and continue." The `AlertSeverity` enum is designed with ordinal spacing so additional levels can be inserted between `CATASTROPHIC` and `WARNING` without restructuring — but they should not be added without a clear, agreed definition of what behavior they trigger.

## Consequences

- `AlertSeverity` enum: `CATASTROPHIC = "CATASTROPHIC"`, `WARNING = "WARNING"`.
- `CATASTROPHIC` triggers: REPL heartbeat stale, SQLite integrity check failure, 3 consecutive IB connectivity failures.
- `WARNING` triggers: single reconciliation failure, single IB connectivity failure, ABANDONED order detected.
- The TUI changes to full red on CATASTROPHIC; amber indicator on WARNING.
- No automatic escalation from WARNING to CATASTROPHIC — only defined triggers can be CATASTROPHIC.

## Future Considerations

If `CRITICAL` (between CATASTROPHIC and WARNING) is needed for "automated action required but not halting", it can be inserted in the enum without changing the CATASTROPHIC or WARNING logic. The enum string values are stored in SQLite, so a migration adding `CRITICAL` rows requires no data transformation of existing rows.

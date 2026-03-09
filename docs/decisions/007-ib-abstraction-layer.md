# ADR-007: IB Abstraction Layer

**Date:** 2026-03-08
**Status:** Accepted

## Decision

All IB API calls go through an abstract interface (`IBClientBase`) defined in `ib/base.py`. The concrete `ib_insync` implementation lives in `ib/insync_client.py`. Engine code imports and depends on `IBClientBase` only — never on `ib_insync` directly.

## Reasoning

`ib_insync` is a third-party library that wraps IB's proprietary API. Depending on it directly throughout the engine would make unit testing impossible without a live IB connection, make future broker changes require rewriting engine code, and prevent testing error conditions that are difficult to reproduce with a live connection. The abstraction layer solves all three: unit tests use a `MockIBClient`, broker changes require only a new implementation of `IBClientBase`, and the mock can simulate any error condition.

## Consequences

- `ib/base.py` defines `IBClientBase` as an abstract base class with `@abstractmethod` decorators.
- `ib/insync_client.py` implements `IBClientBase` and is the only file that imports `ib_insync`.
- `tests/` contains `MockIBClient` implementing `IBClientBase` — all unit and integration tests use it.
- `AppContext.ib` is typed as `IBClientBase` — the concrete type is injected at startup.
- `outsideRth = True` is enforced inside `insync_client.py` — engine code never sets IB order fields directly.

## Future Considerations

If IB releases a REST API or if the system needs to support multiple brokers, a new implementation of `IBClientBase` can be written without touching engine code. The `AppContext` wiring in `repl/main.py` and `daemon/main.py` is the only place that changes.

# ADR 015: Auto-detect paper vs live mode from the Gateway

Date: 2026-04-20
Status: Accepted

## Context

Until now, operators had to declare the target broker surface at startup
via `--paper` (default) or `--live` on both `ib-engine` and `ib-trader`.
The flag selected:

- the Gateway port (4002 paper / 4001 live),
- the account_id env var (`IB_ACCOUNT_ID_PAPER` vs `IB_ACCOUNT_ID`),
- the market data type (3 delayed / 1 realtime),

and a post-connect check (`_validate_account_mode`) refused to start if the
account prefix disagreed with the flag. In practice the operator runs one
Gateway at a time and switches between paper and live Gateways week to
week; the flag was bookkeeping that duplicated information IB already has.

## Decision

Remove the `--paper/--live` flag as the mode selector. Instead, on startup
probe a short list of candidate `(host, port)` endpoints. For the first
one that accepts a connection, read `managedAccounts` and classify the
session:

- Any returned account starting with `DU` → mode is `paper`.
- Otherwise → mode is `live`.

Once classified, pick the matching `account_id` and `market_data_type`
from `.env` / settings and proceed with the normal connect flow.

Default probe order is **live first, paper fallback** (`[4001, 4002]`) so
the engine only lands on paper when the live Gateway is not up. The order
is overridable via `config/settings.yaml` `ib_port_candidates`.

A new `--force-mode {paper,live}` option is kept for scripted environments
that want to assert a specific target and fail fast otherwise.

## Consequences

- **Fewer startup footguns.** The operator cannot forget to update the
  flag after switching Gateway accounts — IB is the source of truth for
  which mode is live, and the app reads it.
- **One extra round-trip at startup.** We open a bare `ib_async` session
  per probe, read `managedAccounts`, then close it. Typical added latency
  is well under a second in the hit case.
- **`_validate_account_mode` is no longer needed.** The check existed only
  to catch flag/account drift, which is impossible under detect-mode. It
  has been removed; `_validate_account_id` (account must be one the
  Gateway manages) remains.
- **The REPL's live-account confirmation modal is unchanged.** That is a
  human safety gate ("real money, are you sure?") and is orthogonal to
  mode detection.
- If both Gateways are up simultaneously on a single host, the probe-order
  setting determines the winner; today the default is live-first, which
  is the safer read of ambiguity (you notice and abort, rather than
  silently landing on paper).

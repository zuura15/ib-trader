# ADR-018: Verify Suspect Cancelled Statuses Against IB's Open-Orders List

**Date:** 2026-04-23
**Status:** Accepted

## Context

`ib_async/wrapper.py:1657-1668` (vendored at
`.venv/lib/python3.12/site-packages/ib_async/wrapper.py`) synthesizes
`OrderStatus.Cancelled` and emits `orderStatusEvent` for *any* non-warning
order error received on a live trade. Its own comment names three cases the
error path can represent — new-order rejection, server-cancelled DAY order,
and "modification to existing order just has an update error, but the order
is STILL LIVE" — and then ignores the third by setting status to Cancelled
and emitting regardless.

The same defect is filed against the upstream library as
[`ib_insync` issue #502](https://github.com/erdewit/ib_insync/issues/502),
unfixed since 2022; `ib_async` inherited it.

We hit case 3 in production on 2026-04-23 (GH #48): a `BUY 1000 PSQ @ mid`
amend during the overnight session was rejected by IB with error 462
("Cannot change to the new Time in Force.DAY"); ib_async manufactured a
`Cancelled` status; our `_on_order_status` handler treated it as terminal,
unregistered callbacks, marked the ledger CANCEL_HELD, and emitted
`ORDER_EXPIRED`. The actual order kept resting at \$28.37 and filled at
IBEOS 100 seconds later. The fill was dispatched into nothing.

The engine had already covered one variant of the same upstream quirk —
pre-routing cancels with `perm_id == 0` (`insync_client.py:1207-1221`).
The amend-rejection variant has a real `perm_id`, so it slipped through.

## Decision

When `_on_order_status` receives a `Cancelled` status whose just-appended
`trade.log[-1]` carries an `errorCode` in `_VERIFY_CANCEL_ERROR_CODES`
(currently `{462}`), defer dispatch and verify the order's liveness by
asking IB directly:

1. Spawn an async verifier task. The status callback returns immediately
   without dispatching to engine handlers.
2. The verifier calls `reqOpenOrdersAsync()` once.
3. If our `ib_order_id` appears in IB's open-orders list → suppress the
   Cancelled. Callbacks remain registered. Whatever IB pushes next (real
   `Filled`, real `Cancelled`, or another synthetic with the same errorCode)
   re-enters the same dispatch loop and is verified again on its own merits.
4. If our `ib_order_id` does not appear → dispatch the Cancelled as today.
5. **On `reqOpenOrdersAsync` failure: default to suppress.**
6. **Race guard:** before propagating after a "not open" verdict, re-read
   `trade.orderStatus.status`; if `Filled` landed during the IB round-trip,
   skip the stale Cancelled.

Other Cancelled events — no `errorCode` (real cancel via `_orderStatus()`,
not `_error()`), `errorCode != 462`, or empty `trade.log` — dispatch
synchronously as before. No latency tax on real cancels.

### Failure-mode rationale

| Wrong choice | Cost |
|---|---|
| Suppress, but order really cancelled | Engine waits up to 120s active+passive timeout, then expires cleanly via 10147 (in `_BENIGN_ORDER_RACE_CODES`). No orphan position. Recoverable via the planned manual exit-bot reattach feature. |
| Propagate, but order really live (today's bug) | Engine thinks done; order fills silently at IB. Orphan position. Money impact. |

The asymmetry favors suppression as the failure default.

### Why not strip the synthetic Cancelled emission outright

Considered. Removing it from ib_async would cause three regressions:

1. **New-order rejections (case 1) emit only `errorEvent`** — no real
   `orderStatus = Cancelled` push from IB. Engine would block on
   `fill_event` / `cancel_event` for the full ~120s timeout instead of
   failing fast on submission errors.
2. `Trade.isDone()` returns False forever for rejected orders →
   ib_async's own `openTrades` / `openOrders` filters
   (`ib_async/ib.py:603,615`) leak phantom active trades.
3. `cancelledEvent` / `cancelOrderEvent` hooks miss case-1 rejections.

Targeted verification on a small whitelist of error codes is the
minimum-blast-radius edit and inherits no risk from upstream changes.

### Why not a pure heuristic on `trade.log`

A first proposal gated suppression on `trade.log[-2].message == "Modify"`
plus `trade.log[-1].errorCode in {110, 462, 463, 464}`. Codex review and a
fetch of IB's `message_codes.html` flagged that `110` and `111` fire for
legitimate new-order rejections too, and that `462–464` are undocumented in
public IB references — so any code-only heuristic is fragile. Asking IB
authoritatively eliminates that fragility.

## Consequences

- Cancel dispatch carries a ~150-300 ms latency penalty when the trigger
  errorCode is in `_VERIFY_CANCEL_ERROR_CODES`. User-initiated cancels and
  ordinary IB-pushed cancels are unaffected.
- One extra `reqOpenOrders` round-trip per qualifying Cancelled. Subject to
  the global 100ms throttle; no detectable rate impact in practice.
- The verifier path is generic: broadening the guard to all Cancelled
  events is a one-line change (remove the errorCode gate). The current
  scoping is a deliberate "start narrow" choice — it covers every false
  cancel we have evidence of in production.

## Hotspot Marker

`ib_async/wrapper.py:1657-1668` is a known-bad upstream chunk. We now stack
two workarounds against it:

1. `IB_PREROUTING_CANCEL_IGNORED` — `insync_client.py:1207-1221`
   (perm_id == 0 case)
2. `IB_CANCEL_VERIFY_DEFERRED` — `insync_client.py:_verify_cancel`
   (this ADR)

If a third workaround lands on the same upstream chunk — or if
`_VERIFY_CANCEL_ERROR_CODES` grows beyond a handful of codes — replace the
piecemeal guards with a clean shadow of the synthetic-cancel emission. The
options at that point:

- Monkey-patch `wrapper.py:_error` similarly to `overnight_patch.py`
  (ADR-014), removing the synthetic Cancelled entirely and routing case 1
  detection through `_order_errors` polling on the engine side.
- Subclass `Wrapper` and override `_error` with the same effect.

## Removal Criteria

Remove this verification path when ib_async (or whatever its successor is)
ships a wrapper that no longer manufactures a Cancelled status from
modify-rejection errors. Update this ADR to "Superseded" at that time.

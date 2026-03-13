# Architectural Addendum #2: IB as Source of Truth

---

> **IMPORTANT — READ BEFORE STARTING**
>
> This codebase was built from two prior prompt files:
> - **`ib_trading_cli_prompt_v7.md`** — the original full build spec (v7)
> - **`add_command_center_tui_v3.md`** — Addendum #1, adding the Textual TUI to the CLI REPL
>
> Both are fully implemented and working. **Do not re-architect, re-implement, or
> second-guess anything that already exists unless explicitly instructed below.**
> Your job is narrowly scoped to the changes listed in this file and nothing else.
>
> If you see something in the existing code you disagree with, add a `# NOTE:` comment and
> move on. Do not fix it unless it directly blocks the work described here.
>
> All engineering standards from `CLAUDE.md` apply in full:
> - Every new class and public method gets a docstring
> - Every new named event type goes in the structured JSON log
> - No exception is ever swallowed silently — always log with full stack trace
> - No hardcoded tunables — everything configurable goes in `settings.yaml`
> - No hardcoded secrets — everything sensitive stays in `.env`
> - All monetary values remain `Decimal`
> - `CHANGELOG.md` must be updated before marking this complete

---

## What This Change Does

The previous design maintained order state in a local SQLite `orders` table and attempted
to keep it in sync with IB. This created a whole class of reconciliation complexity —
conflicts, stale state, orders placed outside the app that the DB didn't know about.

This addendum eliminates that complexity by making **IB the single source of truth for
all live order state.** The local database becomes an immutable audit log of what our
system did, not a mirror of what IB currently holds.

---

## What Must NOT Be Touched

- `engine/` — all order execution, repricing, profit taker logic: **no changes**
- `ib/` — IB abstraction and throttle layer: **no changes**
- `daemon/` — daemon process structure, TUI, alert system: **no changes to daemon behavior**
  unless explicitly listed below
- `repl/commands.py` — command logic: **no changes to execution** — only data source for
  the orders pane changes (see below)
- `tests/` — all existing tests must continue to pass with zero new failures

---

## Core Architectural Change: Orders Pane

### Before
The orders pane queried the local SQLite `orders` table for open orders.

### After
The orders pane shows **all open orders fetched directly from IB** on each poll cycle.
The local `orders` table is no longer the source of truth for the orders pane.

**Marker for system-originated orders:**
- Any open order returned by IB whose order ID exists in our `transactions` table
  (see below) must be visually marked in the orders pane — use a `●` prefix or a
  dedicated `Source` column showing `OUR SYSTEM` vs `EXTERNAL`
- Orders IB returns that have no matching record in `transactions` are shown as external
  — they are displayed but unmarked

**Cancellation still works for all orders**, regardless of source. The user can cancel
any order shown in the pane, whether our system placed it or not.

---

## New Concept: The Transactions Table

Replace the existing `orders` table semantics with a new `transactions` table. This is an
**append-only audit log** — one row per interaction our system has with IB in the context
of an order. Rows are never updated in place. A single order lifecycle may produce
multiple rows.

### Schema

```python
# data/models.py — add TransactionEvent model

class TransactionEvent(Base):
    """
    Append-only audit log of every interaction our system has with IB
    around an order. One row per event. Never updated after insert.
    """
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # IB identifiers
    ib_order_id: Mapped[int | None]          # None if IB never acknowledged
    ib_perm_id: Mapped[int | None]           # IB permanent order ID, if available

    # What our system did
    action: Mapped[str]                      # see TransactionAction enum below
    symbol: Mapped[str]
    side: Mapped[str]                        # BUY / SELL
    order_type: Mapped[str]                  # LIMIT / MARKET
    quantity: Mapped[Decimal]
    limit_price: Mapped[Decimal | None]      # None for market orders
    account_id: Mapped[str]

    # What IB told us
    ib_status: Mapped[str | None]            # IB's status string at time of event
    ib_filled_qty: Mapped[Decimal | None]    # Filled quantity at time of event
    ib_avg_fill_price: Mapped[Decimal | None]
    ib_error_code: Mapped[int | None]        # IB error code if rejected
    ib_error_message: Mapped[str | None]

    # Linking back to the trade (if applicable)
    trade_serial: Mapped[int | None]         # FK to trades table, if this is a fill event

    # Timestamps
    requested_at: Mapped[datetime]           # When our system sent the request
    ib_responded_at: Mapped[datetime | None] # When IB responded

    # Flags
    is_terminal: Mapped[bool] = mapped_column(default=False)
    # True when action is FILLED, CANCELLED, REJECTED, or ERROR_TERMINAL
```

### TransactionAction Enum

```python
# data/models.py

class TransactionAction(str, Enum):
    PLACE_ATTEMPT    = "PLACE_ATTEMPT"     # We sent the order to IB
    PLACE_ACCEPTED   = "PLACE_ACCEPTED"    # IB acknowledged with an order ID
    PLACE_REJECTED   = "PLACE_REJECTED"    # IB rejected immediately
    PARTIAL_FILL     = "PARTIAL_FILL"      # Fill event, not yet complete
    FILLED           = "FILLED"            # Order fully filled
    CANCEL_ATTEMPT   = "CANCEL_ATTEMPT"    # We requested cancellation
    CANCELLED        = "CANCELLED"         # IB confirmed cancellation
    ERROR_TERMINAL   = "ERROR_TERMINAL"    # Non-rejection error that ends the order
    RECONCILED       = "RECONCILED"        # Written by reconciliation job (see below)
```

### Write points

Every time our system interacts with IB around an order, write a row:

1. **Before sending to IB:** write `PLACE_ATTEMPT` with order details. `ib_order_id` is
   null at this point.
2. **IB accepts:** write `PLACE_ACCEPTED` with the returned `ib_order_id`.
3. **IB rejects:** write `PLACE_REJECTED` with error code and message. `is_terminal=True`.
4. **Partial fill callback fires:** write `PARTIAL_FILL` with filled qty and avg price.
5. **Full fill callback fires:** write `FILLED`. `is_terminal=True`.
6. **Cancel requested:** write `CANCEL_ATTEMPT`.
7. **Cancel confirmed:** write `CANCELLED`. `is_terminal=True`.
8. **IB error during live order:** write `ERROR_TERMINAL` if the error ends the order.
9. **Reconciliation finds a discrepancy:** write `RECONCILED` row with current IB state.

**Denormalization is intentional.** Order details are duplicated across rows for the same
order. This is acceptable and desirable — once an order is terminal, nothing changes, and
having the full context on each row makes audit queries simple.

---

## Polling Architecture

### Who polls

The **main application (REPL / TUI)** owns the 60-second IB poll. This is a display
concern. The daemon does not drive display refresh.

### What gets polled

Each poll cycle:
1. Fetch all open orders from IB via `ib_insync` (`ib.openOrders()` or equivalent)
2. Fetch account summary / day P&L from IB
3. Update the orders pane and header in the TUI
4. Record elapsed time since last successful poll

### Elapsed time display

The header pane must show how long ago the last successful IB poll completed:

```
IB Trader │ U1234567 │ ... │ ● CONNECTED │ Last refresh: 23s ago
```

- Format: `Xs ago` for < 60 seconds, `Xm Xs ago` for >= 60 seconds
- If a poll fails, the elapsed timer continues running and the stale data remains visible
  with a `⚠ stale` indicator — do not clear the orders pane on poll failure
- On successful poll, reset the elapsed timer

### Poll interval

Add to `config/settings.yaml`:
```yaml
poll_interval_seconds: 60
```

The poll loop reads this value on startup. Do not hardcode 60 anywhere.

---

## Reconciliation (Daemon)

The daemon gains one new background job: **hourly open-order reconciliation.**

### Purpose

Every hour, compare the set of non-terminal orders in our `transactions` table against
the open orders currently reported by IB. Surface discrepancies — do not auto-heal them.

### Logic

```
our_open = all ib_order_ids in transactions where is_terminal = False
           grouped by ib_order_id — take the most recent row per order
ib_open   = ib.openOrders() — set of IB order IDs currently open

missing_from_ib = our_open - ib_open
```

For each order in `missing_from_ib`:
1. Write a `RECONCILED` row to `transactions` noting the discrepancy
2. Emit a `WARNING` alert: `Order {ib_order_id} ({symbol}) is open in our records but
   not found in IB — manual reconciliation required`
3. Do NOT mark the order as terminal automatically
4. Do NOT attempt to cancel or re-place

The user sees the warning in the daemon TUI and decides what to do.

### Interval

Add to `config/settings.yaml`:
```yaml
reconciliation_interval_seconds: 3600
```

### Startup reconciliation

On daemon startup (not REPL startup), run the reconciliation job once immediately before
starting the hourly loop. This catches any orders that went terminal while the daemon
was offline.

---

## Startup Warning: Live Account Detection

On REPL startup, after the IB connection is established, check the connected account ID.

If the account ID does **not** start with `DU` (IB's paper trading account prefix):

```
⚠  WARNING: Connected to LIVE account {account_id}. Real money is at risk.
   Paper trading accounts begin with 'DU'. Press Enter to continue, or Ctrl-C to abort.
```

- Block the REPL startup sequence until the user presses Enter
- Log a `LIVE_ACCOUNT_CONNECTED` event to SQLite with account ID and timestamp
- If the user presses Ctrl-C, exit cleanly with code 0
- This check runs every time — there is no "I know, stop asking" flag

---

## Schema Migration

Add an Alembic migration for:
1. Creating the new `transactions` table
2. The existing `orders` table is **not dropped** — leave it in place, it may contain
   historical data. The migration only adds `transactions`.

Name the migration: `add_transactions_table`

---

## What Must NOT Change

- The `trades` table and its schema — positions and P&L tracking are unchanged
- The `orders` table — left in place, not written to by new code
- All engine logic for order placement, repricing, profit taker
- The `OutputRouter` and pane routing rules from Addendum #1

---

## Files to Create

- `data/repositories/transaction_repository.py` — repository for `TransactionEvent`
  - `insert(event: TransactionEvent) -> None`
  - `get_open_orders() -> list[TransactionEvent]` — most recent row per `ib_order_id`
    where `is_terminal = False`
  - `get_by_ib_order_id(ib_order_id: int) -> list[TransactionEvent]` — all rows for
    a given IB order ID, sorted by `requested_at` ascending
- `alembic/versions/XXXX_add_transactions_table.py` — migration

## Files to Modify

### `config/context.py`
Add one field:
```python
transactions: TransactionRepository
```

### `config/settings.yaml`
Add:
```yaml
poll_interval_seconds: 60
reconciliation_interval_seconds: 3600
```

### `repl/tui.py` — orders pane data source
- Replace SQLite `orders` table query with IB open orders poll
- Add elapsed-time display to header
- Mark orders whose `ib_order_id` exists in `transactions` table

### `daemon/reconciler.py`
- Add hourly reconciliation job using the logic described above
- Run once on startup before entering the loop

### `engine/order.py` (or wherever order placement occurs)
- Add `TransactionEvent` writes at each interaction point listed above
- Do not change any order execution logic — only add the write calls

### `CLAUDE.md`
Append verbatim. Do not modify existing content:

```markdown
## IB as Source of Truth (Addendum #2)
- IB is the authoritative source for all live order state. The local `orders` table is
  legacy and must not be written to by new code.
- The `transactions` table is append-only — never UPDATE or DELETE rows.
- The orders pane in the TUI is populated from IB's open orders, not from SQLite.
- One `TransactionEvent` row must be written for every interaction with IB around an order.
- Reconciliation (daemon) surfaces discrepancies as WARNINGs — it never auto-heals.
- Poll interval and reconciliation interval are tunables in settings.yaml.
- Live account detection runs on every REPL startup and cannot be bypassed.
```

---

## Testing Requirements

All existing tests must pass unchanged. Additionally:

### `tests/unit/test_transaction_repository.py`
- `insert()` writes a row and it is retrievable
- `get_open_orders()` returns only the most recent row per `ib_order_id` where
  `is_terminal = False`
- `get_open_orders()` excludes orders where the most recent row has `is_terminal = True`
- `get_by_ib_order_id()` returns all rows in ascending `requested_at` order

### `tests/unit/test_reconciler.py`
- Orders present in `transactions` (non-terminal) but absent from IB open orders
  → `RECONCILED` row written, `WARNING` alert emitted
- Orders present in both → no action, no alert
- Orders present in IB but not in `transactions` → no action (these are external orders,
  not our concern for reconciliation)
- Empty `transactions` table → no errors, no alerts

### `tests/unit/test_live_account_warning.py`
- Account ID starting with `DU` → no warning, startup proceeds
- Account ID not starting with `DU` → warning printed, startup blocks until Enter
- `LIVE_ACCOUNT_CONNECTED` event written to SQLite when live account detected

---

## Verification Checklist

Claude Code must verify every item before marking this complete:

**Transactions table:**
- [ ] `TransactionEvent` model created with all fields above
- [ ] Alembic migration `add_transactions_table` created and applies cleanly
- [ ] `TransactionRepository` created with all three methods
- [ ] `transactions` field added to `AppContext`
- [ ] `PLACE_ATTEMPT` row written before any IB call
- [ ] `PLACE_ACCEPTED` or `PLACE_REJECTED` row written after IB response
- [ ] `PARTIAL_FILL` and `FILLED` rows written on fill callbacks
- [ ] `CANCEL_ATTEMPT` and `CANCELLED` rows written on cancellation
- [ ] No existing `orders` table writes introduced by new code
- [ ] Rows are never updated — only inserted

**Orders pane:**
- [ ] Orders pane data comes from IB, not from SQLite `orders` table
- [ ] System-originated orders (matching `transactions`) are visually marked
- [ ] External orders (no `transactions` match) are shown without marker
- [ ] Cancellation works for all orders regardless of source

**Polling:**
- [ ] Poll interval reads from `settings.yaml` — no hardcoded value
- [ ] Header shows elapsed time since last successful poll
- [ ] Stale indicator shown when last poll was not successful
- [ ] Orders pane not cleared on poll failure — shows stale data with indicator

**Reconciliation:**
- [ ] Hourly reconciliation job runs in daemon
- [ ] Job runs once on daemon startup before entering loop
- [ ] Discrepancies produce `RECONCILED` transaction rows and `WARNING` alerts
- [ ] No auto-healing — discrepancies are flagged only
- [ ] Reconciliation interval reads from `settings.yaml`

**Live account warning:**
- [ ] Warning fires for any account ID not starting with `DU`
- [ ] Startup blocks until user presses Enter
- [ ] `LIVE_ACCOUNT_CONNECTED` event logged to SQLite
- [ ] Ctrl-C exits cleanly with code 0
- [ ] Paper trading accounts (`DU` prefix) proceed without any prompt

**Standards:**
- [ ] All new classes and public methods have docstrings
- [ ] All new log events use named event types
- [ ] `CLAUDE.md` updated
- [ ] `CHANGELOG.md` updated
- [ ] All existing tests pass (`make test`)
- [ ] All new unit tests pass

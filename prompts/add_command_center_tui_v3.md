# Feature Increment: Command Center TUI for CLI REPL (v3)

---

> **IMPORTANT — READ BEFORE STARTING**
>
> This codebase was already built from a separate prompt file (`ib_trading_cli_prompt_v7.md`).
> The full application is implemented and working. **Do not re-architect, re-implement, or
> second-guess anything that already exists.** Your job is narrowly scoped to the changes
> listed in this file and nothing else.
>
> If you see something in the existing code you disagree with, add a `# NOTE:` comment and
> move on. Do not fix it unless it directly blocks the work described here.
>
> All engineering standards from `CLAUDE.md` apply in full to this change:
> - Every new class and public method gets a docstring
> - Every new named event type goes in the structured JSON log
> - No exception is ever swallowed silently — always log with full stack trace
> - No hardcoded tunables — everything configurable goes in `settings.yaml`
> - No hardcoded secrets — everything sensitive stays in `.env`
> - All monetary values remain `Decimal` — the TUI layer never touches trading logic
> - `CHANGELOG.md` must be updated before marking this complete

---

## What This Change Does

Right now the CLI REPL (`ib-trader`) is a plain scrolling terminal. All output — command
results, reprice steps, errors, warnings, startup messages — appears in the same stream.
This makes it impossible to distinguish what is happening from what went wrong, and makes
monitoring open positions while trading impossible.

This change replaces the plain REPL loop in `repl/main.py` with a full-screen `textual`
TUI composed of five named panes, each with a fixed height and a single exclusive
responsibility. Pane order, height, and visibility are all configurable via `settings.yaml`
without touching code.

Output is strictly routed — nothing may appear in the wrong pane under any circumstance.

---

## What Must NOT Be Touched

- `engine/` — all order execution, repricing, profit taker logic: **no changes**
- `ib/` — IB abstraction and throttle layer: **no changes**
- `data/` — models, repositories, migrations: **no changes**
- `daemon/` — daemon process, daemon TUI, reconciler, monitor: **no changes**
- `repl/commands.py` — all command handlers: **no changes to command logic** — only how
  their output is delivered changes (via `OutputRouter` — see below)
- `tests/` — all existing tests must continue to pass with zero new failures

---

## Future Architecture Notes — Design For, Do Not Build

### 1. Server / client-renderer model

This TUI is the first rendering layer. In a future increment the architecture will migrate
to a **server/client-renderer model**: the REPL engine runs as a persistent server process
and the TUI becomes a thin client that connects to it and renders its output stream. The
server will publish events; the renderer will subscribe and display them.

**This change must not prevent that migration.** Specifically:

- The `OutputRouter` (defined below) must be the **only** way engine and command handlers
  deliver output to the screen. No direct writes to any TUI widget from engine or command
  code. The router is the single wiring point — in a future increment it will be replaced
  with a network publisher, and the TUI widgets will become subscribers.
- Keep TUI rendering logic (`repl/tui.py`) strictly separate from business logic
  (`repl/commands.py`, `engine/`). No business logic enters `tui.py`. No TUI imports enter
  `commands.py` or anything in `engine/`.
- Do not build the server/client model now. Only ensure the `OutputRouter` abstraction
  makes the future migration a swap, not a rewrite.

### 2. Unrealized P&L in the positions pane

The positions pane currently shows open positions without unrealized P&L. This is a
deliberate deferral — displaying live unrealized P&L requires active IB market data
subscriptions per position, which introduces subscription limit management, staleness
handling, and additional IB API chatter that warrants its own focused design.

**TODO — do not implement now:**
Add a `# TODO: unrealized P&L` comment in the positions pane data query. When this is
implemented, the design must address:
- IB simultaneous market data subscription limits
- Graceful degradation when subscription limit is hit (fall back to last-known price
  with a ⚠ stale indicator)
- Subscription lifecycle — cancel on position close, not on session end
- Staleness threshold — flag values older than 30 seconds
- The `RendererProtocol` will need a `update_position_pnl(serial, unrealized_pnl)` method

---

## Pane Configuration

### `config/settings.yaml` — pane layout block

```yaml
tui:
  panes:
    header:
      rank: 1          # 1 = topmost. Panes render top-to-bottom by ascending rank.
      enabled: true
      height: 1        # Fixed height in terminal lines. Header is always 1 line.

    log:
      rank: 2
      enabled: true
      height: 10

    positions:
      rank: 3
      enabled: true
      height: 10

    command:
      rank: 4
      enabled: true
      height: 5        # Line 1: prompt. Lines 2-5: rolling output from current operation.

    orders:
      rank: 5
      enabled: true
      height: 10
```

### Config rules — enforce at startup:

- `rank` must be a unique positive integer across all enabled panes. Duplicate ranks are
  a startup error: log at ERROR, print clear message, exit non-zero.
- `enabled: false` hides the pane entirely. The remaining enabled panes fill the terminal
  in rank order. Their heights are fixed — they do not expand to fill vacated space.
- `height` is in terminal lines. `header` height is always treated as 1 regardless of
  the configured value — do not allow it to be set otherwise.
- A minimum of 2 panes must be enabled (header + at least one content pane). Fewer than
  2 enabled panes is a startup error.
- Pane order is determined solely by `rank` — lower rank renders higher on screen.
- Ranks do not need to be contiguous (1, 2, 5 is valid). They only determine relative order.
- If `tui.panes` is missing from `settings.yaml`, apply the defaults above and log a
  WARNING that defaults are in use.

### Pane layout implementation

Read pane config at startup into a `PaneConfig` dataclass. Sort enabled panes by rank.
Build the Textual layout dynamically from the sorted list — do not hardcode widget order
in Python. Adding, removing, or reordering panes must require only a `settings.yaml`
change.

```python
# repl/pane_config.py

from dataclasses import dataclass
from typing import Literal

PaneName = Literal["header", "log", "positions", "command", "orders"]

@dataclass(frozen=True)
class PaneConfig:
    """Parsed and validated configuration for a single TUI pane."""
    name: PaneName
    rank: int
    enabled: bool
    height: int   # always 1 for header regardless of configured value


def load_pane_configs(settings: dict) -> list[PaneConfig]:
    """
    Parse and validate pane configuration from settings dict.

    Returns enabled panes sorted by ascending rank.
    Raises ConfigurationError on invalid config (duplicate ranks, < 2 enabled panes).
    """
    ...
```

---

## TUI Layout — Default Appearance

With default config, the terminal renders top to bottom as:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 IB Trader │ U1234567 │ clientId:1 │ 127.0.0.1:7497 │ ● CONNECTED │ ● Daemon │ P&L: +$234.50
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 SYSTEM LOG
 [10:32:01] INFO    IB Trader v1.0 — Gateway connected
 [10:32:01] INFO    5 symbols loaded from symbols.yaml
 [10:33:01] ⚠ WARN  Daemon not running — reconciliation offline
 [10:33:44] INFO    Order #7 placed @ $189.10
 [10:34:01] ✓       Order #4 filled 60/100 @ avg $412.33
 [10:35:22] ✗ ERROR Symbol XYZ not in whitelist
 [10:36:00] INFO    Order #7 profit taker placed @ $194.10
 [10:36:14] ✓       Order #7 filled 50/50 @ avg $189.22
 [10:37:00] INFO    Position #7 closed — P&L: +$106.00
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OPEN POSITIONS  (filled entries not yet closed)
 #   Symbol  Side   Qty   Avg Fill   PT Price   Opened
 4   MSFT    LONG   60    $412.33    $462.33    10:32:01
 9   NVDA    SHORT  10    $875.10    $845.10    10:41:03
 ── No unrealized P&L — see TODO in source ──
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 COMMAND
 > buy MSFT 100 mid 500
 [10:32:02] Amended → $412.32 | step 1/10 (0/100 filled)
 [10:32:03] Amended → $412.34 | step 2/10 (0/100 filled)
 [10:32:04] Amended → $412.36 | step 3/10 (0/100 filled)
 [10:32:05] ✓ Filled 60/100 @ avg $412.33
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OPEN ORDERS  (being actively worked)
 #   Symbol  Side   Qty      Type    Price    Status          Since
 4   MSFT    BUY    60/100   MID     $412.30  REPRICE 3/10    10:32:01
 7   AAPL    BUY    0/50     MID     $189.10  OPEN            10:33:44
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Pane Specifications

### Header pane

**Height:** always 1 terminal line. Fixed. Not scrollable.

**Content** (single line, fields separated by ` │ `):

```
IB Trader │ {account_id} │ clientId:{client_id} │ {ib_host}:{ib_port} │ {ib_status} │ {daemon_status} │ P&L: {day_pnl}
```

| Field | Source | Format |
|---|---|---|
| `account_id` | `AppContext.account_id` | e.g. `U1234567` |
| `client_id` | `IB_CLIENT_ID` from `.env` | e.g. `clientId:1` |
| `ib_host:ib_port` | `settings.yaml` `ib_host` / `ib_port` | e.g. `127.0.0.1:7497` |
| `ib_status` | IB connect/disconnect callbacks | `● CONNECTED` (green) / `✗ DISCONNECTED` (red) |
| `daemon_status` | `HeartbeatRepository` — checked each poll cycle | `● Daemon` (green) / `✗ Daemon offline` (amber) |
| `day_pnl` | `TradeRepository` — sum of `realized_pnl` on trade groups opened today, closed by any means | `P&L: +$234.50` (green if positive, red if negative, white if zero) |

**Day P&L rules:**
- "Today" means UTC date matches current UTC date — consistent with how all datetimes are
  stored in SQLite.
- Includes ALL trade groups where `opened_at` is today, regardless of how they were closed
  (`CLOSED_MANUAL`, `CLOSED_EXTERNAL`, profit taker filled, etc.).
- Only includes `realized_pnl` from fully or partially closed legs — no unrealized component.
- Refreshes on the same poll cycle as the orders and positions panes
  (`tui.panes.header` shares the poll interval from `tui_refresh_interval_seconds`).
- Never shows `None` or blank — if no trades today, show `P&L: $0.00`.
- All P&L values are `Decimal` — never `float`. Format with 2 decimal places and sign.

---

### Log pane

**Widget:** `RichLog`

**Behaviour:**
- New entries appended at the bottom
- Auto-scrolls to the latest entry unless the user has manually scrolled up
- Independently scrollable — scrolling here never affects any other pane
- Never cleared — persists the full session history
- Fixed height — does not grow or shrink. When full, oldest lines scroll off the top.

**Line format:**

```
[HH:MM:SS] INFO    message    ← default (white)
[HH:MM:SS] ⚠ WARN  message    ← amber
[HH:MM:SS] ✗ ERROR message    ← red
[HH:MM:SS] ✓       message    ← green
```

Timestamp is local time (display only — JSON log always uses UTC).

This pane is a **display layer only**. Every message shown here also appears in the JSON
log file. The reverse is not true — `DEBUG` messages go to the JSON log only and are
never shown in any pane.

---

### Positions pane

**Widget:** `DataTable`

**Data source:** SQLite query via `TradeRepository` for trade groups where:
- `status = OPEN` (entry filled, position is live)
- At least one leg has `leg_type = ENTRY` and `status IN (FILLED, PARTIAL)`

Never query SQLite directly from the widget — always go through the repository.

**Columns** in this exact order:

| Column | Source | Format |
|---|---|---|
| `#` | `serial_number` on the entry leg | integer |
| `Symbol` | `symbol` | uppercase string |
| `Side` | `direction` on trade group | `LONG` / `SHORT` |
| `Qty` | `qty_filled` on entry leg | integer |
| `Avg Fill` | `avg_fill_price` on entry leg | `$412.33` (2 d.p.) |
| `PT Price` | `price_placed` on the PROFIT_TAKER leg if it exists | `$462.33` or `—` if none |
| `Opened` | `placed_at` on entry leg | `HH:MM:SS` local time |

**Unrealized P&L:** not shown. Render a single dimmed footer line below the table:
```
── No unrealized P&L — see TODO in source ──
```
This is a visual placeholder so the user knows it is a known gap, not an oversight.
Add a `# TODO: unrealized P&L — see Future Architecture Notes` comment in the data
query function.

**Empty state:** when no open positions exist, show a single dimmed row:
```
  No open positions.
```

**Poll refresh:** same `set_interval` timer as the orders pane, interval from
`tui_refresh_interval_seconds`.

**Error handling:** if the SQLite query raises, log the exception at ERROR level with
full stack trace, leave the existing table contents unchanged, and continue polling.
Never crash the TUI on a transient DB read failure.

---

### Command pane

**Height:** 5 terminal lines (configured in `settings.yaml`, default 5).

**Line allocation:**
- Line 1: `> ` prompt with live text cursor (`Input` widget)
- Lines 2–5: rolling output from the current or most recently completed operation

**Rolling output behaviour:**
- Output lines append from the top of the output area downward, up to 4 lines
- When a 5th line would be added, the oldest line scrolls off — only the 4 most
  recent output lines are visible at any time
- Output is **not** cleared between commands — it rolls. The user always sees the last
  4 lines of output regardless of whether they came from the current or previous command
- A new command submission does NOT clear the output area — the prompt clears and the
  new command's output starts appending immediately below existing lines, rolling as needed

**During repricing**, steps stream here live:
```
> buy MSFT 100 mid 500
[10:32:02] Amended → $412.32 | step 1/10 (0/100 filled)
[10:32:03] Amended → $412.34 | step 2/10 (0/100 filled)
[10:32:04] Amended → $412.36 | step 3/10 (0/100 filled)
[10:32:05] ✓ Filled 60/100 @ avg $412.33
```

**The prompt must remain active at all times**, including while a command is executing.
Commands entered during execution are queued — see Command Queue section.

**Command history:** up/down arrow keys cycle through previous commands within the
session. Not persisted to disk or SQLite. In-memory list, max 100 entries — oldest
discarded when full.

---

### Orders pane

**Widget:** `DataTable`

**Data source:** SQLite query via `OrderRepository` for orders where:
`status IN (OPEN, REPRICING, AMENDING, PARTIAL)` and `leg_type = ENTRY`

Never query SQLite directly from the widget — always go through the repository.

**Columns** in this exact order:

| Column | Source | Format |
|---|---|---|
| `#` | `serial_number` | integer |
| `Symbol` | `symbol` | uppercase string |
| `Side` | `side` | `BUY` / `SELL` |
| `Qty` | `qty_filled` / `qty_requested` | `60/100` |
| `Type` | `order_type` | `MID` / `MARKET` |
| `Price` | `price_placed` | `$412.30` (2 d.p.) |
| `Status` | `status` + reprice step if repricing | `OPEN`, `REPRICING 3/10`, `AMENDING`, `PARTIAL` |
| `Since` | `placed_at` | `HH:MM:SS` local time |

**Real-time reprice updates:** during active repricing, the `Price` and `Status` cells
for the repricing order update on every amendment step via `OutputRouter` /
`RendererProtocol.update_order_row()`. This is a push update — it does not wait for the
next poll cycle.

**Empty state:**
```
  No open orders.
```

**Poll refresh:** `set_interval` timer, interval from `tui_refresh_interval_seconds`.

**Error handling:** same as positions pane — log, preserve existing contents, continue.

---

## Header and Pane Border State Changes

These visual state changes apply regardless of pane rank or order:

| Condition | Visual change |
|---|---|
| IB connection lost | Header `ib_status` → `✗ DISCONNECTED` red. Orders pane border → red. |
| IB connection restored | Header `ib_status` → `● CONNECTED` green. Orders pane border → default. |
| Daemon heartbeat stale | Header `daemon_status` → `✗ Daemon offline` amber. Positions pane border → amber. |
| Daemon heartbeat restored | Header `daemon_status` → `● Daemon` green. Positions pane border → default. |

Border state changes are pushed immediately via IB disconnect/reconnect callbacks and
the heartbeat poll cycle — they do not wait for the next full poll cycle.

---

## The OutputRouter — Specification

This is the most critical new component. It is the **only** mechanism by which command
handlers, engine callbacks, and startup logic deliver output to the TUI. Nothing else may
write to any TUI widget directly.

### Interface

```python
# repl/output_router.py

from enum import Enum, auto
from typing import Protocol


class OutputPane(Enum):
    """Destination pane for a routed message."""
    LOG = auto()      # Log pane — system events, warnings, errors, startup messages
    COMMAND = auto()  # Command pane — direct output of the current operation
    BOTH = auto()     # Log pane AND command pane simultaneously


class OutputSeverity(Enum):
    """Visual severity of a routed message."""
    INFO = auto()
    SUCCESS = auto()   # ✓ green
    WARNING = auto()   # ⚠ amber
    ERROR = auto()     # ✗ red
    DEBUG = auto()     # JSON log file only — never shown in any TUI pane


class RendererProtocol(Protocol):
    """
    Abstract interface for the rendering backend.

    The current implementation is the Textual TUI. In a future server/client
    architecture, this will be replaced with a network publisher. Command handlers
    and engine code depend only on this protocol — never on Textual directly.
    """

    def write_log(self, message: str, severity: OutputSeverity) -> None:
        """Append a line to the log pane."""
        ...

    def write_command_output(self, message: str, severity: OutputSeverity) -> None:
        """Append a line to the command pane output area (rolling, max 4 lines visible)."""
        ...

    def update_order_row(self, serial: int, price: str, status: str) -> None:
        """
        Update the Price and Status cells for a specific order row in the orders pane.
        Called by the reprice loop on each amendment — does not wait for the next poll cycle.
        serial: the order's serial_number (user-facing identifier).
        price: formatted string e.g. '$412.36'
        status: formatted string e.g. 'REPRICING 3/10'
        """
        ...

    def update_header(self) -> None:
        """
        Trigger an immediate re-render of the header bar.
        Called after IB connection state changes or daemon heartbeat changes.
        The header re-reads its data sources (AppContext, HeartbeatRepository,
        TradeRepository) rather than accepting pushed values.
        """
        ...


class OutputRouter:
    """
    Routes output messages to the correct TUI pane via a RendererProtocol.

    This is the single output wiring point for the entire REPL process.
    Command handlers and engine callbacks call this class — never TUI widgets directly.
    In a future server/client architecture, the RendererProtocol implementation will
    be swapped for a network publisher without changing any call sites.

    All messages are also forwarded to the structured JSON logger regardless of
    severity or destination. The TUI is a display layer only — the JSON log file
    is the authoritative record.
    """

    def __init__(self, renderer: RendererProtocol) -> None: ...

    def emit(
        self,
        message: str,
        pane: OutputPane,
        severity: OutputSeverity = OutputSeverity.INFO,
        event: str | None = None,   # structured log event name e.g. "ORDER_FILLED"
        **log_kwargs,               # extra fields forwarded to the JSON logger
    ) -> None:
        """
        Route a message to the specified pane(s) and write to the JSON log.

        Routing rules:
        - DEBUG severity: JSON log only. Never written to any pane.
        - LOG pane: calls renderer.write_log() only.
        - COMMAND pane: calls renderer.write_command_output() only.
        - BOTH: calls renderer.write_log() AND renderer.write_command_output().

        Safety rules:
        - Never raises under any circumstance.
        - If renderer raises: log the failure at ERROR level and continue.
          Output routing must never crash the REPL.
        - If the renderer is not yet initialised (pre-TUI startup): buffer the
          message and flush to the log pane once the renderer is available.
        """
        ...
```

### Routing table — absolute rules, no exceptions

| Event | `pane` | `severity` |
|---|---|---|
| Fill confirmation (full) | `BOTH` | `SUCCESS` |
| Partial fill | `BOTH` | `WARNING` |
| Cancel result (timeout / manual) | `BOTH` | `WARNING` |
| Reprice step update | `COMMAND` | `INFO` |
| `orders` command output | `COMMAND` | `INFO` |
| `stats` command output | `COMMAND` | `INFO` |
| Symbol validation error | `COMMAND` | `ERROR` |
| Safety limit rejection | `COMMAND` | `ERROR` |
| IB order rejection | `COMMAND` | `ERROR` |
| Startup messages (connected, symbols loaded) | `LOG` | `INFO` |
| Abandoned order warnings | `LOG` | `WARNING` |
| Daemon offline warning | `LOG` | `WARNING` |
| IB connection lost | `LOG` | `ERROR` |
| IB connection recovered | `LOG` | `SUCCESS` |
| DEBUG events | `LOG` | `DEBUG` (log file only) |

### Pre-TUI startup buffering

The `OutputRouter` is constructed before the Textual TUI app launches (during health
check and startup sequence). Messages emitted during this window must not be lost.

The router must maintain an internal buffer of `(message, pane, severity)` tuples when
the renderer is not yet available. Once the TUI launches and the renderer is registered
via `router.set_renderer(renderer)`, flush the buffer in order to the appropriate panes.
Buffer is cleared after flush. Maximum buffer size: 100 messages — if exceeded, oldest
are dropped and a WARNING is logged once the renderer is available.

### Wiring OutputRouter into existing code

1. Replace all `print()` calls in `repl/commands.py` and `repl/main.py` with
   `router.emit(...)` calls per the routing table above.
2. Pass the `OutputRouter` instance into command handlers via `AppContext` — add
   `router: OutputRouter` to the `AppContext` dataclass. Do not use a global or
   module-level variable.
3. The engine's reprice loop and fill callbacks must also call `router.emit()` — trace
   exactly how they currently surface output and replace those call sites only.
4. Do not change any command logic — only output delivery.

---

## Command Queue

When a command is submitted while another is executing, the new command must not be
dropped and must not execute concurrently.

```python
command_queue: asyncio.Queue[str]  # max size: 10
```

**Rules:**
- No active command: execute immediately.
- Active command running: enqueue. Append to command pane output area:
  `⏳ Queued: {command_text}` (dimmed).
- Queue full (10 items): reject. Append to command pane:
  `✗ Queue full — please wait`. Do not silently drop.
- On TUI exit: drain without executing. Log each discarded command at WARNING with
  event name `COMMAND_DISCARDED_ON_EXIT`.
- On command execution error: log full stack trace at ERROR with event name
  `COMMAND_EXECUTION_ERROR`. Append `✗ Command failed — see log` to command pane.
  Dequeue and run next command. Never let one failure block the queue.

---

## Startup Sequence

1. Load config and construct `AppContext` (unchanged)
2. Construct `OutputRouter` with no renderer yet — buffering mode active
3. Add `router` to `AppContext`
4. Launch Textual TUI app — all enabled panes render immediately:
   - Header: shows app name and config values; IB/daemon status show `connecting…`
   - Log pane: empty
   - Positions pane: shows `Loading…`
   - Command pane: shows `> ` prompt, disabled (greyed) until startup completes
   - Orders pane: shows `Loading…`
5. Register renderer: `router.set_renderer(textual_renderer)` — buffered messages flush
6. Run existing health check — each step result → `router.emit(..., pane=LOG)`
7. Scan for ABANDONED orders — each warning → `router.emit(..., pane=LOG, severity=WARNING)`
8. Check daemon heartbeat — if stale → `router.emit(..., pane=LOG, severity=WARNING)`
9. Write `REPL_STARTED` event and PID to SQLite (unchanged)
10. Warm contract cache — log result → `router.emit(..., pane=LOG)`
11. Execute first poll: populate positions and orders panes, compute header P&L
12. Enable command pane prompt — REPL is ready

**Startup failure:** if any step fails, log the failure to the log pane at ERROR,
display the failure reason in the command pane output area, and exit cleanly. Do not
freeze or show a blank screen. The existing health check error handling is unchanged —
only output delivery changes.

---

## asyncio and ib_insync Event Loop

**This is the highest-risk integration point. Validate before writing any code.**

The existing `repl/main.py` uses:
```python
from ib_insync import util
util.startLoop()   # patches asyncio event loop for ib_insync
asyncio.run(run_repl(ctx))
```

`textual` also owns the asyncio event loop via its internal `asyncio.run()`. Calling
both will conflict.

**Required approach:**
1. Read the existing `repl/main.py` carefully before touching anything.
2. Validate that `ib_insync`'s async API works correctly when scheduled as
   `asyncio.create_task()` within Textual's event loop without `util.startLoop()`.
3. If this works: remove `util.startLoop()` from the REPL entrypoint. Run all
   `ib_insync` coroutines as tasks within Textual's loop.
4. If this does not work: document exactly what fails and propose an alternative
   before proceeding. Do not paper over an event loop conflict with workarounds.

The daemon process is completely unaffected — it has its own independent event loop.

Document the chosen approach in `ADR-013`.

---

## Files to Create

### `repl/output_router.py`
`OutputPane`, `OutputSeverity`, `RendererProtocol`, `OutputRouter` as specified above.
Full docstrings on all classes and public methods. No Textual imports — must be
importable without Textual installed (for unit testing).

### `repl/pane_config.py`
`PaneName`, `PaneConfig`, `load_pane_configs()` as specified above.
Full docstrings. No Textual imports.

### `repl/tui.py`
The Textual `App` subclass and `TextualRenderer` (concrete `RendererProtocol`
implementation). Contains:
- Dynamic layout construction from sorted `PaneConfig` list — no hardcoded widget order
- `TextualRenderer` implementing all `RendererProtocol` methods
- `set_interval` poll callback: queries positions, orders, and header P&L via repositories
- `Input` widget `on_submitted` handler feeding the command queue
- Command queue processing loop (`asyncio.Queue`)
- IB disconnect/reconnect event handlers updating header and pane borders
- Daemon heartbeat staleness check wired to header and positions pane border

No business logic. No repository calls outside the poll callback and the positions/orders
data queries. No direct IB calls. Rendering layer only.

### `docs/decisions/013-textual-ib-insync-event-loop.md`

Standard ADR format (from `CLAUDE.md`). Must document:
- What was validated before deciding
- What `util.startLoop()` does and whether it was removed or retained
- Consequences for the REPL process
- Daemon is unaffected
- Future note: when server/client-renderer migration happens, the event loop
  architecture will change again

---

## Files to Modify

### `repl/main.py`
- Construct `OutputRouter` before TUI launch (buffering mode)
- Add `router` to `AppContext`
- Replace `asyncio.run(run_repl(ctx))` with Textual TUI app launch
- Handle `util.startLoop()` per ADR-013
- Replace all `print()` calls with `router.emit()`
- Startup sequence per above — existing logic unchanged, only output delivery changes

### `repl/commands.py`
- Replace all `print()` calls with `router.emit()` per routing table
- Access router via `AppContext` — no imports of `tui.py`
- Zero changes to command logic

### `config/settings.yaml`
Add the full `tui:` block as specified in the Pane Configuration section above.

### `config/context.py` (`AppContext` dataclass)
Add one field:
```python
router: OutputRouter
```
This is the only change to `AppContext`. All other fields unchanged.

### `CLAUDE.md`
Append verbatim. Do not modify existing content:

```markdown
## REPL TUI Output Routing
- ALL output from command handlers and engine callbacks goes through `OutputRouter.emit()`.
- NEVER write directly to any Textual widget from command or engine code.
- NEVER import `tui.py` from `commands.py` or anything in `engine/`.
- OutputRouter is the single wiring point — in a future increment it becomes a network publisher.
- Log pane: system events, warnings, errors, startup messages only.
- Command pane: direct output of the current operation only (reprice steps, results, errors).
- Fill and partial fill confirmations route to BOTH panes.
- DEBUG severity: JSON log file only — never shown in any TUI pane.
- No output type may appear in the wrong pane under any circumstance.

## TUI Pane Layout
- Pane order, visibility, and height are configured in settings.yaml under tui.panes.
- NEVER hardcode pane order or widget construction order in Python.
- Layout is built dynamically from sorted PaneConfig list at startup.
- Adding, removing, or reordering panes requires only a settings.yaml change.
- Duplicate ranks and fewer than 2 enabled panes are startup errors — fail loudly.

## Unrealized P&L
- Unrealized P&L is not implemented — see Future Architecture Notes in the TUI increment prompt.
- NEVER add market data subscriptions to the REPL TUI without a dedicated design review.
- The positions pane renders a placeholder footer line indicating this is a known gap.
```

---

## Testing Requirements

All existing tests must pass unchanged. Additionally:

### `tests/unit/test_pane_config.py`
- Valid config loads and sorts correctly by rank
- Duplicate ranks raise `ConfigurationError`
- Fewer than 2 enabled panes raises `ConfigurationError`
- Missing `tui.panes` block applies defaults and does not raise
- `enabled: false` panes are excluded from sorted output
- Header height is always forced to 1 regardless of configured value
- Non-contiguous ranks sort correctly

### `tests/unit/test_output_router.py`
- `DEBUG` severity: JSON logger called, renderer methods NOT called
- `pane=LOG`: `write_log()` called, `write_command_output()` NOT called
- `pane=COMMAND`: `write_command_output()` called, `write_log()` NOT called
- `pane=BOTH`: both `write_log()` and `write_command_output()` called
- Renderer raises on `write_log()`: exception is caught, not propagated, failure logged
- Renderer raises on `write_command_output()`: same
- Pre-renderer buffer: messages emitted before `set_renderer()` are flushed in order
  once renderer is registered
- Buffer overflow (>100 messages): oldest dropped, WARNING logged on flush

### `tests/unit/test_command_queue.py`
- Empty queue: command executes immediately
- Active command: new command enqueued, queued feedback shown
- Full queue (10 items): rejection message shown, command not enqueued, not silently dropped
- TUI exit: queued commands discarded with `COMMAND_DISCARDED_ON_EXIT` log events
- Command execution error: `COMMAND_EXECUTION_ERROR` logged, queue continues with next command

All tests use a mock `RendererProtocol` — no Textual imports in any unit test file.

---

## Dependency

Add to `requirements.txt` if not already present:
```
textual>=0.50.0
```

---

## Verification Checklist

Claude Code must verify every item before marking this complete:

**Config:**
- [ ] Pane order changes in `settings.yaml` are reflected in TUI without code changes
- [ ] `enabled: false` hides a pane cleanly — remaining panes unaffected
- [ ] Duplicate rank values cause a clear startup error and non-zero exit
- [ ] Missing `tui.panes` block applies defaults with a WARNING logged

**Header:**
- [ ] Shows: app name, account ID, clientId, host:port, IB status, daemon status, day P&L
- [ ] `● CONNECTED` green / `✗ DISCONNECTED` red updates immediately on IB state change
- [ ] `● Daemon` green / `✗ Daemon offline` amber updates on heartbeat staleness
- [ ] Day P&L is green when positive, red when negative, white when zero
- [ ] Day P&L shows `$0.00` when no trades today — never blank or `None`
- [ ] All P&L values are `Decimal` — never `float`

**Log pane:**
- [ ] Startup messages appear here only — not in command pane
- [ ] Abandoned order warnings appear here only
- [ ] Daemon offline warning appears here only
- [ ] IB lost/recovered appears here only
- [ ] Scrolls independently without affecting other panes

**Positions pane:**
- [ ] Shows filled, unclosed positions with correct columns
- [ ] `PT Price` shows `—` when no profit taker exists
- [ ] Unrealized P&L placeholder footer line is present
- [ ] `# TODO: unrealized P&L` comment is present in the data query function
- [ ] Empty state shows `No open positions.`
- [ ] Pane border turns amber when daemon heartbeat is stale
- [ ] DB query failure leaves existing contents intact — does not crash TUI

**Command pane:**
- [ ] Height is 5 lines: 1 prompt + 4 rolling output lines
- [ ] Reprice steps stream here and roll correctly when > 4 lines
- [ ] Fill confirmation appears here AND in log pane
- [ ] Symbol validation error appears here only — not log pane
- [ ] IB order rejection appears here only — not log pane
- [ ] Prompt stays active during execution
- [ ] Up/down arrow cycles session command history

**Orders pane:**
- [ ] Shows OPEN/REPRICING/AMENDING/PARTIAL entry orders with correct columns
- [ ] `Price` and `Status` cells update in real time during repricing (not waiting for poll)
- [ ] Empty state shows `No open orders.`
- [ ] Pane border turns red when IB connection is lost
- [ ] DB query failure leaves existing contents intact — does not crash TUI

**Command queue:**
- [ ] Command during repricing shows `⏳ Queued:` feedback and executes after
- [ ] Full queue shows rejection — nothing silently dropped
- [ ] Failed command logs `COMMAND_EXECUTION_ERROR` and queue continues

**Startup:**
- [ ] All panes render immediately on launch — no blank screen
- [ ] Buffered pre-renderer messages appear in log pane after TUI launches
- [ ] Startup failure shows error in log and command panes, exits cleanly

**Standards compliance:**
- [ ] No `print()` calls remain in `repl/main.py` or `repl/commands.py`
- [ ] No Textual imports in `commands.py`, `engine/`, or any unit test file
- [ ] `output_router.py` and `pane_config.py` have no Textual imports
- [ ] Full docstrings on all new classes and public methods
- [ ] All new log events use named event types — no freeform strings
- [ ] `ADR-013` created with full decision rationale
- [ ] `CLAUDE.md` updated with output routing, layout, and P&L rules
- [ ] `CHANGELOG.md` updated
- [ ] All existing tests pass (`make test`)
- [ ] All new unit tests pass

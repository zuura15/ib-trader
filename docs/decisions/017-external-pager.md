# ADR 017: External pager via Healthchecks.io + ntfy.sh

Date: 2026-04-23
Status: Accepted

## Context

Any financial application running real money needs a pager path
**independent of the thing being monitored**. The in-app alert
system — `log_and_alert`, `/api/alerts`, CatastrophicOverlay — is
valuable but structurally insufficient: it only tells us about
problems it's healthy enough to detect and publish. It cannot
surface the engine dying, the host powering off, Redis going down
(no publisher), the `make dev` wrapper crashing, or the network
being out. For a trading system where "silent for hours" translates
directly to uncovered positions, that's exactly the gap the pager
exists to close.

A real incident on 2026-04-22 made this concrete. The IB Gateway
dropped at 23:45 PDT. The engine saw `IB_DISCONNECTED`, two code
bugs (fixed in #46 / `99e2d11`) caused the in-app CATASTROPHIC
alert to never reach the UI, and the operator SSH'd in the next
morning to find the box had been running with a dead broker for
over eight hours. Even after fixing those bugs, no signal is
**external** to the box — a kernel panic, power blip, or severed
network would still go unnoticed.

## Decision

Implement a small bash monitoring script under systemd user timers
that wires to two free services:

- **Healthchecks.io** — dead-man's-switch. Our script curls a ping
  URL every 60 s when healthy; if the ping stops arriving,
  Healthchecks pages us via their ntfy integration. This is the
  only layer that catches "monitor script crashed / box dead /
  network out" — those are exactly the cases where nothing local
  can tell us.
- **ntfy.sh** — live push. The script pushes a descriptive message
  the moment any CATASTROPHIC condition is detected locally.
  Covers "box is alive, broker/bot/process is dead".

Healthchecks.io's native ntfy integration is configured to post
its "check failed / recovered" notifications to the **same** ntfy
topic our local script uses. One Android app (ntfy, free on Play
Store / F-Droid) subscribed to one secret topic; two producers
feeding it. No extra inbox to watch.

### Maintenance handling

The host is also the dev box. We will restart processes and reboot
daily. The monitor must respect intentional downtime without creating
a global off-switch.

Two mechanisms:

- **Explicit window**: `ops/maint start [duration]` writes a
  lockfile at `~/.config/ibtrader-maint.lock` with a UNIX timestamp
  expiry. Monitor checks this first on every tick and exits 0
  silently if the window is active. Also POSTs Healthchecks.io's
  `/start` endpoint so HC's own dead-man's-switch pauses for the
  same window. `ops/maint end` clears it. The lockfile
  auto-expires (default 30 min, cap 8 h) — "I forgot" degrades to
  "alerts resume automatically" instead of "silent forever".
- **Auto-detected Ctrl+C**: if the `make dev` wrapper process is
  gone **and** a graceful-shutdown log event (`SHUTDOWN_REQUESTED`,
  `ENGINE_STOPPED`, `API_SERVER_STOPPED`, `BOT_RUNNER_STOPPED`, or
  `IB_DISCONNECTED expected=true`) appeared in the last 30 s, the
  monitor enters a 5-minute grace window without operator action.
  If the user is re-running `make dev` after a Ctrl+C, this covers
  them. If they Ctrl+C'd and walked away, alerts resume after 5 min.

Wrapper-dead **without** a graceful-shutdown signal — kernel panic,
OOM kill, power blip — pages immediately; the local monitor may go
dark shortly after, and Healthchecks.io takes over.

## Why not

Alternatives considered and rejected:

- **PagerDuty / Opsgenie**: overkill for a solo operator, costs real
  money, and adds a third-party chain longer than the problem
  warrants.
- **"Email-every-minute" dead-man's-switch** (operator-proposed
  first cut): reliable enough but polluting an inbox with
  per-minute heartbeats creates its own alert fatigue, and
  noticing silence in an inbox is slower than a purpose-built
  service comparing timestamps against a grace window.
- **Telegram-only** (single producer): works, but Telegram's
  notification behaviour isn't tuned for urgent alerts the way
  ntfy's priority / tagging system is, and it doesn't have a
  dead-man's-switch layer without wiring it to a second service
  anyway.
- **In-app only (fix alerting bugs and call it done)**: does not
  solve the class of "entire stack silent" failures. Structurally
  inadequate for a trading system regardless of how polished the
  in-app alerts get.

## Consequences

- One new artifact tree under `ops/`: `health_check.sh`, `maint`,
  `install-pager.sh`, `ibtrader-health.service`,
  `ibtrader-health.timer`.
- Two tiny app-side additions: `GET /api/system/health` on
  `ib-api`, `GET /health` on `ib-bots`. Under 15 lines total. Both
  dependency-free (no Redis, no DB) — they exist solely as
  liveness probes.
- One new out-of-repo config file, mode 600:
  `~/.config/ibtrader-pager.env` holding the HC ping URL and ntfy
  topic URL. Created by the installer. Never committed.
- A single transient lockfile, `~/.config/ibtrader-maint.lock`,
  used by `ops/maint`.
- Zero change to the app's runtime behaviour. The monitor runs as
  a systemd user unit entirely outside the app process tree.
- ~15 min operator setup (signup for HC, install ntfy app,
  subscribe to topic, run the installer).

## Verification

Documented in GH issue #47 and exercised by
`tests/unit/test_ops_pager_scripts.py` +
`tests/unit/test_bots_internal_api_health.py` +
`tests/unit/test_api_routes.py::TestSystemRoutes::test_system_health_endpoint`.

Live end-to-end: kill each daemon in turn and observe ntfy push
within ~60–120 s; stop the systemd timer and observe HC
"heartbeat missing" within the 120 s grace; `ops/maint start 2m` +
kill daemons and confirm no page for 2 min, then alerts resume.

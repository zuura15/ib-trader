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

### Operator-presence model

The host is also the dev box. IB Gateway is a GUI app that requires
an interactive login, so the trading stack intrinsically cannot run
until the operator is sitting at the box (physically or via a GUI
session). The pager follows the same invariant:

> **Pager alarms ⇔ operator is logged in and `make dev` is running.**

Concretely: the monitor's first check is whether the `make dev`
wrapper process exists. If it doesn't, the entire stack is
expected to be down and we stay quiet — just heartbeat HC so its
dead-man's-switch doesn't fire. If the wrapper is alive and
something beneath it is broken, we page.

This is why we **do not** enable `loginctl enable-linger`:

- Enabling linger would start the timer at boot, before the
  operator has logged in and brought the stack up. Every tick
  during the unattended pre-login window would see "wrapper dead"
  and — without the presence gate — would page bogusly.
- With linger *off*, the user systemd instance starts on login
  and terminates on full logout. The timer naturally tracks
  operator presence.
- Closing an individual terminal (e.g., the one running `make
  dev`) does **not** end the user's session as long as any other
  session (GUI, another SSH) remains — so the installer can be
  run from any terminal, the operator can close it, and the
  timer keeps going.

Trade-off: if OOM or the kernel kills the `make dev` wrapper while
the operator is logged in, the pager quietly stops alarming for
daemon failures too. This is an accepted loss in exchange for (a)
avoiding bogus boot-time alerts with zero extra state machinery
and (b) matching the operator's mental model that "pager runs
while I'm trading." A future iteration could add a "wrapper died
unexpectedly" alert by comparing wrapper state across ticks if it
becomes a real gap.

**Explicit maintenance window**: `ops/maint start [duration]`
writes a lockfile at `~/.config/ibtrader-maint.lock` with a UNIX
timestamp expiry. Monitor checks this first on every tick and
exits 0 silently if the window is active. Also POSTs
Healthchecks.io's `/start` endpoint so HC's own dead-man's-switch
pauses for the same window. `ops/maint end` clears it. The
lockfile auto-expires (default 30 min, cap 8 h) — "I forgot"
degrades to "alerts resume automatically" instead of "silent
forever". Primarily useful for longer planned work (VPN blip,
Gateway reinstall) that takes more than a quick restart.

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

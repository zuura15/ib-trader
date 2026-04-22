# Deploy

How to move this setup from one machine to another, and how to run it
as always-on services on the target. For a fresh install on a single
machine, start with `README.md` — this doc covers only the extra bits
(state transfer, systemd, two-host gotchas).

## When to use this doc

- Migrating the live trader from a laptop to a desktop (or any
  always-on host).
- Adding a second node that runs alongside the first (see the
  two-host gotcha below).
- Cloning a dev box to a colleague's machine.

**Not needed for same-machine updates** — just `git pull origin main`,
rebuild deps if they changed, and restart services.

## 1. Path invariant

Clone to **`/home/zuura/projects/ib-trader`** on every machine. Two
things depend on this absolute path:

- The systemd unit files in `deploy/*.service` hard-code
  `User=zuura` and `WorkingDirectory=/home/zuura/projects/ib-trader`.
- Claude Code sessions are keyed on a hyphen-encoded form of the
  absolute path (`~/.claude/projects/-home-zuura-projects-ib-trader`).
  A different path means sessions start fresh on the target.

If the target username or path must differ, edit the four
`deploy/ib-*.service` files (change `User=` and `WorkingDirectory=`)
and accept that Claude sessions won't carry over.

## 2. Prerequisites on the target

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv nodejs npm redis-server sqlite3

# uv (package manager this repo uses)
curl -LsSf https://astral.sh/uv/install.sh | sh

# IB Gateway — install the SAME version you run on the source machine.
# Download from https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
# Run it once by hand to accept the license + configure API access:
#   Configuration → Settings → API → Enable ActiveX and Socket Clients.

# GitHub auth (SSH key or `gh auth login`) for the clone step.
```

## 3. One-time migration

### On the target — install the code (no data yet)

```bash
mkdir -p ~/projects && cd ~/projects
git clone git@github.com:zuura15/ib-trader.git
cd ib-trader

uv venv && uv pip install -e .
uv pip install websockets httpx
cd frontend && npm install && cd ..

uv run alembic -c migrations/alembic.ini upgrade head
```

### On the source — stop trading, then snapshot

The SQLite DB must be quiesced before copying, otherwise you risk
grabbing a half-flushed page and landing a corrupt file on the target.

```bash
# Source machine — in the repo dir
./deploy/stop.sh                              # stops engine/api/frontend
# If running under systemd instead:
#   sudo systemctl stop ib-bots ib-daemon ib-api ib-engine

# Atomic snapshot (safe even if writes are happening, but stopping
# first is cheap insurance):
sqlite3 trader.db ".backup /tmp/trader.db.snap"
```

### Transfer

```bash
# From the target, pulling files off the source:
scp laptop:/home/zuura/projects/ib-trader/.env .
scp laptop:/tmp/trader.db.snap trader.db
chmod 600 .env trader.db

# Claude Code sessions + memory (optional but recommended — lets
# Claude continue prior conversations and keeps the /memory/ files).
mkdir -p ~/.claude/projects
scp -r laptop:/home/zuura/.claude/projects/-home-zuura-projects-ib-trader \
       ~/.claude/projects/

# Optional: preserve bot FSM state in Redis so running bots don't
# boot to OFF. Skip this if you're fine restarting bots by hand.
ssh laptop 'redis-cli SAVE && cat /var/lib/redis/dump.rdb' > /tmp/dump.rdb
sudo systemctl stop redis-server
sudo cp /tmp/dump.rdb /var/lib/redis/dump.rdb
sudo chown redis:redis /var/lib/redis/dump.rdb
sudo systemctl start redis-server
```

### Verify

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/smoke
# Expect 600+ pass on a clean DB; a few alert-list tests may fail if
# Redis has leftover alert hashes — dismiss in the UI later.
```

## 4. Run as always-on services

```bash
# Installs the four unit files under /etc/systemd/system/
sudo bash deploy/setup.sh

# Enable at boot + start now. Skip ib-daemon (it's a Textual TUI —
# run it in a terminal when you want it, not as a service).
sudo systemctl enable --now ib-engine ib-api ib-bots

# Check:
sudo systemctl status ib-engine ib-api ib-bots
journalctl -u ib-engine -f
```

Stop order (engine LAST): `sudo systemctl stop ib-bots ib-api ib-engine`.

### IB Gateway on an always-on box

Gateway is a GUI Java app. Two options:

**(a) Auto-login on desktop session (simplest)** — log in once in the
Gateway UI, tick "Auto-restart" in its settings, and add a desktop
autostart entry so it launches when the user session boots. Requires
a logged-in desktop session; if you reboot without logging in, the
Gateway won't be up.

**(b) Headless with `xvfb-run`** — runs Gateway against a virtual X
display so it doesn't need a desktop session. Works for true
server-style boxes. Requires configuring auto-login in
`~/Jts/jts.ini` and wrapping Gateway's launcher:

```bash
xvfb-run -a /opt/ibgateway/ibgateway
```

Both need the API socket listener on (4001 live / 4002 paper).

## 5. Ongoing workflow

```bash
cd ~/projects/ib-trader
git pull origin main

# Re-install deps only when the manifest changed:
uv pip install -e .                     # if pyproject.toml changed
cd frontend && npm install && cd ..     # if package.json changed
uv run alembic -c migrations/alembic.ini upgrade head  # if new migration landed

# Restart. Engine restarts LAST on stop but FIRST on start.
sudo systemctl restart ib-engine ib-api ib-bots
```

## 6. Two-host gotcha

If the laptop and the desktop both try to connect to the same IB
Gateway with the same `IB_CLIENT_ID`, the second connection kicks
the first off. Two ways to avoid it:

- **Single trader, single dev box.** Keep services running on only
  one machine. Treat the other as a dev/inspection-only node — open
  the code, run tests, but never `systemctl start`.
- **Separate client IDs.** Use distinct `IB_CLIENT_ID` values per
  machine's `.env`. IB allows multiple concurrent clients against
  one Gateway. Bots + positions still live on exactly one box;
  this just avoids the disconnection war if both are up.

The laptop's own recent bots/positions end up on whichever machine
owns the SQLite snapshot you copied — **don't run both as "the
trader" at the same time.**

## 7. What NOT to copy

- `logs/`, `run/`, `test-results/` — per-machine noise.
- `.venv/`, `frontend/node_modules/` — rebuild on the target; they
  bake absolute paths.
- `prototypes/` — experimental scratch, gitignored.
- `trader.db-journal`, `trader.db-wal` — SQLite transaction files;
  the `.backup` snapshot in step 3 already captures a consistent
  view. Copying the journal files separately can land an
  inconsistent database.

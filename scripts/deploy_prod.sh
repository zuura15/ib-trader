#!/usr/bin/env bash
# Deploy origin/main to local-prod and restart the trading stack.
#
# Run from the dev box. Steps (each one aborts the deploy on failure):
#   1. Preflight: prod checkout is clean; show the commits about to land.
#   2. Refuse while a bot holds a live position (same guard as Ctrl+C).
#   3. git pull --ff-only on prod; npm ci if package-lock changed;
#      alembic upgrade if a migration landed.
#   4. Ctrl+C the `make prod` running in prod's `screen` session, wait
#      for 8000/8081/8082 to free, then type `make prod` back into the
#      same window — the operator can still `screen -r trading` and see
#      the live output exactly as with a manual restart.
#   5. Health check: ports listening + engine heartbeat fresh.
#
# Working orders and positions live at IB, so a restart doesn't touch
# them. Redis is shut down `nosave` by make prod's trap (as on any
# manual restart) — in-memory runtime state is rebuilt on startup.
#
# Usage: scripts/deploy_prod.sh [--yes] [--dry-run]
#   --yes      skip the confirmation prompt
#   --dry-run  run preflight only (shows incoming commits), change nothing
# Env: PROD_HOST (default 192.168.4.66), PROD_DIR, SCREEN_SESSION,
#      SCREEN_WINDOW.
set -euo pipefail

PROD_HOST="${PROD_HOST:-192.168.4.66}"
PROD_DIR="${PROD_DIR:-/home/zuura/projects/ib-trader}"
SCREEN_SESSION="${SCREEN_SESSION:-trading}"
SCREEN_WINDOW="${SCREEN_WINDOW:-0}"
PORTS="8000 8081 8082"

YES=0
DRY=0
for arg in "$@"; do
    case "$arg" in
        --yes) YES=1 ;;
        --dry-run) DRY=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
# Non-interactive ssh doesn't source the profile, so put the user-local
# tool dirs (uv lives in ~/.local/bin) on PATH explicitly.
remote() {
    ssh -o ConnectTimeout=8 "$PROD_HOST" \
        "export PATH=\"\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH\"; cd '$PROD_DIR' && $1"
}

# --- 1. Preflight -----------------------------------------------------
local_ahead=$(git rev-list --count origin/main..main 2>/dev/null || echo 0)
if [ "$local_ahead" != "0" ]; then
    say "note: local main is $local_ahead commit(s) ahead of origin — only pushed commits deploy."
fi

dirty=$(remote "git status --porcelain --untracked-files=no") \
    || die "can't reach $PROD_HOST"
[ -z "$dirty" ] || die "prod checkout has local changes:
$dirty"

remote "git fetch -q origin main" || die "git fetch failed on prod"
before=$(remote "git rev-parse --short HEAD")
incoming=$(remote "git log --oneline HEAD..origin/main")
if [ -z "$incoming" ]; then
    say "prod is already at origin/main ($before)."
    if [ "$DRY" = 1 ]; then exit 0; fi
    say "restarting anyway (no new commits)."
else
    say "prod $before → origin/main; incoming:"
    printf '%s\n' "$incoming" | sed 's/^/    /'
fi
[ "$DRY" = 1 ] && { say "dry run — nothing changed."; exit 0; }

# --- 2. Live-position guard --------------------------------------------
remote "scripts/dev/check_dev_can_shutdown.sh" \
    || die "a bot holds a live position — not restarting."

remote "screen -ls | grep -q '\.$SCREEN_SESSION[[:space:]]'" \
    || die "no screen session '$SCREEN_SESSION' on prod — start make prod in it first."

if [ "$YES" != 1 ]; then
    read -r -p "[deploy] pull and restart prod now? [y/N] " ans
    [ "$ans" = y ] || [ "$ans" = Y ] || die "aborted."
fi

engine_pid() {
    remote "curl -sf --max-time 3 localhost:8000/api/status" 2>/dev/null \
        | python3 -c 'import json,sys; hb=[h for h in json.load(sys.stdin).get("heartbeats",[]) if h.get("process")=="ENGINE" and h.get("alive")]; print(hb[0]["pid"] if hb else "")' 2>/dev/null || true
}
old_pid=$(engine_pid)

# --- 3. Pull + dependency steps -----------------------------------------
remote "git pull -q --ff-only origin main" || die "git pull --ff-only failed"
after=$(remote "git rev-parse --short HEAD")
changed=$(remote "git diff --name-only $before $after" || true)
if printf '%s\n' "$changed" | grep -q '^frontend/package-lock.json$'; then
    say "package-lock changed — npm ci."
    remote "cd frontend && npm ci --silent" || die "npm ci failed"
fi
if printf '%s\n' "$changed" | grep -q '^migrations/versions/'; then
    say "new migration — alembic upgrade head."
    remote "uv run alembic -c migrations/alembic.ini upgrade head" \
        || die "alembic upgrade failed (code is pulled, stack NOT restarted)"
fi

# --- 4. Restart inside the screen session --------------------------------
say "stopping make prod (Ctrl+C in screen $SCREEN_SESSION:$SCREEN_WINDOW)..."
remote "screen -S '$SCREEN_SESSION' -p '$SCREEN_WINDOW' -X stuff \$'\\003'"
for _ in $(seq 1 30); do
    busy=$(remote "for p in $PORTS; do lsof -ti tcp:\$p -sTCP:LISTEN 2>/dev/null; done" || true)
    [ -z "$busy" ] && break
    sleep 1
done
[ -z "${busy:-}" ] || die "services still listening after 30s (PIDs: $busy) — check screen -r $SCREEN_SESSION"

say "starting make prod..."
remote "screen -S '$SCREEN_SESSION' -p '$SCREEN_WINDOW' -X stuff \$'make prod\\r'"

# --- 5. Health check -------------------------------------------------------
say "waiting for services..."
ok=0
for _ in $(seq 1 90); do
    up=$(remote "n=0; for p in $PORTS; do lsof -ti tcp:\$p -sTCP:LISTEN >/dev/null 2>&1 && n=\$((n+1)); done; echo \$n" || echo 0)
    if [ "$up" = 3 ]; then
        # A fresh heartbeat from a NEW engine pid — the old process's
        # heartbeat can still read "alive" for a few seconds.
        pid=$(engine_pid)
        if [ -n "$pid" ] && [ "$pid" != "$old_pid" ]; then ok=1; break; fi
    fi
    sleep 2
done
if [ "$ok" = 1 ]; then
    say "deployed $after — services up, new engine (pid $pid) heartbeating."
else
    say "services did not come up healthy within 3 min. Recent log:"
    remote "tail -n 30 logs/ib_trader.log" || true
    exit 1
fi

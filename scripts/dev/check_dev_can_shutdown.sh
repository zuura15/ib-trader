#!/usr/bin/env bash
# Exit 0 if it's safe to shut down `make dev` (no bots holding a live
# position); exit 1 if at least one bot is in ENTRY_ORDER_PLACED /
# AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED. Called from the Ctrl+C
# trap in the `dev` target — a non-zero exit re-arms the trap and
# keeps the session running.
#
# API failure (not yet up, network blip) is treated as "safe to
# shut down" — the alternative is locking the session forever if
# the API never came up, which is worse than letting Ctrl+C through
# in the pre-trading-state window.

set -u

URL="http://127.0.0.1:8000/api/bots"
RESP="$(curl -sf --max-time 2 "$URL" 2>/dev/null)" || {
    echo "[DEV] ib-api not responding — proceeding with shutdown." >&2
    exit 0
}

# Pass the response via env var, not stdin — a heredoc on python's
# stdin would otherwise shadow the curl output.
export DEV_BOTS_JSON="$RESP"
python3 <<'PY'
import json, os, sys

POSITION_STATES = {
    "ENTRY_ORDER_PLACED",
    "AWAITING_EXIT_TRIGGER",
    "EXIT_ORDER_PLACED",
}

try:
    bots = json.loads(os.environ.get("DEV_BOTS_JSON", "[]"))
except Exception as e:
    print(f"[DEV] failed to parse /api/bots ({e}); proceeding with shutdown.",
          file=sys.stderr)
    sys.exit(0)

hot = [b for b in bots if b.get("state") in POSITION_STATES]
if not hot:
    sys.exit(0)

print("", file=sys.stderr)
print("[DEV] ⚠  Live positions — shutdown blocked:", file=sys.stderr)
for b in hot:
    syms = b.get("symbols_json") or "[]"
    print(f"        {b.get('id')} ({syms}) — state={b.get('state')}",
          file=sys.stderr)
print("", file=sys.stderr)
print("[DEV] Force-quit each bot from the UI, then Ctrl+C again.",
      file=sys.stderr)
print("[DEV] Or press Ctrl+\\ (SIGQUIT) to bypass the check.",
      file=sys.stderr)
sys.exit(1)
PY

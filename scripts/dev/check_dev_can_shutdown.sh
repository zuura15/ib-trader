#!/usr/bin/env bash
# Exit 0 if it's safe to shut down `make dev` (no bots holding a live
# position); exit 1 if at least one bot is in ENTRY_ORDER_PLACED /
# AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED. Called from the Ctrl+C
# trap in the `dev` target — a non-zero exit re-arms the trap and
# keeps the session running.
#
# API not reachable (not yet up, network blip) is treated as "safe to
# shut down" — the alternative is locking the session forever if
# the API never came up, which is worse than letting Ctrl+C through
# in the pre-trading-state window. An API that IS up but rejects the
# access key is NOT safe: bot state is unknown, so we refuse (Ctrl+\
# still force-quits).

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="http://127.0.0.1:8000/api/bots"
HTTP_CODE=0
RESP="$("$HERE/../api_curl.sh" -s --max-time 2 -w '\n%{http_code}' "$URL" 2>/dev/null)"
CURL_RC=$?
if [ "$CURL_RC" -eq 100 ]; then
    # No key: ib-api refuses to start without one, so it's most likely
    # down. Only refuse if something is actually answering on :8000.
    if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8000/api/auth/session; then
        echo "[DEV] API_SECRET_KEY missing from .env — cannot check bot state; refusing shutdown (Ctrl+\\ to force)." >&2
        exit 1
    fi
    echo "[DEV] ib-api not responding — proceeding with shutdown." >&2
    exit 0
fi
if [ "$CURL_RC" -ne 0 ]; then
    echo "[DEV] ib-api not responding — proceeding with shutdown." >&2
    exit 0
fi
HTTP_CODE="${RESP##*$'\n'}"
RESP="${RESP%$'\n'*}"
if [ "$HTTP_CODE" = 401 ] || [ "$HTTP_CODE" = 403 ]; then
    echo "[DEV] ib-api rejected the access key — cannot check bot state; refusing shutdown (Ctrl+\\ to force)." >&2
    exit 1
fi
if [ "$HTTP_CODE" != 200 ]; then
    echo "[DEV] ib-api returned HTTP $HTTP_CODE — proceeding with shutdown." >&2
    exit 0
fi

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

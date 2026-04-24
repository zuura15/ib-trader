#!/usr/bin/env bash
# External pager: 60s liveness + log scan. Runs under systemd --user.
#
# Two producers feed the same ntfy topic on the operator's phone:
#   - This script pushes CATASTROPHIC / WARNING signals it detects
#     locally on every tick.
#   - Healthchecks.io's native ntfy integration pushes when this
#     script stops pinging HC (box dead / network dead / monitor
#     crashed).
#
# When to alarm:
#   Pager only raises CATASTROPHIC for failures of a RUNNING stack.
#   Stack absence is never an alert. The `make dev` wrapper process
#   is the gate — if it's dead, operator is not trading and we stay
#   quiet (HC still heartbeats so its dead-man's-switch doesn't fire
#   spuriously).
#
#   This intentional choice means: OOM-killing `make dev` won't page
#   you. Trade-off accepted so that (a) closing the operator's
#   terminal naturally stops alerts, and (b) post-reboot alerts stay
#   quiet until operator starts the stack — no `loginctl linger`
#   needed.
#
# Maintenance respect:
#   - If ~/.config/ibtrader-maint.lock holds a future expiry, skip
#     all checks (lockfile is set by `ops/maint start`).
#
# Env vars (sourced from ~/.config/ibtrader-pager.env, mode 600):
#   HC_PING_URL     https://hc-ping.com/<uuid>
#   NTFY_TOPIC_URL  https://ntfy.sh/<topic>
#
# See GH issue #47 for design.
set -u

PAGER_ENV="${PAGER_ENV:-$HOME/.config/ibtrader-pager.env}"
MAINT_LOCK="${MAINT_LOCK:-$HOME/.config/ibtrader-maint.lock}"
REPO_ROOT="${REPO_ROOT:-$HOME/projects/ib-trader}"
LOG_FILE="${LOG_FILE:-$REPO_ROOT/logs/ib_trader.log}"

# Benign IB error codes (see engine/insync_client error callbacks).
# 462 = "Order modify failed. Cannot change to the new TIF" — handled by
# the cancel-verification path (ADR-018 / GH #48). The synthetic Cancelled
# is suppressed when IB confirms the order is still open, so a 462 in the
# logs is expected noise, not a pageable failure.
BENIGN_IB_CODES="202 462 2103 2104 2105 2107 2108 2158 2174"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

errors=()
warnings=()

_now_epoch() { date -u +%s; }

_add_error()   { errors+=("$1");   }
_add_warning() { warnings+=("$1"); }

# Load env (HC_PING_URL, NTFY_TOPIC_URL). Refuse to run without it —
# we never want silent no-ops where the monitor thinks it's running
# but can't actually reach anybody.
if [[ -r "$PAGER_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$PAGER_ENV"
fi
if [[ -z "${HC_PING_URL:-}" || -z "${NTFY_TOPIC_URL:-}" ]]; then
    echo "health_check.sh: missing HC_PING_URL / NTFY_TOPIC_URL in $PAGER_ENV" >&2
    echo "  Run ops/install-pager.sh to bootstrap." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# maintenance gate
# ---------------------------------------------------------------------------

_maint_active() {
    [[ -f "$MAINT_LOCK" ]] || return 1
    local expiry
    expiry=$(cat "$MAINT_LOCK" 2>/dev/null | head -1 | tr -d '[:space:]')
    [[ -n "$expiry" ]] || return 1
    [[ "$expiry" -gt "$(_now_epoch)" ]]
}

if _maint_active; then
    # Explicit maintenance window — silent exit, HC will be paused
    # server-side via its /start endpoint (see ops/maint).
    exit 0
fi

# ---------------------------------------------------------------------------
# operator-presence gate: `make dev` wrapper alive?
# ---------------------------------------------------------------------------
#
# If the operator hasn't started `make dev` (box just booted / logged
# out / Ctrl+C'd for maintenance / deliberately stopped), the whole
# stack will look missing. That's expected, not alert-worthy. Gate
# on the wrapper: wrapper dead → heartbeat HC (so its dead-man's
# switch stays green) and exit quietly.

_wrapper_alive() {
    # The top-level `sh -c 'trap … & wait'` that make dev invokes.
    # Any of these signals indicates the dev stack is up:
    pgrep -f 'uv run ib-engine' >/dev/null 2>&1 \
        || pgrep -f 'make dev' >/dev/null 2>&1 \
        || pgrep -f '\buv run ib-' >/dev/null 2>&1
}

if ! _wrapper_alive; then
    curl -fsS --max-time 5 "$HC_PING_URL" >/dev/null 2>&1 || true
    exit 0
fi

# ---------------------------------------------------------------------------
# A. per-daemon liveness
# ---------------------------------------------------------------------------

_check_http() {
    # _check_http <label> <url>
    local label=$1 url=$2
    if ! curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
        _add_error "$label: HTTP probe failed ($url)"
    fi
}

_check_process() {
    # _check_process <label> <pgrep-pattern>
    local label=$1 pattern=$2
    if ! pgrep -f "$pattern" >/dev/null 2>&1; then
        _add_error "$label: process not found ($pattern)"
    fi
}

_check_port_open() {
    # _check_port_open <label> <host> <port>
    local label=$1 host=$2 port=$3
    if ! (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
        _add_error "$label: port $host:$port refused"
    else
        exec 3<&-
        exec 3>&-
    fi
}

_check_log_fresh() {
    # _check_log_fresh <label> <path> <max_age_seconds>
    local label=$1 path=$2 max_age=$3
    if [[ ! -r "$path" ]]; then
        _add_warning "$label: log not readable ($path)"
        return
    fi
    local mtime now age
    mtime=$(stat -c %Y "$path" 2>/dev/null) || return
    now=$(_now_epoch)
    age=$(( now - mtime ))
    if (( age > max_age )); then
        _add_warning "$label: log stale (${age}s, limit ${max_age}s)"
    fi
}

_check_process "ib-engine"      "ib-engine"
_check_process "ib-api"         "ib-api"
_check_process "ib-bots"        "ib-bots"
_check_process "redis-server"   "redis-server"

_check_http "ib-engine"  "http://127.0.0.1:8081/engine/health"
_check_http "ib-api"     "http://127.0.0.1:8000/api/system/health"
_check_http "ib-bots"    "http://127.0.0.1:8082/health"

if command -v redis-cli >/dev/null 2>&1; then
    if ! redis-cli -p 6379 ping 2>/dev/null | grep -q '^PONG'; then
        _add_error "redis: ping failed"
    fi
else
    # Not fatal; try TCP probe instead.
    _check_port_open "redis" 127.0.0.1 6379
fi

# IB Gateway (best effort — process name varies; port probe is primary).
_check_port_open "ib-gateway" 127.0.0.1 4001

_check_log_fresh "logs/ib_trader.log" "$LOG_FILE" 120

# ---------------------------------------------------------------------------
# B. per-log pattern scans (last minute only, so we don't re-alert on
#    old issues on every tick)
# ---------------------------------------------------------------------------

_scan_recent_log() {
    [[ -r "$LOG_FILE" ]] || return
    local cutoff_epoch=$(( $(_now_epoch) - 60 ))

    local recent
    recent=$(tail -n 2000 "$LOG_FILE" 2>/dev/null | python3 -c '
import sys, re
from datetime import datetime, timezone
cutoff = int(sys.argv[1])
for line in sys.stdin:
    m = re.search(r"\"timestamp\": \"([^\"]+)\"", line)
    if not m:
        continue
    try:
        dt = datetime.fromisoformat(m.group(1))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() >= cutoff:
            sys.stdout.write(line)
    except Exception:
        pass
' "$cutoff_epoch" 2>/dev/null)
    [[ -n "$recent" ]] || return

    # Generic ERROR-line scan. Pages only on truly unclassified errors —
    # CATASTROPHIC alerts come through the dedicated Redis path below, and
    # IB_ORDER_ERROR has its own benign-codes-aware scan further down.
    # Goal: alert on "something we haven't seen before", not on every ERROR
    # log line. Adding to the exclusion list is correct when an error pattern
    # is handled by the engine and surfaces only as expected log noise.
    local err_signal
    err_signal=$(echo "$recent" \
        | grep '"level": "ERROR"' \
        | grep -v 'alert resolve skipped' \
        | grep -v 'tick heartbeat cancel' \
        | grep -v '"event": "IB_ORDER_ERROR"' \
        | head -3 || true)
    if [[ -n "$err_signal" ]]; then
        local err_count
        err_count=$(echo "$err_signal" | wc -l)
        _add_error "unclassified ERROR log lines ($err_count in 60s): $(echo "$err_signal" | head -1 | head -c 240)"
    fi

    # IB_ORDER_ERROR with non-benign code.
    local bad_codes
    bad_codes=$(echo "$recent" \
        | grep '"event": "IB_ORDER_ERROR"' \
        | grep -oE '"code": [0-9]+' \
        | awk '{print $2}' | sort -u || true)
    local code
    for code in $bad_codes; do
        local benign=false
        for b in $BENIGN_IB_CODES; do
            [[ "$code" == "$b" ]] && { benign=true; break; }
        done
        if ! $benign; then
            _add_error "IB_ORDER_ERROR code=$code (non-benign) in last 60s"
        fi
    done

    # WARNING rate alarm.
    local warn_count
    warn_count=$(echo "$recent" | grep -c '"level": "WARNING"' || true)
    if (( warn_count > 10 )); then
        _add_warning "WARNING rate: $warn_count lines in last 60s"
    fi

    # Specific red-flag events.
    local flag
    for flag in BOT_CRASH BOT_STARTUP_FORCED_OFF_WITH_PANIC; do
        if echo "$recent" | grep -q "\"event\": \"$flag\""; then
            _add_error "event $flag in last 60s"
        fi
    done
}

_scan_recent_log

# ---------------------------------------------------------------------------
# C. Redis + API signals (CATASTROPHIC alerts → verbatim push)
# ---------------------------------------------------------------------------

_check_active_alerts() {
    local payload
    payload=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/alerts 2>/dev/null) || return
    # Pull CATASTROPHIC entries and surface their trigger+message verbatim.
    local cata
    cata=$(echo "$payload" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in (data if isinstance(data, list) else []):
    if a.get("severity") == "CATASTROPHIC":
        print(f"{a.get(\"trigger\",\"?\")}: {a.get(\"message\",\"\")}")
' 2>/dev/null)
    if [[ -n "$cata" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && _add_error "app CATASTROPHIC alert → $line"
        done <<< "$cata"
    fi
}

_check_active_alerts

# ---------------------------------------------------------------------------
# D. bot heartbeat staleness
# ---------------------------------------------------------------------------

_check_bot_heartbeats() {
    local payload
    payload=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/bots 2>/dev/null) || return
    local stale
    stale=$(echo "$payload" | python3 -c '
import json, sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
try:
    bots = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for b in bots:
    if b.get("status") != "RUNNING":
        continue
    hb = b.get("last_heartbeat")
    if not hb:
        print(f"{b.get(\"name\",\"?\")}: RUNNING but no heartbeat")
        continue
    try:
        hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
    except Exception:
        continue
    age = (now - hb_dt).total_seconds()
    if age > 300:
        print(f"{b.get(\"name\",\"?\")}: heartbeat {int(age)}s stale")
' 2>/dev/null)
    if [[ -n "$stale" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && _add_error "bot stale → $line"
        done <<< "$stale"
    fi
}

_check_bot_heartbeats

# ---------------------------------------------------------------------------
# E. infrastructure
# ---------------------------------------------------------------------------

_check_disk() {
    local mount=$1
    local pct
    pct=$(df --output=pcent "$mount" 2>/dev/null | tail -1 | tr -dc '0-9')
    [[ -n "$pct" ]] || return
    if (( pct > 90 )); then
        _add_error "disk $mount at ${pct}% full"
    fi
}

_check_disk /
_check_disk /home 2>/dev/null || true

# Load avg (1-min).
if [[ -r /proc/loadavg ]]; then
    load1=$(awk '{print $1}' /proc/loadavg)
    # Integer compare on the integer part — we care about > 8.
    load1_int=${load1%.*}
    if (( ${load1_int:-0} > 8 )); then
        _add_warning "1-min load avg $load1"
    fi
fi

# ---------------------------------------------------------------------------
# publish results
# ---------------------------------------------------------------------------

if (( ${#errors[@]} == 0 && ${#warnings[@]} == 0 )); then
    # All green — heartbeat HC, no ntfy.
    curl -fsS --max-time 5 "$HC_PING_URL" >/dev/null 2>&1 || true
    exit 0
fi

# Build message. Errors first, then warnings.
{
    printf 'IB Trader health check:\n'
    if (( ${#errors[@]} > 0 )); then
        printf '\n[ERRORS]\n'
        printf '  - %s\n' "${errors[@]}"
    fi
    if (( ${#warnings[@]} > 0 )); then
        printf '\n[WARNINGS]\n'
        printf '  - %s\n' "${warnings[@]}"
    fi
    printf '\nHost: %s  ts=%s\n' "$(hostname)" "$(date -u +%FT%TZ)"
} > /tmp/ibtrader-health-msg.$$

# Only push via ntfy if we have ERRORS (catastrophic). WARNINGS go to
# HC only — visible in the dashboard, and the alert window catches
# sustained WARNINGs when the /fail call is made.
if (( ${#errors[@]} > 0 )); then
    curl -fsS --max-time 5 \
        -H "Title: IB Trader CATASTROPHIC" \
        -H "Priority: urgent" \
        -H "Tags: rotating_light,warning" \
        --data-binary "@/tmp/ibtrader-health-msg.$$" \
        "$NTFY_TOPIC_URL" >/dev/null 2>&1 || true
    # Signal HC failure so its integration also fires — belt and suspenders.
    curl -fsS --max-time 5 --data-binary "@/tmp/ibtrader-health-msg.$$" \
        "${HC_PING_URL%/}/fail" >/dev/null 2>&1 || true
    rm -f /tmp/ibtrader-health-msg.$$
    exit 1
fi

# Warnings only: just heartbeat (green for HC) and exit 0. The HC
# dashboard + the log line are the record.
curl -fsS --max-time 5 "$HC_PING_URL" >/dev/null 2>&1 || true
rm -f /tmp/ibtrader-health-msg.$$
exit 0

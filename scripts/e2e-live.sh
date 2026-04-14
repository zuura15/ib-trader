#!/usr/bin/env bash
# Orchestrate the full live stack for Playwright E2E tests.
#
# Starts Redis, ib-engine, ib-api (in that order, each with a health
# check) and then invokes Playwright. Services that were already running
# when the script started are reused as-is and NOT killed at teardown —
# only services this script brought up get stopped.
#
# Usage:
#   scripts/e2e-live.sh [extra playwright args]
#
# Env overrides:
#   IB_TRADER_E2E_KEEP_RUNNING=1   Do not tear down anything on exit.
#   IB_TRADER_E2E_EXTRA_PW_ARGS    Passed through to playwright test.
#   HEALTH_TIMEOUT=60              Seconds to wait for each healthcheck.
#
# Exits non-zero if any service fails to come up or Playwright fails.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

REDIS_CLI="$REPO_ROOT/.local/bin/redis-cli"
REDIS_SERVER="$REPO_ROOT/.local/bin/redis-server"
REDIS_CONF="$REPO_ROOT/config/redis.conf"

API_HOST="127.0.0.1"
API_PORT="${IB_TRADER_API_PORT:-8000}"
ENGINE_INTERNAL_PORT="${IB_TRADER_ENGINE_INTERNAL_PORT:-8081}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"

REDIS_STARTED=0
ENGINE_STARTED_PID=""
API_STARTED_PID=""
BOTS_STARTED_PID=""

log() { printf '[e2e-live] %s\n' "$*" >&2; }

wait_for() {
    # wait_for NAME CHECK_CMD [timeout]
    local name="$1" check="$2" t="${3:-$HEALTH_TIMEOUT}"
    local i=0
    while (( i < t )); do
        if bash -c "$check" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    log "TIMEOUT waiting for $name after ${t}s"
    return 1
}

ensure_redis() {
    if "$REDIS_CLI" ping >/dev/null 2>&1; then
        log "Redis already running — reusing"
        return 0
    fi
    log "Starting Redis..."
    mkdir -p run/redis-data logs
    "$REDIS_SERVER" "$REDIS_CONF" --daemonize yes
    REDIS_STARTED=1
    wait_for "redis" "\"$REDIS_CLI\" ping" 10
}

ensure_engine() {
    if curl -fsS "http://$API_HOST:$ENGINE_INTERNAL_PORT/engine/health" >/dev/null 2>&1; then
        log "Engine already running on :$ENGINE_INTERNAL_PORT — reusing"
        return 0
    fi
    log "Starting engine (logs → logs/e2e-engine.log)..."
    mkdir -p logs
    # Run the engine detached from the script's pgroup so Ctrl+C on
    # Playwright doesn't kill it before we trap it ourselves.
    (
        uv run ib-engine >logs/e2e-engine.log 2>&1 &
        echo $! >run/e2e-engine.pid
    )
    ENGINE_STARTED_PID="$(cat run/e2e-engine.pid)"
    wait_for "engine (:$ENGINE_INTERNAL_PORT)" \
        "curl -fsS http://$API_HOST:$ENGINE_INTERNAL_PORT/engine/health" \
        "$HEALTH_TIMEOUT"
}

ensure_api() {
    if curl -fsS "http://$API_HOST:$API_PORT/api/status" >/dev/null 2>&1; then
        log "API already running on :$API_PORT — reusing"
        return 0
    fi
    log "Starting API (logs → logs/e2e-api.log)..."
    mkdir -p logs
    (
        uv run ib-api >logs/e2e-api.log 2>&1 &
        echo $! >run/e2e-api.pid
    )
    API_STARTED_PID="$(cat run/e2e-api.pid)"
    wait_for "api (:$API_PORT)" \
        "curl -fsS http://$API_HOST:$API_PORT/api/status" \
        "$HEALTH_TIMEOUT"
}

ensure_bot_runner() {
    # The bot runner (ib-bots) subscribes to Redis control streams so
    # start/stop/force-buy actually wake a running bot task. Without it
    # the API can flip SQLite status but nothing executes. No HTTP
    # health endpoint — health is "pid is alive + heartbeat written".
    if pgrep -f "ib_trader.bots.main" >/dev/null 2>&1; then
        log "Bot runner already running — reusing"
        return 0
    fi
    log "Starting bot runner (logs → logs/e2e-bots.log)..."
    mkdir -p logs
    (
        uv run ib-bots >logs/e2e-bots.log 2>&1 &
        echo $! >run/e2e-bots.pid
    )
    BOTS_STARTED_PID="$(cat run/e2e-bots.pid)"
    # Liveness check: bot runner writes its SystemHeartbeat row within
    # ~2 s of startup. We poll the api for it to confirm the runner is
    # actually up before Playwright starts driving bot lifecycle events.
    wait_for "bot runner (BOT_RUNNER heartbeat)" \
        "curl -fsS http://$API_HOST:$API_PORT/api/status | grep -q 'BOT_RUNNER'" \
        15
}

teardown() {
    local rc=$?
    if [[ "${IB_TRADER_E2E_KEEP_RUNNING:-0}" == "1" ]]; then
        log "KEEP_RUNNING=1 — leaving services up"
        exit "$rc"
    fi
    # Only kill what we started. Reuses untouched.
    if [[ -n "$BOTS_STARTED_PID" ]]; then
        log "Stopping bot runner (pid=$BOTS_STARTED_PID)"
        kill "$BOTS_STARTED_PID" 2>/dev/null || true
    fi
    if [[ -n "$API_STARTED_PID" ]]; then
        log "Stopping API (pid=$API_STARTED_PID)"
        kill "$API_STARTED_PID" 2>/dev/null || true
    fi
    if [[ -n "$ENGINE_STARTED_PID" ]]; then
        log "Stopping engine (pid=$ENGINE_STARTED_PID)"
        kill "$ENGINE_STARTED_PID" 2>/dev/null || true
    fi
    if (( REDIS_STARTED == 1 )); then
        log "Stopping Redis"
        "$REDIS_CLI" shutdown nosave >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap teardown EXIT INT TERM

ensure_redis
ensure_engine
ensure_api
ensure_bot_runner

log "Stack up. Handing off to Playwright..."
cd frontend
# shellcheck disable=SC2086
npx playwright test --config=playwright.live.config.ts ${IB_TRADER_E2E_EXTRA_PW_ARGS:-} "$@"

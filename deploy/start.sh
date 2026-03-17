#!/bin/bash
# Start IB Trader services (engine, API, frontend) as background processes.
#
# The daemon is NOT started here because it runs a Textual TUI that
# requires its own terminal. Run it separately if needed:
#   .venv/bin/ib-daemon        (in its own terminal)
#
# Usage:
#   ./deploy/start.sh          # live trading (default)
#   ./deploy/start.sh --paper  # paper trading
#
# To stop everything:
#   ./deploy/stop.sh

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv/bin"
LOGS="$DIR/logs"
FRONTEND_DIR="$DIR/prototypes/claude-proto/ib-control-center-prototypes"
PIDFILE="$DIR/logs/.pids"

mkdir -p "$LOGS"

PAPER=""
if [ "$1" = "--paper" ]; then
    PAPER="--paper"
fi

# Kill any leftover processes from a previous run
"$DIR/deploy/stop.sh" 2>/dev/null || true

echo "Starting IB Trader platform..."
echo ""

# Start engine
echo "[1/3] Starting Engine..."
$VENV/ib-engine $PAPER >> "$LOGS/engine.log" 2>&1 &
echo $! >> "$PIDFILE"
ENGINE_PID=$!

sleep 2

# Start API
echo "[2/3] Starting API server on port 8000..."
$VENV/ib-api >> "$LOGS/api.log" 2>&1 &
echo $! >> "$PIDFILE"
API_PID=$!

sleep 1

# Start frontend
echo "[3/3] Starting Frontend dev server..."
cd "$FRONTEND_DIR"
VITE_DATA_MODE=live npm run dev >> "$LOGS/frontend.log" 2>&1 &
echo $! >> "$PIDFILE"
FRONTEND_PID=$!
cd "$DIR"

echo ""
echo "Services running:"
echo "  Engine:   pid=$ENGINE_PID"
echo "  API:      pid=$API_PID     → http://localhost:8000"
echo "  Frontend: pid=$FRONTEND_PID  → http://localhost:5173"
echo ""
echo "Logs:"
echo "  tail -f logs/engine.log"
echo "  tail -f logs/api.log"
echo "  tail -f logs/frontend.log"
echo ""
echo "Stop all:  ./deploy/stop.sh"
echo ""
echo "NOTE: Daemon has a TUI — run it separately in its own terminal:"
echo "  .venv/bin/ib-daemon $PAPER"

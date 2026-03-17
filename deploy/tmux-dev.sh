#!/bin/bash
# Launch all IB Trader services in a single tmux session.
# All process output is tee'd to logs/ so you can always read them with:
#   tail -f logs/engine.log
#   tail -f logs/api.log
#   tail -f logs/daemon.log
#   tail -f logs/frontend.log
#   tail -f logs/bots.log
#
# Usage:
#   ./deploy/tmux-dev.sh          # paper trading (default)
#   ./deploy/tmux-dev.sh --live   # live trading
#
# Layout (Window 1 "main"):
#   ┌──────────────────┬──────────────────┐
#   │  ENGINE          │  API             │
#   │                  │                  │
#   ├──────────────────┼──────────────────┤
#   │  DAEMON          │  FRONTEND        │
#   │                  │                  │
#   └──────────────────┴──────────────────┘
#
# Window 2 "bots": bot runner (full screen)

set -e

SESSION="trader"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv/bin"
LOGS="$DIR/logs"
FRONTEND_DIR="$DIR/prototypes/claude-proto/ib-control-center-prototypes"

mkdir -p "$LOGS"

PAPER="--paper"
if [ "$1" = "--live" ]; then
    PAPER=""
    echo "WARNING: Starting in LIVE trading mode!"
    read -p "Press Enter to confirm or Ctrl+C to abort..."
fi

# Kill existing session if running
tmux kill-session -t "$SESSION" 2>/dev/null || true

# ── Window 1 "main": engine, api, daemon, frontend ──

tmux new-session -d -s "$SESSION" -n "main" -c "$DIR"
tmux select-pane -T "ENGINE"
tmux send-keys -t "$SESSION" "$VENV/ib-engine $PAPER 2>&1 | tee $LOGS/engine.log" Enter

# Split right: API server
tmux split-window -h -t "$SESSION" -c "$DIR"
tmux select-pane -T "API"
tmux send-keys -t "$SESSION" "sleep 2 && $VENV/ib-api 2>&1 | tee $LOGS/api.log" Enter

# Split bottom-left: daemon
tmux select-pane -t "$SESSION:main.0"
tmux split-window -v -t "$SESSION" -c "$DIR"
tmux select-pane -T "DAEMON"
tmux send-keys -t "$SESSION" "sleep 3 && $VENV/ib-daemon $PAPER 2>&1 | tee $LOGS/daemon.log" Enter

# Split bottom-right: frontend
tmux select-pane -t "$SESSION:main.2"
tmux split-window -v -t "$SESSION" -c "$FRONTEND_DIR"
tmux select-pane -T "FRONTEND"
tmux send-keys -t "$SESSION" "VITE_DATA_MODE=live npm run dev 2>&1 | tee $LOGS/frontend.log" Enter

# Even out the layout
tmux select-layout -t "$SESSION:main" tiled

# ── Window 2 "bots": bot runner ──

tmux new-window -t "$SESSION" -n "bots" -c "$DIR"
tmux select-pane -T "BOTS"
tmux send-keys -t "$SESSION:bots" "sleep 3 && $VENV/ib-bots 2>&1 | tee $LOGS/bots.log" Enter

# ── Focus on window 1 ──

tmux select-window -t "$SESSION:main"

echo ""
echo "IB Trader session '$SESSION' started."
echo ""
echo "  Logs (always available, even if tmux is messy):"
echo "    tail -f $LOGS/engine.log"
echo "    tail -f $LOGS/api.log"
echo "    tail -f $LOGS/daemon.log"
echo "    tail -f $LOGS/frontend.log"
echo "    tail -f $LOGS/bots.log"
echo ""
echo "  Tmux controls:"
echo "    Click pane to select (mouse enabled)"
echo "    Ctrl+b z    — zoom/unzoom pane to full screen"
echo "    Ctrl+b n/p  — switch windows (main / bots)"
echo "    Ctrl+b d    — detach (services keep running)"
echo "    tmux kill-session -s $SESSION  — stop everything"
echo ""

# Attach
tmux attach -t "$SESSION"

#!/bin/bash
# Stop all IB Trader services.
#
# Usage:
#   ./deploy/stop.sh

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/logs/.pids"

if [ ! -f "$PIDFILE" ]; then
    echo "No running services found."
    exit 0
fi

echo "Stopping IB Trader services..."

while read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        echo "  Stopped pid=$pid"
    fi
done < "$PIDFILE"

rm -f "$PIDFILE"

# Also kill any orphaned processes on our ports
for port in 8000 5173; do
    pid=$(lsof -t -i:$port 2>/dev/null)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        echo "  Killed orphan on port $port (pid=$pid)"
    fi
done

echo "All services stopped."

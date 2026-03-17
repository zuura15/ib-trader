#!/bin/bash
# Install and enable IB Trader systemd services.
# Run as: sudo bash deploy/setup.sh

set -e

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES=(ib-engine ib-api ib-daemon ib-bots)

echo "Installing systemd service files..."
for svc in "${SERVICES[@]}"; do
    sudo cp "$DEPLOY_DIR/$svc.service" /etc/systemd/system/
    echo "  Installed $svc.service"
done

sudo systemctl daemon-reload

echo ""
echo "Services installed. Usage:"
echo ""
echo "  Start all:     sudo systemctl start ib-engine ib-api ib-daemon ib-bots"
echo "  Stop all:      sudo systemctl stop ib-bots ib-daemon ib-api ib-engine"
echo "  Enable boot:   sudo systemctl enable ib-engine ib-api ib-daemon ib-bots"
echo "  Check status:  sudo systemctl status ib-engine ib-api ib-daemon ib-bots"
echo "  View logs:     journalctl -u ib-engine -f"
echo ""
echo "Start order: ib-engine first (other services depend on it)."
echo "Stop order:  ib-bots, ib-daemon, ib-api, ib-engine last."
echo ""
echo "Remove --paper from the .service files for live trading."

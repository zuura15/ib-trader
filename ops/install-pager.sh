#!/usr/bin/env bash
# One-shot installer for the external pager.
# Prompts for the Healthchecks.io ping URL and ntfy.sh topic URL,
# writes them to ~/.config/ibtrader-pager.env (mode 600), installs
# the systemd user unit files, and enables the timer.
#
# See GH #47 for design + prerequisite setup (HC account, ntfy app).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
OPS_DIR="$REPO_ROOT/ops"
PAGER_ENV="$HOME/.config/ibtrader-pager.env"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "IB Trader external pager installer"
echo "  repo: $REPO_ROOT"
echo "  env file: $PAGER_ENV"
echo "  systemd units: $SYSTEMD_USER_DIR"
echo

# ---------------------------------------------------------------------------
# 1. env file (HC ping URL + ntfy topic URL)
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$PAGER_ENV")"

if [[ -f "$PAGER_ENV" ]]; then
    echo "Existing env file found at $PAGER_ENV:"
    grep -E '^(HC_PING_URL|NTFY_TOPIC_URL)=' "$PAGER_ENV" | sed 's/=.*$/=<redacted>/' || true
    read -r -p "Reuse it? [Y/n] " reuse
    reuse=${reuse:-Y}
else
    reuse=N
fi

if [[ "${reuse^^}" != Y* ]]; then
    echo
    echo "-- Healthchecks.io --"
    echo "  Create a check at https://healthchecks.io (free tier)."
    echo "  Period: 60s, Grace: 120s. Add 'ntfy' to Integrations"
    echo "  pointing at the SAME topic URL you'll enter below."
    echo "  Copy the ping URL now."
    read -r -p "  HC_PING_URL (https://hc-ping.com/<uuid>): " HC_PING_URL
    echo
    echo "-- ntfy.sh --"
    echo "  Install the ntfy Android app (Play Store / F-Droid)."
    echo "  Subscribe to a NEW topic named like 'ibtrader-<10 random>'"
    echo "  (the topic name is the only auth — keep it secret)."
    read -r -p "  NTFY_TOPIC_URL (https://ntfy.sh/<topic>): " NTFY_TOPIC_URL

    if [[ -z "$HC_PING_URL" || -z "$NTFY_TOPIC_URL" ]]; then
        echo "Both URLs are required. Aborting." >&2
        exit 1
    fi

    umask 077
    cat > "$PAGER_ENV" <<EOF
# IB Trader pager config. See GH #47. Mode 600. Never committed.
HC_PING_URL=$HC_PING_URL
NTFY_TOPIC_URL=$NTFY_TOPIC_URL
EOF
    chmod 600 "$PAGER_ENV"
    echo "Wrote $PAGER_ENV (mode 600)."
fi

# ---------------------------------------------------------------------------
# 2. systemd user units
# ---------------------------------------------------------------------------

mkdir -p "$SYSTEMD_USER_DIR"
cp -v "$OPS_DIR/ibtrader-health.service" "$SYSTEMD_USER_DIR/"
cp -v "$OPS_DIR/ibtrader-health.timer"   "$SYSTEMD_USER_DIR/"
chmod +x "$OPS_DIR/health_check.sh" "$OPS_DIR/maint" "$OPS_DIR/install-pager.sh"

systemctl --user daemon-reload
systemctl --user enable --now ibtrader-health.timer
systemctl --user list-timers ibtrader-health.timer --no-pager || true

# loginctl linger is optional.
#
# With the wrapper-alive gate in ops/health_check.sh, linger is
# safe either way:
#   - linger OFF: timer runs only while you have an active
#     session. Closing a single terminal (e.g., the one running
#     `make dev`) doesn't end the session — GUI or another SSH
#     keeps it alive — so the timer naturally survives the usual
#     Ctrl+C/restart dev loop. Full logout stops it; next login
#     auto-restarts it (enabled).
#   - linger ON: timer runs across full logouts and boots. Between
#     boot and operator login, it ticks, finds no `make dev`
#     wrapper, and stays silent — no bogus pages. Slightly more
#     robust; costs ~nothing. Enable with:
#         sudo loginctl enable-linger $USER
#
# Either choice works. Leaving the default (OFF) alone unless the
# operator explicitly wants linger.
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    echo
    echo "NOTE: loginctl Linger is ON for '$USER' — timer will run"
    echo "      across logouts and reboots. Wrapper-alive gate keeps"
    echo "      it quiet during unattended periods. All good."
else
    echo
    echo "NOTE: loginctl Linger is OFF for '$USER'. The timer runs only"
    echo "      while you have an active session (closing one terminal"
    echo "      is fine; full logout stops it, next login restarts it)."
    echo "      If you'd rather have it run across logouts / reboots:"
    echo "          sudo loginctl enable-linger $USER"
    echo "      (safe either way — the wrapper-alive gate keeps the"
    echo "       pager silent when \`make dev\` isn't running.)"
fi

echo
echo "Done. First tick fires ~10s after boot; subsequent ticks every 60s."
echo
echo "Verify:"
echo "  systemctl --user status ibtrader-health.timer"
echo "  journalctl --user -u ibtrader-health.service -f"
echo
echo "Next steps:"
echo "  - Wait ~1 min; your Healthchecks.io dashboard should turn green."
echo "  - No ntfy push expected when healthy."
echo "  - Test: ops/maint start 2m && pkill -f ib-engine  # no page for 2m,"
echo "    then page once window expires."

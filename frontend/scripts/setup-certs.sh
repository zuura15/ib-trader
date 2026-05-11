#!/usr/bin/env bash
# Generate a locally-trusted Vite dev cert via mkcert.
#
# Why: ``@vitejs/plugin-basic-ssl`` produces a fresh self-signed cert on
# every restart. Browsers don't trust it (CA isn't in any store), the
# "Your connection is not private" warning fires every load, and we
# semi-regularly see Vite hang on the TLS handshake from cold-start
# tabs. mkcert generates a cert signed by a local CA that the system
# trust store accepts after one install.
#
# Each box runs this script once. The cert covers localhost + the
# box's actual LAN IP + hostname + (optionally) Tailscale IP if the
# ``tailscale`` CLI is present.
#
# Prereqs (per box):
#   sudo apt install mkcert libnss3-tools
#   mkcert -install   # one-time CA install in OS/browser trust stores
#                      # — needs sudo for system-level on Linux; without
#                      # sudo it still installs to per-user NSS so
#                      # Chrome/Chromium trust the CA.
#
# Output:
#   frontend/certs/dev.pem        — leaf certificate
#   frontend/certs/dev-key.pem    — leaf private key
#
# Both files are gitignored. Vite reads them via ``vite.config.ts``
# and falls back to ``@vitejs/plugin-basic-ssl`` if either is missing.

set -euo pipefail

if ! command -v mkcert >/dev/null 2>&1; then
  echo "error: mkcert not on PATH. Install with:" >&2
  echo "  sudo apt install mkcert libnss3-tools  &&  mkcert -install" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="$SCRIPT_DIR/../certs"
mkdir -p "$CERTS_DIR"

# Build the SAN list from what this box actually answers to. The
# script is conservative — if it can't detect a value, it's omitted
# (a SAN you don't put in won't auto-fail validation; a SAN you put
# in but isn't reachable just isn't used).
SANS=(localhost 127.0.0.1 ::1)

# LAN hostname — usually the simple hostname is what browsers type.
if HOST_SHORT="$(hostname -s 2>/dev/null)" && [[ -n "$HOST_SHORT" ]]; then
  SANS+=("$HOST_SHORT")
fi

# Primary LAN IP — ``hostname -I`` prints all assigned IPs; take the
# first private-range one. Skip Tailscale's 100.64/10 range and IPv6.
LAN_IP=""
for ip in $(hostname -I 2>/dev/null || true); do
  if [[ "$ip" =~ ^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\. ]]; then
    LAN_IP="$ip"
    break
  fi
done
if [[ -n "$LAN_IP" ]]; then
  SANS+=("$LAN_IP")
fi

# Tailscale IPv4 if available — handy for cross-machine access from
# laptops/phones that are on the tailnet.
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -n "$TS_IP" ]]; then
    SANS+=("$TS_IP")
  fi
fi

echo "Generating cert for SANs: ${SANS[*]}"

mkcert \
  -cert-file "$CERTS_DIR/dev.pem" \
  -key-file  "$CERTS_DIR/dev-key.pem" \
  "${SANS[@]}"

chmod 600 "$CERTS_DIR/dev-key.pem"
chmod 644 "$CERTS_DIR/dev.pem"

echo
echo "Done."
echo "  cert: $CERTS_DIR/dev.pem"
echo "  key:  $CERTS_DIR/dev-key.pem"
echo
echo "Restart vite ($ npm run dev) and load https://${LAN_IP:-localhost}:5173"
echo "— browsers with the mkcert CA installed should show a green padlock."

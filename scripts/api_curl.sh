#!/usr/bin/env bash
# curl wrapper for ib-api that adds the access key from .env.
#
#   scripts/api_curl.sh [curl options...] http://127.0.0.1:8000/api/status
#
# The key (API_SECRET_KEY in the repo's .env, or $API_SECRET_KEY if set) is
# handed to curl on stdin via --config, so it never shows up in `ps` output.
# Exits 100 when no key is available (outside curl's own exit-code range).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${IB_TRADER_ENV_FILE:-$ROOT/.env}"

key="${API_SECRET_KEY:-}"
if [[ -z "$key" && -r "$ENV_FILE" ]]; then
    key="$(sed -n 's/^[[:space:]]*API_SECRET_KEY[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n 1)"
    key="${key%\"}"; key="${key#\"}"; key="${key%\'}"; key="${key#\'}"
fi
if [[ -z "$key" ]]; then
    echo "api_curl: API_SECRET_KEY not found in $ENV_FILE" >&2
    exit 100
fi

printf 'header = "Authorization: Bearer %s"\n' "$key" | exec curl --config - "$@"

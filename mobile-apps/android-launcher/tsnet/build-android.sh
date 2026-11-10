#!/usr/bin/env bash
# Bind the userspace tsnet node to an Android AAR (arm64-v8a and x86_64).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AAR="$ROOT/app/libs/tsnetmobile.aar"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
if [[ -z "${ANDROID_NDK_HOME:-}" && -d "$ANDROID_HOME/ndk/29.0.14206865" ]]; then
  export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/29.0.14206865"
fi
export PATH="${HOME}/go-sdk/bin:${HOME}/go/bin:${PATH}"
export GOMAXPROCS="${GOMAXPROCS:-2}"

if [[ -f "$AAR" ]]; then
  newest=0
  while IFS= read -r stamp; do
    if [[ "$stamp" -gt "$newest" ]]; then newest="$stamp"; fi
  done < <(find "$ROOT/tsnet" -maxdepth 1 \( -name '*.go' -o -name 'go.mod' -o -name 'go.sum' \) -printf '%T@\n' | cut -d. -f1)
  aar_time="$(stat -c %Y "$AAR")"
  if [[ "$aar_time" -ge "$newest" ]]; then
    echo "tsnet AAR is up to date ($AAR)"
    exit 0
  fi
fi

command -v go >/dev/null
command -v gomobile >/dev/null || {
  go install golang.org/x/mobile/cmd/gomobile@v0.0.0-20261011034528-197770444867
  go install golang.org/x/mobile/cmd/gobind@v0.0.0-20261011034528-197770444867
}
gomobile init
mkdir -p "$ROOT/app/libs"
cd "$ROOT/tsnet"
gomobile bind -target=android/arm64,android/amd64 -androidapi 26 -o "$AAR" .
echo "wrote $AAR"

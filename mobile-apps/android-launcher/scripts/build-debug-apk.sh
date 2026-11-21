#!/usr/bin/env bash
# Build tsnetmobile.aar and the debug APK from a clean tree.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$ANDROID_HOME/ndk/29.0.14206865}"
export PATH="${HOME}/go-sdk/bin:${HOME}/go/bin:${PATH}"

if [[ ! -f "$ROOT/local.properties" ]]; then
  printf 'sdk.dir=%s\n' "$ANDROID_HOME" > "$ROOT/local.properties"
fi

bash "$ROOT/tsnet/build-android.sh"
cd "$ROOT"
chmod +x ./gradlew
./gradlew --no-daemon :app:testDebugUnitTest :app:assembleDebug
APK="$ROOT/app/build/outputs/apk/debug/app-debug.apk"
test -f "$APK"
if mkdir -p /opt/cursor/artifacts 2>/dev/null; then
  cp "$APK" /opt/cursor/artifacts/android-launcher-debug.apk
  echo "artifact /opt/cursor/artifacts/android-launcher-debug.apk"
fi
echo "APK $APK"

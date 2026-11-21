#!/usr/bin/env bash
# Install the toolchain used to build the launcher. Versions match the README.
set -euo pipefail

GO_VERSION=1.27.3
NDK_VERSION=29.0.14206865
CMDLINE_TOOLS_REV=16111833
ANDROID_PLATFORM=platforms;android-35
BUILD_TOOLS=build-tools;35.0.0

SDK="${ANDROID_HOME:-$HOME/android-sdk}"
mkdir -p "$SDK/cmdline-tools" "$HOME"

if [[ ! -x "$HOME/go-sdk/bin/go" ]]; then
  tmp="$(mktemp)"
  curl -fsSL -o "$tmp" "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
  rm -rf "$HOME/go-sdk"
  mkdir -p "$HOME/go-sdk"
  tar -C "$HOME/go-sdk" --strip-components=1 -xzf "$tmp"
  rm -f "$tmp"
fi
export PATH="$HOME/go-sdk/bin:$HOME/go/bin:$PATH"

if [[ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]]; then
  tmp="$(mktemp)"
  curl -fL -o "$tmp" "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_REV}_latest.zip"
  rm -rf /tmp/cmdtools-extract
  mkdir -p /tmp/cmdtools-extract
  unzip -q "$tmp" -d /tmp/cmdtools-extract
  rm -rf "$SDK/cmdline-tools/latest"
  mv /tmp/cmdtools-extract/cmdline-tools "$SDK/cmdline-tools/latest"
  rm -f "$tmp"
fi

export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"
yes | "$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" --licenses >/dev/null || true
"$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" "$ANDROID_PLATFORM" "$BUILD_TOOLS" "platform-tools" "ndk;${NDK_VERSION}"
export ANDROID_NDK_HOME="$SDK/ndk/${NDK_VERSION}"

go install golang.org/x/mobile/cmd/gomobile@v0.0.0-20261011034528-197770444867
go install golang.org/x/mobile/cmd/gobind@v0.0.0-20261011034528-197770444867
gomobile init

echo "JDK: $(java -version 2>&1 | head -1)"
echo "Go: $(go version)"
echo "NDK: $ANDROID_NDK_HOME"
echo "SDK: $ANDROID_HOME"

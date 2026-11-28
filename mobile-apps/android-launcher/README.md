# IB Launcher

Sideloaded Android home screen. It embeds a Tailscale tsnet node (userspace only) and opens the IB Trader web app, served by ib-api, in a WebView. Ulagaariyan and SSH are small tiles on the same node. There is no native copy of the trading UI.

The activity handles `MAIN` / `LAUNCHER` and `HOME` / `DEFAULT`, locked to portrait. The SPA picks its phone layout itself: `frontend/src/app/layoutBreakpoint.ts` uses `(max-width: 1100px)`, and `frontend/index.html` sets `width=device-width`. A portrait phone is well under that width. This tree does not modify the frontend.

## Build from scratch

Verified on Linux x86_64 with:

| Tool | Version |
| --- | --- |
| JDK | 21 (build used 21.0.12; Temurin or distro OpenJDK 21) |
| Go | 1.27.3 (`https://go.dev/dl/go1.27.3.linux-amd64.tar.gz`) |
| Android cmdline-tools | `commandlinetools-linux-16111833_latest.zip` (package 23.0.0) |
| Android SDK platform | `platforms;android-35` |
| Build-tools | `35.0.0` |
| NDK | `29.0.14206865` (side-by-side, r29) |
| gomobile / gobind | `golang.org/x/mobile` `v0.0.0-20261011034528-197770444867` |
| tsnet module | `tailscale.com` `v1.104.5` |
| Android Gradle Plugin | 8.13.2 |
| Gradle | 8.13 |
| Kotlin | 2.2.21 |

The bind targets are `android/arm64` and `android/amd64` (`arm64-v8a`, `x86_64`), `androidapi` 26. `minSdk` is 26.

```bash
# JDK 21 must already be on PATH (`java -version`).
cd mobile-apps/android-launcher
bash scripts/bootstrap-toolchain.sh

export ANDROID_HOME="$HOME/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/29.0.14206865"
export PATH="$HOME/go-sdk/bin:$HOME/go/bin:$PATH"

bash scripts/build-debug-apk.sh
```

`scripts/build-debug-apk.sh` writes `local.properties` (gitignored), runs `tsnet/build-android.sh` (`gomobile bind` → `app/libs/tsnetmobile.aar`), then `./gradlew :app:testDebugUnitTest :app:assembleDebug`. The debug APK is `app/build/outputs/apk/debug/app-debug.apk`.

Go tests only, without the SDK:

```bash
cd mobile-apps/android-launcher/tsnet
go test ./...
```

Nothing under this directory is a secret. Do not commit an auth key, a `.aar` you built on a machine that had one, or `local.properties`.

## Sideload

On the phone: Settings → Security (or Apps) → allow install from unknown sources for the app you use to open the APK (Files, Chrome, or adb).

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Open **IB Launcher** from the drawer, or set it as the default home app when Android asks (the `HOME` category). Both entry points are the same activity. Allow the notification so the userspace node can keep a foreground service. That service is `specialUse`. It is not a VPN.

## Tailscale login

First launch offers both:

1. **Sign in with Tailscale.** The app starts the node with no auth key, shows the login URL, and can open it in the system browser. It polls until the backend state is `Running`.
2. **Paste an auth key** (`tskey-auth-...`) on that screen or under Settings. The key is sealed with an Android Keystore AES-GCM key. It is not written into the repo.

The node key and machine key live in app-private storage as AES-GCM blobs. The data key is wrapped by the Keystore. See [docs/credentials.md](docs/credentials.md) and [docs/tsnet-networking.md](docs/tsnet-networking.md).

After `Running`, kill the app and open it again. It reconnects with no prompt and does not send the auth key. A one-shot key is only for the registration that created the node.

Settings → Tailscale shows logged in or out, the node name, tailnet IPs, key expiry (or —), and the last error. **Re-login** starts the interactive URL again or takes a new key (that replaces the node state). **Clear stored credentials** asks for confirmation, logs out, wipes the state directory and the saved key, and returns to first launch. Origin and SSH fields stay.

Optional control URL: leave it blank for Tailscale's default coordination server, or set a Headscale URL before signing in. Hostname changes apply on **Save and restart node**.

## Point the WebView at prod

In Settings → IB Trader, set the origin to the MagicDNS name or the 100.x address of the machine running ib-api, for example:

```text
http://ib-api.tailnet-name.ts.net:8000
http://100.x.y.z:8000
```

A missing port becomes 8000. The optional bearer is sent as `Authorization: Bearer …` on HTTP requests to that host, and as `?token=` on `/ws`, because that is what `APIKeyMiddleware` checks. Prod currently does not enforce it; see below.

Open the **IB Trader** tile. The WebView loads that origin with JavaScript and DOM storage on, through `ProxyController` aimed at `http://127.0.0.1:<tsnet-proxy>`. Back walks WebView history before leaving the tile. If the node is down, the origin is empty, the proxy feature is missing, or the load fails, the tile shows an error and Reload.

## First-test checklist

1. Install the APK and open IB Launcher (drawer or default home).
2. Sign in with the login URL, or paste an auth key. Confirm Status shows `Running`, one or more tailnet IPs, peers, and "userspace (no VPN, no TUN)".
3. Force-stop or swipe the app away, open it again, and confirm it reconnects with no login prompt and no auth key field.
4. Set the prod origin on port 8000. From a tailnet peer, the same URL should answer.
5. Open IB Trader. The page loads in portrait and the SPA uses the mobile layout (viewport under 1100px). Quotes or bot rows move when `/ws` is live.
6. Ulagaariyan: set a URL that is only reachable on the tailnet and Fetch. The status line is the HTTP code, not a phone-network error.
7. SSH: Test TCP against a tailnet host and port. With user, password, and a command, Run opens one SSH exec over the tsnet socket. Host keys are not checked.
8. Clear stored credentials, confirm, and check that the next screen is first launch.

## Security

ib-api binds `0.0.0.0` by default (`ib_trader/api/main.py`, `--host`, port 8000). `main()` calls `create_app(session_factory, cors_origins=cors_origins)` and does not pass `api_key`. `APIKeyMiddleware` is installed only when that argument is set, so `API_SECRET_KEY` in `.env` is not enforced. Anyone who can open port 8000 can call the API and `/ws`.

Put the API host on the tailnet and write an ACL so only this phone's node can reach `:8000`. Then pass the key into `create_app` (this repo's Python is intentionally unchanged) and set the same value as the bearer in the launcher. Until then, treat the tailnet ACL as the only access control, and do not publish port 8000 on a public interface.

The node is an app-level tailnet member, not a device VPN. Other apps are not on the tailnet. The WebView proxy refuses to load if it cannot force the proxy.

## Gaps

- No handset was attached in the build environment, so WebView, `ProxyController`, and the Keystore path were not exercised on a device. The proxy's WebSocket `CONNECT` behavior is covered by `go test`. Keystore encryption is the same layout as the JVM `AesGcmAead` tests; the Keystore provider itself is Android-only.
- SSH uses `InsecureIgnoreHostKey`. The screen says so.
- Filch log files can remain plaintext in app-private storage while the process is alive. Node and machine keys are ciphertext. See [docs/credentials.md](docs/credentials.md).
- ABIs are arm64-v8a and x86_64 only.
- A crash in the moment after `Running` and before the registration flag is sealed shows the first-launch screen once more. State is still on disk; signing in again reuses it.

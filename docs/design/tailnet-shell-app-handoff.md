# Handoff: Tailnet shell app (Android)

Status: design discussion, nothing built yet on main. Written 2026-11-10 to
resume from another machine.

## Goal

A native Android "master client" for the operator's private Tailscale
network. It is a shell app: it owns the tailnet connection, and each
internally hosted service (IB Trader first) is a module inside it. More
vertical modules get added later. Constraints from the operator:

- The servers are never exposed to the internet. Tailscale only.
- Tailscale must not run for the whole phone. Only this app joins the tailnet.

## Decision so far: embed Tailscale in the app (tsnet)

Two ways were considered.

1. Official Tailscale Android app with app-based split tunneling (only this
   app routed). Zero networking code, but it occupies the phone's single VPN
   slot and depends on the Tailscale app.
2. **Chosen direction:** embed Tailscale's Go library (`tailscale.com/tsnet`)
   in the app via gomobile. The app is its own tailnet device, with userspace
   networking: no VpnService, no TUN, rest of the phone untouched. Picked
   because the shell is meant to be reused for many internal apps.

## How option 2 works: the local proxy

Embedded tsnet runs its own TCP/IP stack inside the app's memory. Android has
no route to the `100.x` tailnet addresses, so the app's normal networking
(OkHttp, WebSocket client, WebView) can't reach them directly.

The bridge is a small HTTP proxy that the Go library runs on
`127.0.0.1:<random port>`:

```
OkHttp / WebView ──► 127.0.0.1:port (proxy, inside the app)
                           │  dials via the embedded tailnet stack
                           ▼
                     local-prod:8000 (over Tailscale)
```

- Plain HTTP is forwarded; WebSockets go through `CONNECT`.
- WebView only accepts a proxy setting (`androidx.webkit.ProxyController`),
  so the proxy is the one mechanism that covers native screens and WebViews.
- Only traffic the app explicitly sends to the proxy uses it. Other apps on
  the phone, and the app's own non-tailnet traffic, use the normal network.
- Risk: any app on the phone can connect to `127.0.0.1:<port>`. Required
  hardening: a per-launch proxy secret (`Proxy-Authorization`) known only to
  the app, and a host allowlist (refuse anything not on it).

## Proposed architecture

**Shell (built once)**

- One embedded tsnet node (one tailnet device, e.g. `zuura-client`); login
  once (interactive URL or auth key); node keys sealed with Android Keystore.
- Gateway = the hardened local proxy. The only path to the tailnet.
- Foreground service (`specialUse`, not VPN) keeps the node up; status
  notification.
- Home screen: one tile per module, plus Status (node, IPs, peers, which hosts
  are up) and Settings.
- Shared services for modules: proxied HTTP/WS client, secure token storage,
  notifications, optional biometric unlock.

**Modules (added over time)**

- Each declares name/icon, the tailnet hosts it needs (merged into the
  allowlist), and either native screens or a URL to wrap in a WebView.
- Wrapping an internal web app is cheap (an afternoon). Native screens only
  where they add value.
- IB Trader = first module: wrap the existing mobile web layout first
  (`frontend/src/layout/MobileLayout.tsx`, Pos / Graphs / Watch, picked by
  `frontend/src/app/layoutBreakpoint.ts` under 1100 px), then add native
  pieces (positions, B/S/C, push alerts) as needed. A fully native UI means
  re-doing the charts (lightweight-charts), S/R and the signal logic, which
  is most of the effort.

## Existing starting point (another agent's branch)

Branch `origin/cursor/android-launcher-2d37`, one commit (`08ab0cb`), all under
`mobile-apps/android-launcher/`. Not merged; touches no Python or frontend.
It is essentially v0 of this design:

- `tsnet/` (Go): `node.go` (tsnet userspace node, status, login, logout),
  `proxy.go` (loopback HTTP + CONNECT proxy, optional bearer injection for
  the origin), `store.go` (AES-GCM encrypted tsnet state), `dialops.go`
  (Fetch / ProbeTCP / SSHExec), with Go tests.
- `app/` (Kotlin, Compose): `TailnetRuntime` + `TailnetService`,
  `CredentialStore` (Keystore-wrapped data key), `WebViewProxy`
  (ProxyController; refuses to load if proxy override is unsupported),
  `LauncherRoot` with tiles: IB Trader (WebView), Ulagaariyan (fetch a
  tailnet URL), SSH, Status, Settings. Also registers as a HOME launcher.
- Build: `scripts/bootstrap-toolchain.sh` then `scripts/build-debug-apk.sh`
  (JDK 21, Go 1.27, Android SDK 35, NDK r29, gomobile, tsnet v1.104.5).
- Never run on a real phone.

Gaps found in review:

1. Proxy has no secret and no allowlist: any app on the phone could reach
   the whole tailnet through it. Must fix first.
2. No module structure yet (tiles are hard-coded screens).
3. SSH tile uses `InsecureIgnoreHostKey` and stores a password.
4. "Ulagaariyan" tile: purpose unclear, ask the operator.

## Prerequisites outside the app

- **Prod is signed out of Tailscale.** `local-prod` (192.168.4.66) showed
  `NeedsLogin`; run `sudo tailscale up` there. Once up, IB Trader is
  `http://local-prod.tail7b3267.ts.net:8000` (or `http://100.66.39.52:8000`).
- **ib-api has no auth.** `APIKeyMiddleware` exists but `ib_trader/api/main.py`
  never passes `api_key` to `create_app`, so anyone who reaches :8000 can trade.
  Enable it (token in `.env`), and have the shell send the token
  (`Authorization: Bearer`, and `?token=` on `/ws`).
- Tailscale ACL: allow only the shell's node to reach `local-prod:8000`.

## Open decisions

1. Repo: separate repo for the shell (recommended, it serves more than
   IB Trader) or `mobile-apps/` in this repo.
2. Start by hardening the existing branch (proxy secret + allowlist, module
   structure, enable ib-api auth), then build a debug APK to sideload on the
   Galaxy Z Fold7?
3. One tailnet identity for the whole shell (recommended) vs per module.

## Suggested next steps

1. Answer the open decisions.
2. Bring prod back onto Tailscale; turn on ib-api auth.
3. Harden the proxy (secret + allowlist) with Go tests.
4. Introduce the module abstraction; port IB Trader as a WebView module.
5. Build the debug APK, sideload, run the README's first-test checklist.
6. Open a GitHub issue recording the agreed design (repo convention).

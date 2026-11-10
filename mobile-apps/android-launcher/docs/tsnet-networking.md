# tsnet networking

The launcher embeds `tailscale.com/tsnet` v1.104.5 in-process. `Server.Tun` is left nil, so tsnet's gVisor netstack carries sockets for this process only. The app does not declare a VPN permission, does not call `VpnService`, and does not create a TUN interface. The phone's other apps keep using the normal network. Status JSON reports `userspace: true`, `tun: false`, and `vpnService: false` when the node is healthy.

## Why an HTTP CONNECT proxy

WebView traffic has to enter that netstack without a TUN. Two shapes were considered:

- A SOCKS5 proxy on loopback (`socks://127.0.0.1:port` in `ProxyConfig`).
- An HTTP proxy that also implements `CONNECT`.

`androidx.webkit.ProxyController.setProxyOverride` installs a Chromium proxy rule for every WebView in the process. Chromium sends ordinary HTTP (the document, assets, and `/api`) to an `http://` proxy as absolute-form requests, and it opens WebSockets by sending `CONNECT host:port` and then the `GET /ws` upgrade inside the tunnel. That is the path `WebView` uses for `ws://` and `wss://` when an HTTP proxy is configured. A SOCKS proxy is a different code path and is not what this WebView integration is built on.

The Go proxy in `tsnet/proxy.go` therefore:

1. Binds `127.0.0.1` only.
2. Dials every target with `tsnet.Server.Dial`, which is the userspace stack.
3. Forwards plain HTTP.
4. Hijacks `CONNECT` and, when the first bytes are an HTTP request (cleartext `ws://`), injects headers before splicing the rest of the bytes. A TLS ClientHello is spliced raw.

`go test` in `tsnet/` covers a forwarded GET, a raw `CONNECT` byte relay, and a WebSocket handshake plus a masked text frame through `CONNECT`. The WebSocket test also checks that a configured bearer is added as `Authorization` and as `/ws?token=`, which is the query parameter `APIKeyMiddleware` reads for `/ws`. The token is not added to ordinary REST paths.

Kotlin calls `ProxyController` with the rule `http://127.0.0.1:<port>` from `ProxyConfig.proxyRule`. If `WebViewFeature.PROXY_OVERRIDE` is missing, the trading page is not loaded. Falling back to a direct WebView request would leave the tailnet.

The origin itself is plain HTTP on port 8000, so Chromium does not use QUIC for it. QUIC would not honor an HTTP proxy the same way.

Ulagaariyan and the SSH probe do not use the WebView. They call `Server.Dial` directly (`Fetch`, `ProbeTCP`, `SSHExec`).

## Login

With an empty auth key and no node state, tsnet prints a login URL and `LocalClient.Status` exposes `AuthURL`. The app also watches the IPN bus for `BrowseToURL`. The URL is on the public login host, so the system browser opens it. The node itself reaches the coordination server from the phone's normal network; that is not tailnet traffic. After the node reaches `Running`, later launches pass an empty auth key and reuse the encrypted state. A one-shot key is not required again. `TS_AUTHKEY` is cleared in the process when the start config has an empty key.

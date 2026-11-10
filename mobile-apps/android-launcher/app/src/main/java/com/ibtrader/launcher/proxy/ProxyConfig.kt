package com.ibtrader.launcher.proxy

import java.net.URI

/** Pure decisions for the WebView proxy and the IB Trader origin. */
object ProxyConfig {
    /**
     * Accepts a host, host:port, or absolute URL. A missing port becomes 8000,
     * which is ib-api's default. An explicit port is preserved.
     */
    fun normalizeOrigin(raw: String): String {
        val trimmed = raw.trim()
        require(trimmed.isNotEmpty()) { "IB Trader origin is empty" }
        val withScheme = if ("://" in trimmed) trimmed else "http://$trimmed"
        val uri = URI(withScheme)
        val scheme = (uri.scheme ?: "http").lowercase()
        require(scheme == "http" || scheme == "https") { "origin scheme must be http or https" }
        val host = uri.host ?: throw IllegalArgumentException("origin is missing a host")
        val port = if (uri.port == -1) 8000 else uri.port
        val path = (uri.rawPath ?: "").trimEnd('/')
        return URI(scheme, null, host, port, path, null, null).toASCIIString()
    }

    /** ProxyController rule. Chromium uses this HTTP proxy for pages and for WebSocket CONNECT. */
    fun proxyRule(port: Int): String {
        require(port in 1..65535) { "proxy port out of range" }
        return "http://127.0.0.1:$port"
    }

    fun mainFrameHeaders(bearer: String): Map<String, String> {
        val token = bearer.trim()
        if (token.isEmpty()) return emptyMap()
        return mapOf("Authorization" to "Bearer $token")
    }

    /**
     * Why the trading page must not load. A null return means the WebView may
     * navigate. Missing proxy support is a hard stop so traffic cannot leave
     * the tailnet by falling back to the phone's network.
     */
    fun loadBlockReason(tsnetUp: Boolean, origin: String, proxySupported: Boolean): String? {
        if (!tsnetUp) return "Tailscale is not connected."
        if (origin.isBlank()) return "Set the IB Trader origin in Settings."
        if (!proxySupported) {
            return "This WebView cannot force a proxy, so the page was not loaded."
        }
        return null
    }
}

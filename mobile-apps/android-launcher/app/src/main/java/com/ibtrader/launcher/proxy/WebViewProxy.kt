package com.ibtrader.launcher.proxy

import android.os.Handler
import android.os.Looper
import androidx.webkit.ProxyConfig
import androidx.webkit.ProxyController
import androidx.webkit.WebViewFeature
import java.util.concurrent.Executor

/**
 * Points every WebView in this process at the tsnet loopback proxy.
 * Chromium routes ws:// and wss:// through an HTTP proxy with CONNECT,
 * which the Go proxy tunnels. See docs/tsnet-networking.md.
 */
object WebViewProxy {
    private val mainExecutor = Executor { command ->
        if (Looper.myLooper() == Looper.getMainLooper()) command.run()
        else Handler(Looper.getMainLooper()).post(command)
    }

    fun supported(): Boolean = WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)

    fun apply(port: Int, onReady: () -> Unit): Boolean {
        if (!supported()) return false
        val config = ProxyConfig.Builder()
            .addProxyRule(com.ibtrader.launcher.proxy.ProxyConfig.proxyRule(port))
            .build()
        ProxyController.getInstance().setProxyOverride(config, mainExecutor, Runnable(onReady))
        return true
    }

    fun clear() {
        if (!supported()) return
        ProxyController.getInstance().clearProxyOverride(mainExecutor, Runnable { })
    }
}

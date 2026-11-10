package com.ibtrader.launcher.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.runtime.collectAsState
import com.ibtrader.launcher.LauncherApp
import com.ibtrader.launcher.proxy.ProxyConfig
import com.ibtrader.launcher.proxy.WebViewProxy
import com.ibtrader.launcher.settings.LauncherSettings
import com.ibtrader.launcher.tailnet.TailnetRuntime
import com.ibtrader.launcher.tailnet.TailnetSnapshot
import kotlinx.coroutines.launch

private sealed interface Route {
    data object Home : Route
    data object Login : Route
    data object Settings : Route
    data object Status : Route
    data object Trader : Route
    data object Ulagaariyan : Route
    data object Ssh : Route
}

@Composable
fun LauncherRoot() {
    val context = LocalContext.current
    val runtime = (context.applicationContext as LauncherApp).runtime
    val snapshot by runtime.snapshot.collectAsState()
    val needsLogin by runtime.needsLogin.collectAsState()
    var route by remember { mutableStateOf<Route>(if (needsLogin) Route.Login else Route.Home) }
    LaunchedEffect(needsLogin) {
        if (needsLogin && route == Route.Home) route = Route.Login
    }
    LaunchedEffect(snapshot.loggedIn) {
        if (snapshot.loggedIn && route == Route.Login) route = Route.Home
    }
    Box(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(16.dp),
    ) {
        when (route) {
            Route.Home -> HomeScreen(runtime, snapshot) { route = it }
            Route.Login -> LoginScreen(runtime, snapshot) { route = Route.Settings }
            Route.Settings -> SettingsScreen(
                runtime,
                snapshot,
                onBack = { route = if (needsLogin) Route.Login else Route.Home },
                onCleared = { route = Route.Login },
            )
            Route.Status -> StatusScreen(snapshot) { route = Route.Home }
            Route.Trader -> TraderScreen(runtime) { route = Route.Home }
            Route.Ulagaariyan -> UlagaariyanScreen(runtime) { route = Route.Home }
            Route.Ssh -> SshScreen(runtime) { route = Route.Home }
        }
    }
}

@Composable
private fun HomeScreen(runtime: TailnetRuntime, snapshot: TailnetSnapshot, open: (Route) -> Unit) {
    val tiles = listOf(
        Tile("IB Trader", "Web app on the tailnet", Route.Trader),
        Tile("Ulagaariyan", "Fetch a tailnet URL", Route.Ulagaariyan),
        Tile("SSH", "TCP and a command", Route.Ssh),
        Tile("Status", "Node, IPs, peers", Route.Status),
        Tile("Settings", "Login and origin", Route.Settings),
    )
    Column(Modifier.fillMaxSize()) {
        Text("IB Launcher", style = MaterialTheme.typography.headlineMedium)
        Text(
            when {
                snapshot.loggedIn -> "Tailscale ${snapshot.ips.firstOrNull() ?: snapshot.hostname}"
                snapshot.nodeRunning -> "Tailscale ${snapshot.backendState}"
                else -> "Tailscale is stopped"
            },
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (snapshot.lastError.isNotBlank() && !snapshot.loggedIn) {
            Spacer(Modifier.height(8.dp))
            Text(snapshot.lastError, color = MaterialTheme.colorScheme.error)
        }
        Spacer(Modifier.height(16.dp))
        LazyVerticalGrid(
            columns = GridCells.Fixed(2),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            modifier = Modifier.fillMaxSize(),
        ) {
            items(tiles) { tile ->
                Card(
                    onClick = { open(tile.route) },
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
                    shape = RoundedCornerShape(18.dp),
                    modifier = Modifier.height(128.dp),
                ) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.SpaceBetween) {
                        Text(tile.title, style = MaterialTheme.typography.titleMedium)
                        Text(tile.subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

private data class Tile(val title: String, val subtitle: String, val route: Route)

@Composable
private fun LoginScreen(runtime: TailnetRuntime, snapshot: TailnetSnapshot, openSettings: () -> Unit) {
    var hostname by remember { mutableStateOf(runtime.settings.value.hostname) }
    var control by remember { mutableStateOf(runtime.settings.value.controlUrl) }
    var key by remember { mutableStateOf(runtime.loadAuthKey()) }
    val context = LocalContext.current
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        Text("Sign in to Tailscale", style = MaterialTheme.typography.headlineSmall)
        Text(
            "This phone joins your tailnet as its own node. Only this app's traffic uses it.",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))
        LabeledField("Node hostname", hostname) { hostname = it }
        LabeledField("Control URL (optional)", control) { control = it }
        Button(onClick = {
            runtime.saveSettings(runtime.settings.value.copy(hostname = hostname, controlUrl = control))
            runtime.launchInteractive()
        }, modifier = Modifier.fillMaxWidth()) { Text("Sign in with Tailscale") }
        if (snapshot.loginUrl.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(snapshot.loginUrl, fontFamily = FontFamily.Monospace)
            Row {
                TextButton(onClick = {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(snapshot.loginUrl)))
                }) { Text("Open login page") }
                TextButton(onClick = {
                    val cm = context.getSystemService(ClipboardManager::class.java)
                    cm.setPrimaryClip(ClipData.newPlainText("tailscale", snapshot.loginUrl))
                }) { Text("Copy") }
            }
        }
        if (snapshot.backendState.isNotBlank()) {
            Text("State: ${snapshot.backendState}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (snapshot.lastError.isNotBlank()) {
            Text(snapshot.lastError, color = MaterialTheme.colorScheme.error)
        }
        Spacer(Modifier.height(16.dp))
        Text("Or paste an auth key", style = MaterialTheme.typography.titleMedium)
        LabeledField("Auth key", key, password = true) { key = it }
        Button(onClick = {
            runtime.saveSettings(runtime.settings.value.copy(hostname = hostname, controlUrl = control))
            runtime.launchWithKey(key, replaceNode = false)
        }, modifier = Modifier.fillMaxWidth()) { Text("Use auth key") }
        TextButton(onClick = openSettings) { Text("Other settings") }
    }
}

@Composable
private fun SettingsScreen(
    runtime: TailnetRuntime,
    snapshot: TailnetSnapshot,
    onBack: () -> Unit,
    onCleared: () -> Unit,
) {
    val saved by runtime.settings.collectAsState()
    val appliedHost by runtime.appliedHostname.collectAsState()
    val appliedControl by runtime.appliedControl.collectAsState()
    var draft by remember(saved) { mutableStateOf(saved) }
    var confirmClear by remember { mutableStateOf(false) }
    var relogin by remember { mutableStateOf(false) }
    var newKey by remember { mutableStateOf("") }
    BackHandler(onBack = onBack)
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        TopBar("Settings", onBack)
        Text("IB Trader", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        LabeledField("Origin (port 8000)", draft.origin) { draft = draft.copy(origin = it) }
        LabeledField("Bearer token (optional)", draft.bearerToken, password = true) { draft = draft.copy(bearerToken = it) }
        Text("Ulagaariyan", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        LabeledField("URL", draft.ulagaariyanUrl) { draft = draft.copy(ulagaariyanUrl = it) }
        Text("SSH", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        LabeledField("Host", draft.sshHost) { draft = draft.copy(sshHost = it) }
        LabeledField("Port", draft.sshPort.toString(), keyboard = KeyboardType.Number) {
            draft = draft.copy(sshPort = it.toIntOrNull() ?: draft.sshPort)
        }
        LabeledField("User", draft.sshUser) { draft = draft.copy(sshUser = it) }
        LabeledField("Password", draft.sshPassword, password = true) { draft = draft.copy(sshPassword = it) }
        Text("Node", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        LabeledField("Hostname", draft.hostname) { draft = draft.copy(hostname = it) }
        LabeledField("Control URL", draft.controlUrl) { draft = draft.copy(controlUrl = it) }
        if (snapshot.nodeRunning && (draft.hostname != appliedHost || draft.controlUrl != appliedControl)) {
            Text("Restart Tailscale to apply the hostname and control URL.")
        }
        Button(onClick = { runtime.saveSettings(draft) }, modifier = Modifier.fillMaxWidth()) { Text("Save") }
        OutlinedButton(onClick = { runtime.saveSettings(draft); runtime.restart() }, modifier = Modifier.fillMaxWidth()) {
            Text("Save and restart node")
        }
        Spacer(Modifier.height(16.dp))
        Text("Tailscale", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        AuthBlock(snapshot)
        Button(onClick = { relogin = true }, modifier = Modifier.fillMaxWidth()) { Text("Re-login") }
        OutlinedButton(onClick = { confirmClear = true }, modifier = Modifier.fillMaxWidth()) {
            Text("Clear stored credentials")
        }
    }
    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("Clear Tailscale credentials?") },
            text = { Text("This logs out, deletes the node key and the saved auth key, and returns to first launch. IB Trader and SSH settings stay.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    runtime.clearCredentials()
                    onCleared()
                }) { Text("Clear") }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Cancel") } },
        )
    }
    if (relogin) {
        AlertDialog(
            onDismissRequest = { relogin = false },
            title = { Text("Re-login") },
            text = {
                Column {
                    Text("Open the Tailscale login page again, or replace this node with a new auth key.")
                    LabeledField("New auth key", newKey, password = true) { newKey = it }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    relogin = false
                    runtime.saveSettings(draft)
                    runtime.launchWithKey(newKey, replaceNode = true)
                }) { Text("Use key") }
            },
            dismissButton = {
                TextButton(onClick = {
                    relogin = false
                    runtime.saveSettings(draft)
                    runtime.relaunchInteractive()
                }) { Text("Interactive") }
            },
        )
    }
}

@Composable
private fun AuthBlock(snapshot: TailnetSnapshot) {
    val expiry = snapshot.keyExpiry.ifBlank { "—" }
    val err = snapshot.lastError.ifBlank { "—" }
    Text(if (snapshot.loggedIn) "Logged in" else "Logged out")
    Text("Node: ${snapshot.hostname.ifBlank { "—" }}")
    Text("IPs: ${snapshot.ips.joinToString().ifBlank { "—" }}")
    Text("Key expiry: $expiry")
    Text("Last error: $err", color = if (snapshot.lastError.isBlank()) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error)
}

@Composable
private fun StatusScreen(snapshot: TailnetSnapshot, onBack: () -> Unit) {
    BackHandler(onBack = onBack)
    Column(Modifier.fillMaxSize()) {
        TopBar("Status", onBack)
        Text("State: ${snapshot.backendState.ifBlank { "Stopped" }}")
        Text(if (snapshot.userspace && !snapshot.tun) "Networking: userspace (no VPN, no TUN)" else "Networking: TUN=${snapshot.tun}")
        Text("VpnService: not used")
        Text("Proxy: ${if (snapshot.proxyPort == 0) "—" else "127.0.0.1:${snapshot.proxyPort}"}")
        Text("Name: ${snapshot.dnsName.ifBlank { snapshot.hostname }.ifBlank { "—" }}")
        Text("IPs: ${snapshot.ips.joinToString().ifBlank { "—" }}")
        Text("Key expiry: ${snapshot.keyExpiry.ifBlank { "—" }}")
        if (snapshot.loginUrl.isNotBlank()) Text(snapshot.loginUrl, fontFamily = FontFamily.Monospace)
        if (snapshot.lastError.isNotBlank()) Text(snapshot.lastError, color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(8.dp))
        Text("Peers", style = MaterialTheme.typography.titleMedium)
        LazyColumn(contentPadding = PaddingValues(vertical = 8.dp)) {
            items(snapshot.peers.size) { index ->
                val peer = snapshot.peers[index]
                Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier
                            .width(10.dp)
                            .height(10.dp)
                            .background(if (peer.online) Color(0xFF3DDC97) else Color(0xFF667085), RoundedCornerShape(5.dp)),
                    )
                    Spacer(Modifier.width(8.dp))
                    Column {
                        Text(peer.name.ifBlank { peer.dnsName })
                        Text(
                            listOf(peer.os, peer.ips.joinToString()).filter { it.isNotBlank() }.joinToString(" · "),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
        if (snapshot.peers.isEmpty()) Text("No peers yet.", color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun TraderScreen(runtime: TailnetRuntime, onBack: () -> Unit) {
    val context = LocalContext.current
    val snapshot by runtime.snapshot.collectAsState()
    val settings by runtime.settings.collectAsState()
    var error by remember { mutableStateOf<String?>(null) }
    var proxyReady by remember { mutableStateOf(false) }
    val origin = remember(settings.origin) {
        settings.origin.trim().let { raw ->
            if (raw.isEmpty()) "" else runCatching { ProxyConfig.normalizeOrigin(raw) }.getOrDefault("")
        }
    }
    val webView = remember {
        WebView(context).apply {
            setBackgroundColor(android.graphics.Color.parseColor("#0E1116"))
            this.settings.javaScriptEnabled = true
            @Suppress("DEPRECATION")
            this.settings.databaseEnabled = true
            this.settings.domStorageEnabled = true
            this.settings.useWideViewPort = true
            this.settings.loadWithOverviewMode = true
            this.settings.mediaPlaybackRequiresUserGesture = true
            this.settings.allowFileAccess = false
            this.settings.allowContentAccess = false
            this.settings.setSupportZoom(false)
        }
    }
    androidx.compose.runtime.DisposableEffect(webView) {
        onDispose { webView.destroy() }
    }
    LaunchedEffect(snapshot.proxyPort, snapshot.loggedIn) {
        proxyReady = false
        if (snapshot.loggedIn && snapshot.proxyPort > 0) {
            val ok = WebViewProxy.apply(snapshot.proxyPort) { proxyReady = true }
            if (!ok) error = ProxyConfig.loadBlockReason(true, origin, false)
        }
    }
    fun load() {
        val reason = ProxyConfig.loadBlockReason(snapshot.loggedIn, origin, WebViewProxy.supported())
        if (reason != null || !proxyReady) {
            error = reason ?: "The tailnet proxy is not ready yet."
            return
        }
        error = null
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean = false
            override fun onReceivedError(view: WebView, request: WebResourceRequest, err: WebResourceError) {
                if (request.isForMainFrame) {
                    error = err.description?.toString() ?: "The origin could not be loaded."
                }
            }
        }
        webView.loadUrl(origin, ProxyConfig.mainFrameHeaders(settings.bearerToken))
    }
    LaunchedEffect(proxyReady, origin, snapshot.loggedIn) {
        if (proxyReady && snapshot.loggedIn && origin.isNotBlank()) load()
    }
    BackHandler {
        if (error == null && webView.canGoBack()) webView.goBack() else onBack()
    }
    Column(Modifier.fillMaxSize()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack) { Text("Home") }
            Text("IB Trader", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            TextButton(onClick = { load() }) { Text("Reload") }
        }
        Box(Modifier.fillMaxSize()) {
            AndroidView(factory = { webView }, modifier = Modifier.fillMaxSize())
            val block = error
                ?: ProxyConfig.loadBlockReason(snapshot.loggedIn, origin, WebViewProxy.supported())
                ?: if (!proxyReady) "Connecting the tailnet proxy…" else null
            if (block != null) {
                Column(
                    Modifier
                        .fillMaxSize()
                        .background(MaterialTheme.colorScheme.background)
                        .padding(8.dp),
                    verticalArrangement = Arrangement.Center,
                ) {
                    Text(block, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(12.dp))
                    Button(onClick = { load() }) { Text("Retry") }
                }
            }
        }
    }
}

@Composable
private fun UlagaariyanScreen(runtime: TailnetRuntime, onBack: () -> Unit) {
    val settings by runtime.settings.collectAsState()
    val snapshot by runtime.snapshot.collectAsState()
    var url by remember(settings.ulagaariyanUrl) { mutableStateOf(settings.ulagaariyanUrl) }
    var output by remember { mutableStateOf("Fetch runs through the tailnet node.") }
    val scope = rememberCoroutineScope()
    BackHandler(onBack = onBack)
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        TopBar("Ulagaariyan", onBack)
        LabeledField("URL", url) { url = it }
        Button(
            onClick = {
                runtime.saveSettings(settings.copy(ulagaariyanUrl = url))
                if (!snapshot.loggedIn) {
                    output = "Tailscale is not connected."
                    return@Button
                }
                output = "Fetching…"
                scope.launch {
                    val result = runtime.fetch(url)
                    output = if (result.ok) {
                        "HTTP ${result.status} in ${result.elapsedMs} ms\n\n${result.body}"
                    } else {
                        result.error.ifBlank { "request failed" }
                    }
                }
            },
            enabled = url.isNotBlank(),
        ) { Text("Fetch") }
        Spacer(Modifier.height(12.dp))
        Text(output, fontFamily = FontFamily.Monospace)
    }
}

@Composable
private fun SshScreen(runtime: TailnetRuntime, onBack: () -> Unit) {
    val settings by runtime.settings.collectAsState()
    val snapshot by runtime.snapshot.collectAsState()
    var host by remember(settings.sshHost) { mutableStateOf(settings.sshHost) }
    var port by remember(settings.sshPort) { mutableStateOf(settings.sshPort.toString()) }
    var user by remember(settings.sshUser) { mutableStateOf(settings.sshUser) }
    var password by remember(settings.sshPassword) { mutableStateOf(settings.sshPassword) }
    var command by remember { mutableStateOf("uname -a") }
    var output by remember { mutableStateOf("Host keys are not verified. TCP probe does not start a session.") }
    val scope = rememberCoroutineScope()
    fun persist() {
        runtime.saveSettings(
            settings.copy(
                sshHost = host,
                sshPort = port.toIntOrNull() ?: 22,
                sshUser = user,
                sshPassword = password,
            ),
        )
    }
    BackHandler(onBack = onBack)
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        TopBar("SSH", onBack)
        LabeledField("Host", host) { host = it }
        LabeledField("Port", port, keyboard = KeyboardType.Number) { port = it }
        LabeledField("User", user) { user = it }
        LabeledField("Password", password, password = true) { password = it }
        LabeledField("Command", command) { command = it }
        Row {
            Button(onClick = {
                persist()
                if (!snapshot.loggedIn) {
                    output = "Tailscale is not connected."
                    return@Button
                }
                val portNum = port.toIntOrNull() ?: 22
                scope.launch {
                    val probe = runtime.probe(host, portNum)
                    output = if (probe.ok) "TCP open ${probe.addr}\n${probe.banner}" else probe.error.ifBlank { "TCP failed" }
                }
            }) { Text("Test TCP") }
            Spacer(Modifier.width(8.dp))
            Button(onClick = {
                persist()
                if (!snapshot.loggedIn) {
                    output = "Tailscale is not connected."
                    return@Button
                }
                val portNum = port.toIntOrNull() ?: 22
                scope.launch {
                    val ssh = runtime.ssh(host, portNum, user, password, command)
                    output = buildString {
                        if (ssh.ok) append("exit 0\n") else append(ssh.error.ifBlank { "ssh failed" } + "\n")
                        if (ssh.stdout.isNotBlank()) append(ssh.stdout)
                        if (ssh.stderr.isNotBlank()) append("\n" + ssh.stderr)
                    }
                }
            }) { Text("Run") }
        }
        Spacer(Modifier.height(12.dp))
        Text(output, fontFamily = FontFamily.Monospace)
    }
}

@Composable
private fun TopBar(title: String, onBack: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = onBack) { Text("Home") }
        Text(title, style = MaterialTheme.typography.titleLarge)
    }
}

@Composable
private fun LabeledField(
    label: String,
    value: String,
    password: Boolean = false,
    keyboard: KeyboardType = KeyboardType.Text,
    onChange: (String) -> Unit,
) {
    Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        modifier = Modifier.fillMaxWidth(),
        singleLine = true,
        visualTransformation = if (password) PasswordVisualTransformation() else androidx.compose.ui.text.input.VisualTransformation.None,
        keyboardOptions = KeyboardOptions(keyboardType = keyboard),
    )
    Spacer(Modifier.height(8.dp))
}

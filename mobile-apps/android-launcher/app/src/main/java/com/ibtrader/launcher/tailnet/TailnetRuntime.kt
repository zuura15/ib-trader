package com.ibtrader.launcher.tailnet

import com.ibtrader.launcher.security.CredentialStore
import com.ibtrader.launcher.settings.LauncherSettings
import com.ibtrader.launcher.settings.NodeConfigJson
import com.ibtrader.launcher.settings.SettingsStore
import com.ibtrader.launcher.settings.Startup
import com.ibtrader.launcher.settings.StartupPlan
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Starts the userspace node from sealed state, or waits for a first-launch login.
 * Automatic starts pass an empty auth key once the node has registered.
 */
class TailnetRuntime(
    private val credentials: CredentialStore,
    private val settingsStore: SettingsStore,
    private val stateDir: File,
    private val nodeFactory: TailnetNodeFactory,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val mutex = Mutex()
    private var node: TailnetNode? = null
    private var poll: Job? = null

    private val _snapshot = MutableStateFlow(TailnetSnapshot())
    val snapshot: StateFlow<TailnetSnapshot> = _snapshot.asStateFlow()

    private val _settings = MutableStateFlow(loadSettingsSafe())
    val settings: StateFlow<LauncherSettings> = _settings.asStateFlow()

    private val _needsLogin = MutableStateFlow(currentPlan() == StartupPlan.FirstLaunch)
    val needsLogin: StateFlow<Boolean> = _needsLogin.asStateFlow()

    private val _appliedHostname = MutableStateFlow("")
    val appliedHostname: StateFlow<String> = _appliedHostname.asStateFlow()

    private val _appliedControl = MutableStateFlow("")
    val appliedControl: StateFlow<String> = _appliedControl.asStateFlow()

    fun currentPlan(): StartupPlan =
        Startup.decide(credentials.isRegistered(), credentials.hasEncryptedNodeState())

    fun loadAuthKey(): String = credentials.loadAuthKey().orEmpty()

    fun bootAutoReconnect() {
        scope.launch {
            mutex.withLock {
                if (node != null) return@withLock
                if (currentPlan() != StartupPlan.AutoReconnect) return@withLock
                startLocked(explicitAuthKey = null)
            }
        }
    }

    fun launchInteractive() {
        scope.launch {
            mutex.withLock {
                stopLocked()
                startLocked(explicitAuthKey = "")
                runCatching { node?.beginInteractiveLogin() }
                    .onFailure { note(it.message ?: "interactive login failed") }
            }
        }
    }

    fun launchWithKey(key: String, replaceNode: Boolean) {
        val trimmed = key.trim()
        scope.launch {
            mutex.withLock {
                if (trimmed.isEmpty()) {
                    note("Paste an auth key first.")
                    return@withLock
                }
                credentials.saveAuthKey(trimmed)
                // A stored node ignores a new auth key. Wipe unless this node
                // is already the registered one and the caller did not ask
                // to replace it.
                val wipe = replaceNode || !credentials.isRegistered()
                if (wipe || node != null) {
                    stopLocked()
                    if (wipe) credentials.wipeNodeState()
                }
                startLocked(explicitAuthKey = trimmed)
            }
        }
    }

    fun relaunchInteractive() {
        scope.launch {
            mutex.withLock {
                if (node == null) startLocked(explicitAuthKey = "")
                runCatching { node?.beginInteractiveLogin() }
                    .onFailure { note(it.message ?: "interactive login failed") }
            }
        }
    }

    fun clearCredentials() {
        scope.launch {
            mutex.withLock {
                runCatching { node?.logout() }
                stopLocked()
                credentials.clear()
                _snapshot.value = TailnetSnapshot()
                _needsLogin.value = true
                _appliedHostname.value = ""
                _appliedControl.value = ""
            }
        }
    }

    fun saveSettings(next: LauncherSettings) {
        val origin = next.origin.trim().let { raw ->
            if (raw.isEmpty()) "" else runCatching { com.ibtrader.launcher.proxy.ProxyConfig.normalizeOrigin(raw) }.getOrElse {
                note(it.message ?: "origin is invalid")
                raw
            }
        }
        val saved = next.copy(
            hostname = next.hostname.trim().ifEmpty { LauncherSettings.DEFAULT_HOSTNAME },
            controlUrl = next.controlUrl.trim(),
            origin = origin,
            bearerToken = next.bearerToken.trim(),
            ulagaariyanUrl = next.ulagaariyanUrl.trim(),
            sshHost = next.sshHost.trim(),
            sshUser = next.sshUser.trim(),
            sshPort = next.sshPort.coerceIn(1, 65535),
        )
        settingsStore.save(saved)
        _settings.value = saved
        node?.updateRoute(saved.origin, saved.bearerToken)
    }

    fun restart() {
        scope.launch {
            mutex.withLock {
                val plan = currentPlan()
                stopLocked()
                val explicit = if (plan == StartupPlan.AutoReconnect) null else ""
                startLocked(explicitAuthKey = explicit)
            }
        }
    }

    suspend fun fetch(url: String): FetchResult = withContext(Dispatchers.IO) {
        val current = node ?: return@withContext FetchResult(url = url, error = "Tailscale is not running")
        runCatching { StatusJson.fetch(current.fetch(url)) }
            .getOrElse { FetchResult(url = url, error = it.message ?: "fetch failed") }
    }

    suspend fun probe(host: String, port: Int): ProbeResult = withContext(Dispatchers.IO) {
        val current = node ?: return@withContext ProbeResult(error = "Tailscale is not running", addr = host)
        runCatching { StatusJson.probe(current.probeTcp(host, port)) }
            .getOrElse { ProbeResult(error = it.message ?: "probe failed", addr = host) }
    }

    suspend fun ssh(host: String, port: Int, user: String, password: String, command: String): SshResult =
        withContext(Dispatchers.IO) {
            val current = node ?: return@withContext SshResult(error = "Tailscale is not running")
            runCatching { StatusJson.ssh(current.sshExec(host, port, user, password, command)) }
                .getOrElse { SshResult(error = it.message ?: "ssh failed") }
        }

    private fun startLocked(explicitAuthKey: String?) {
        val settings = _settings.value
        runCatching { credentials.restoreSidecars() }
        val plan = currentPlan()
        val authKey = Startup.authKeyFor(plan, credentials.loadAuthKey(), explicitAuthKey)
        val dek = try {
            credentials.ensureDek()
        } catch (err: Exception) {
            note(err.message ?: "could not unwrap the state key")
            return
        }
        val config = NodeConfigJson.build(
            stateDir = stateDir.absolutePath,
            hostname = settings.hostname.ifBlank { LauncherSettings.DEFAULT_HOSTNAME },
            authKey = authKey,
            controlUrl = settings.controlUrl,
            origin = settings.origin,
            bearerToken = settings.bearerToken,
            dek = dek,
        )
        try {
            val started = nodeFactory.start(config)
            node = started
            _appliedHostname.value = settings.hostname
            _appliedControl.value = settings.controlUrl
            _needsLogin.value = plan == StartupPlan.FirstLaunch && explicitAuthKey != null && !credentials.isRegistered()
            poll?.cancel()
            poll = scope.launch { pollLoop(started) }
            scope.launch {
                delay(3_000)
                if (node === started) runCatching { credentials.sealLogConf() }
            }
        } catch (err: Throwable) {
            note(err.message ?: "failed to start Tailscale")
        }
    }

    private suspend fun pollLoop(started: TailnetNode) {
        while (true) {
            if (node !== started) return
            val snap = runCatching { StatusJson.snapshot(started.statusJSON()) }.getOrElse {
                TailnetSnapshot(lastError = it.message ?: "status failed", nodeRunning = true, userspace = true)
            }
            if (node !== started) return
            _snapshot.value = snap
            if (snap.loggedIn) {
                credentials.setRegistered(true)
                _needsLogin.value = false
            }
            delay(1_000)
        }
    }

    private fun stopLocked() {
        val current = node
        node = null
        poll?.cancel()
        poll = null
        if (current != null) {
            runCatching { current.stop() }
            runCatching { credentials.sealPlaintextFiles() }
        }
    }

    private fun note(message: String) {
        _snapshot.value = _snapshot.value.copy(lastError = message)
    }

    private fun loadSettingsSafe(): LauncherSettings =
        runCatching { settingsStore.load() }.getOrElse { LauncherSettings() }
}

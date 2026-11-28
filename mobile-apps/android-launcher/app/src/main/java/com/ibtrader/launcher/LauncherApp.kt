package com.ibtrader.launcher

import android.app.Application
import com.ibtrader.launcher.security.AndroidKeystoreAead
import com.ibtrader.launcher.security.CredentialStore
import com.ibtrader.launcher.settings.SettingsStore
import com.ibtrader.launcher.tailnet.GomobileTailnetNode
import com.ibtrader.launcher.tailnet.TailnetRuntime
import java.io.File

/** Composition root. The node is not started until [TailnetRuntime.bootAutoReconnect] or a login action. */
class LauncherApp : Application() {
    lateinit var runtime: TailnetRuntime
        private set

    override fun onCreate() {
        super.onCreate()
        val vaultDir = File(filesDir, "vault")
        val stateDir = File(filesDir, "tsnet")
        val aead = AndroidKeystoreAead()
        runtime = TailnetRuntime(
            credentials = CredentialStore(vaultDir, aead, stateDir),
            settingsStore = SettingsStore(vaultDir, aead),
            stateDir = stateDir,
            nodeFactory = GomobileTailnetNode.factory(),
        )
    }
}

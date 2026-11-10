package com.ibtrader.launcher.settings

import com.ibtrader.launcher.security.Aead
import com.ibtrader.launcher.security.SealedFiles
import java.io.File

/** Sealed settings blob. A missing file loads defaults. */
class SettingsStore(dir: File, aead: Aead) {
    private val files = SealedFiles(dir, aead)

    fun save(settings: LauncherSettings) {
        files.write(NAME, settings.encode(), AAD)
    }

    fun load(): LauncherSettings {
        val blob = files.read(NAME, AAD) ?: return LauncherSettings()
        return LauncherSettings.decode(blob)
    }

    companion object {
        private const val NAME = "settings.bin"
        private val AAD = "settings-v1".encodeToByteArray()
    }
}

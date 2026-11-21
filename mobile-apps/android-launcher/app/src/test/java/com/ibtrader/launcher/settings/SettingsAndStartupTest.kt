package com.ibtrader.launcher.settings

import com.ibtrader.launcher.security.AesGcmAead
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsAndStartupTest {
    @Test
    fun settingsRoundTrip() {
        val dir = File.createTempFile("settings", "dir")
        dir.delete()
        val store = SettingsStore(dir, AesGcmAead.random())
        assertEquals(LauncherSettings.DEFAULT_HOSTNAME, store.load().hostname)
        val saved = LauncherSettings(
            hostname = "phone",
            controlUrl = "https://headscale.example",
            origin = "http://ib.example:8000",
            bearerToken = "token with spaces",
            ulagaariyanUrl = "http://ulagaariyan:9/health",
            sshHost = "shell",
            sshPort = 2222,
            sshUser = "alice",
            sshPassword = "p@ss",
        )
        store.save(saved)
        assertEquals(saved, store.load())
        val blob = File(dir, "settings.bin").readBytes()
        assertFalse(String(blob, Charsets.ISO_8859_1).contains("p@ss"))
        assertFalse(String(blob, Charsets.ISO_8859_1).contains("token with spaces"))
    }

    @Test
    fun firstLaunchVersusAutoReconnect() {
        assertEquals(StartupPlan.FirstLaunch, Startup.decide(false, false))
        assertEquals(StartupPlan.FirstLaunch, Startup.decide(false, true))
        assertEquals(StartupPlan.FirstLaunch, Startup.decide(true, false))
        assertEquals(StartupPlan.AutoReconnect, Startup.decide(true, true))
    }

    @Test
    fun autoReconnectDoesNotResendAuthKey() {
        val stored = "tskey-auth-oneshot"
        assertEquals("", Startup.authKeyFor(StartupPlan.AutoReconnect, stored, null))
        assertEquals(stored, Startup.authKeyFor(StartupPlan.FirstLaunch, stored, null))
        assertEquals("", Startup.authKeyFor(StartupPlan.FirstLaunch, stored, ""))
        assertEquals("tskey-auth-new", Startup.authKeyFor(StartupPlan.AutoReconnect, stored, " tskey-auth-new "))
        val json = NodeConfigJson.build(
            stateDir = "/data/tsnet",
            hostname = "ib-launcher",
            authKey = Startup.authKeyFor(StartupPlan.AutoReconnect, stored, null),
            controlUrl = "",
            origin = "http://ib:8000",
            bearerToken = "",
            dek = ByteArray(32) { 7 },
        )
        assertTrue(json.contains("\"authKey\":\"\""))
        assertFalse(json.contains(stored))
        assertTrue(json.contains("\"stateDir\":\"/data/tsnet\""))
    }

    @Test
    fun quotesEscape() {
        assertEquals("\"a\\\\b\\\"c\"", NodeConfigJson.quote("a\\b\"c"))
    }
}

package com.ibtrader.launcher.security

import com.ibtrader.launcher.settings.LauncherSettings
import com.ibtrader.launcher.settings.SettingsStore
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CredentialStoreTest {
    @Test
    fun saveLoadAndClearAuthKey() {
        val (creds, vault) = store()
        creds.saveAuthKey("  tskey-auth-abc  ")
        assertEquals("tskey-auth-abc", creds.loadAuthKey())
        val blob = File(vault, "auth-key.bin").readBytes()
        assertFalse(String(blob, Charsets.ISO_8859_1).contains("tskey-auth-abc"))
        creds.clear()
        assertNull(creds.loadAuthKey())
    }

    @Test
    fun dekRoundTripAndClear() {
        val (creds, _) = store()
        val dek = creds.ensureDek()
        assertEquals(32, dek.size)
        assertTrue(creds.loadDek()!!.contentEquals(dek))
        val again = creds.ensureDek()
        assertTrue(again.contentEquals(dek))
        creds.clear()
        assertNull(creds.loadDek())
    }

    @Test
    fun clearDropsNodeStateAndKeepsSettings() {
        val root = File.createTempFile("vault", "dir")
        root.delete()
        val vault = File(root, "vault")
        val state = File(root, "tsnet")
        val aead = AesGcmAead.random()
        val creds = CredentialStore(vault, aead, state)
        val settings = SettingsStore(vault, aead)
        settings.save(LauncherSettings(origin = "http://node.ts.net:8000", bearerToken = "sekret"))
        creds.saveAuthKey("tskey-auth-abc")
        creds.setRegistered(true)
        creds.ensureDek()
        state.mkdirs()
        File(state, "aa.aead").writeBytes(byteArrayOf(1, 2, 3))
        File(state, "tailscaled.log.conf").writeText("private-log-id")
        creds.sealPlaintextFiles()
        assertFalse(File(state, "tailscaled.log.conf").exists())
        assertTrue(creds.hasEncryptedNodeState())
        creds.restoreSidecars()
        assertEquals("private-log-id", File(state, "tailscaled.log.conf").readText())
        creds.clear()
        assertNull(creds.loadAuthKey())
        assertNull(creds.loadDek())
        assertFalse(creds.isRegistered())
        assertFalse(creds.hasEncryptedNodeState())
        assertFalse(state.exists())
        assertEquals("http://node.ts.net:8000", settings.load().origin)
        assertEquals("sekret", settings.load().bearerToken)
    }

    @Test
    fun rejectsZipSlip() {
        val root = File.createTempFile("state", "dir")
        root.delete()
        root.mkdirs()
        try {
            safeChild(root, "../secrets")
            throw AssertionError("expected rejection")
        } catch (err: IllegalArgumentException) {
            assertTrue(err.message!!.contains("escapes"))
        }
    }

    private fun store(): Pair<CredentialStore, File> {
        val root = File.createTempFile("cred", "dir")
        root.delete()
        val vault = File(root, "vault")
        val creds = CredentialStore(vault, AesGcmAead.random(), File(root, "tsnet"))
        return creds to vault
    }
}

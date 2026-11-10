package com.ibtrader.launcher.security

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.SecureRandom
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * Keystore-sealed Tailscale credentials and the wrapped data key for the
 * tsnet state directory. Settings live in [com.ibtrader.launcher.settings.SettingsStore]
 * and survive [clear].
 */
class CredentialStore(
    vaultDir: File,
    private val aead: Aead,
    private val stateDir: File,
) {
    private val files = SealedFiles(vaultDir, aead)

    fun saveAuthKey(key: String) {
        files.write(AUTH, key.trim().encodeToByteArray(), AAD_AUTH)
    }

    fun loadAuthKey(): String? {
        val raw = files.read(AUTH, AAD_AUTH) ?: return null
        return raw.decodeToString().trim().ifEmpty { null }
    }

    fun saveDek(dek: ByteArray) {
        require(dek.size == 32) { "dek must be 32 bytes" }
        files.write(DEK, dek.copyOf(), AAD_DEK)
    }

    fun loadDek(): ByteArray? = files.read(DEK, AAD_DEK)?.copyOf()

    /** Creates a data key on first use and wraps it with [aead]. */
    fun ensureDek(): ByteArray {
        loadDek()?.let { return it }
        val dek = ByteArray(32)
        SecureRandom().nextBytes(dek)
        saveDek(dek)
        return dek
    }

    fun isRegistered(): Boolean = files.read(REG, AAD_REG)?.decodeToString() == "1"

    fun setRegistered(value: Boolean) {
        if (value) files.write(REG, "1".encodeToByteArray(), AAD_REG)
        else files.delete(REG)
    }

    fun hasEncryptedNodeState(): Boolean {
        if (!stateDir.isDirectory) return false
        return stateDir.walkTopDown().any { it.isFile && it.name.endsWith(".aead") }
    }

    /** Drops the node identity. Keeps the wrapped DEK and any saved auth key. */
    fun wipeNodeState() {
        setRegistered(false)
        files.delete(SIDECARS)
        if (stateDir.exists()) stateDir.deleteRecursively()
    }

    /**
     * Logout wipe: auth key, wrapped DEK, registration flag, sealed sidecars,
     * and the state directory. Launcher settings are not touched.
     */
    fun clear() {
        files.delete(AUTH)
        files.delete(DEK)
        wipeNodeState()
    }

    /** After tsnet stops, seal every plaintext file left in the state dir. */
    fun sealPlaintextFiles() {
        seal(names = null)
    }

    /** The log id file is only needed at process start. Seal it while running. */
    fun sealLogConf() {
        val conf = File(stateDir, "tailscaled.log.conf")
        if (conf.isFile) seal(names = setOf(conf.name))
    }

    fun restoreSidecars() {
        val blob = files.read(SIDECARS, AAD_SIDECARS) ?: return
        if (!stateDir.exists()) stateDir.mkdirs()
        ZipInputStream(ByteArrayInputStream(blob)).use { zip ->
            var entry = zip.nextEntry
            while (entry != null) {
                if (!entry.isDirectory) {
                    val out = safeChild(stateDir, entry.name)
                    if (!out.exists()) {
                        out.parentFile?.mkdirs()
                        out.writeBytes(zip.readBytes())
                    }
                }
                zip.closeEntry()
                entry = zip.nextEntry
            }
        }
    }

    private fun seal(names: Set<String>?) {
        if (!stateDir.isDirectory) return
        val chosen = stateDir.walkTopDown().filter { file ->
            file.isFile && !file.name.endsWith(".aead") && !file.name.endsWith(".aead.tmp") &&
                (names == null || file.name in names)
        }.toList()
        if (chosen.isEmpty()) return
        val bos = ByteArrayOutputStream()
        ZipOutputStream(bos).use { zip ->
            for (file in chosen) {
                val rel = stateDir.toPath().relativize(file.toPath()).toString().replace('\\', '/')
                zip.putNextEntry(ZipEntry(rel))
                zip.write(file.readBytes())
                zip.closeEntry()
            }
        }
        // When sealing a single live file, merge it into the existing archive
        // so a later full restore still has the other sidecars.
        val payload = if (names != null && files.exists(SIDECARS)) {
            mergeZip(files.read(SIDECARS, AAD_SIDECARS), bos.toByteArray())
        } else {
            bos.toByteArray()
        }
        files.write(SIDECARS, payload, AAD_SIDECARS)
        chosen.forEach { it.delete() }
    }

    private fun mergeZip(existing: ByteArray?, addition: ByteArray): ByteArray {
        if (existing == null) return addition
        val entries = linkedMapOf<String, ByteArray>()
        fun readInto(blob: ByteArray) {
            ZipInputStream(ByteArrayInputStream(blob)).use { zip ->
                var entry = zip.nextEntry
                while (entry != null) {
                    if (!entry.isDirectory) entries[entry.name] = zip.readBytes()
                    zip.closeEntry()
                    entry = zip.nextEntry
                }
            }
        }
        readInto(existing)
        readInto(addition)
        val bos = ByteArrayOutputStream()
        ZipOutputStream(bos).use { zip ->
            for ((name, bytes) in entries) {
                zip.putNextEntry(ZipEntry(name))
                zip.write(bytes)
                zip.closeEntry()
            }
        }
        return bos.toByteArray()
    }

    companion object {
        private const val AUTH = "auth-key.bin"
        private const val DEK = "dek.bin"
        private const val REG = "registered.bin"
        private const val SIDECARS = "sidecars.bin"
        val AAD_AUTH = "auth-key-v1".encodeToByteArray()
        val AAD_DEK = "dek-v1".encodeToByteArray()
        val AAD_REG = "registered-v1".encodeToByteArray()
        private val AAD_SIDECARS = "sidecars-v1".encodeToByteArray()
    }
}

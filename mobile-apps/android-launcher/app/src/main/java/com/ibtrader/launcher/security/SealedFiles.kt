package com.ibtrader.launcher.security

import java.io.File
import java.io.FileOutputStream

/** Writes AEAD blobs into a private directory. Callers choose the AAD. */
internal class SealedFiles(private val dir: File, private val aead: Aead) {
    private val lock = Any()

    fun write(name: String, plaintext: ByteArray, aad: ByteArray) = synchronized(lock) {
        dir.mkdirs()
        val out = File(dir, name)
        val tmp = File(dir, "$name.tmp")
        FileOutputStream(tmp).use { it.write(aead.encrypt(plaintext, aad)) }
        if (!tmp.renameTo(out)) {
            out.writeBytes(tmp.readBytes())
            tmp.delete()
        }
    }

    fun read(name: String, aad: ByteArray): ByteArray? = synchronized(lock) {
        val file = File(dir, name)
        if (!file.isFile) return null
        aead.decrypt(file.readBytes(), aad)
    }

    fun delete(name: String) = synchronized(lock) {
        File(dir, name).delete()
        File(dir, "$name.tmp").delete()
    }

    fun exists(name: String): Boolean = File(dir, name).isFile
}

/** Resolves a zip entry under [root] and refuses paths that escape it. */
internal fun safeChild(root: File, relative: String): File {
    require(relative.isNotBlank()) { "empty zip entry" }
    require(!relative.startsWith("/") && !relative.startsWith("\\")) { "absolute zip entry" }
    require(!relative.split('/', '\\').contains("..")) { "zip entry escapes the state dir" }
    val rootCanon = root.canonicalFile
    val out = File(rootCanon, relative).canonicalFile
    val prefix = rootCanon.path + File.separator
    require(out.path == rootCanon.path || out.path.startsWith(prefix)) {
        "zip entry escapes the state dir"
    }
    return out
}

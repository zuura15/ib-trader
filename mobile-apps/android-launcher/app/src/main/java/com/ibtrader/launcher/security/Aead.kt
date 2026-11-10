package com.ibtrader.launcher.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** AES-GCM used for auth keys, settings, the state-dir DEK, and log sidecars. */
interface Aead {
    fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray
    fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray
}

/**
 * Software AES-GCM with the same iv||ciphertext layout as [AndroidKeystoreAead].
 * Unit tests use this. Production uses a key that never leaves the Android Keystore.
 */
class AesGcmAead(private val key: SecretKey) : Aead {
    override fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        if (aad.isNotEmpty()) cipher.updateAAD(aad)
        val iv = cipher.iv
        check(iv.size == IV_LEN) { "unexpected GCM nonce length" }
        return iv + cipher.doFinal(plaintext)
    }

    override fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(ciphertext.size > IV_LEN) { "ciphertext too short" }
        val iv = ciphertext.copyOfRange(0, IV_LEN)
        val body = ciphertext.copyOfRange(IV_LEN, ciphertext.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, iv))
        if (aad.isNotEmpty()) cipher.updateAAD(aad)
        return cipher.doFinal(body)
    }

    companion object {
        fun random(): AesGcmAead {
            val raw = ByteArray(32)
            SecureRandom().nextBytes(raw)
            return AesGcmAead(SecretKeySpec(raw, "AES"))
        }
    }
}

/** Keystore-backed AES-256-GCM. The key is not exportable. */
class AndroidKeystoreAead(private val alias: String = KEY_ALIAS) : Aead {
    override fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        if (aad.isNotEmpty()) cipher.updateAAD(aad)
        val iv = cipher.iv
        check(iv.size == IV_LEN) { "unexpected GCM nonce length" }
        return iv + cipher.doFinal(plaintext)
    }

    override fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(ciphertext.size > IV_LEN) { "ciphertext too short" }
        val iv = ciphertext.copyOfRange(0, IV_LEN)
        val body = ciphertext.copyOfRange(IV_LEN, ciphertext.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
        if (aad.isNotEmpty()) cipher.updateAAD(aad)
        return cipher.doFinal(body)
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        val existing = ks.getKey(alias, null) as? SecretKey
        if (existing != null) return existing
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        val spec = KeyGenParameterSpec.Builder(
            alias,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .setRandomizedEncryptionRequired(true)
            .build()
        generator.init(spec)
        return generator.generateKey()
    }

    companion object {
        const val KEY_ALIAS = "ib-launcher-vault"
    }
}

private const val TRANSFORMATION = "AES/GCM/NoPadding"
private const val IV_LEN = 12
private const val ANDROID_KEYSTORE = "AndroidKeyStore"

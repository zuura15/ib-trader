package com.ibtrader.launcher.settings

import java.util.Base64

/** Whether this process should prompt for Tailscale or reconnect from sealed state. */
enum class StartupPlan {
    FirstLaunch,
    AutoReconnect,
}

object Startup {
    /**
     * Auto-reconnect only after a node has reached Running at least once
     * and the encrypted state files are still on disk. Anything else is a
     * first launch, including a half-finished login.
     */
    fun decide(registered: Boolean, hasEncryptedNodeState: Boolean): StartupPlan {
        return if (registered && hasEncryptedNodeState) {
            StartupPlan.AutoReconnect
        } else {
            StartupPlan.FirstLaunch
        }
    }

    /**
     * [explicitAuthKey] is non-null only when the user just chose a login
     * action (empty string means interactive login). Automatic reconnect
     * never resends a stored one-shot auth key.
     */
    fun authKeyFor(plan: StartupPlan, storedAuthKey: String?, explicitAuthKey: String?): String {
        if (explicitAuthKey != null) return explicitAuthKey.trim()
        if (plan == StartupPlan.AutoReconnect) return ""
        return storedAuthKey?.trim().orEmpty()
    }
}

object NodeConfigJson {
    fun build(
        stateDir: String,
        hostname: String,
        authKey: String,
        controlUrl: String,
        origin: String,
        bearerToken: String,
        dek: ByteArray,
    ): String {
        require(dek.size == 32) { "dek must be 32 bytes" }
        val dekB64 = Base64.getEncoder().encodeToString(dek)
        return buildString {
            append('{')
            append("\"stateDir\":").append(quote(stateDir)).append(',')
            append("\"hostname\":").append(quote(hostname)).append(',')
            append("\"authKey\":").append(quote(authKey)).append(',')
            append("\"controlUrl\":").append(quote(controlUrl)).append(',')
            append("\"origin\":").append(quote(origin)).append(',')
            append("\"bearerToken\":").append(quote(bearerToken)).append(',')
            append("\"dekB64\":").append(quote(dekB64))
            append('}')
        }
    }

    internal fun quote(value: String): String {
        val sb = StringBuilder(value.length + 2)
        sb.append('"')
        for (ch in value) {
            when (ch) {
                '\\' -> sb.append("\\\\")
                '"' -> sb.append("\\\"")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> sb.append(ch)
            }
        }
        sb.append('"')
        return sb.toString()
    }
}

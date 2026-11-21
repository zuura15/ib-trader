package com.ibtrader.launcher.settings

import java.io.ByteArrayOutputStream
import java.util.Properties

/** Launcher preferences. The whole blob is sealed, including the bearer token and SSH password. */
data class LauncherSettings(
    val hostname: String = DEFAULT_HOSTNAME,
    val controlUrl: String = "",
    val origin: String = "",
    val bearerToken: String = "",
    val ulagaariyanUrl: String = "",
    val sshHost: String = "",
    val sshPort: Int = 22,
    val sshUser: String = "",
    val sshPassword: String = "",
) {
    fun encode(): ByteArray {
        val props = Properties()
        props.setProperty("hostname", hostname)
        props.setProperty("controlUrl", controlUrl)
        props.setProperty("origin", origin)
        props.setProperty("bearerToken", bearerToken)
        props.setProperty("ulagaariyanUrl", ulagaariyanUrl)
        props.setProperty("sshHost", sshHost)
        props.setProperty("sshPort", sshPort.toString())
        props.setProperty("sshUser", sshUser)
        props.setProperty("sshPassword", sshPassword)
        val out = ByteArrayOutputStream()
        props.store(out, null)
        return out.toByteArray()
    }

    companion object {
        const val DEFAULT_HOSTNAME = "ib-launcher"

        fun decode(bytes: ByteArray): LauncherSettings {
            val props = Properties()
            props.load(bytes.inputStream())
            return LauncherSettings(
                hostname = props.getProperty("hostname", DEFAULT_HOSTNAME),
                controlUrl = props.getProperty("controlUrl", ""),
                origin = props.getProperty("origin", ""),
                bearerToken = props.getProperty("bearerToken", ""),
                ulagaariyanUrl = props.getProperty("ulagaariyanUrl", ""),
                sshHost = props.getProperty("sshHost", ""),
                sshPort = props.getProperty("sshPort", "22").toIntOrNull() ?: 22,
                sshUser = props.getProperty("sshUser", ""),
                sshPassword = props.getProperty("sshPassword", ""),
            )
        }
    }
}

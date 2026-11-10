package com.ibtrader.launcher.tailnet

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

val AppJson = Json {
    ignoreUnknownKeys = true
    coerceInputValues = true
}

@Serializable
data class TailnetPeer(
    val name: String = "",
    val dnsName: String = "",
    val online: Boolean = false,
    val os: String = "",
    val ips: List<String> = emptyList(),
)

@Serializable
data class TailnetSnapshot(
    val backendState: String = "Stopped",
    val loggedIn: Boolean = false,
    val loginUrl: String = "",
    val hostname: String = "",
    val dnsName: String = "",
    val ips: List<String> = emptyList(),
    val keyExpiry: String = "",
    val lastError: String = "",
    val tun: Boolean = false,
    val userspace: Boolean = true,
    val vpnService: Boolean = false,
    val proxyPort: Int = 0,
    val peers: List<TailnetPeer> = emptyList(),
    val nodeRunning: Boolean = false,
)

@Serializable
data class FetchResult(
    val ok: Boolean = false,
    val status: Int = 0,
    val elapsedMs: Long = 0,
    val body: String = "",
    val error: String = "",
    val url: String = "",
)

@Serializable
data class ProbeResult(
    val ok: Boolean = false,
    val banner: String = "",
    val error: String = "",
    val addr: String = "",
)

@Serializable
data class SshResult(
    val ok: Boolean = false,
    val stdout: String = "",
    val stderr: String = "",
    val error: String = "",
)

object StatusJson {
    fun snapshot(raw: String): TailnetSnapshot = AppJson.decodeFromString(raw)
    fun fetch(raw: String): FetchResult = AppJson.decodeFromString(raw)
    fun probe(raw: String): ProbeResult = AppJson.decodeFromString(raw)
    fun ssh(raw: String): SshResult = AppJson.decodeFromString(raw)
}

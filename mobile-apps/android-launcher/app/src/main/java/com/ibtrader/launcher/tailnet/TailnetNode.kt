package com.ibtrader.launcher.tailnet

/** The process-local tsnet node. Production binds the gomobile AAR. */
interface TailnetNode {
    fun statusJSON(): String
    fun beginInteractiveLogin(): String
    fun updateRoute(origin: String, bearer: String)
    fun fetch(url: String): String
    fun probeTcp(host: String, port: Int): String
    fun sshExec(host: String, port: Int, user: String, password: String, command: String): String
    fun logout()
    fun stop()
}

fun interface TailnetNodeFactory {
    fun start(configJson: String): TailnetNode
}

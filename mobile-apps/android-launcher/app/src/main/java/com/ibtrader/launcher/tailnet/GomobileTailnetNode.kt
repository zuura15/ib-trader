package com.ibtrader.launcher.tailnet

import tsnetmobile.Node
import tsnetmobile.Tsnetmobile

/** Adapter over the gomobile-generated tsnetmobile bindings. */
class GomobileTailnetNode(private val node: Node) : TailnetNode {
    override fun statusJSON(): String = node.statusJSON()
    override fun beginInteractiveLogin(): String = node.beginInteractiveLogin()
    override fun updateRoute(origin: String, bearer: String) = node.updateRoute(origin, bearer)
    override fun fetch(url: String): String = node.fetch(url)
    override fun probeTcp(host: String, port: Int): String = node.probeTCP(host, port)
    override fun sshExec(host: String, port: Int, user: String, password: String, command: String): String =
        node.sshExec(host, port, user, password, command)
    override fun logout() = node.logout()
    override fun stop() = node.stop()

    companion object {
        fun factory(): TailnetNodeFactory = TailnetNodeFactory { json ->
            GomobileTailnetNode(Tsnetmobile.start(json))
        }
    }
}

package com.ibtrader.launcher.tailnet

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.ibtrader.launcher.LauncherApp
import com.ibtrader.launcher.MainActivity
import com.ibtrader.launcher.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/** Keeps the userspace node alive. This is not a VPN service. */
class TailnetService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        promote("Connecting to Tailscale")
        val runtime = (application as LauncherApp).runtime
        scope.launch {
            runtime.snapshot.collect { snap ->
                val text = when {
                    snap.loggedIn -> "Connected ${snap.ips.firstOrNull() ?: snap.hostname}"
                    snap.nodeRunning -> snap.backendState.ifBlank { "Starting" }
                    snap.lastError.isNotBlank() -> snap.lastError
                    else -> "Waiting for Tailscale login"
                }
                val manager = getSystemService(NotificationManager::class.java)
                manager.notify(NOTIF_ID, notification(text))
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        promote("Connecting to Tailscale")
        (application as LauncherApp).runtime.bootAutoReconnect()
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun promote(text: String) {
        val built = notification(text)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, built, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, built)
        }
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("IB Launcher")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(CHANNEL, "Tailscale", NotificationManager.IMPORTANCE_LOW)
        channel.description = "Userspace Tailscale node for this app"
        manager.createNotificationChannel(channel)
    }

    companion object {
        private const val CHANNEL = "tailnet"
        private const val NOTIF_ID = 42
    }
}

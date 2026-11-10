package com.ibtrader.launcher

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.ibtrader.launcher.tailnet.TailnetService
import com.ibtrader.launcher.ui.LauncherRoot
import com.ibtrader.launcher.ui.LauncherTheme

/** Home-screen activity. Also launches from the app drawer. */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        runCatching {
            ContextCompat.startForegroundService(this, Intent(this, TailnetService::class.java))
        }
        setContent {
            LauncherTheme {
                LauncherRoot()
            }
        }
    }
}

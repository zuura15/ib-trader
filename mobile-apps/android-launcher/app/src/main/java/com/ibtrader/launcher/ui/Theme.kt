package com.ibtrader.launcher.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Colors = darkColorScheme(
    primary = Color(0xFFE8A838),
    onPrimary = Color(0xFF1A1203),
    secondary = Color(0xFF8FB8C9),
    background = Color(0xFF0E1116),
    surface = Color(0xFF171C24),
    surfaceVariant = Color(0xFF222A36),
    onBackground = Color(0xFFE7ECF3),
    onSurface = Color(0xFFE7ECF3),
    onSurfaceVariant = Color(0xFFB7C0CE),
    error = Color(0xFFE15A4A),
)

@Composable
fun LauncherTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Colors, content = content)
}

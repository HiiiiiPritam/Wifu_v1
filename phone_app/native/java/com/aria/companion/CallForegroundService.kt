package com.aria.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/** Keeps the app in the foreground for the duration of a call, typed as a
 * microphone service.
 *
 * This exists for one reason: Android 11+ "while-in-use" permissions.
 * When our process is started in the BACKGROUND -- which is exactly what
 * happens when Firebase wakes us for an incoming call -- the OS refuses
 * microphone access for the life of that process, even once you've tapped
 * Accept and an activity is on screen. RECORD_AUDIO shows as granted in
 * Settings, a mic device is visible to the page, and AudioRecord still
 * won't start. The WebView surfaces that as the thoroughly misleading
 * "NotReadableError: Could not start audio source", which reads like the
 * hardware is busy and is why this took so long to pin down.
 *
 * A foreground service with FOREGROUND_SERVICE_TYPE_MICROPHONE is the
 * documented exemption. It also earns us the ongoing "on a call"
 * notification a call app should have had regardless. */
class CallForegroundService : Service() {

    companion object {
        private const val CHANNEL_ID = "aria_ongoing_call"
        private const val NOTIFICATION_ID = 4242

        fun start(context: Context) {
            val intent = Intent(context, CallForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, CallForegroundService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        createChannel()

        val tapToReturn = PendingIntent.getActivity(
            this,
            0,
            Intent(this, InCallActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("On a call with Aria")
            .setContentText("Tap to return to the call")
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setOngoing(true)
            .setContentIntent(tapToReturn)
            .build()

        // The typed overload is what actually grants the mic exemption;
        // the untyped one on older releases doesn't need it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        return START_NOT_STICKY
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID, "Ongoing call", NotificationManager.IMPORTANCE_LOW
            )
        )
    }
}

package com.aria.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * The whole point of the Android app, really: this is what fires even if
 * the app is closed and the phone is locked, because FCM wakes it up for
 * exactly long enough to handle the push. call_server.py's proactive-ring
 * loop calls Firebase Admin's send() with data={"type": "ring"} whenever
 * she'd otherwise have shown the in-browser ring.
 */
class AriaMessagingService : FirebaseMessagingService() {

    companion object {
        const val CHANNEL_ID = "aria_incoming_call"
    }

    override fun onMessageReceived(message: RemoteMessage) {
        if (message.data["type"] != "ring") return
        showIncomingCallNotification()
    }

    override fun onNewToken(token: String) {
        val serverUrl = ServerConfig.get(this) ?: return
        Thread {
            try {
                val body = JSONObject().put("token", token).toString()
                    .toRequestBody("application/json".toMediaType())
                val request = Request.Builder().url("$serverUrl/register_device").post(body).build()
                TrustAllCerts.client().newCall(request).execute().close()
            } catch (_: Exception) {
                // best-effort -- if this fails, re-opening MainActivity and
                // tapping register again will fix it
            }
        }.start()
    }

    private fun showIncomingCallNotification() {
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) == null) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Incoming calls", NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Aria calling you"
                setBypassDnd(true)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            }
            manager.createNotificationChannel(channel)
        }

        val fullScreenIntent = Intent(this, IncomingCallActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val fullScreenPendingIntent = PendingIntent.getActivity(
            this, 0, fullScreenIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentTitle("Aria")
            .setContentText("Incoming call")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setFullScreenIntent(fullScreenPendingIntent, true)
            .setContentIntent(fullScreenPendingIntent)
            .setAutoCancel(true)
            .setOngoing(true)
            .build()

        manager.notify(1, notification)
    }
}

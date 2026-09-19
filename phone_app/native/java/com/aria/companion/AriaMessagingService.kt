package com.aria.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import androidx.core.app.NotificationCompat
import androidx.core.app.Person
import androidx.core.graphics.drawable.IconCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * The whole point of the Android app, really: this fires even if the app
 * is closed and the phone is locked, because FCM wakes it up for exactly
 * long enough to handle the push. The call server sends:
 *   ring   -- she's calling (name, number shown on screen)
 *   cancel -- she gave up / the ring was answered elsewhere: stop ringing
 *   missed -- leave a "missed call" notification
 */
class AriaMessagingService : FirebaseMessagingService() {

    companion object {
        // New id: channel sound can't be changed once a channel exists,
        // and the old "aria_incoming_call" channel had no ringtone -- so an
        // unlocked phone showed the call silently.
        const val RING_CHANNEL_ID = "aria_ringing"
        private const val OLD_RING_CHANNEL_ID = "aria_incoming_call"
        const val MISSED_CHANNEL_ID = "aria_missed_calls"

        /** Matches IncomingCallActivity's timeout and the server's. */
        private const val RING_TIMEOUT_MS = 45_000L
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        when (data["type"]) {
            "ring" -> showIncomingCall(data["name"] ?: "Aria", data["number"] ?: "")
            "cancel" -> {
                CallShared.cancelRinging(this)
                IncomingCallActivity.dismissIfShowing()
            }
            "missed" -> showMissedCall(data["name"] ?: "Aria", data["number"] ?: "")
        }
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
                // best-effort -- tapping "Save & register" in the app again fixes it
            }
        }.start()
    }

    private fun ensureChannels(manager: NotificationManager) {
        manager.deleteNotificationChannel(OLD_RING_CHANNEL_ID)
        if (manager.getNotificationChannel(RING_CHANNEL_ID) == null) {
            val ringtone = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            manager.createNotificationChannel(
                NotificationChannel(RING_CHANNEL_ID, "Incoming calls", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "When she calls you"
                    setBypassDnd(true)
                    lockscreenVisibility = Notification.VISIBILITY_PUBLIC
                    enableVibration(true)
                    vibrationPattern = longArrayOf(0, 1000, 1000)
                    setSound(
                        ringtone,
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                }
            )
        }
        if (manager.getNotificationChannel(MISSED_CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(MISSED_CHANNEL_ID, "Missed calls", NotificationManager.IMPORTANCE_DEFAULT)
            )
        }
    }

    private fun caller(name: String): Person {
        val builder = Person.Builder().setName(name).setImportant(true)
        CallShared.avatar(this, 256)?.let { builder.setIcon(IconCompat.createWithBitmap(it)) }
        return builder.build()
    }

    private fun activityIntent(cls: Class<*>, name: String, number: String, requestCode: Int): PendingIntent {
        val intent = Intent(this, cls).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(CallShared.EXTRA_NAME, name)
            putExtra(CallShared.EXTRA_NUMBER, number)
        }
        return PendingIntent.getActivity(
            this, requestCode, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    /** Locked phone: Android opens IncomingCallActivity full-screen.
     * Unlocked phone: Android shows this as a heads-up call notification
     * -- photo, name, red Decline and green Answer -- ringing with the
     * device ringtone until answered, declined, or RING_TIMEOUT_MS. */
    private fun showIncomingCall(name: String, number: String) {
        val manager = getSystemService(NotificationManager::class.java)
        ensureChannels(manager)

        val fullScreen = activityIntent(IncomingCallActivity::class.java, name, number, 0)
        val answer = activityIntent(InCallActivity::class.java, name, number, 1)
        val decline = PendingIntent.getBroadcast(
            this, 2,
            Intent(this, CallActionReceiver::class.java)
                .putExtra(CallActionReceiver.EXTRA_ACTION, "decline"),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, RING_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentTitle(name)
            .setContentText(number.ifBlank { "Incoming call" })
            .setStyle(NotificationCompat.CallStyle.forIncomingCall(caller(name), decline, answer))
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setFullScreenIntent(fullScreen, true)
            .setContentIntent(fullScreen)
            .setOngoing(true)
            .setTimeoutAfter(RING_TIMEOUT_MS)
            .build()
        // Keeps the ringtone looping for as long as the notification is up,
        // instead of playing it once like a message.
        notification.flags = notification.flags or Notification.FLAG_INSISTENT

        manager.notify(CallShared.RING_NOTIFICATION_ID, notification)
    }

    private fun showMissedCall(name: String, number: String) {
        val manager = getSystemService(NotificationManager::class.java)
        ensureChannels(manager)
        CallShared.cancelRinging(this)
        IncomingCallActivity.dismissIfShowing()

        val callBack = activityIntent(InCallActivity::class.java, name, number, 3)
        val openApp = packageManager.getLaunchIntentForPackage(packageName)?.let {
            PendingIntent.getActivity(this, 4, it, PendingIntent.FLAG_IMMUTABLE)
        } ?: callBack

        val builder = NotificationCompat.Builder(this, MISSED_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_missed_call)
            .setContentTitle("Missed call")
            .setContentText(if (number.isBlank()) name else "$name  ·  $number")
            .setContentIntent(openApp)
            .setAutoCancel(true)
            .addAction(android.R.drawable.sym_action_call, "Call back", callBack)
        CallShared.avatar(this, 256)?.let { builder.setLargeIcon(it) }
        manager.notify(CallShared.MISSED_NOTIFICATION_ID, builder.build())
    }
}

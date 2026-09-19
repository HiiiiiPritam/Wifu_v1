package com.aria.companion

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File

/** What the incoming-call screen and the call notification both need:
 * who's calling, her photo, and a way to tell the server what you did
 * with the call (declined, "remind me", "can't talk"). */
object CallShared {
    const val RING_NOTIFICATION_ID = 1
    const val MISSED_NOTIFICATION_ID = 2

    const val EXTRA_NAME = "aria_name"
    const val EXTRA_NUMBER = "aria_number"

    // Written by the app's settings screen (aria-native module), read here.
    // Same file, same app -- the two just live in different Gradle modules.
    private const val AVATAR_FILE = "avatar.img"

    fun avatarFile(context: Context) = File(context.filesDir, AVATAR_FILE)

    /** Her profile photo, downscaled -- a full-size camera photo would be
     * a waste of memory for a 150dp circle or a notification icon. */
    fun avatar(context: Context, maxPx: Int = 512): Bitmap? {
        val file = avatarFile(context)
        if (!file.exists()) return null
        return try {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(file.path, bounds)
            var sample = 1
            while (bounds.outWidth / (sample * 2) >= maxPx && bounds.outHeight / (sample * 2) >= maxPx) {
                sample *= 2
            }
            BitmapFactory.decodeFile(file.path, BitmapFactory.Options().apply { inSampleSize = sample })
        } catch (_: Exception) {
            null
        }
    }

    fun name(intent: Intent?): String =
        intent?.getStringExtra(EXTRA_NAME)?.takeIf { it.isNotBlank() } ?: "Aria"

    /** The number if the server sent one, else "calling…" -- the same way
     * a real incoming-call screen shows an unknown number. */
    fun subtitle(intent: Intent?): String =
        intent?.getStringExtra(EXTRA_NUMBER)?.takeIf { it.isNotBlank() } ?: "calling…"

    fun cancelRinging(context: Context) {
        context.getSystemService(NotificationManager::class.java)?.cancel(RING_NOTIFICATION_ID)
    }

    /** POST /call_action on a background thread -- best effort: if the
     * server can't be reached the call is simply left to time out. */
    fun sendAction(
        context: Context, action: String, minutes: Int = 10, onDone: (() -> Unit)? = null
    ) {
        val serverUrl = ServerConfig.get(context)
        if (serverUrl == null) {
            onDone?.invoke()
            return
        }
        Thread {
            try {
                val body = JSONObject().put("action", action).put("minutes", minutes).toString()
                    .toRequestBody("application/json".toMediaType())
                val request = Request.Builder().url("$serverUrl/call_action").post(body).build()
                TrustAllCerts.client().newCall(request).execute().close()
            } catch (_: Exception) {
            } finally {
                onDone?.invoke()
            }
        }.start()
    }
}

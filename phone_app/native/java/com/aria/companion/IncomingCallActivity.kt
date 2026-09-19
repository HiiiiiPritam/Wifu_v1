package com.aria.companion

import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.graphics.drawable.RoundedBitmapDrawableFactory
import java.lang.ref.WeakReference

/** Shown full-screen over the lock screen (the fullScreenIntent set in
 * AriaMessagingService). Made to look like a stock incoming call -- her
 * name, number and photo, "Remind me" / "Message", Decline / Accept --
 * with the device's real ringtone looping. When the phone is unlocked,
 * Android shows the call notification instead (see AriaMessagingService). */
class IncomingCallActivity : AppCompatActivity() {

    companion object {
        /** She gives up after this, like a real call going unanswered (the
         * server sends a "missed call" at the same point). */
        private const val RING_TIMEOUT_MS = 45_000L

        private var current: WeakReference<IncomingCallActivity>? = null

        /** Called when the server says the ring is over (she hung up). */
        fun dismissIfShowing() {
            current?.get()?.let { it.runOnUiThread { it.stopAndFinish() } }
        }
    }

    private var ringtonePlayer: MediaPlayer? = null
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
            )
        }

        setContentView(R.layout.activity_incoming_call)
        current = WeakReference(this)
        showCaller(intent)

        // This screen plays its own ringtone, so the notification's (which
        // would otherwise ring in parallel) goes.
        CallShared.cancelRinging(this)
        startRingtone()
        handler.postDelayed({ stopAndFinish() }, RING_TIMEOUT_MS)

        findViewById<View>(R.id.acceptButton).setOnClickListener {
            stopRingtone()
            startActivity(Intent(this, InCallActivity::class.java))
            finish()
        }
        findViewById<View>(R.id.declineButton).setOnClickListener {
            CallShared.sendAction(this, "decline")
            stopAndFinish()
        }
        findViewById<View>(R.id.remindButton).setOnClickListener {
            CallShared.sendAction(this, "remind", 10)
            Toast.makeText(this, "She'll call you back in 10 minutes", Toast.LENGTH_SHORT).show()
            stopAndFinish()
        }
        findViewById<View>(R.id.messageButton).setOnClickListener {
            CallShared.sendAction(this, "busy")
            Toast.makeText(this, "Sent: \"Can't talk right now\"", Toast.LENGTH_SHORT).show()
            stopAndFinish()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        showCaller(intent)
    }

    private fun showCaller(intent: Intent?) {
        val name = CallShared.name(intent)
        findViewById<TextView>(R.id.callerName).text = name
        findViewById<TextView>(R.id.callerSubtitle).text = CallShared.subtitle(intent)
        findViewById<TextView>(R.id.callerInitial).text = name.take(1).uppercase()

        val photo = CallShared.avatar(this)
        val photoView = findViewById<ImageView>(R.id.callerPhoto)
        if (photo != null) {
            photoView.setImageDrawable(
                RoundedBitmapDrawableFactory.create(resources, photo).apply { isCircular = true }
            )
            photoView.visibility = View.VISIBLE
        } else {
            photoView.visibility = View.GONE
        }
    }

    private fun stopAndFinish() {
        stopRingtone()
        CallShared.cancelRinging(this)
        if (!isFinishing) finish()
    }

    private fun startRingtone() {
        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE) ?: return
            ringtonePlayer = MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                setDataSource(this@IncomingCallActivity, uri)
                isLooping = true
                setOnPreparedListener { it.start() }
                prepareAsync()
            }
        } catch (_: Exception) {
            // No ringtone available (e.g. silent device) -- the visual
            // screen alone is still a reasonable fallback, don't crash.
        }
    }

    private fun stopRingtone() {
        ringtonePlayer?.apply {
            try {
                if (isPlaying) stop()
            } catch (_: Exception) {
            }
            release()
        }
        ringtonePlayer = null
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        stopRingtone()
        if (current?.get() === this) current = null
        super.onDestroy()
    }
}

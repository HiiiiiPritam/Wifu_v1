package com.aria.companion

import android.app.NotificationManager
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.widget.Button
import androidx.appcompat.app.AppCompatActivity

/** Shown full-screen over the lock screen (see AndroidManifest + the
 * fullScreenIntent set in AriaMessagingService). Deliberately as close to
 * a real incoming-call screen as a plain Activity can look -- including
 * an actual looping ringtone, not just a silent visual. */
class IncomingCallActivity : AppCompatActivity() {

    private var ringtonePlayer: MediaPlayer? = null

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

        getSystemService(NotificationManager::class.java).cancel(1)
        startRingtone()

        findViewById<Button>(R.id.acceptButton).setOnClickListener {
            stopRingtone()
            startActivity(Intent(this, InCallActivity::class.java))
            finish()
        }
        findViewById<Button>(R.id.declineButton).setOnClickListener {
            stopRingtone()
            finish()
        }
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
        stopRingtone()
        super.onDestroy()
    }
}

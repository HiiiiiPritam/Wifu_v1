package com.aria.companion

import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.widget.Button
import androidx.appcompat.app.AppCompatActivity

/** Shown full-screen over the lock screen (see AndroidManifest + the
 * fullScreenIntent set in AriaMessagingService). Deliberately as close to
 * a real incoming-call screen as a plain Activity can look. */
class IncomingCallActivity : AppCompatActivity() {

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

        findViewById<Button>(R.id.acceptButton).setOnClickListener {
            startActivity(Intent(this, InCallActivity::class.java))
            finish()
        }
        findViewById<Button>(R.id.declineButton).setOnClickListener {
            finish()
        }
    }
}

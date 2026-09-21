package com.aria.companion

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.webkit.JavascriptInterface
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/** Wraps the existing browser call UI (call.html) in a WebView.
 *
 * Critical ordering detail: the page is only loaded AFTER the OS-level
 * RECORD_AUDIO permission is actually resolved. Loading it first (which
 * an earlier version did) meant getUserMedia ran while the permission
 * dialog was still pending -- and in a WebView that doesn't throw, it
 * hands back a SILENT stream, so speech was never detected and no error
 * ever surfaced. Granting the WebView-level permission via
 * onPermissionRequest is not enough on its own; the app itself needs the
 * OS permission too. */
class InCallActivity : AppCompatActivity() {

    companion object {
        private const val MIC_PERMISSION_REQUEST = 2
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Same as IncomingCallActivity: a call answered from the lock screen
        // continues over it, like a real phone call. Without this, tapping
        // Accept made Android demand an unlock first, while this screen
        // loaded hidden behind the lock screen -- so her greeting played
        // before you could hear it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
            )
        }
        // The screen going to sleep mid-call pauses this screen, and with it
        // the page that listens to you.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_in_call)
        // Must be running BEFORE the page calls getUserMedia -- this is
        // what lifts Android's while-in-use mic block on a process Firebase
        // started in the background. See CallForegroundService.
        CallForegroundService.start(this)
        // Answered from the notification: stop it ringing, and close the
        // full-screen incoming screen if that's up too.
        CallShared.cancelRinging(this)
        IncomingCallActivity.dismissIfShowing()
        configureWebView()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            loadCallPage()
        } else {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.RECORD_AUDIO), MIC_PERMISSION_REQUEST
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != MIC_PERMISSION_REQUEST) return
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            Toast.makeText(
                this,
                "Mic permission denied -- she won't be able to hear you.",
                Toast.LENGTH_LONG
            ).show()
        }
        // Load either way: if denied, the page's own error handling shows
        // what's wrong on screen rather than just sitting there silent.
        loadCallPage()
    }

    /** Exposed to call.html as window.AriaCall: things a web page can't do
     * on its own. Methods run on a WebView background thread. */
    inner class CallBridge {
        @JavascriptInterface
        fun isSpeakerOn(): Boolean = speakerIsOn()

        /** Returns the resulting state, so the button shows what actually
         * happened rather than what was asked for. */
        @JavascriptInterface
        fun setSpeaker(on: Boolean): Boolean {
            routeAudio(on)
            val result = speakerIsOn()
            runOnUiThread { updateProximityLock(earpiece = !result) }
            return result
        }

        /** The call ended: close the call screen, like a real call. */
        @JavascriptInterface
        fun endCall() {
            runOnUiThread { finish() }
        }
    }

    private val audioManager by lazy { getSystemService(AudioManager::class.java) }
    private var proximityLock: PowerManager.WakeLock? = null

    // The page's getUserMedia (with echo cancellation) puts the phone in
    // voice-call audio mode, which is what makes speaker/earpiece routing
    // apply to her voice at all.
    private fun routeAudio(speaker: Boolean) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val wanted = if (speaker) AudioDeviceInfo.TYPE_BUILTIN_SPEAKER else AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
            audioManager.availableCommunicationDevices.firstOrNull { it.type == wanted }
                ?.let { audioManager.setCommunicationDevice(it) }
        } else {
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = speaker
        }
    }

    private fun speakerIsOn(): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager.communicationDevice?.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
        } else {
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn
        }

    /** On the earpiece, the screen turns off when the phone is against your
     * ear (so your cheek doesn't press Mute or End), like a real call. */
    private fun updateProximityLock(earpiece: Boolean) {
        if (earpiece) {
            if (proximityLock == null) {
                val power = getSystemService(PowerManager::class.java)
                if (power.isWakeLockLevelSupported(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK)) {
                    proximityLock = power.newWakeLock(
                        PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK, "aria:in_call_proximity"
                    )
                }
            }
            proximityLock?.let { if (!it.isHeld) it.acquire(4 * 60 * 60 * 1000L) }
        } else {
            proximityLock?.let { if (it.isHeld) it.release() }
        }
    }

    private fun configureWebView() {
        val webView = findViewById<WebView>(R.id.callWebView)
        webView.addJavascriptInterface(CallBridge(), "AriaCall")
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_NO_CACHE
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                // The page only ever asks for the mic (getUserMedia audio) --
                // grant exactly what it asks for, nothing more. (Chromium
                // also needs MODIFY_AUDIO_SETTINGS in the manifest to then
                // actually open the mic -- see app.json.)
                runOnUiThread { request.grant(request.resources) }
            }
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedSslError(
                view: WebView, handler: SslErrorHandler, error: SslError
            ) {
                // Same self-signed cert your browser clicks through manually
                // (see certs.py) -- this app IS the trusted client, so it
                // proceeds automatically instead of showing a dead end.
                handler.proceed()
            }
        }
    }

    private fun loadCallPage() {
        val serverUrl = ServerConfig.get(this)
        if (serverUrl.isNullOrBlank()) {
            Toast.makeText(this, "No server URL saved -- open the app and register first.",
                Toast.LENGTH_LONG).show()
            finish()
            return
        }
        // Tells call.html to skip the idle "Call her" screen and jump
        // straight into the call -- you already accepted natively, making
        // you tap it again on the page would be redundant/confusing.
        //
        // micdenied is passed so the page can say plainly that the OS
        // permission is off. Without it the page still calls getUserMedia,
        // WebView grants the page-level request, AudioRecord then fails,
        // and Chromium reports "NotReadableError: Could not start audio
        // source" -- which reads as "the mic is busy" and sent us hunting
        // for a phantom app holding the microphone for hours.
        val micGranted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        val suffix = if (micGranted) "" else "&micdenied=1"
        findViewById<WebView>(R.id.callWebView).loadUrl("$serverUrl/?autoanswer=1$suffix")
    }

    /** Reaching this screen again while it's open (e.g. tapping the "On a
     * call" notification) just brings it back. It used to reload the page,
     * and the reloaded page dialed a brand-new call over the live one. A
     * finished call closes this screen (the page calls AriaCall.endCall),
     * so a genuinely new call always gets a fresh instance via onCreate. */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
    }

    override fun onBackPressed() {
        super.onBackPressed()
        finish()
    }

    /** A WebView holds its microphone track until it is destroyed -- not
     * until its activity finishes. Skipping this is what produced
     * "NotReadableError: Could not start audio source" on the next call:
     * the previous call screen still owned the mic hardware, so the OS had
     * nothing left to hand over. about:blank first so the page's own
     * pagehide handler runs and stops the track cleanly. */
    override fun onDestroy() {
        CallForegroundService.stop(this)
        updateProximityLock(earpiece = false)
        // Hand audio routing back to normal for whatever plays next.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager.clearCommunicationDevice()
        }
        val webView = findViewById<WebView>(R.id.callWebView)
        webView?.let {
            it.loadUrl("about:blank")
            it.stopLoading()
            (it.parent as? ViewGroup)?.removeView(it)
            it.destroy()
        }
        super.onDestroy()
    }
}

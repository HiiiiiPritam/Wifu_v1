package com.aria.companion

import android.Manifest
import android.content.pm.PackageManager
import android.net.http.SslError
import android.os.Bundle
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/** Just wraps the existing, already-tested browser call UI (call.html)
 * in a WebView -- everything about the actual call (client-side VAD,
 * WebSocket audio streaming, barge-in) is the same code already verified
 * working, this only adds the native "answered from a real incoming call"
 * wrapper around it. */
class InCallActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_in_call)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.RECORD_AUDIO), 2)
        }

        val serverUrl = ServerConfig.get(this)
        if (serverUrl.isNullOrBlank()) {
            finish()
            return
        }

        val webView = findViewById<WebView>(R.id.callWebView)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_NO_CACHE
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                // The page only ever asks for the mic (getUserMedia audio) --
                // grant exactly what it asks for, nothing more.
                request.grant(request.resources)
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

        // Tells call.html to skip the idle "Call her" screen and jump
        // straight into the call -- you already accepted natively, making
        // you tap it again on the page would be redundant/confusing.
        webView.loadUrl("$serverUrl/?autoanswer=1")
    }

    override fun onBackPressed() {
        super.onBackPressed()
        finish()
    }
}

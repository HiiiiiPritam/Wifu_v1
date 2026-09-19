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
        setContentView(R.layout.activity_in_call)
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

    private fun configureWebView() {
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
        findViewById<WebView>(R.id.callWebView).loadUrl("$serverUrl/?autoanswer=1")
    }

    override fun onBackPressed() {
        super.onBackPressed()
        finish()
    }
}

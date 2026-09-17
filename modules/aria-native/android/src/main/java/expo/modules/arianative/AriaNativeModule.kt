package expo.modules.arianative

import android.content.Context
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Bridges the JS setup screen to the same "aria_prefs" SharedPreferences
 * file that the pure-native call screens (AriaMessagingService,
 * IncomingCallActivity, InCallActivity -- injected separately via the
 * withAriaNative config plugin, living in com.aria.companion) read the
 * server URL from. Deliberately self-contained rather than importing
 * those classes: this module compiles as its own Gradle module, and
 * :app depends on it (not the other way around), so it can't reach into
 * com.aria.companion's code -- but SharedPreferences is scoped by file
 * name within the app's process, not by Kotlin package/Gradle module,
 * so both sides land on the exact same file on disk regardless.
 */
private const val PREFS = "aria_prefs"
private const val KEY_SERVER_URL = "server_url"

class AriaNativeModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("AriaNative")

    AsyncFunction("saveServerUrl") { url: String ->
      prefs().edit().putString(KEY_SERVER_URL, url.trimEnd('/')).apply()
    }

    AsyncFunction("getServerUrl") {
      prefs().getString(KEY_SERVER_URL, null)
    }

    AsyncFunction("registerDevice") { serverUrl: String, token: String ->
      registerDeviceBlocking(serverUrl, token)
    }
  }

  private fun prefs() =
    (appContext.reactContext ?: throw IllegalStateException("No Android context available"))
      .getSharedPreferences(PREFS, Context.MODE_PRIVATE)

  /** call_server.py uses a self-signed cert (see phase1/certs.py) -- fine
   * for our own local server, needs an explicit trust-all client since
   * this is plain OkHttp, not a WebView with an SSL-error override. */
  private fun registerDeviceBlocking(serverUrl: String, token: String): String {
    val trustManager = object : X509TrustManager {
      override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
      override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
      override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
    }
    val sslContext = SSLContext.getInstance("TLS")
    sslContext.init(null, arrayOf(trustManager), SecureRandom())
    val client = OkHttpClient.Builder()
      .sslSocketFactory(sslContext.socketFactory, trustManager)
      .hostnameVerifier { _, _ -> true }
      .build()

    val body = JSONObject().put("token", token).toString()
      .toRequestBody("application/json".toMediaType())
    val request = Request.Builder().url("$serverUrl/register_device").post(body).build()

    client.newCall(request).execute().use { response ->
      if (!response.isSuccessful) {
        throw Exception("Server responded with ${response.code}")
      }
      return "ok"
    }
  }
}

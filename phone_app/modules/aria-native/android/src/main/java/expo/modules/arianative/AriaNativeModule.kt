package expo.modules.arianative

import android.content.Context
import android.content.Intent
import android.util.Base64
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
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

    // Settings travel as JSON strings rather than typed maps: the server
    // owns the schema (and validates/clamps every value), so this bridge
    // doesn't need to change whenever a setting is added.
    AsyncFunction("getSettings") { serverUrl: String ->
      val request = Request.Builder().url("$serverUrl/settings").get().build()
      executeRequest(request)
    }

    /** Any other server call (the schedule endpoints). body = "" for none. */
    AsyncFunction("request") { serverUrl: String, method: String, path: String, body: String ->
      val builder = Request.Builder().url("$serverUrl$path")
      val payload = if (body.isEmpty()) null else body.toRequestBody("application/json".toMediaType())
      when (method.uppercase()) {
        "GET" -> builder.get()
        "DELETE" -> if (payload == null) builder.delete() else builder.delete(payload)
        else -> builder.method(method.uppercase(), payload ?: "{}".toRequestBody("application/json".toMediaType()))
      }
      executeRequest(builder.build())
    }

    /** Her profile photo: saved on the phone (the incoming-call screen
     * must show it even with the server unreachable) AND sent to the
     * server (for the in-call page). */
    AsyncFunction("uploadAvatar") { serverUrl: String, base64: String ->
      avatarFile().writeBytes(Base64.decode(base64, Base64.DEFAULT))
      val body = JSONObject().put("image", base64).toString()
        .toRequestBody("application/json".toMediaType())
      executeRequest(Request.Builder().url("$serverUrl/avatar").post(body).build())
    }

    /** Refreshes the phone's copy of her photo from the server. Returns
     * whether one exists. */
    AsyncFunction("cacheAvatar") { serverUrl: String ->
      val response = client.newCall(Request.Builder().url("$serverUrl/avatar").get().build()).execute()
      response.use { r ->
        when {
          r.code == 404 -> {
            avatarFile().delete()
            false
          }
          !r.isSuccessful -> throw Exception("Server responded with ${r.code}")
          else -> {
            val bytes = r.body?.bytes()
            if (bytes == null) {
              false
            } else {
              avatarFile().writeBytes(bytes)
              true
            }
          }
        }
      }
    }

    /** A file:// URI for the phone's copy of her photo (React Native's
     * Image can show that directly), or null. The query string changes
     * whenever the file does, so a new photo isn't hidden by caching. */
    AsyncFunction("avatarUri") {
      val file = avatarFile()
      if (file.exists()) "file://${file.absolutePath}?v=${file.lastModified()}" else null
    }

    AsyncFunction("saveSettings") { serverUrl: String, json: String ->
      val body = json.toRequestBody("application/json".toMediaType())
      val request = Request.Builder().url("$serverUrl/settings").post(body).build()
      executeRequest(request)
    }

    AsyncFunction("openCallScreen") {
      val context = appContext.reactContext
        ?: throw IllegalStateException("No Android context available")
      // Class.forName rather than a direct import: InCallActivity lives in
      // the app module (com.aria.companion, injected by the withAriaNative
      // config plugin), and :app depends on this module, not the reverse --
      // so it can't be referenced at compile time from here.
      val intent = Intent(context, Class.forName("com.aria.companion.InCallActivity"))
      intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
      context.startActivity(intent)
    }
  }

  // Same file name CallShared.kt (the native call screens) reads.
  private fun avatarFile() = File(
    (appContext.reactContext ?: throw IllegalStateException("No Android context available")).filesDir,
    "avatar.img"
  )

  private fun prefs() =
    (appContext.reactContext ?: throw IllegalStateException("No Android context available"))
      .getSharedPreferences(PREFS, Context.MODE_PRIVATE)

  /** The call server uses a self-signed cert (see phase1/call/certs.py) --
   * fine for our own local server, but needs an explicit trust-all client
   * since this is plain OkHttp, not a WebView with an SSL-error override.
   * (React Native's own fetch() would reject the cert outright, which is
   * why every server call from the app goes through here.) */
  private val client: OkHttpClient by lazy {
    val trustManager = object : X509TrustManager {
      override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
      override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
      override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
    }
    val sslContext = SSLContext.getInstance("TLS")
    sslContext.init(null, arrayOf(trustManager), SecureRandom())
    OkHttpClient.Builder()
      .sslSocketFactory(sslContext.socketFactory, trustManager)
      .hostnameVerifier { _, _ -> true }
      .build()
  }

  /** Returns the response body. On failure, throws with the server's own
   * reason when it gave one ({"error": "..."}) -- e.g. "there's already a
   * call scheduled at that time" -- instead of a bare HTTP code. */
  private fun executeRequest(request: Request): String {
    client.newCall(request).execute().use { response ->
      val body = response.body?.string() ?: ""
      if (!response.isSuccessful) {
        val reason = try {
          JSONObject(body).optString("error")
        } catch (_: Exception) {
          ""
        }
        throw Exception(reason.ifBlank { "Server responded with ${response.code}" })
      }
      return body
    }
  }

  private fun registerDeviceBlocking(serverUrl: String, token: String): String {
    val body = JSONObject().put("token", token).toString()
      .toRequestBody("application/json".toMediaType())
    val request = Request.Builder().url("$serverUrl/register_device").post(body).build()
    executeRequest(request)
    return "ok"
  }
}

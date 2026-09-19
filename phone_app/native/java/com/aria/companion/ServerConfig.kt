package com.aria.companion

import android.content.Context

/** Where call_server.py lives -- saved once from MainActivity, read
 * everywhere else that needs to reach it. */
object ServerConfig {
    private const val PREFS = "aria_prefs"
    private const val KEY_SERVER_URL = "server_url"

    fun get(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_SERVER_URL, null)

    fun set(context: Context, url: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_SERVER_URL, url.trimEnd('/'))
            .apply()
    }
}

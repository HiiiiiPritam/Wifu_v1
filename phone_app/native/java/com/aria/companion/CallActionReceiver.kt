package com.aria.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** The call notification's Decline button (Accept opens InCallActivity
 * directly -- Android 12+ blocks a receiver from starting an activity, so
 * only actions that don't open a screen come through here). */
class CallActionReceiver : BroadcastReceiver() {

    companion object {
        const val EXTRA_ACTION = "aria_action"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.getStringExtra(EXTRA_ACTION) ?: return
        CallShared.cancelRinging(context)
        // goAsync keeps the receiver alive until the server has been told;
        // otherwise Android can kill the process mid-request.
        val pending = goAsync()
        CallShared.sendAction(context, action) { pending.finish() }
    }
}

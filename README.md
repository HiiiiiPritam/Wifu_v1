# Aria Companion (Expo)

A thin native shell around the existing call server. The only thing this
app's own screen does is register your phone for push notifications --
the actual incoming-call screen and the call itself are pure native
Android code (see `native/` + `plugins/withAriaNative.js`) that Android
launches directly from the push, bypassing this app's JS entirely. Once
you tap Accept, it opens the same tested browser call page
(`phase1/static/call.html`) in a WebView.

Built with Expo + EAS Build specifically so **no Android Studio or local
Android SDK is needed** -- the APK is compiled in Expo's cloud, you just
download the finished file and install it.

## One-time setup

1. **Firebase** -- already done if you're continuing from earlier:
   `google-services.json` should already be sitting in this folder
   (`phone_app/google-services.json`), and `phase1/.env`'s
   `FIREBASE_SERVICE_ACCOUNT_JSON` should point at the service-account
   key file. If you don't have these yet, see the note in
   `../android_app/README.md` for the exact Firebase Console steps.

2. **Create a free Expo account** at expo.dev (just an email+password,
   no payment info needed for the free build tier).

3. **Log in from your terminal**:
   ```
   cd phone_app
   npx eas-cli login
   ```
   (follow the prompt -- email/password or browser login)

4. **Trigger the build** (compiles in Expo's cloud, takes a few minutes):
   ```
   npx eas-cli build --profile preview --platform android
   ```
   First run will ask a couple of setup questions (like whether to
   generate a new Android keystore -- say yes, let it manage that for
   you). When it finishes it prints a URL to the finished `.apk`.

5. **Install the APK on your phone**: open that URL on your phone (or
   scan the QR code EAS prints) and download it directly -- Android will
   ask you to allow installs from this source once, approve it, install.

6. **Run `python call_server.py`** on your PC, open the app, paste in the
   URL it printed, tap "Save and register".

That's it -- close the app, lock your phone, and whenever she'd
otherwise have shown the in-browser ring, you should get a real
full-screen call notification instead.

## Re-building after code changes

Any time you change something under `native/`, `plugins/`, or
`modules/aria-native/`, you need a new build (`eas build` again) --
these are compiled native changes, not something Metro/Fast Refresh can
hot-reload. Changes to `App.tsx` alone also need a rebuild for now since
we're not running a dev client, just the built APK directly (simplest
option given no local Android SDK).

## What I verified vs. what still needs your real device

Verified locally (no Android SDK needed for these):
- `npx expo prebuild` succeeds and the config plugin correctly injects
  all native files + manifest entries into the generated project.
- `npx expo-modules-autolinking resolve` confirms the local `aria-native`
  module is discovered correctly.
- TypeScript compiles clean (`npx tsc --noEmit`).

**Not yet verified** (needs an actual EAS cloud build + your physical
phone, which I can't do from here): whether the compiled APK actually
runs, whether the full-screen notification actually shows over your lock
screen the way Android 14+ expects, and the real end-to-end ring/answer/
talk flow. Expect a first-build iteration cycle together once you have
an APK installed.

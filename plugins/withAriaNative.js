/**
 * Injects the pure-native call/FCM code (see ../native/) into the
 * generated android/ project during prebuild -- AriaMessagingService,
 * IncomingCallActivity, InCallActivity, TrustAllCerts, ServerConfig, plus
 * their layout/resource files and the manifest entries they need.
 *
 * These files never touch the React Native/JS layer at all: Android
 * launches IncomingCallActivity directly from the FCM push, and it in
 * turn launches InCallActivity (a WebView wrapper around the already-
 * tested call.html) -- the RN app (App.tsx) only handles the one-time
 * setup screen. See ../native/README (if present) or project-overview
 * memory for the full reasoning.
 */
const { withDangerousMod, withAndroidManifest, withAppBuildGradle } = require('@expo/config-plugins');
const fs = require('fs');
const path = require('path');

const PACKAGE_PATH = 'com/aria/companion';

function copyDir(srcDir, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
    const srcPath = path.join(srcDir, entry.name);
    const destPath = path.join(destDir, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

const withAriaNativeFiles = (config) =>
  withDangerousMod(config, [
    'android',
    async (config) => {
      const projectRoot = config.modRequest.projectRoot;
      const androidRoot = config.modRequest.platformProjectRoot;
      const nativeSrc = path.join(projectRoot, 'native');

      copyDir(
        path.join(nativeSrc, 'java', 'com', 'aria', 'companion'),
        path.join(androidRoot, 'app', 'src', 'main', 'java', PACKAGE_PATH)
      );
      copyDir(
        path.join(nativeSrc, 'res', 'layout'),
        path.join(androidRoot, 'app', 'src', 'main', 'res', 'layout')
      );
      copyDir(
        path.join(nativeSrc, 'res', 'values'),
        path.join(androidRoot, 'app', 'src', 'main', 'res', 'values')
      );

      return config;
    },
  ]);

const withAriaManifest = (config) =>
  withAndroidManifest(config, (config) => {
    const app = config.modResults.manifest.application[0];

    const activities = [
      {
        $: {
          'android:name': '.IncomingCallActivity',
          'android:exported': 'false',
          'android:showOnLockScreen': 'true',
          'android:excludeFromRecents': 'true',
          'android:launchMode': 'singleInstance',
          'android:theme': '@style/Theme.AriaCompanion.Call',
        },
      },
      {
        $: {
          'android:name': '.InCallActivity',
          'android:exported': 'false',
          'android:showOnLockScreen': 'true',
          'android:launchMode': 'singleInstance',
          'android:theme': '@style/Theme.AriaCompanion.Call',
        },
      },
    ];
    app.activity = app.activity || [];
    for (const activity of activities) {
      const already = app.activity.some((a) => a.$['android:name'] === activity.$['android:name']);
      if (!already) app.activity.push(activity);
    }

    const service = {
      $: { 'android:name': '.AriaMessagingService', 'android:exported': 'false' },
      'intent-filter': [
        { action: [{ $: { 'android:name': 'com.google.firebase.MESSAGING_EVENT' } }] },
      ],
    };
    app.service = app.service || [];
    if (!app.service.some((s) => s.$['android:name'] === '.AriaMessagingService')) {
      app.service.push(service);
    }

    return config;
  });

// google-services.json only feeds the google-services Gradle plugin's
// config parsing -- it does NOT add the actual Firebase Messaging library
// to the app module, which AriaMessagingService.kt needs to compile at
// all. Same story for OkHttp (TrustAllCerts.kt / AriaMessagingService's
// onNewToken) -- neither dependency exists in Expo's generated
// app/build.gradle by default.
const withAriaAppDependencies = (config) =>
  withAppBuildGradle(config, (config) => {
    if (!config.modResults.contents.includes('firebase-messaging')) {
      config.modResults.contents = config.modResults.contents.replace(
        /dependencies\s*\{/,
        `dependencies {
    implementation(platform("com.google.firebase:firebase-bom:33.1.2"))
    implementation("com.google.firebase:firebase-messaging-ktx")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")`
      );
    }
    return config;
  });

module.exports = function withAriaNative(config) {
  config = withAriaNativeFiles(config);
  config = withAriaManifest(config);
  config = withAriaAppDependencies(config);
  return config;
};

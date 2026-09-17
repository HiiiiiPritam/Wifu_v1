import React, { useEffect, useState } from 'react';
import {
  Button,
  Platform,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import * as Notifications from 'expo-notifications';
import AriaNative from './modules/aria-native/src/AriaNativeModule';

// This whole screen is genuinely all the JS/React Native side does --
// once registered, Android launches IncomingCallActivity and InCallActivity
// (pure native Kotlin, see native/ + plugins/withAriaNative.js) directly
// from the FCM push, completely bypassing this app's JS layer.
export default function App() {
  const [serverUrl, setServerUrl] = useState('');
  const [status, setStatus] = useState('');

  useEffect(() => {
    AriaNative.getServerUrl().then((saved) => {
      if (saved) setServerUrl(saved);
    });
  }, []);

  async function handleRegister() {
    if (!serverUrl.trim()) {
      setStatus('Enter the server URL first.');
      return;
    }
    const url = serverUrl.trim().replace(/\/+$/, '');

    try {
      setStatus('Requesting notification permission...');
      const { status: permStatus } = await Notifications.requestPermissionsAsync();
      if (permStatus !== 'granted') {
        setStatus('Notification permission denied -- the call feature needs this to work.');
        return;
      }

      if (Platform.OS === 'android') {
        // Required before getDevicePushTokenAsync on Android 13+.
        await Notifications.setNotificationChannelAsync('default', {
          name: 'default',
          importance: Notifications.AndroidImportance.DEFAULT,
        });
      }

      setStatus('Getting notification token...');
      const { data: token } = await Notifications.getDevicePushTokenAsync();

      setStatus('Registering with server...');
      await AriaNative.saveServerUrl(url);
      await AriaNative.registerDevice(url, token);

      setStatus('Registered! You’ll get a real call notification when she reaches out.');
    } catch (err: any) {
      setStatus(
        `Couldn't finish setup: ${err?.message ?? err}\n` +
          'Make sure call_server.py is running and your phone is on the same WiFi.'
      );
    }
  }

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Aria Companion</Text>
      <Text style={styles.label}>Server address (from call_server.py's startup log):</Text>
      <TextInput
        style={styles.input}
        placeholder="https://your-pc-lan-ip:8765"
        autoCapitalize="none"
        autoCorrect={false}
        value={serverUrl}
        onChangeText={setServerUrl}
      />
      <Button title="Save and register" onPress={handleRegister} />
      {!!status && <Text style={styles.status}>{status}</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: 'center', padding: 24 },
  title: { fontSize: 24, fontWeight: 'bold', marginBottom: 24, textAlign: 'center' },
  label: { marginBottom: 8 },
  input: {
    borderWidth: 1,
    borderColor: '#999',
    borderRadius: 8,
    padding: 12,
    marginBottom: 16,
  },
  status: { marginTop: 16, textAlign: 'center' },
});

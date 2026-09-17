import { NativeModule, requireNativeModule } from 'expo';

declare class AriaNativeModule extends NativeModule<{}> {
  /** Persists the server URL into the SharedPreferences file that the
   * native call/FCM code (com.aria.companion) reads from. */
  saveServerUrl(url: string): Promise<void>;
  getServerUrl(): Promise<string | null>;
  /** POSTs the FCM token to call_server.py's /register_device, trusting
   * its self-signed cert (this is our own local server). */
  registerDevice(serverUrl: string, token: string): Promise<string>;
  /** Opens the native in-call screen (which auto-starts a call), so you
   * can ring her yourself instead of only ever receiving. */
  openCallScreen(): Promise<void>;
}

export default requireNativeModule<AriaNativeModule>('AriaNative');

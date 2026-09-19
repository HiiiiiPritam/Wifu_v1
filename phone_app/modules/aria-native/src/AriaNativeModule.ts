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
  /** GET /settings -- the server's current settings, as a JSON string. */
  getSettings(serverUrl: string): Promise<string>;
  /** POST /settings with a JSON string (partial updates are fine). Returns
   * the saved settings as the server validated/clamped them. */
  saveSettings(serverUrl: string, json: string): Promise<string>;
  /** Any other server call; body is a JSON string, or '' for none. */
  request(serverUrl: string, method: string, path: string, body: string): Promise<string>;
  /** Saves her photo on the phone and uploads it to the server. */
  uploadAvatar(serverUrl: string, base64: string): Promise<string>;
  /** Refreshes the phone's copy of her photo; true if one exists. */
  cacheAvatar(serverUrl: string): Promise<boolean>;
  /** file:// URI of the phone's copy of her photo, or null. */
  avatarUri(): Promise<string | null>;
}

export default requireNativeModule<AriaNativeModule>('AriaNative');

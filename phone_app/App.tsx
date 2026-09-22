import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import * as ImagePicker from 'expo-image-picker';
import * as Notifications from 'expo-notifications';
import AriaNative from './modules/aria-native/src/AriaNativeModule';

// Home + Settings. The call screens themselves are pure native
// (IncomingCallActivity / InCallActivity, see native/), launched straight
// from the FCM push without touching this JS at all.
//
// Settings and the schedule live on the SERVER (phase1/settings.json and
// schedule.json), since the server decides when she calls. Everything goes
// through the native module -- React Native's own fetch() would reject
// the server's self-signed certificate.

type QuietWindow = { label: string; start: string; end: string };

type Settings = {
  name: string;
  voice: string;
  phone_number: string;
  show_number: boolean;
  calling_enabled: boolean;
  checkin_minutes: number;
  active_start: string;
  active_end: string;
  quiet_windows: QuietWindow[];
  max_call_duration_minutes: number;
  silence_nudge_seconds: number;
  end_of_speech_ms: number;
  mic_threshold: number;
  fillers_enabled: boolean;
  stt_engine: 'groq' | 'local';
};

type ScheduledCall = {
  id: string;
  at: string; // "YYYY-MM-DDTHH:MM", PC's local time
  note: string;
  status: 'pending' | 'done' | 'missed';
};

const VOICES: { id: string; label: string }[] = [
  { id: 'en-US-AnaNeural', label: 'Ana · cute' },
  { id: 'en-US-AvaNeural', label: 'Ava · warm' },
  { id: 'en-US-EmmaNeural', label: 'Emma · cheerful' },
  { id: 'en-US-JennyNeural', label: 'Jenny · friendly' },
  { id: 'en-US-AriaNeural', label: 'Aria · confident' },
  { id: 'en-GB-SoniaNeural', label: 'Sonia · British' },
  { id: 'en-IN-NeerjaNeural', label: 'Neerja · Indian' },
  { id: 'en-AU-NatashaNeural', label: 'Natasha · Australian' },
];

const COLORS = {
  bg: '#14121f',
  card: '#1f1b30',
  border: '#2f2946',
  text: '#f5f0ff',
  muted: '#a79fc2',
  accent: '#7b5fe0',
  pink: '#ff9ecf',
  good: '#2ecc71',
  bad: '#e74c3c',
};

// ------------------------------------------------------------- time helpers

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

function formatClock(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number);
  const suffix = h < 12 ? 'AM' : 'PM';
  const twelve = h % 12 === 0 ? 12 : h % 12;
  return `${twelve}:${pad(m)} ${suffix}`;
}

function shiftClock(hhmm: string, minutes: number): string {
  const [h, m] = hhmm.split(':').map(Number);
  const total = (((h * 60 + m + minutes) % 1440) + 1440) % 1440;
  return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
}

function formatMinutes(m: number): string {
  if (m < 60) return `${m} min`;
  const hours = m / 60;
  return Number.isInteger(hours) ? `${hours} hr` : `${hours.toFixed(1)} hr`;
}

// Bigger steps for bigger values, so going from 1 minute to 4 hours
// doesn't take a hundred taps.
function checkinStep(m: number, direction: 1 | -1): number {
  const probe = direction === 1 ? m : m - 1;
  if (probe < 10) return 1;
  if (probe < 60) return 5;
  if (probe < 240) return 15;
  return 60;
}

function dayLabel(offset: number): string {
  if (offset === 0) return 'Today';
  if (offset === 1) return 'Tomorrow';
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' });
}

function describeWhen(at: string): string {
  const [date, time] = at.split('T');
  const target = new Date(`${date}T00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((target.getTime() - today.getTime()) / 86400000);
  const day = days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : days === -1 ? 'Yesterday'
    : target.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  return `${day}, ${formatClock(time)}`;
}

function nextRoundTime(): string {
  const d = new Date(Date.now() + 15 * 60000);
  return `${pad(d.getHours())}:${pad(Math.ceil(d.getMinutes() / 5) * 5 % 60)}`;
}

// ---------------------------------------------------------------------- app

export default function App() {
  const [tab, setTab] = useState<'home' | 'settings'>('home');
  const [serverUrl, setServerUrl] = useState('');
  const [status, setStatus] = useState('');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [scheduled, setScheduled] = useState<ScheduledCall[]>([]);
  const [avatarUri, setAvatarUri] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveStatus, setSaveStatus] = useState('');

  const cleanUrl = () => serverUrl.trim().replace(/\/+$/, '');

  useEffect(() => {
    AriaNative.avatarUri().then(setAvatarUri);
    AriaNative.getServerUrl().then((saved) => {
      if (saved) {
        setServerUrl(saved);
        loadAll(saved);
      }
    });
  }, []);

  async function loadAll(url: string) {
    setBusy(true);
    try {
      setSettings(JSON.parse(await AriaNative.getSettings(url)));
      setDirty(false);
      setSaveStatus('');
      setStatus('');
      await loadSchedule(url);
      // Keep the phone's copy of her photo in sync with the server's, so
      // the incoming-call screen shows the right one.
      await AriaNative.cacheAvatar(url);
      setAvatarUri(await AriaNative.avatarUri());
    } catch (err: any) {
      setStatus(`Couldn't reach the call server: ${err?.message ?? err}\nIs it running?`);
    } finally {
      setBusy(false);
    }
  }

  async function loadSchedule(url: string) {
    setScheduled(JSON.parse(await AriaNative.request(url, 'GET', '/schedule', '')));
  }

  // Builds on the latest state, not this render's copy, so two updates
  // in one tap (e.g. start AND end time) don't overwrite each other.
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    setSettings((prev) => (prev ? { ...prev, [key]: value } : prev));
    setDirty(true);
    setSaveStatus('');
  }

  async function saveSettings() {
    if (!settings) return;
    setBusy(true);
    try {
      // The server validates and clamps; show what it actually saved.
      const saved = await AriaNative.saveSettings(cleanUrl(), JSON.stringify(settings));
      setSettings(JSON.parse(saved));
      setDirty(false);
      setSaveStatus('Saved ✓');
    } catch (err: any) {
      setSaveStatus(`Couldn't save: ${err?.message ?? err}`);
    } finally {
      setBusy(false);
    }
  }

  async function pickPhoto() {
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images'],
      allowsEditing: true,
      aspect: [1, 1],
      quality: 0.6,
      base64: true,
    });
    if (result.canceled || !result.assets[0]?.base64) return;
    try {
      await AriaNative.uploadAvatar(cleanUrl(), result.assets[0].base64);
      setAvatarUri(await AriaNative.avatarUri());
    } catch (err: any) {
      setStatus(`Couldn't save her photo: ${err?.message ?? err}`);
    }
  }

  async function handleRegister() {
    const url = cleanUrl();
    if (!url) {
      setStatus('Enter the server URL first.');
      return;
    }
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
      setStatus('Registered! She can call you now.');
      loadAll(url);
    } catch (err: any) {
      setStatus(
        `Couldn't finish setup: ${err?.message ?? err}\n` +
          'Make sure the call server is running and your phone is on the same WiFi.'
      );
    }
  }

  async function handleCallHer() {
    const url = cleanUrl();
    if (!url) {
      setTab('settings');
      setStatus('Enter and save the server URL first.');
      return;
    }
    try {
      // Saved first: the native call screen reads the URL from storage,
      // not from this screen's state.
      await AriaNative.saveServerUrl(url);
      await AriaNative.openCallScreen();
    } catch (err: any) {
      setStatus(`Couldn't open the call screen: ${err?.message ?? err}`);
    }
  }

  const name = settings?.name || 'Aria';
  const upcoming = scheduled.filter((c) => c.status === 'pending');

  return (
    <View style={styles.root}>
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
        {tab === 'home' ? (
          <HomeTab
            name={name}
            number={settings?.show_number ? settings.phone_number : ''}
            avatarUri={avatarUri}
            upcoming={upcoming}
            connected={!!settings}
            busy={busy}
            status={status}
            onCall={handleCallHer}
            onRetry={() => loadAll(cleanUrl())}
            onOpenSettings={() => setTab('settings')}
          />
        ) : (
          <>
            <Text style={styles.pageTitle}>Settings</Text>
            {settings ? (
              <SettingsTab
                settings={settings}
                update={update}
                avatarUri={avatarUri}
                onPickPhoto={pickPhoto}
                scheduled={scheduled}
                serverUrl={cleanUrl()}
                reloadSchedule={() => loadSchedule(cleanUrl())}
              />
            ) : busy ? (
              <ActivityIndicator color={COLORS.pink} style={{ margin: 24 }} />
            ) : null}
            <Section title="Connection">
              <Text style={styles.label}>Server address (from the call server's startup log)</Text>
              <TextInput
                style={styles.input}
                placeholder="https://192.168.x.x:8765"
                placeholderTextColor={COLORS.muted}
                autoCapitalize="none"
                autoCorrect={false}
                value={serverUrl}
                onChangeText={setServerUrl}
              />
              <View style={styles.row}>
                <SmallButton label="Save & register" onPress={handleRegister} />
                <SmallButton label="Reload" onPress={() => loadAll(cleanUrl())} />
              </View>
              {!!status && <Text style={styles.status}>{status}</Text>}
            </Section>
          </>
        )}
      </ScrollView>

      {tab === 'settings' && settings && (dirty || !!saveStatus) && (
        <View style={styles.saveBar}>
          <Text style={styles.saveBarText}>{dirty ? 'Unsaved changes' : saveStatus}</Text>
          {dirty && (
            <Pressable style={styles.saveButton} onPress={saveSettings} disabled={busy}>
              <Text style={styles.saveButtonText}>{busy ? 'Saving…' : 'Save'}</Text>
            </Pressable>
          )}
        </View>
      )}

      <View style={styles.tabBar}>
        <TabButton label="Home" icon="♡" active={tab === 'home'} onPress={() => setTab('home')} />
        <TabButton label="Settings" icon="⚙" active={tab === 'settings'} onPress={() => setTab('settings')} />
      </View>
    </View>
  );
}

// -------------------------------------------------------------------- home

function HomeTab(props: {
  name: string;
  number: string;
  avatarUri: string | null;
  upcoming: ScheduledCall[];
  connected: boolean;
  busy: boolean;
  status: string;
  onCall: () => void;
  onRetry: () => void;
  onOpenSettings: () => void;
}) {
  return (
    <View style={styles.home}>
      <Image source={require('./assets/wordmark.png')} style={styles.wordmark} resizeMode="contain" />
      <Avatar uri={props.avatarUri} name={props.name} size={132} />
      <Text style={styles.homeName}>{props.name}</Text>
      {!!props.number && <Text style={styles.homeNumber}>{props.number}</Text>}

      <Pressable style={styles.callButton} onPress={props.onCall}>
        <Text style={styles.callButtonText}>📞  Call {props.name}</Text>
      </Pressable>

      {props.upcoming.length > 0 && (
        <View style={[styles.card, styles.nextCall]}>
          <Text style={styles.label}>
            {props.upcoming.length === 1 ? 'Scheduled call' : `${props.upcoming.length} scheduled calls`}
          </Text>
          {props.upcoming.map((c, i) => (
            <View key={c.id} style={i > 0 ? styles.upcomingRow : undefined}>
              <Text style={styles.nextCallWhen}>{describeWhen(c.at)}</Text>
              {!!c.note && <Text style={styles.nextCallNote}>{c.note}</Text>}
            </View>
          ))}
        </View>
      )}

      {!props.connected && (
        <View style={[styles.card, styles.nextCall]}>
          {props.busy ? (
            <ActivityIndicator color={COLORS.pink} />
          ) : (
            <>
              <Text style={styles.rowLabel}>Not connected to the call server.</Text>
              {!!props.status && <Text style={styles.status}>{props.status}</Text>}
              <View style={[styles.row, { marginTop: 12 }]}>
                <SmallButton label="Retry" onPress={props.onRetry} />
                <SmallButton label="Set up" onPress={props.onOpenSettings} />
              </View>
            </>
          )}
        </View>
      )}
    </View>
  );
}

// ---------------------------------------------------------------- settings

function SettingsTab(props: {
  settings: Settings;
  update: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
  avatarUri: string | null;
  onPickPhoto: () => void;
  scheduled: ScheduledCall[];
  serverUrl: string;
  reloadSchedule: () => Promise<void>;
}) {
  const { settings, update } = props;
  const allDay = settings.active_start === settings.active_end;

  function setWindow(i: number, change: Partial<QuietWindow>) {
    update('quiet_windows', settings.quiet_windows.map((w, j) => (j === i ? { ...w, ...change } : w)));
  }

  return (
    <>
      <Section title="Her">
        <View style={styles.profileRow}>
          <Pressable onPress={props.onPickPhoto}>
            <Avatar uri={props.avatarUri} name={settings.name} size={76} />
            <Text style={styles.changePhoto}>Change photo</Text>
          </Pressable>
          <View style={{ flex: 1 }}>
            <Text style={styles.label}>Name</Text>
            <TextInput
              style={styles.input}
              value={settings.name}
              maxLength={40}
              onChangeText={(v) => update('name', v)}
            />
          </View>
        </View>
        <Text style={styles.label}>Phone number (shown when she calls -- any number you like)</Text>
        <TextInput
          style={styles.input}
          value={settings.phone_number}
          keyboardType="phone-pad"
          maxLength={24}
          onChangeText={(v) => update('phone_number', v)}
        />
        <ToggleRow
          label="Show her number on calls"
          value={settings.show_number}
          onChange={(v) => update('show_number', v)}
        />
        <Text style={styles.label}>Voice</Text>
        <View style={styles.chips}>
          {VOICES.map((v) => (
            <Chip key={v.id} label={v.label} selected={settings.voice === v.id} onPress={() => update('voice', v.id)} />
          ))}
        </View>
      </Section>

      <Section title="Check-in calls">
        <ToggleRow
          label="She calls me on her own"
          value={settings.calling_enabled}
          onChange={(v) => update('calling_enabled', v)}
        />
        <Stepper
          label="After I've been quiet for"
          display={formatMinutes(settings.checkin_minutes)}
          onMinus={() => update('checkin_minutes', Math.max(1, settings.checkin_minutes - checkinStep(settings.checkin_minutes, -1)))}
          onPlus={() => update('checkin_minutes', Math.min(1440, settings.checkin_minutes + checkinStep(settings.checkin_minutes, 1)))}
        />
        <Text style={styles.hint}>
          If she rings and you don't answer, she waits twice as long before trying again.
        </Text>
        <ToggleRow
          label="Any time of day"
          value={allDay}
          onChange={(v) => {
            update('active_start', v ? '00:00' : '09:00');
            update('active_end', v ? '00:00' : '23:00');
          }}
        />
        {!allDay && (
          <>
            <Stepper
              label="Only from"
              display={formatClock(settings.active_start)}
              onMinus={() => update('active_start', shiftClock(settings.active_start, -30))}
              onPlus={() => update('active_start', shiftClock(settings.active_start, 30))}
            />
            <Stepper
              label="until"
              display={formatClock(settings.active_end)}
              onMinus={() => update('active_end', shiftClock(settings.active_end, -30))}
              onPlus={() => update('active_end', shiftClock(settings.active_end, 30))}
            />
          </>
        )}
      </Section>

      <Section title="Don't call during">
        {settings.quiet_windows.length === 0 && (
          <Text style={styles.hint}>No quiet times -- she can call whenever check-ins allow.</Text>
        )}
        {settings.quiet_windows.map((w, i) => (
          <View key={i} style={styles.windowCard}>
            <View style={styles.windowHeader}>
              <TextInput
                style={[styles.input, styles.windowLabel]}
                value={w.label}
                maxLength={30}
                placeholder="Label (e.g. Class)"
                placeholderTextColor={COLORS.muted}
                onChangeText={(v) => setWindow(i, { label: v })}
              />
              <Pressable
                hitSlop={10}
                onPress={() => update('quiet_windows', settings.quiet_windows.filter((_, j) => j !== i))}
              >
                <Text style={styles.remove}>Remove</Text>
              </Pressable>
            </View>
            <Stepper
              label="From"
              display={formatClock(w.start)}
              onMinus={() => setWindow(i, { start: shiftClock(w.start, -30) })}
              onPlus={() => setWindow(i, { start: shiftClock(w.start, 30) })}
            />
            <Stepper
              label="To"
              display={formatClock(w.end)}
              onMinus={() => setWindow(i, { end: shiftClock(w.end, -30) })}
              onPlus={() => setWindow(i, { end: shiftClock(w.end, 30) })}
            />
          </View>
        ))}
        {settings.quiet_windows.length < 10 && (
          <SmallButton
            label="+ Add quiet time"
            onPress={() => update('quiet_windows', [...settings.quiet_windows, { label: 'Busy', start: '10:00', end: '13:00' }])}
          />
        )}
        <Text style={[styles.hint, { marginTop: 10 }]}>
          Scheduled calls still ring during these -- you set those up for that exact time.
        </Text>
      </Section>

      <ScheduleSection
        scheduled={props.scheduled}
        serverUrl={props.serverUrl}
        reload={props.reloadSchedule}
      />

      <Section title="During a call">
        <Stepper
          label="She speaks up after silence of"
          display={`${settings.silence_nudge_seconds} s`}
          onMinus={() => update('silence_nudge_seconds', Math.max(5, settings.silence_nudge_seconds - 5))}
          onPlus={() => update('silence_nudge_seconds', Math.min(300, settings.silence_nudge_seconds + 5))}
        />
        <Stepper
          label="My pause before she answers"
          display={`${(settings.end_of_speech_ms / 1000).toFixed(1)} s`}
          onMinus={() => update('end_of_speech_ms', Math.max(500, settings.end_of_speech_ms - 100))}
          onPlus={() => update('end_of_speech_ms', Math.min(4000, settings.end_of_speech_ms + 100))}
        />
        <Text style={styles.hint}>Raise this if she cuts you off mid-sentence.</Text>
        <Stepper
          label="Longest call"
          display={formatMinutes(settings.max_call_duration_minutes)}
          onMinus={() => update('max_call_duration_minutes', Math.max(1, settings.max_call_duration_minutes - (settings.max_call_duration_minutes > 10 ? 5 : 1)))}
          onPlus={() => update('max_call_duration_minutes', Math.min(180, settings.max_call_duration_minutes + (settings.max_call_duration_minutes >= 10 ? 5 : 1)))}
        />
        <Stepper
          label="Mic sensitivity threshold"
          display={`${settings.mic_threshold}`}
          onMinus={() => update('mic_threshold', Math.max(2, settings.mic_threshold - 1))}
          onPlus={() => update('mic_threshold', Math.min(60, settings.mic_threshold + 1))}
        />
        <Text style={styles.hint}>Lower hears quieter speech; raise it if background noise sets it off.</Text>
        <ToggleRow
          label={'"Hmm..." sounds while she thinks'}
          value={settings.fillers_enabled}
          onChange={(v) => update('fillers_enabled', v)}
        />
        <Text style={styles.label}>Speech recognition</Text>
        <View style={styles.chips}>
          <Chip label="Groq · faster" selected={settings.stt_engine === 'groq'} onPress={() => update('stt_engine', 'groq')} />
          <Chip label="On my PC · private" selected={settings.stt_engine === 'local'} onPress={() => update('stt_engine', 'local')} />
        </View>
      </Section>
    </>
  );
}

// One-time calls save immediately (no Save button): each is its own small
// thing, and a scheduled call silently not existing because you forgot to
// press Save would be exactly the wrong kind of surprise.
function ScheduleSection(props: {
  scheduled: ScheduledCall[];
  serverUrl: string;
  reload: () => Promise<void>;
}) {
  const [dayOffset, setDayOffset] = useState(0);
  const [time, setTime] = useState(nextRoundTime());
  const [note, setNote] = useState('');
  const [message, setMessage] = useState('');
  // A ref, not state: a double tap fires twice before a state update would
  // re-render, and the second request used to come back as "already a call
  // scheduled at that time" even though the first had saved it.
  const saving = useRef(false);

  const pending = props.scheduled.filter((c) => c.status === 'pending');
  const finished = props.scheduled.filter((c) => c.status !== 'pending').slice(-3).reverse();

  async function add() {
    if (saving.current) return;
    saving.current = true;
    const d = new Date();
    d.setDate(d.getDate() + dayOffset);
    const at = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${time}`;
    try {
      await AriaNative.request(props.serverUrl, 'POST', '/schedule', JSON.stringify({ at, note }));
      setNote('');
      setMessage(`Scheduled for ${describeWhen(at)} ✓`);
      await props.reload();
    } catch (err: any) {
      setMessage(`Couldn't schedule it: ${err?.message ?? err}`);
    } finally {
      saving.current = false;
    }
  }

  async function remove(id: string) {
    try {
      await AriaNative.request(props.serverUrl, 'DELETE', `/schedule/${id}`, '');
      await props.reload();
    } catch (err: any) {
      setMessage(`Couldn't remove it: ${err?.message ?? err}`);
    }
  }

  return (
    <Section title="Scheduled calls">
      {pending.map((c) => (
        <View key={c.id} style={styles.scheduledRow}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>{describeWhen(c.at)}</Text>
            {!!c.note && <Text style={styles.scheduledNote}>{c.note}</Text>}
          </View>
          <Pressable hitSlop={10} onPress={() => remove(c.id)}>
            <Text style={styles.remove}>Cancel</Text>
          </Pressable>
        </View>
      ))}
      {finished.map((c) => (
        <View key={c.id} style={[styles.scheduledRow, { opacity: 0.5 }]}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>{describeWhen(c.at)} · {c.status === 'done' ? 'done' : 'missed'}</Text>
            {!!c.note && <Text style={styles.scheduledNote}>{c.note}</Text>}
          </View>
        </View>
      ))}

      <Text style={[styles.label, { marginTop: pending.length || finished.length ? 14 : 0 }]}>New call</Text>
      <View style={styles.chips}>
        {[0, 1, 2, 3, 4, 5, 6].map((o) => (
          <Chip key={o} label={dayLabel(o)} selected={dayOffset === o} onPress={() => setDayOffset(o)} />
        ))}
      </View>
      <Stepper
        label="At"
        display={formatClock(time)}
        onMinus={() => setTime(shiftClock(time, -5))}
        onPlus={() => setTime(shiftClock(time, 5))}
      />
      <View style={[styles.row, { marginBottom: 8 }]}>
        <SmallButton label="−1 hr" onPress={() => setTime(shiftClock(time, -60))} />
        <SmallButton label="+1 hr" onPress={() => setTime(shiftClock(time, 60))} />
      </View>
      <TextInput
        style={styles.input}
        value={note}
        maxLength={200}
        placeholder="Why she's calling (optional) -- e.g. reminder for medicine"
        placeholderTextColor={COLORS.muted}
        onChangeText={setNote}
      />
      <SmallButton label="Schedule call" onPress={add} />
      {!!message && <Text style={styles.status}>{message}</Text>}
      <Text style={[styles.hint, { marginTop: 10 }]}>
        She opens the call with the reason. A scheduled call replaces any check-in call within 30
        minutes of it, and if you're already talking she just brings it up then.
      </Text>
    </Section>
  );
}

// ------------------------------------------------------------------- parts

function Avatar({ uri, name, size }: { uri: string | null; name: string; size: number }) {
  const style = { width: size, height: size, borderRadius: size / 2 };
  if (uri) return <Image source={{ uri }} style={[style, styles.avatarImage]} />;
  return (
    <View style={[style, styles.avatarFallback]}>
      <Text style={{ color: 'white', fontSize: size * 0.42 }}>{(name || 'A').charAt(0).toUpperCase()}</Text>
    </View>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <View style={styles.card}>{children}</View>
    </View>
  );
}

function TabButton(props: { label: string; icon: string; active: boolean; onPress: () => void }) {
  return (
    <Pressable style={styles.tabButton} onPress={props.onPress}>
      <Text style={[styles.tabIcon, props.active && styles.tabActive]}>{props.icon}</Text>
      <Text style={[styles.tabLabel, props.active && styles.tabActive]}>{props.label}</Text>
    </Pressable>
  );
}

function SmallButton({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable style={styles.smallButton} onPress={onPress}>
      <Text style={styles.smallButtonText}>{label}</Text>
    </Pressable>
  );
}

function Chip({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable style={[styles.chip, selected && styles.chipSelected]} onPress={onPress}>
      <Text style={[styles.chipText, selected && styles.chipTextSelected]}>{label}</Text>
    </Pressable>
  );
}

function ToggleRow({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <View style={styles.stepperRow}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Switch
        value={value}
        onValueChange={onChange}
        trackColor={{ true: COLORS.accent, false: COLORS.border }}
        thumbColor={value ? COLORS.pink : '#ccc'}
      />
    </View>
  );
}

function Stepper(props: { label: string; display: string; onMinus: () => void; onPlus: () => void }) {
  return (
    <View style={styles.stepperRow}>
      <Text style={styles.rowLabel}>{props.label}</Text>
      <View style={styles.stepper}>
        <Pressable style={styles.stepButton} onPress={props.onMinus} hitSlop={8}>
          <Text style={styles.stepButtonText}>−</Text>
        </Pressable>
        <Text style={styles.stepValue}>{props.display}</Text>
        <Pressable style={styles.stepButton} onPress={props.onPlus} hitSlop={8}>
          <Text style={styles.stepButtonText}>+</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: COLORS.bg },
  scroll: { padding: 20, paddingTop: 56, paddingBottom: 140 },
  pageTitle: { color: COLORS.text, fontSize: 28, fontWeight: '700', marginBottom: 4 },

  home: { alignItems: 'center', paddingTop: 4 },
  wordmark: { width: 190, height: 49, marginBottom: 36 },
  homeName: { color: COLORS.text, fontSize: 30, fontWeight: '600', marginTop: 16 },
  homeNumber: { color: COLORS.muted, fontSize: 15, marginTop: 4 },
  callButton: {
    marginTop: 28,
    backgroundColor: COLORS.good,
    borderRadius: 30,
    paddingVertical: 16,
    paddingHorizontal: 40,
  },
  callButtonText: { color: 'white', fontSize: 18, fontWeight: '700' },
  nextCall: { alignSelf: 'stretch', marginTop: 32 },
  nextCallWhen: { color: COLORS.pink, fontSize: 18, fontWeight: '600' },
  nextCallNote: { color: COLORS.text, marginTop: 4 },
  upcomingRow: { marginTop: 12, paddingTop: 12, borderTopWidth: 1, borderTopColor: COLORS.border },

  avatarImage: { borderWidth: 3, borderColor: COLORS.pink },
  avatarFallback: {
    backgroundColor: '#b967d6',
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 3,
    borderColor: COLORS.pink,
  },
  profileRow: { flexDirection: 'row', gap: 16, alignItems: 'center', marginBottom: 4 },
  changePhoto: { color: COLORS.pink, fontSize: 12, textAlign: 'center', marginTop: 6 },

  section: { marginTop: 20 },
  sectionTitle: {
    color: COLORS.muted,
    fontSize: 13,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 1,
    marginBottom: 8,
    marginLeft: 4,
  },
  card: {
    backgroundColor: COLORS.card,
    borderRadius: 16,
    padding: 16,
    borderWidth: 1,
    borderColor: COLORS.border,
  },
  label: { color: COLORS.muted, fontSize: 13, marginBottom: 6, marginTop: 4 },
  input: {
    borderWidth: 1,
    borderColor: COLORS.border,
    backgroundColor: COLORS.bg,
    color: COLORS.text,
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
  },
  row: { flexDirection: 'row', gap: 10, flexWrap: 'wrap' },
  smallButton: {
    backgroundColor: COLORS.accent,
    borderRadius: 10,
    paddingVertical: 10,
    paddingHorizontal: 14,
    alignSelf: 'flex-start',
  },
  smallButtonText: { color: 'white', fontWeight: '600' },
  status: { color: COLORS.muted, marginTop: 12, lineHeight: 20 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 8 },
  chip: {
    borderWidth: 1,
    borderColor: COLORS.border,
    borderRadius: 18,
    paddingVertical: 7,
    paddingHorizontal: 12,
  },
  chipSelected: { backgroundColor: COLORS.accent, borderColor: COLORS.accent },
  chipText: { color: COLORS.muted, fontSize: 13 },
  chipTextSelected: { color: 'white', fontWeight: '600' },
  stepperRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 8,
    gap: 12,
  },
  rowLabel: { color: COLORS.text, fontSize: 15, flexShrink: 1 },
  stepper: { flexDirection: 'row', alignItems: 'center' },
  stepButton: {
    width: 34,
    height: 34,
    borderRadius: 17,
    backgroundColor: COLORS.border,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepButtonText: { color: COLORS.text, fontSize: 20, lineHeight: 22 },
  stepValue: { color: COLORS.pink, fontSize: 15, fontWeight: '600', minWidth: 84, textAlign: 'center' },
  hint: { color: COLORS.muted, fontSize: 12, marginTop: -2, marginBottom: 6, lineHeight: 17 },

  windowCard: { borderBottomWidth: 1, borderBottomColor: COLORS.border, paddingBottom: 8, marginBottom: 12 },
  windowHeader: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  windowLabel: { flex: 1, marginBottom: 0, paddingVertical: 8 },
  remove: { color: COLORS.bad, fontWeight: '600' },

  scheduledRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.border,
    gap: 12,
  },
  scheduledNote: { color: COLORS.muted, marginTop: 2 },

  saveBar: {
    position: 'absolute',
    left: 16,
    right: 16,
    bottom: 76,
    backgroundColor: COLORS.card,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.accent,
    paddingVertical: 10,
    paddingHorizontal: 14,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  saveBarText: { color: COLORS.text },
  saveButton: { backgroundColor: COLORS.accent, borderRadius: 10, paddingVertical: 8, paddingHorizontal: 18 },
  saveButtonText: { color: 'white', fontWeight: '700' },

  tabBar: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    height: 64,
    flexDirection: 'row',
    backgroundColor: '#1a1729',
    borderTopWidth: 1,
    borderTopColor: COLORS.border,
  },
  tabButton: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  tabIcon: { color: COLORS.muted, fontSize: 20 },
  tabLabel: { color: COLORS.muted, fontSize: 12, marginTop: 2 },
  tabActive: { color: COLORS.pink },
});

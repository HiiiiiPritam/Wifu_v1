"""
Call settings, editable from the phone app (GET/POST /settings) so
nothing needs a code edit + server restart. Stored in phase1/settings.json
(gitignored). Every value is validated and clamped on the way in, so a bad
value from the app can't break the server.

One-time scheduled calls are NOT here -- they're data, not settings, and
live in schedule.py / schedule.json.
"""
import json
import re

from shared import ROOT
from shared.tts import DEFAULT_VOICE

SETTINGS_PATH = ROOT / "settings.json"

DEFAULTS = {
    # --- Her ---
    "name": "Aria",
    "voice": DEFAULT_VOICE,
    # Shown under her name on the incoming-call screen, so it looks like a
    # real contact calling. Purely cosmetic -- no real number is involved.
    "phone_number": "+91 98765 43210",
    "show_number": True,
    # --- Periodic calls ("she checks in on her own") ---
    "calling_enabled": True,
    "checkin_minutes": 120,  # quiet this long since you last talked -> she calls
    # Periodic calls only happen inside this window (24h "HH:MM", local
    # time). Equal start and end = any time of day.
    "active_start": "00:00",
    "active_end": "00:00",
    # She never STARTS a periodic call inside any of these. Scheduled calls
    # still ring -- you set those up explicitly for that exact time.
    "quiet_windows": [{"label": "Sleep", "start": "23:00", "end": "08:00"}],
    "max_call_duration_minutes": 30,
    # --- During a call ---
    "silence_nudge_seconds": 20,  # quiet this long mid-call -> she speaks up
    # How long you have to stop talking before it counts as the end of your
    # turn. Too low and she cuts you off mid-thought; too high and every
    # reply feels slow.
    "end_of_speech_ms": 1400,
    "mic_threshold": 14,  # mic level that counts as speech; raise if noise triggers it
    "fillers_enabled": True,  # "hmm..." / "mm-hmm" while she thinks
    # "groq": fast + accurate, the audio clip is sent to Groq.
    # "local": stays on your PC, slower (runs on the CPU).
    "stt_engine": "groq",
}

MAX_QUIET_WINDOWS = 10

# // comments are allowed in settings.json even though plain JSON forbids
# them: a single hand-added comment used to make the whole file unreadable,
# which silently reset EVERY setting to its default (calls went from every
# minute to every two hours with no visible sign why). The group keeps
# "//" inside quoted strings (e.g. URLs) intact.
_COMMENT_RE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*')
_last_error: str | None = None


def load() -> dict:
    global _last_error
    if not SETTINGS_PATH.exists():
        return _validate(dict(DEFAULTS))
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        data = json.loads(_COMMENT_RE.sub(lambda m: m.group(1) or "", text))
        _last_error = None
    except Exception as e:
        # Loud, but only once per distinct problem -- this runs every 15s.
        if str(e) != _last_error:
            print(
                f"\n!!! settings.json can't be read ({e}).\n"
                "!!! Using DEFAULT settings until it's fixed -- your changes are NOT applied.\n"
            )
            _last_error = str(e)
        return _validate(dict(DEFAULTS))
    return _validate({**DEFAULTS, **_migrate(data)})


def save(settings: dict) -> dict:
    settings = _validate({**DEFAULTS, **_migrate(settings)})
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def _migrate(data: dict) -> dict:
    """Older settings files had a single whole-hour quiet window
    (quiet_hours_start/end). Carried over as the first quiet window."""
    data = dict(data)
    if "quiet_windows" not in data and "quiet_hours_start" in data:
        start = _hour(data.get("quiet_hours_start"), 23)
        end = _hour(data.get("quiet_hours_end"), 8)
        data["quiet_windows"] = [
            {"label": "Quiet hours", "start": f"{start:02d}:00", "end": f"{end:02d}:00"}
        ]
    data.pop("quiet_hours_start", None)
    data.pop("quiet_hours_end", None)
    return data


def _validate(s: dict) -> dict:
    out = dict(DEFAULTS)
    out["name"] = str(s.get("name") or "").strip()[:40] or DEFAULTS["name"]
    voice = str(s.get("voice") or "").strip()
    # edge-tts voice ids look like "en-US-AnaNeural"
    out["voice"] = voice if voice.endswith("Neural") and len(voice) < 60 else DEFAULT_VOICE
    out["phone_number"] = re.sub(r"[^0-9+()\- ]", "", str(s.get("phone_number") or ""))[:24]
    out["show_number"] = bool(s.get("show_number", True))
    out["calling_enabled"] = bool(s.get("calling_enabled", True))
    out["checkin_minutes"] = _clamp(s.get("checkin_minutes"), 1, 24 * 60, DEFAULTS["checkin_minutes"])
    out["active_start"] = _hhmm(s.get("active_start"), DEFAULTS["active_start"])
    out["active_end"] = _hhmm(s.get("active_end"), DEFAULTS["active_end"])
    windows = s.get("quiet_windows")
    if not isinstance(windows, list):
        windows = DEFAULTS["quiet_windows"]
    out["quiet_windows"] = [
        {
            "label": str(w.get("label") or "").strip()[:30] or "Quiet",
            "start": _hhmm(w.get("start"), "23:00"),
            "end": _hhmm(w.get("end"), "08:00"),
        }
        for w in windows[:MAX_QUIET_WINDOWS]
        if isinstance(w, dict)
    ]
    out["max_call_duration_minutes"] = _clamp(
        s.get("max_call_duration_minutes"), 1, 180, DEFAULTS["max_call_duration_minutes"]
    )
    out["silence_nudge_seconds"] = _clamp(
        s.get("silence_nudge_seconds"), 5, 300, DEFAULTS["silence_nudge_seconds"]
    )
    out["end_of_speech_ms"] = int(
        _clamp(s.get("end_of_speech_ms"), 500, 4000, DEFAULTS["end_of_speech_ms"])
    )
    out["mic_threshold"] = _clamp(s.get("mic_threshold"), 2, 60, DEFAULTS["mic_threshold"])
    out["fillers_enabled"] = bool(s.get("fillers_enabled", True))
    out["stt_engine"] = s.get("stt_engine") if s.get("stt_engine") in ("groq", "local") else "groq"
    return out


def _hour(value, default: int) -> int:
    try:
        return int(value) % 24
    except (TypeError, ValueError):
        return default


def _hhmm(value, default: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    v = max(lo, min(hi, v))
    return int(v) if v == int(v) else v


def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(start: str, end: str, minute_of_day: int) -> bool:
    """start == end means "all day". start > end wraps past midnight
    (23:00 -> 08:00 covers the night)."""
    a, b = minutes_of(start), minutes_of(end)
    if a == b:
        return True
    if a < b:
        return a <= minute_of_day < b
    return minute_of_day >= a or minute_of_day < b


def periodic_allowed_now(settings: dict, minute_of_day: int) -> bool:
    """Inside her active hours and outside every quiet window."""
    if not in_window(settings["active_start"], settings["active_end"], minute_of_day):
        return False
    return not any(
        w["start"] != w["end"] and in_window(w["start"], w["end"], minute_of_day)
        for w in settings["quiet_windows"]
    )

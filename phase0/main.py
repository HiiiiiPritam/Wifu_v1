"""
Phase 2: she remembers you across sessions, and can check in first.

Mic (auto-stop on silence) -> faster-whisper (local STT)
                            -> Groq (cloud LLM, free tier)
                            -> edge-tts (cloud TTS, free, expressive voices)
                            -> VTube Studio (expression hotkey + live lip sync)
                            -> speakers

Setup (one time):
    pip install -r requirements.txt
    copy .env.example to .env and add your GROQ_API_KEY
    Open VTube Studio, load a model, enable Settings -> Start API

Run:
    python main.py
"""
import asyncio
import msvcrt
import os
import queue
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import edge_tts
import numpy as np
import soundfile as sf
import sounddevice as sd
import webrtcvad
import websockets
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from groq import Groq

import memory
import vts_face
from persona import SYSTEM_PROMPT

SAMPLE_RATE = 16000  # required by both webrtcvad and Whisper
LLM_MODEL = "openai/gpt-oss-20b"  # fast + free-tier friendly on Groq

# Cute/soft-spoken free neural voice. Swap for any name from:
#   python -m edge_tts --list-voices
VOICE_NAME = "en-US-AnaNeural"

FRAME_MS = 30  # webrtcvad only accepts 10/20/30ms frames
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_TIMEOUT_MS = 900  # stop ~0.9s after you stop talking
MAX_LEADING_SILENCE_MS = 6000  # give up if you never start talking

# --- Proactive check-in ("she calls you first") ---
# If you haven't said anything for this long while she's waiting, she
# speaks up unprompted. Lower this for testing (e.g. 30) so you don't
# have to actually wait 15 minutes to see it happen.
PROACTIVE_CHECKIN_SECONDS = 0.5 * 60
# Local 24h hours during which she won't proactively check in (start, end).
# (22, 9) = won't initiate between 10pm and 9am. Doesn't stop her from
# replying if you talk to her during quiet hours, just from starting it.
PROACTIVE_QUIET_HOURS = (22, 9)
# After this many proactive check-ins in a row with no real reply from
# you, stop initiating and just wait silently -- don't talk to an empty
# room forever burning API/TTS usage while you're actually away.
MAX_CONSECUTIVE_PROACTIVE = 2

# Maps persona.py's emotion tags to expression files (Settings -> Hotkeys ->
# each hotkey's assigned .exp3.json on your model). Driven directly via
# vts_face.set_expression (explicit on/off), NOT via hotkey triggering --
# VTube Studio's "Set/Unset Expression" hotkeys actually TOGGLE, which
# desyncs across a multi-turn conversation (an expression can get stuck
# active with nothing left to turn it off). Rename the values here if your
# model's expression files are named differently.
EMOTION_TAG_RE = re.compile(r"^\[(\w+)\]\s*", re.IGNORECASE)
EMOTION_TO_EXPRESSION_FILE = {
    "heart": "Blushing.exp3.json",
    "cry": "Sad.exp3.json",
    "angry": "Angry.exp3.json",
    "shock": "Surprised.exp3.json",
    "neutral": "Normal.exp3.json",
}
ALL_EXPRESSION_FILES = set(EMOTION_TO_EXPRESSION_FILE.values())


async def wait_for_enter_or_timeout(timeout_s: float | None) -> bool:
    """Waits for you to press Enter. Returns True if you did, False if
    timeout_s elapsed first with no keypress. timeout_s=None waits forever.
    Polls msvcrt instead of blocking on input() so we can give up on a
    timeout without leaving an orphaned blocking read behind (which the
    executor/asyncio.wait_for approach would do on Windows)."""
    start = time.monotonic()
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                return True
            # any other key: ignore, keep waiting -- mirrors input()'s
            # "only Enter submits" behavior closely enough for our purposes
        if timeout_s is not None and (time.monotonic() - start) >= timeout_s:
            return False
        await asyncio.sleep(0.15)


def capture_until_silence(vad: webrtcvad.Vad) -> np.ndarray:
    print("Listening... (stops automatically when you stop talking)")

    frame_queue: "queue.Queue[bytes]" = queue.Queue()

    def callback(indata, frame_count, time_info, status):
        frame_queue.put(bytes(indata))

    frames: list[bytes] = []
    triggered = False
    silence_ms = 0
    leading_silence_ms = 0

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
        dtype="int16",
        channels=1,
        callback=callback,
    ):
        while True:
            frame = frame_queue.get()
            is_speech = vad.is_speech(frame, SAMPLE_RATE)
            if not triggered:
                if is_speech:
                    triggered = True
                    frames.append(frame)
                else:
                    leading_silence_ms += FRAME_MS
                    if leading_silence_ms >= MAX_LEADING_SILENCE_MS:
                        return np.zeros(0, dtype="float32")
            else:
                frames.append(frame)
                silence_ms = 0 if is_speech else silence_ms + FRAME_MS
                if silence_ms >= SILENCE_TIMEOUT_MS:
                    break

    audio_bytes = b"".join(frames)
    return np.frombuffer(audio_bytes, dtype="int16").astype("float32") / 32768.0


def transcribe(whisper: WhisperModel, audio: np.ndarray) -> str:
    if audio.size == 0:
        return ""
    segments, _ = whisper.transcribe(audio, language="en", beam_size=1)
    text = " ".join(seg.text.strip() for seg in segments)
    return text.strip()


def _chat_and_parse(
    client: Groq, system_prompt: str, messages: list[dict]
) -> tuple[str, str]:
    """Calls the LLM and splits its reply into (emotion_tag, clean_text)."""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        temperature=0.9,
        max_tokens=200,
        reasoning_effort="low",  # gpt-oss burns tokens "thinking" otherwise;
        # low keeps replies snappy for casual chat
    )
    raw_reply = response.choices[0].message.content.strip()

    match = EMOTION_TAG_RE.match(raw_reply)
    if match:
        tag = match.group(1).lower()
        clean_reply = raw_reply[match.end() :].strip()
    else:
        tag, clean_reply = "neutral", raw_reply
    if tag not in EMOTION_TO_EXPRESSION_FILE:
        tag = "neutral"
    return tag, clean_reply, raw_reply


def ask_llm(
    client: Groq, system_prompt: str, history: list[dict], user_text: str
) -> tuple[str, str]:
    """Returns (emotion_tag, clean_reply_text). Appends both turns to
    history in place."""
    history.append({"role": "user", "content": user_text})
    tag, clean_reply, raw_reply = _chat_and_parse(client, system_prompt, history)
    history.append({"role": "assistant", "content": raw_reply})
    return tag, clean_reply


def _describe_idle_duration(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 60:
        return f"about {max(1, round(minutes))} minutes"
    hours = minutes / 60
    if hours < 24:
        return f"about {hours:.1f} hours"
    return f"about {hours / 24:.1f} days"


def generate_proactive_opener(
    client: Groq, system_prompt: str, history: list[dict], seconds_idle: float | None
) -> tuple[str, str]:
    """She speaks first, unprompted. Doesn't store the meta-instruction in
    history (only the reply itself), so it never leaks into what
    memory.extract_facts sees or what future turns look back on."""
    idle_desc = (
        _describe_idle_duration(seconds_idle) if seconds_idle is not None else "a while"
    )
    instruction = (
        f"He hasn't said anything in {idle_desc}. Reach out to him first, "
        "completely unprompted -- like you're the one calling him, not "
        "waiting to be talked to. If something in your memory of him fits "
        "naturally, bring it up. Otherwise just open with something warm "
        "and specific to your mood, not generic. Still follow all your "
        "normal rules (emotion tag, short reply)."
    )
    messages = history + [{"role": "system", "content": instruction}]
    tag, clean_reply, raw_reply = _chat_and_parse(client, system_prompt, messages)
    history.append({"role": "assistant", "content": raw_reply})
    return tag, clean_reply


def in_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start, end = PROACTIVE_QUIET_HOURS
    if start <= end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


async def synthesize(text: str) -> tuple[np.ndarray, int]:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        communicate = edge_tts.Communicate(text, VOICE_NAME)
        await communicate.save(str(out_path))
        audio, samplerate = sf.read(out_path, dtype="float32")
        return audio, samplerate
    finally:
        out_path.unlink(missing_ok=True)


class VtsConnection:
    """Holds the current VTube Studio connection and transparently
    reconnects on drop. VTube Studio (at least this version) has a bug
    where it silently kills plugin connections after roughly 60 seconds
    (logged as a Unity-side "ObjectDisposedException", nothing we're doing
    wrong) -- without this, that would crash the whole conversation loop
    mid-session."""

    def __init__(self, vts):
        self.vts = vts

    async def run(self, coro_factory):
        try:
            return await coro_factory(self.vts)
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            print(f"(VTube Studio connection dropped, reconnecting: {e})")
            self.vts = await vts_face.connect()
            return await coro_factory(self.vts)


async def react(vts, tag: str) -> None:
    target = EMOTION_TO_EXPRESSION_FILE.get(tag, "Normal.exp3.json")
    # Explicitly turn off every other expression first so nothing from a
    # previous turn stays stuck active, then turn on only the current one.
    for expression_file in ALL_EXPRESSION_FILES:
        if expression_file != target:
            await vts_face.set_expression(vts, expression_file, False)
    await vts_face.set_expression(vts, target, True)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        sys.exit(
            "Missing GROQ_API_KEY. Copy .env.example to .env and add your "
            "free key from https://console.groq.com"
        )

    print("Connecting to VTube Studio...")
    try:
        vts_conn = VtsConnection(await vts_face.connect())
    except OSError:
        sys.exit(
            "Could not reach VTube Studio on ws://localhost:8001\n"
            "Open VTube Studio and enable Settings -> Start API, then retry."
        )

    async def reset_expressions(vts) -> None:
        for expression_file in ALL_EXPRESSION_FILES:
            await vts_face.set_expression(vts, expression_file, False)
        await vts_face.set_expression(vts, "Normal.exp3.json", True)

    # Reset to a clean, known expression state at startup, in case a
    # previous run (or manual testing) left one stuck active.
    await vts_conn.run(reset_expressions)

    print("Loading speech recognition model (first run downloads it)...")
    whisper = WhisperModel("base", device="cpu", compute_type="int8")
    vad = webrtcvad.Vad(2)  # 0-3, higher = more aggressive about filtering noise

    client = Groq(api_key=api_key)
    mem = memory.load()
    system_prompt = SYSTEM_PROMPT + memory.facts_as_context(mem)
    history: list[dict] = []
    consecutive_proactive = 0

    print("\nReady. Talk to her -- or just wait, she might check in on her own.")
    print("Ctrl+C to quit.")
    try:
        while True:
            if consecutive_proactive >= MAX_CONSECUTIVE_PROACTIVE or in_quiet_hours():
                timeout = None  # wait silently, don't initiate right now
            else:
                timeout = PROACTIVE_CHECKIN_SECONDS

            print("\nPress Enter to start talking...")
            got_enter = await wait_for_enter_or_timeout(timeout)

            if got_enter:
                audio = capture_until_silence(vad)
                user_text = transcribe(whisper, audio)
                if not user_text:
                    print("(didn't catch that, try again)")
                    continue
                print(f"You: {user_text}")

                tag, reply = ask_llm(client, system_prompt, history, user_text)
                consecutive_proactive = 0
                memory.touch_last_seen(mem)
                memory.save(mem)  # cheap; keeps last_seen accurate even on a crash
            else:
                print("(she's reaching out first...)")
                seconds_idle = memory.seconds_since_last_seen(mem)
                tag, reply = generate_proactive_opener(
                    client, system_prompt, history, seconds_idle
                )
                consecutive_proactive += 1

            print(f"Aria [{tag}]: {reply}")

            await vts_conn.run(lambda v: react(v, tag))
            reply_audio, samplerate = await synthesize(reply)
            await vts_conn.run(
                lambda v: vts_face.speak_with_lipsync(v, reply_audio, samplerate)
            )
    except KeyboardInterrupt:
        print("\nBye!")

    print("Updating what she remembers about you...")
    memory.extract_facts(client, LLM_MODEL, mem, history)
    memory.save(mem)

    await vts_conn.vts.close()


if __name__ == "__main__":
    asyncio.run(main())

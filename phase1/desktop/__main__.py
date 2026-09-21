"""
Desktop companion: she has a face (VTube Studio avatar) and you're at
your PC. Push-to-talk + terminal check-ins. For "she calls your phone"
when you're away, run the call server instead (python -m call) -- run one
or the other, not both, since they share memory.json.

Mic (auto-stop on silence) -> faster-whisper (local STT)
                            -> Groq (cloud LLM, free tier)
                            -> edge-tts (cloud TTS, free, expressive voices)
                            -> VTube Studio (expression hotkey + live lip sync)
                            -> speakers

Setup (one time):
    pip install -r requirements.txt
    copy .env.example to .env and add your GROQ_API_KEY
    Open VTube Studio, load a model, enable Settings -> Start API

Run (from the phase1 folder):
    python -m desktop
"""
import asyncio
import io
import msvcrt
import queue
import sys
import time

import numpy as np
import soundfile as sf
import sounddevice as sd
import webrtcvad
import websockets

from desktop import brain, vts_face
from shared import llm, memory, stt, tts

SAMPLE_RATE = 16000  # required by both webrtcvad and Whisper
FRAME_MS = 30  # webrtcvad only accepts 10/20/30ms frames
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_TIMEOUT_MS = 900  # stop ~0.9s after you stop talking
MAX_LEADING_SILENCE_MS = 6000  # give up if you never start talking


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


def transcribe(audio: np.ndarray) -> str:
    if audio.size == 0:
        return ""
    return stt.transcribe_local(audio)


async def synthesize_audio_array(text: str) -> tuple[np.ndarray, int]:
    mp3_bytes = await tts.synthesize_mp3(text)
    audio, samplerate = sf.read(io.BytesIO(mp3_bytes), dtype="float32")
    return audio, samplerate


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
    target = brain.EMOTION_TO_EXPRESSION_FILE.get(tag, "Normal.exp3.json")
    # Explicitly turn off every other expression first so nothing from a
    # previous turn stays stuck active, then turn on only the current one.
    for expression_file in brain.ALL_EXPRESSION_FILES:
        if expression_file != target:
            await vts_face.set_expression(vts, expression_file, False)
    await vts_face.set_expression(vts, target, True)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = llm.sync_client()

    print("Connecting to VTube Studio...")
    try:
        vts_conn = VtsConnection(await vts_face.connect())
    except OSError:
        sys.exit(
            "Could not reach VTube Studio on ws://localhost:8001\n"
            "Open VTube Studio and enable Settings -> Start API, then retry."
        )

    async def reset_expressions(vts) -> None:
        for expression_file in brain.ALL_EXPRESSION_FILES:
            await vts_face.set_expression(vts, expression_file, False)
        await vts_face.set_expression(vts, "Normal.exp3.json", True)

    # Reset to a clean, known expression state at startup, in case a
    # previous run (or manual testing) left one stuck active.
    await vts_conn.run(reset_expressions)

    stt.local_model()  # load now rather than stalling your first sentence
    vad = webrtcvad.Vad(2)  # 0-3, higher = more aggressive about filtering noise

    mem = memory.load()
    system_prompt = brain.SYSTEM_PROMPT + memory.render_context(mem)
    session_start = memory.now_local()
    history: list[dict] = []
    consecutive_proactive = 0
    recent_proactive_openers: list[str] = []

    print("\nReady. Talk to her -- or just wait, she might check in on her own.")
    print("Ctrl+C to quit.")
    try:
        while True:
            timeout = (
                brain.PROACTIVE_CHECKIN_SECONDS
                if brain.should_initiate_proactively(consecutive_proactive)
                else None
            )

            print("\nPress Enter to start talking...")
            got_enter = await wait_for_enter_or_timeout(timeout)

            if got_enter:
                audio = capture_until_silence(vad)
                user_text = transcribe(audio)
                if not user_text:
                    print("(didn't catch that, try again)")
                    continue
                print(f"You: {user_text}")

                tag, reply = brain.ask_llm(client, system_prompt, history, user_text)
                consecutive_proactive = 0
                memory.touch_last_seen(mem)
                memory.save(mem)  # cheap; keeps last_seen accurate even on a crash
            else:
                print("(she's reaching out first...)")
                seconds_idle = memory.seconds_since_last_seen(mem)
                tag, reply = brain.generate_proactive_opener(
                    client, system_prompt, history, seconds_idle, recent_proactive_openers
                )
                recent_proactive_openers.append(reply)
                consecutive_proactive += 1

            print(f"Aria [{tag}]: {reply}")

            await vts_conn.run(lambda v: react(v, tag))
            reply_audio, samplerate = await synthesize_audio_array(reply)
            await vts_conn.run(
                lambda v: vts_face.speak_with_lipsync(v, reply_audio, samplerate)
            )
    except KeyboardInterrupt:
        print("\nBye!")

    print("Updating what she remembers about you...")
    # Same memory as the phone calls: this session becomes an episode, and
    # what he said becomes facts/plans/threads (see shared/memory.py).
    mem = memory.load()  # fresh, in case a call updated it meanwhile
    memory.touch_last_seen(mem)
    if any(m["role"] == "user" for m in history):
        memory.update_from_conversation(
            client, llm.MEMORY_MODEL, mem, history, final=True, call_start=session_start
        )
    memory.compact(mem, memory.now_local(), memory.summarizer(client, llm.MEMORY_MODEL))
    memory.enforce(mem, memory.now_local())
    memory.save(mem)

    await vts_conn.vts.close()


if __name__ == "__main__":
    asyncio.run(main())

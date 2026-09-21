"""
The phone-call server. The Android app (or any browser) connects here;
she rings via Firebase push, and once a call is up it's hands-free: the
page detects when you start and stop talking on its own.

How a turn flows (and why it's fast):
  you stop talking -> page sends the audio clip
  -> speech-to-text (Groq by default, see settings.stt_engine)
  -> if it sounds unfinished ("and then I..."), wait a moment for more
  -> LLM reply is STREAMED, split into sentences as it arrives
  -> each sentence is synthesized and sent the moment it's ready, so she
     starts talking after the first sentence, not after the whole reply
  -> if that's still slow, a cached "hmm..." fills the gap

If you start talking again before she's begun answering, her pending
reply is cancelled and both parts of what you said are answered together.

Run (from the phase1 folder):
    python -m call
Run this OR the desktop companion, not both -- they share memory.json.
"""
import asyncio
import base64
import difflib
import json
import random
import re
import socket
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from call import certs, conversation, mood, phrases, push, schedule
from call import persona
from call import settings as settings_module
from shared import ROOT, llm, memory, stt, tts

PORT = 8765
HTML_PATH = Path(__file__).parent / "static" / "call.html"

# Fillers only play if her real reply hasn't started within this long of
# your words being transcribed -- a fast reply doesn't need one. The small
# random skip keeps it from becoming a predictable tic; it used to be 25%,
# which in testing left two long silences in a row unfilled.
FILLER_AFTER_SECONDS = 0.7
FILLER_CHANCE = 0.9
# How long she lets it ring before giving up (a "missed call"), and how
# often the ring loop checks whether it's time to call.
RING_SECONDS = 45
RING_LOOP_SECONDS = 10
# Incoming-screen answers other than Accept (see _end_ring).
DECLINE_SNOOZE_MINUTES = 30
BUSY_SNOOZE_MINUTES = 60
BUSY_RETRY_SCHEDULED_MINUTES = 15
AVATAR_PATH = ROOT / "avatar.img"
# Memory work during a call (see shared/memory.py). The running note of the
# call's earlier part is refreshed once this many exchanges have scrolled
# past the word-for-word window; a mid-call memory checkpoint runs every
# CHECKPOINT_EVERY of your lines, so a crash or a long call can't lose it.
NOTE_REFRESH_EXCHANGES = 3
CHECKPOINT_EVERY = 10
# The running note uses a different model from her replies, so it never
# eats into the budget her voice depends on (Groq's free tier gives each
# model 8,000 tokens a minute).
NOTE_MODEL = llm.LLM_MODEL
KEEP_CALL_LOGS = 5  # raw transcripts of the most recent calls; older ones are deleted
# A call whose connection drops (mobile data blip, or Android rebuilding the
# call screen) is kept alive this long for the phone to reconnect, instead
# of ending on the spot. 20s wasn't enough: a 5 AM call lost the phone's
# network for 51s and ended just before it came back.
RESUME_GRACE_SECONDS = 90
# A "call her" arriving this soon after a call ended, with nothing ringing,
# is Android replaying the call screen's launch -- not you dialing again.
REPLAY_GUARD_SECONDS = 8
# How long answering a scheduled call waits for her prepared opening line
# before falling back to a plain hello (it's normally ready long before).
REASON_GREETING_WAIT = 6
MAX_AVATAR_BYTES = 3 * 1024 * 1024
# After an unanswered ring, the wait before the next one doubles, up to this.
MAX_RING_BACKOFF_MINUTES = 240
# Your "you said" transcript is thrown away as echo if at least this share
# of its words repeat one of her recent lines word for word, in order
# (see _looks_like_echo).
ECHO_WORD_OVERLAP = 0.8

app = FastAPI()
aclient = None  # AsyncGroq: replies, nudges, speech-to-text
sclient = None  # Groq: end-of-call memory update (run in a thread)

# One user, one call at a time -- a deliberate simplification.
state = {
    "connected_sockets": set(),
    "in_call": False,
    "call_ws": None,
    "history": [],
    "mem": None,
    "consecutive_proactive": 0,
    "last_ring_at": 0.0,
    "ring": None,  # the ring in progress, if any (see _ring)
    "snooze_until": 0.0,  # no periodic calls before this (monotonic)
    "on_hold": False,
    "resume_task": None,  # ends a call if the phone doesn't reconnect in time
    # Memory within a call (Layer 1).
    "call_id": 0,
    "call_started_at": None,
    "call_note": "",  # running note of everything older than the last few exchanges
    "noted_upto": 0,  # history index the note covers up to
    "checkpoint_upto": 0,  # history index already written to long-term memory
    "note_task": None,
    "checkpoint_task": None,
    "last_call_ended_at": 0.0,
    "recent_greetings": [],
    "recent_nudges": [],
    "last_filler": None,
    "her_recent_speech": [],  # her last few spoken lines, for the echo check
    "utterances_in_flight": 0,  # your clips still being transcribed
    "last_activity_at": 0.0,
    "silence_watcher_task": None,
    "duration_timer_task": None,
    # Turn-taking. pending_text = what you've said that she hasn't started
    # answering yet; reply_committed flips once her first words are sent,
    # after which a reply is no longer cancellable.
    "pending_text": "",
    "reply_task": None,
    "reply_committed": False,
}
_stt_lock = asyncio.Lock()
_memory_lock = asyncio.Lock()  # every read-modify-write of memory.json
_turn_lock = asyncio.Lock()
_background_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _system_prompt(s: dict) -> str:
    """Rebuilt for every reply: her personality, her memories (every time
    in them relative to right now), and the running note of this call's
    earlier part. A name change in the app applies immediately."""
    prompt = persona.build_system_prompt(s["name"]) + memory.render_context(state["mem"])
    if state["call_note"]:
        prompt += f"\nEarlier in this call (before the last few lines):\n{state['call_note']}\n"
    return prompt


def _reply_running() -> bool:
    task = state["reply_task"]
    return task is not None and not task.done()


def _busy() -> bool:
    """True while she's answering OR your words are still being
    transcribed. The second part matters: the silence nudge used to fire in
    that gap, so her nudge ("let's have a pizza party!") played straight
    into her real answer ("sure, I'm in!") and she seemed to answer herself."""
    return _reply_running() or state["utterances_in_flight"] > 0


# One plain-text transcript per call in phase1/call_logs/, so when a call
# does something odd there's a record of exactly what was said instead of
# a reconstruction from memory. Only the last KEEP_CALL_LOGS calls are kept.
LOG_DIR = ROOT / "call_logs"
_log_path: Path | None = None  # the current (or last) call's transcript


def _new_call_log() -> None:
    """Starts a transcript file for a new call and deletes the oldest ones
    beyond KEEP_CALL_LOGS. Lines logged between calls (a replayed request
    right after hang-up) go into the last call's file."""
    global _log_path
    try:
        LOG_DIR.mkdir(exist_ok=True)
        _log_path = LOG_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        _log_path.touch()
        _delete_old_call_logs()
    except OSError:
        pass


def _log(line: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = _log_path or LOG_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] {line}\n")
    except OSError:
        pass  # logging must never break a call


def _describe_api_failure(err: Exception) -> str:
    name = type(err).__name__
    if "Connection" in name or "Timeout" in name:
        return "can't reach the AI service -- check the PC's internet connection"
    return f"{name}: {err}"


# ---------------------------------------------------------------- startup


def local_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@app.on_event("startup")
async def on_startup() -> None:
    global aclient, sclient
    aclient = llm.async_client()
    sclient = llm.sync_client()
    state["mem"] = memory.load()
    s = settings_module.load()

    print("\nCall server ready.")
    print(f"On this PC:      https://localhost:{PORT}")
    interfaces = certs.local_ipv4_interfaces()
    if interfaces:
        print("\nFrom your phone -- pick the one on the same network as the phone:")
        for ip, name in interfaces:
            print(f"    https://{ip}:{PORT}".ljust(38) + f"({certs.describe_interface(ip, name)})")
    else:
        print(f"From your phone: https://{local_lan_ip()}:{PORT}")
    print(f"\nSpeech-to-text: {s['stt_engine']}   voice: {s['voice']}")
    if s["stt_engine"] == "local":
        _spawn(asyncio.to_thread(stt.local_model))

    _spawn(phrases.warm_cache(s["voice"]))
    _spawn(_ring_loop())
    _spawn(_memory_maintenance())


async def _memory_maintenance() -> None:
    """At startup: retry memory updates that failed earlier, compact old
    episodes, apply every store's limits, and delete raw call transcripts
    past their keep date. Also re-run daily from the ring loop."""
    try:
        async with _memory_lock:
            mem = memory.load()
            await _retry_pending(mem)
            await asyncio.to_thread(memory.compact, mem, memory.now_local(), memory.summarizer(sclient, llm.MEMORY_MODEL))
            memory.enforce(mem, memory.now_local())
            memory.save(mem)
            if not state["in_call"]:
                state["mem"] = mem
        _delete_old_call_logs()
    except Exception as e:
        print(f"(memory maintenance failed: {type(e).__name__}: {e})")


def _delete_old_call_logs() -> None:
    """Raw transcripts are never shown to her -- they're kept only to see
    what went wrong in a recent call -- so only the newest KEEP_CALL_LOGS
    stay. (This also clears out the old one-file-per-day transcripts.)"""
    try:
        paths = sorted(LOG_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in paths[KEEP_CALL_LOGS:]:
        try:
            path.unlink()
        except OSError:
            continue


# ------------------------------------------------------------ ringing
#
# Two kinds of calls:
# - periodic: she checks in after you've been quiet for checkin_minutes,
#   only inside her active hours and outside every quiet window.
# - scheduled: one-time, at an exact time, with an optional note ("reminder
#   for medicine"). Rings even inside a quiet window -- you set it up for
#   exactly then -- and a periodic call that would land within
#   schedule.OVERLAP_MINUTES of one is skipped, so the scheduled one wins.


def _periodic_due(s: dict, now: datetime) -> bool:
    if not s["calling_enabled"]:
        return False
    if time.monotonic() < state["snooze_until"]:
        return False  # you tapped "Message: can't talk" or declined
    if not settings_module.periodic_allowed_now(s, now.hour * 60 + now.minute):
        return False
    if schedule.blocks_periodic(now):
        return False
    # From disk each time: a desktop session also counts as talking to her.
    seconds_idle = memory.seconds_since_last_seen(memory.load())
    # None = no conversation yet, which makes her MORE eligible to call.
    if seconds_idle is not None and seconds_idle < s["checkin_minutes"] * 60:
        return False
    # Unanswered rings back off (each wait doubles, capped) instead of her
    # giving up for good. She used to stop entirely after two missed rings
    # until YOU called -- which, with nothing on screen saying so, just
    # looked like calls had broken.
    unanswered = state["consecutive_proactive"]
    if unanswered:
        gap = min(s["checkin_minutes"] * 60 * 2**unanswered, MAX_RING_BACKOFF_MINUTES * 60)
        if time.monotonic() - state["last_ring_at"] < gap:
            return False
    return True


def _shown_number(s: dict) -> str:
    return s["phone_number"] if s["show_number"] else ""


async def _ring(s: dict, reason: str = "", schedule_id: str | None = None) -> bool:
    """Rings the phone (FCM push, works locked/closed) and any open page
    (WebSocket). A scheduled call's opening line is generated and
    synthesized while it rings, so she can say it the instant you pick up."""
    ring_id = uuid.uuid4().hex[:10]
    delivered = push.send_ring(s["name"], _shown_number(s), reason, ring_id)
    if state["connected_sockets"]:
        await _broadcast(
            {"type": "ring", "name": s["name"], "number": _shown_number(s), "reason": reason}
        )
        delivered = True
    if not delivered:
        print("Wanted to ring you, but no phone is registered or reachable.")
        return False

    ring = {"id": ring_id, "reason": reason, "schedule_id": schedule_id, "greeting": None}
    if reason:
        ring["greeting"] = _spawn(_prepare_reason_greeting(s, reason))
    ring["timeout"] = _spawn(_ring_timeout(ring))
    state["ring"] = ring
    state["last_ring_at"] = time.monotonic()
    if schedule_id:
        schedule.record_ring(schedule_id)
        print(f"Ringing your phone -- scheduled call: {reason or '(no note)'}")
    else:
        state["consecutive_proactive"] += 1
        print(f"Ringing your phone (unanswered so far: {state['consecutive_proactive'] - 1}).")
    return True


async def _prepare_reason_greeting(s: dict, reason: str) -> tuple[str, bytes]:
    text = await conversation.generate_reason_greeting(aclient, _system_prompt(s), reason)
    return text, await tts.synthesize_hedged(text, s["voice"])


async def _ring_timeout(ring: dict) -> None:
    """Unanswered after RING_SECONDS: stop the phone ringing and leave a
    "missed call" notification, like a real call would."""
    await asyncio.sleep(RING_SECONDS)
    if state["ring"] is not ring:
        return  # answered or declined meanwhile
    state["ring"] = None
    s = settings_module.load()
    push.send_cancel()
    push.send_missed(s["name"], _shown_number(s))
    await _broadcast({"type": "ring_ended"})
    if ring["schedule_id"]:
        schedule.mark_unanswered(ring["schedule_id"])
    print("Missed call -- she hung up after ringing.")


def _end_ring(action: str, remind_minutes: int = 10) -> None:
    """You answered from the incoming-call screen with something other
    than Accept: "decline", "remind" (call me back in a bit), or "busy"
    ("Message: can't talk right now")."""
    ring = state["ring"]
    state["ring"] = None
    if ring is None:
        return
    for key in ("timeout", "greeting"):
        if ring.get(key) is not None:
            ring[key].cancel()
    reason = ring["reason"]
    if ring["schedule_id"]:
        schedule.update(ring["schedule_id"], status="done")
    if action == "remind":
        at = datetime.now() + timedelta(minutes=remind_minutes)
        schedule.add(at.strftime(schedule.FORMAT), reason or "He asked you to call him back")
        print(f"You asked her to call back in {remind_minutes} minutes.")
    elif action == "busy":
        state["snooze_until"] = time.monotonic() + BUSY_SNOOZE_MINUTES * 60
        if reason:  # a reminder shouldn't just vanish because you were busy
            at = datetime.now() + timedelta(minutes=BUSY_RETRY_SCHEDULED_MINUTES)
            schedule.add(at.strftime(schedule.FORMAT), reason)
        print(f"You're busy -- no check-in calls for {BUSY_SNOOZE_MINUTES} minutes.")
    else:
        state["snooze_until"] = time.monotonic() + DECLINE_SNOOZE_MINUTES * 60
        print("You declined her call.")
    state["consecutive_proactive"] = 0


async def _ring_loop() -> None:
    while True:
        await asyncio.sleep(RING_LOOP_SECONDS)
        try:
            await _ring_tick()
        except Exception as e:
            # One bad tick (a corrupt file, a network blip) must not kill
            # calling for the rest of the server's life.
            print(f"(ring check failed: {type(e).__name__}: {e})")


async def _ring_tick() -> None:
    s = settings_module.load()
    now = datetime.now()
    if state.get("maintained_on") != now.date() and now.hour >= 4 and not state["in_call"]:
        state["maintained_on"] = now.date()
        _spawn(_memory_maintenance())  # once a day, in the small hours
    entry = schedule.due(now)
    if state["in_call"]:
        if entry is not None and not _busy():
            await _remind_in_call(s, entry)
        return
    if state["ring"] is not None:
        return  # already ringing
    if entry is not None:
        await _ring(s, entry["note"], entry["id"])
    elif _periodic_due(s, now):
        await _ring(s)


async def _remind_in_call(s: dict, entry: dict) -> None:
    """A scheduled call came due mid-call: she brings it up instead."""
    schedule.update(entry["id"], status="done")
    ws = state["call_ws"]
    if not entry["note"] or ws is None:
        return
    reply = await conversation.generate_in_call_reminder(
        aclient, _system_prompt(s), state["history"], entry["note"]
    )
    if not reply or _busy() or not state["in_call"]:
        return
    state["history"].append({"role": "assistant", "content": reply})
    _log(f"HER (scheduled reminder: {entry['note']}): {reply}")
    await _send_line(ws, reply, await tts.synthesize_hedged(reply, s["voice"]), first=True)
    await ws.send_json({"type": "state", "value": "listening"})
    state["last_activity_at"] = time.monotonic()


async def _broadcast(message: dict) -> None:
    dead = set()
    for ws in state["connected_sockets"]:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    state["connected_sockets"] -= dead


# --------------------------------------------------------------- routes


@app.get("/")
async def index(request: Request) -> HTMLResponse:
    """The page is served under the secret path (see access.py), so it's
    told where it lives -- every request it makes is relative to that.
    (A <base> tag would have been simpler but breaks the inline SVG icons,
    which reference themselves by fragment.)"""
    base = request.scope.get("root_path", "") + "/"
    html = HTML_PATH.read_text(encoding="utf-8").replace(
        "<script>", f'<script>window.API_BASE = "{base}";</script>\n<script>', 1
    )
    return HTMLResponse(html)


@app.get("/settings")
async def get_settings() -> JSONResponse:
    return JSONResponse(settings_module.load())


@app.post("/settings")
async def update_settings(request: Request) -> JSONResponse:
    """Partial updates are merged over the current values. Applies
    immediately -- everything re-reads settings when it needs them."""
    incoming = await request.json()
    old = settings_module.load()
    updated = settings_module.save({**old, **incoming})
    if updated["voice"] != old["voice"]:
        _spawn(phrases.warm_cache(updated["voice"]))
    return JSONResponse(updated)


@app.get("/avatar")
async def get_avatar() -> Response:
    if not AVATAR_PATH.exists():
        return Response(status_code=404)
    data = AVATAR_PATH.read_bytes()
    kind = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "no-cache"})


@app.post("/avatar")
async def set_avatar(request: Request) -> JSONResponse:
    """Her profile picture, from the app's settings ({"image": base64})."""
    try:
        data = base64.b64decode((await request.json())["image"], validate=True)
    except Exception:
        return JSONResponse({"error": "expected {\"image\": <base64>}"}, status_code=400)
    if len(data) > MAX_AVATAR_BYTES:
        return JSONResponse({"error": "image too large"}, status_code=413)
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return JSONResponse({"error": "not a JPEG or PNG"}, status_code=400)
    AVATAR_PATH.write_bytes(data)
    return JSONResponse({"status": "ok"})


@app.get("/schedule")
async def get_schedule(all: bool = False) -> JSONResponse:
    """Upcoming calls only. Finished ones (done/missed) are kept on disk
    for a while but aren't worth showing -- ?all=true includes them."""
    entries = schedule.load()
    if not all:
        entries = [e for e in entries if e["status"] == "pending"]
    return JSONResponse(entries)


@app.post("/schedule")
async def add_scheduled_call(request: Request) -> JSONResponse:
    """{"at": "2026-09-20T21:00", "note": "reminder for medicine"}"""
    data = await request.json()
    try:
        entry = schedule.add(str(data.get("at", "")), str(data.get("note", "")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(entry)


@app.delete("/schedule/{entry_id}")
async def delete_scheduled_call(entry_id: str) -> JSONResponse:
    return JSONResponse({"removed": schedule.remove(entry_id)})


@app.post("/call_action")
async def call_action(request: Request) -> JSONResponse:
    """From the phone's incoming-call screen / notification: "decline",
    "remind" (call me back in N minutes), or "busy"."""
    data = await request.json()
    action = data.get("action")
    if action not in ("decline", "remind", "busy"):
        return JSONResponse({"error": "unknown action"}, status_code=400)
    minutes = int(data.get("minutes") or 10)
    _end_ring(action, max(1, min(minutes, 24 * 60)))
    await _broadcast({"type": "ring_ended"})
    return JSONResponse({"status": "ok"})


@app.post("/register_device")
async def register_device(request: Request) -> JSONResponse:
    data = await request.json()
    token = data.get("token")
    if not token:
        return JSONResponse({"error": "missing token"}, status_code=400)
    push.save_device_token(token)
    print("Phone registered for push notifications.")
    return JSONResponse({"status": "ok"})


# ------------------------------------------------------- speech in / out


async def _transcribe(audio: bytes, s: dict) -> str:
    """Groq first when selected, falling back to the local model if Groq
    fails, so a network blip doesn't make her deaf."""
    if s["stt_engine"] == "groq":
        try:
            return await stt.transcribe_groq(aclient, audio, "speech.webm")
        except Exception as e:
            print(f"(Groq speech-to-text failed, using local model: {e})")
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(audio)
        path = tmp.name
    try:
        return await stt.transcribe_local_async(path)
    finally:
        Path(path).unlink(missing_ok=True)


async def _send_line(ws: WebSocket, text: str, audio: bytes, first: bool) -> None:
    """first=True starts a new line of hers in the transcript; later
    sentences of the same reply are appended to it."""
    state["her_recent_speech"] = (state["her_recent_speech"] + [text])[-8:]
    if first:
        await ws.send_json({"type": "state", "value": "speaking", "text": text})
    else:
        await ws.send_json({"type": "her_more", "text": text})
    if audio:
        await ws.send_bytes(audio)


def _looks_like_echo(text: str) -> bool:
    """True if a transcript of "you" is really her own voice picked up by
    the mic. The page already mutes the mic while she plays, but speaker
    output can still leak in right at the edges. Short replies ("yeah",
    "okay") are never filtered -- too likely to be you genuinely saying
    them.

    Echo repeats her words in order, so what counts is the longest run of
    consecutive words shared with one of her lines, not how many words are
    shared. Answering her naturally reuses her words ("What place? I forgot
    it." after "...the place I picked?" shares 4 of 5), and a plain
    word-overlap check threw that away as echo."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    if len(words) < 3:
        return False
    for line in state["her_recent_speech"]:
        hers = re.findall(r"[a-z0-9']+", line.lower())
        run = difflib.SequenceMatcher(None, words, hers, autojunk=False).find_longest_match()
        if run.size / len(words) >= ECHO_WORD_OVERLAP:
            return True
    return False


# ----------------------------------------------------------- turn-taking


async def _handle_utterance(ws: WebSocket, audio: bytes) -> None:
    """One clip of you talking. Transcribed in arrival order; then either
    starts her reply, or -- if she hasn't started speaking yet -- cancels
    the pending reply and merges this onto what you said before."""
    state["utterances_in_flight"] += 1
    try:
        await _handle_utterance_inner(ws, audio)
    finally:
        state["utterances_in_flight"] -= 1


async def _handle_utterance_inner(ws: WebSocket, audio: bytes) -> None:
    received_at = time.monotonic()
    state["last_activity_at"] = received_at
    s = settings_module.load()
    async with _stt_lock:
        try:
            text = await _transcribe(audio, s)
        except Exception as err:
            print(f"(couldn't transcribe: {err})")
            text = ""
    stt_ms = (time.monotonic() - received_at) * 1000
    if text and _looks_like_echo(text):
        print(f"(ignored {text!r} -- sounded like her own voice)")
        _log(f"(ignored as echo) {text}")
        text = ""

    async with _turn_lock:
        if not text or not state["in_call"]:
            if not _reply_running() and state["in_call"]:
                await ws.send_json({"type": "state", "value": "listening"})
            return

        merged = False
        previous = state["reply_task"]
        if previous is not None and not previous.done():
            if not state["reply_committed"]:
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
                text = f"{state['pending_text']} {text}".strip()
                merged = True
                print("(you kept talking -- merged into one message)")
                _log("(merged with your previous words)")
            else:
                # She's already answering; let her finish, then take this.
                await asyncio.gather(previous, return_exceptions=True)

        state["pending_text"] = text
        state["reply_committed"] = False
        await ws.send_json({"type": "you_said", "text": text, "merge": merged})
        _log(f"HIM: {text}")
        state["reply_task"] = _spawn(_respond(ws, text, s, stt_ms))


async def _respond(ws: WebSocket, user_text: str, s: dict, stt_ms: float) -> None:
    t_start = time.monotonic()
    first_audio_sent = asyncio.Event()
    spoken: list[str] = []
    tts_tasks: list[asyncio.Task] = []
    filler_task = None
    producer = None
    held = False
    try:
        await ws.send_json({"type": "state", "value": "thinking"})

        if conversation.sounds_unfinished(user_text):
            held = True
            await asyncio.sleep(conversation.UNFINISHED_HOLD_SECONDS)

        if s["fillers_enabled"]:
            filler_task = _spawn(_maybe_filler(ws, s, first_audio_sent, user_text))

        messages = conversation.build_context(
            state["history"], [{"role": "user", "content": user_text}]
        )
        system_prompt = _system_prompt(s)
        # Rough count (4 characters ~ 1 token) -- enough to watch the
        # per-minute budget without another API call.
        prompt_tokens = (len(system_prompt) + sum(len(m["content"]) for m in messages)) // 4
        queue: asyncio.Queue = asyncio.Queue()
        t_llm = time.monotonic()
        first_sentence_ms = None

        async def produce() -> None:
            nonlocal first_sentence_ms
            try:
                async for sentence in conversation.stream_sentences(
                    aclient, system_prompt, messages
                ):
                    if first_sentence_ms is None:
                        first_sentence_ms = (time.monotonic() - t_llm) * 1000
                    # TTS for each sentence starts right away, in parallel
                    # with the LLM still generating the next one.
                    task = asyncio.create_task(tts.synthesize_hedged(sentence, s["voice"]))
                    tts_tasks.append(task)
                    await queue.put((sentence, task))
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        while True:
            item = await queue.get()
            if item is None:
                break
            sentence, task = item
            audio = await task
            if not spoken:
                # Point of no return: from here the reply can't be merged
                # away, and your words become part of the history.
                state["reply_committed"] = True
                state["history"].append({"role": "user", "content": user_text})
                state["pending_text"] = ""
                first_audio_sent.set()
                total_ms = (time.monotonic() - t_start) * 1000
                print(
                    f"[latency] stt {stt_ms:.0f}ms"
                    + (f" + hold {conversation.UNFINISHED_HOLD_SECONDS:.1f}s" if held else "")
                    + f" | llm first sentence {first_sentence_ms or 0:.0f}ms"
                    + f" | her first words sent {stt_ms + total_ms:.0f}ms after your clip arrived"
                    + f" | prompt ~{prompt_tokens} tokens"
                )
            await _send_line(ws, sentence, audio, first=not spoken)
            spoken.append(sentence)
        await producer  # surfaces an LLM error, if any

        if not spoken:
            raise RuntimeError("empty reply from the model")
        await ws.send_json({"type": "state", "value": "listening"})
    except asyncio.CancelledError:
        raise
    except Exception as err:
        print(f"(couldn't answer you: {err})")
        try:
            await ws.send_json({"type": "error", "message": _describe_api_failure(err)})
            await ws.send_json({"type": "state", "value": "listening"})
        except Exception:
            pass
    finally:
        for task in [producer, filler_task, *tts_tasks]:
            if task is not None and not task.done():
                task.cancel()
        if spoken:
            state["history"].append({"role": "assistant", "content": " ".join(spoken)})
            _log(f"HER: {' '.join(spoken)}")
            _after_turn()
        state["last_activity_at"] = time.monotonic()


# ------------------------------------------------------- memory in a call


def _after_turn() -> None:
    """After each exchange: keep the running note covering whatever has
    scrolled out of the word-for-word window, and checkpoint long-term
    memory every CHECKPOINT_EVERY of your lines. Both run in the
    background -- she never waits on them."""
    history = state["history"]
    window = memory.SHORT_TERM_TURNS * 2
    note_task = state["note_task"]
    if (note_task is None or note_task.done()) and (
        len(history) - window - state["noted_upto"] >= NOTE_REFRESH_EXCHANGES * 2
    ):
        upto = len(history) - window
        state["note_task"] = _spawn(_refresh_note(state["call_id"], history[state["noted_upto"]:upto], upto))
    since = history[state["checkpoint_upto"]:]
    checkpoint = state["checkpoint_task"]
    if (checkpoint is None or checkpoint.done()) and sum(m["role"] == "user" for m in since) >= CHECKPOINT_EVERY:
        state["checkpoint_task"] = _spawn(_checkpoint(state["call_id"], since, len(history)))


async def _refresh_note(call_id: int, segment: list[dict], upto: int) -> None:
    try:
        note = await asyncio.to_thread(
            memory.update_call_note, sclient, NOTE_MODEL, state["call_note"], segment, llm.MEMORY_MODEL
        )
    except Exception as e:
        print(f"(couldn't update the call note: {e})")
        return
    if state["in_call"] and state["call_id"] == call_id:
        state["call_note"] = note
        state["noted_upto"] = upto


async def _checkpoint(call_id: int, segment: list[dict], upto: int) -> int | None:
    """Mid-call memory update: what you've said so far becomes memory now,
    not only when the call ends -- and she knows it for the rest of the call.
    Returns the history index it covered, or None if it failed (the
    end-of-call update then covers that part instead)."""
    async with _memory_lock:
        mem = memory.load()
        ok = await asyncio.to_thread(
            memory.update_from_conversation, sclient, llm.MEMORY_MODEL, mem, segment,
            final=False, earlier_note=state["call_note"],
        )
        if not ok:
            return None
        memory.save(mem)
        if state["in_call"] and state["call_id"] == call_id:
            state["mem"] = mem
            state["checkpoint_upto"] = upto
    print("(memory checkpoint saved)")
    return upto


async def _finish_call_memory(snapshot: dict) -> None:
    """End of a call: the rest of the conversation becomes memory, plus a
    dated episode of the whole call; then older episodes are compacted.
    Runs in the background so hanging up is instant."""
    history = snapshot["history"]
    covered = snapshot["checkpoint_upto"]
    checkpoint = snapshot["checkpoint_task"]
    if checkpoint is not None:
        # A checkpoint still running when you hung up: wait for it, and
        # start from wherever it got to.
        (result,) = await asyncio.gather(checkpoint, return_exceptions=True)
        if isinstance(result, int):
            covered = max(covered, result)
    tail = history[covered:]
    async with _memory_lock:
        mem = memory.load()
        memory.touch_last_seen(mem)
        if any(m["role"] == "user" for m in history):
            print("Updating what she remembers about you...")
            started = snapshot["call_started_at"] or memory.now_local()
            minutes = (memory.now_local() - started).total_seconds() / 60
            ok = await asyncio.to_thread(
                memory.update_from_conversation, sclient, llm.MEMORY_MODEL, mem, tail,
                final=True, earlier_note=snapshot["call_note"], call_start=started, minutes=minutes,
            )
            if not ok:
                # Kept for a retry at the next call or restart, rather than
                # losing what was said.
                mem["pending"].append({
                    "call_start": memory._iso(started), "minutes": round(minutes, 1),
                    "note": snapshot["call_note"], "messages": tail,
                })
                print("(memory update failed -- saved for a retry)")
        await _retry_pending(mem)
        await asyncio.to_thread(memory.compact, mem, memory.now_local(), memory.summarizer(sclient, llm.MEMORY_MODEL))
        memory.enforce(mem, memory.now_local())
        memory.save(mem)
        state["mem"] = mem


async def _retry_pending(mem: dict) -> None:
    """Conversations whose memory update failed earlier (model down, rate
    limit) get another go. Called with _memory_lock held."""
    still = []
    for item in mem["pending"]:
        ok = await asyncio.to_thread(
            memory.update_from_conversation, sclient, llm.MEMORY_MODEL, mem, item["messages"],
            final=True, earlier_note=item.get("note", ""),
            call_start=memory._parse(item["call_start"]), minutes=item.get("minutes"),
        )
        if not ok:
            still.append(item)
    mem["pending"] = still


async def _maybe_filler(
    ws: WebSocket, s: dict, reply_started: asyncio.Event, user_text: str
) -> None:
    try:
        await asyncio.wait_for(reply_started.wait(), FILLER_AFTER_SECONDS)
        return  # her reply was quick enough; no filler needed
    except asyncio.TimeoutError:
        pass
    if random.random() > FILLER_CHANCE:
        return
    feeling = mood.classify(user_text)
    if feeling is None:
        return  # mixed news: no sound beats the wrong one
    filler = phrases.pick_filler(feeling, state["last_filler"])
    print(f"(filler: {feeling} -> {filler!r})")
    state["last_filler"] = filler
    audio = await phrases.audio_for(filler, s["voice"])
    if audio and not reply_started.is_set():
        await ws.send_json({"type": "filler"})
        await ws.send_bytes(audio)


# --------------------------------------------------------------- calls


async def _in_call_silence_watcher() -> None:
    """Fills a long silence by having her speak up on her own."""
    while state["in_call"]:
        await asyncio.sleep(3)
        s = settings_module.load()
        if not state["in_call"] or _busy() or state["on_hold"]:
            continue
        if time.monotonic() - state["last_activity_at"] < s["silence_nudge_seconds"]:
            continue
        ws = state["call_ws"]
        if ws is None:
            continue
        stamp = time.monotonic()
        state["last_activity_at"] = stamp
        try:
            reply = await conversation.generate_nudge(
                aclient, _system_prompt(s), state["history"], state["recent_nudges"]
            )
            # Any activity since the stamp means you started talking while
            # she was thinking of what to say -- drop the nudge.
            if not reply or _busy() or not state["in_call"] or state["last_activity_at"] != stamp:
                continue
            state["recent_nudges"].append(reply)
            _log(f"HER (filling silence): {reply}")
            state["history"].append({"role": "assistant", "content": reply})
            audio = await tts.synthesize_hedged(reply, s["voice"])
            await _send_line(ws, reply, audio, first=True)
            await ws.send_json({"type": "state", "value": "listening"})
        except Exception as e:
            print(f"(silence nudge failed: {e})")
        finally:
            state["last_activity_at"] = time.monotonic()


async def _call_duration_timer(max_minutes: float) -> None:
    await asyncio.sleep(max_minutes * 60)
    if state["in_call"]:
        print(f"Call hit the {max_minutes:g}-minute limit, ending it.")
        await _end_call()


async def _start_call(ws: WebSocket) -> None:
    if state["in_call"]:
        return  # never two calls at once
    s = settings_module.load()
    # Answering while she's ringing = her call; otherwise you called her.
    ring, state["ring"] = state["ring"], None
    she_is_calling = ring is not None
    if ring is not None:
        ring["timeout"].cancel()
        if ring["schedule_id"]:
            schedule.update(ring["schedule_id"], status="done")
    # Fresh from disk: picks up anything learned in a desktop session or
    # edited by hand since the server started. Open threads she's about to
    # see count one more showing, so an unanswered one stops nagging.
    async with _memory_lock:
        mem = memory.load()
        idle_seconds = memory.seconds_since_last_seen(mem)
        memory.mark_threads_shown(mem, memory.now_local())
        memory.enforce(mem, memory.now_local())
        memory.save(mem)
    state["mem"] = mem
    state.update(
        call_id=state["call_id"] + 1,
        call_started_at=memory.now_local(),
        call_note="",
        noted_upto=0,
        checkpoint_upto=0,
        note_task=None,
        checkpoint_task=None,
        in_call=True,
        call_ws=ws,
        consecutive_proactive=0,
        recent_nudges=[],
        her_recent_speech=[],
        pending_text="",
        reply_task=None,
        reply_committed=False,
        on_hold=False,
    )
    await ws.send_json(
        {
            "type": "call_started",
            "name": s["name"],
            "number": _shown_number(s),
            "has_avatar": AVATAR_PATH.exists(),
        }
    )

    # A scheduled call opens with its reason, prepared while it rang; any
    # other call opens with a plain canned greeting.
    greeting, audio = None, None
    if ring is not None and ring["greeting"] is not None:
        try:
            greeting, audio = await asyncio.wait_for(ring["greeting"], REASON_GREETING_WAIT)
        except Exception as err:
            print(f"(couldn't prepare her reason for calling, using a plain hello: {err})")
    if not greeting:
        greeting = phrases.pick_greeting(she_is_calling, idle_seconds, state["recent_greetings"])
        state["recent_greetings"] = (state["recent_greetings"] + [greeting])[-10:]
    state["history"].append({"role": "assistant", "content": greeting})
    why = f" -- scheduled: {ring['reason']}" if ring is not None and ring["reason"] else ""
    _new_call_log()
    _log(f"===== call started ({'she called' if she_is_calling else 'you called'}{why}) =====")
    _log(f"HER: {greeting}")
    try:
        if audio is None:
            audio = await phrases.audio_for(greeting, s["voice"])
        await _send_line(ws, greeting, audio, first=True)
    except Exception as err:
        print(f"(couldn't play her greeting: {err})")
        await ws.send_json({"type": "error", "message": _describe_api_failure(err)})
    await ws.send_json({"type": "state", "value": "listening"})

    state["last_activity_at"] = time.monotonic()
    state["silence_watcher_task"] = _spawn(_in_call_silence_watcher())
    state["duration_timer_task"] = _spawn(_call_duration_timer(s["max_call_duration_minutes"]))


async def _end_call() -> None:
    if not state["in_call"]:
        return
    ws = state["call_ws"]
    state["in_call"] = False
    state["call_ws"] = None
    state["last_call_ended_at"] = time.monotonic()
    current = asyncio.current_task()
    for key in ("silence_watcher_task", "duration_timer_task", "reply_task", "resume_task"):
        task = state[key]
        # Skip self-cancel: the duration timer calls this from inside itself.
        if task is not None and task is not current and not task.done():
            task.cancel()
        state[key] = None
    if ws is not None:
        try:
            await ws.send_json({"type": "call_ended"})
        except Exception:
            pass  # socket may already be gone

    history, state["history"] = state["history"], []
    _log("===== call ended =====")
    note_task = state["note_task"]
    if note_task is not None and not note_task.done():
        note_task.cancel()
    checkpoint = state["checkpoint_task"]
    _spawn(_finish_call_memory({
        "history": history,
        "call_note": state["call_note"],
        "call_started_at": state["call_started_at"],
        "checkpoint_upto": state["checkpoint_upto"],
        "checkpoint_task": checkpoint if checkpoint is not None and not checkpoint.done() else None,
    }))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    state["connected_sockets"].add(ws)
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text") is not None:
                data = json.loads(message["text"])
                kind = data.get("type")
                if kind in ("call_me_now", "answer", "resume"):
                    await _call_request(ws, kind)
                elif kind == "decline":
                    _end_ring("decline")
                elif kind == "hold":
                    # Hold: no silence nudges while you're away. Coming back
                    # counts as activity so she doesn't pounce immediately.
                    state["on_hold"] = bool(data.get("on"))
                    state["last_activity_at"] = time.monotonic()
                elif kind == "activity":
                    # Heartbeat from the page while someone is audible: her
                    # audio still playing on the phone, or you talking. The
                    # silence timer counts from the END of this, not from
                    # when the server finished sending her audio -- a long
                    # reply used to eat most of your turn before you could
                    # answer, and she'd speak again over the gap.
                    state["last_activity_at"] = time.monotonic()
                elif kind == "hangup":
                    if state["call_ws"] is ws:
                        await _end_call()
            elif message.get("bytes") is not None and state["in_call"] and state["call_ws"] is ws:
                # Not awaited: the receive loop must keep reading so a
                # second clip (you kept talking) can arrive and cancel the
                # first one's pending reply.
                _spawn(_handle_utterance(ws, message["bytes"]))
    except WebSocketDisconnect:
        pass
    finally:
        state["connected_sockets"].discard(ws)
        if state["in_call"] and state["call_ws"] is ws:
            # Don't end the call yet -- give the phone a chance to come back.
            state["call_ws"] = None
            _log("(connection lost -- waiting for the phone to reconnect)")
            state["resume_task"] = _spawn(_end_if_not_resumed())


async def _end_if_not_resumed() -> None:
    await asyncio.sleep(RESUME_GRACE_SECONDS)
    if state["in_call"] and state["call_ws"] is None:
        print("Call ended -- the phone didn't reconnect.")
        await _end_call()


async def _call_request(ws: WebSocket, kind: str) -> None:
    """A page asking for a call: "call_me_now" (you tapped Call, or the
    call screen opened after Accept), "answer" (browser ring screen), or
    "resume" (the page reconnected and believes a call is still on).

    Android rebuilds the call screen more often than you'd think -- screen
    off/on, the lock screen coming back, switching apps -- and every
    rebuild reloads the page, which asks for a call again. Before this, that
    either dialed a brand-new call a few seconds after you hung up, or,
    mid-call, left the new page connected to nothing while the old
    connection's closing ended the call: she just went silent."""
    if state["in_call"]:
        if state["call_ws"] is not ws:
            await _reattach(ws)
        return
    if kind == "resume":
        # The page thinks a call is on, but it already ended here.
        await ws.send_json({"type": "call_ended"})
        return
    if state["ring"] is None and time.monotonic() - state["last_call_ended_at"] < REPLAY_GUARD_SECONDS:
        print("(ignored a call request right after hang-up -- the call screen was rebuilt)")
        _log("(ignored a replayed call request right after hang-up)")
        await ws.send_json({"type": "call_ended", "replay": True})
        return
    await _start_call(ws)


async def _reattach(ws: WebSocket) -> None:
    """Moves the live call onto this connection -- the newest page wins."""
    old = state["call_ws"]
    state["call_ws"] = ws
    task = state["resume_task"]
    if task is not None and not task.done():
        task.cancel()
    state["resume_task"] = None
    s = settings_module.load()
    await ws.send_json(
        {
            "type": "call_started",
            "resumed": True,
            "name": s["name"],
            "number": _shown_number(s),
            "has_avatar": AVATAR_PATH.exists(),
        }
    )
    if not _busy():
        await ws.send_json({"type": "state", "value": "listening"})
    state["last_activity_at"] = time.monotonic()
    _log("(reconnected -- same call continues)")
    print("Phone reconnected -- call continues.")
    if old is not None:
        try:
            await old.close()  # its cleanup sees it no longer holds the call
        except Exception:
            pass

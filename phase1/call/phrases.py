"""
Canned lines: call greetings and "thinking" fillers.

Neither goes through the LLM. A real call starts with "hey, how are you?",
not a generated anecdote -- and because these are fixed, their audio can
be synthesized ahead of time and cached, so she speaks the instant the call
connects and fills the gap the instant you stop talking.
"""
import asyncio
import hashlib
import random
from datetime import datetime

from shared import ROOT
from shared.tts import synthesize_mp3

CACHE_DIR = ROOT / ".audio_cache"

# She's calling you.
GREETINGS_HER_CALLING = [
    "Hey! How are you?",
    "Hiii, what are you up to?",
    "Hey you. Busy?",
    "Hey, how's your day going?",
    "Heyy, got a minute?",
    "Hi! What are you doing right now?",
    "Hey, it's me. How's it going?",
    "Hey! Are you free to talk?",
]
GREETINGS_MORNING = [
    "Good morning! Did you sleep okay?",
    "Morning, you. Are you up yet?",
]
GREETINGS_NIGHT = [
    "Hey, still up?",
    "Hi. Winding down for the night?",
]
GREETINGS_LONG_GAP = [
    "Hey stranger! It's been a while, how are you?",
    "Hey! Where have you been? How are you?",
]
# You called her.
GREETINGS_HIM_CALLING = [
    "Hey! You called?",
    "Hi you! What's up?",
    "Hey, perfect timing. I was just thinking about you.",
    "Hiii! What's going on?",
    "Hey! How are you?",
    "Oh, hey! What's up?",
]

FILLERS = [
    "Hmm...",
    "Mm-hmm.",
    "Ohh...",
    "Hmm, okay...",
    "Mmm...",
    "Oh, hmm...",
]

LONG_GAP_SECONDS = 2 * 24 * 3600
RECENT_TO_AVOID = 3


def _pick(pool: list[str], recent: list[str]) -> str:
    fresh = [p for p in pool if p not in recent[-RECENT_TO_AVOID:]]
    return random.choice(fresh or pool)


def pick_greeting(she_is_calling: bool, seconds_idle: float | None, recent: list[str]) -> str:
    if not she_is_calling:
        return _pick(GREETINGS_HIM_CALLING, recent)
    if seconds_idle is not None and seconds_idle > LONG_GAP_SECONDS:
        return _pick(GREETINGS_LONG_GAP, recent)
    hour = datetime.now().hour
    # Time-of-day lines only half the time, so mornings don't ALWAYS open
    # with "good morning".
    if random.random() < 0.5:
        if 5 <= hour < 11:
            return _pick(GREETINGS_MORNING, recent)
        if hour >= 22 or hour < 3:
            return _pick(GREETINGS_NIGHT, recent)
    return _pick(GREETINGS_HER_CALLING, recent)


def pick_filler(last: str | None) -> str:
    return random.choice([f for f in FILLERS if f != last] or FILLERS)


def _cache_path(text: str, voice: str):
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key}.mp3"


async def audio_for(text: str, voice: str) -> bytes:
    """Cached per voice, so switching voices in settings just means the
    first use of each line synthesizes it once."""
    path = _cache_path(text, voice)
    if path.exists():
        return path.read_bytes()
    audio = await synthesize_mp3(text, voice)
    if audio:
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_bytes(audio)
    return audio


ALL_LINES = (
    GREETINGS_HER_CALLING
    + GREETINGS_MORNING
    + GREETINGS_NIGHT
    + GREETINGS_LONG_GAP
    + GREETINGS_HIM_CALLING
    + FILLERS
)


async def warm_cache(voice: str) -> None:
    """Pre-synthesize every canned line in the background at startup."""
    missing = [t for t in ALL_LINES if not _cache_path(t, voice).exists()]
    if not missing:
        return
    sem = asyncio.Semaphore(4)

    async def one(text: str) -> None:
        async with sem:
            try:
                await audio_for(text, voice)
            except Exception as e:
                print(f"(couldn't pre-cache '{text}': {e})")

    await asyncio.gather(*(one(t) for t in missing))
    print(f"Pre-cached {len(missing)} greeting/filler clips for {voice}.")

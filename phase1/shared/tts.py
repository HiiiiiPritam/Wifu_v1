"""Text-to-speech via edge-tts (free Microsoft neural voices)."""
import asyncio
import re

import edge_tts

# Cute/soft-spoken free neural voice. List others with:
#   python -m edge_tts --list-voices
DEFAULT_VOICE = "en-US-AnaNeural"

# Emoji and pictographs: the model sometimes adds them despite being told
# not to, and TTS either reads them out loud or chokes on them.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]+", re.UNICODE
)


def clean_for_speech(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\*[^*]*\*", "", text)  # *giggles*-style stage directions
    return re.sub(r"\s+", " ", text).strip()


HEDGE_AFTER_SECONDS = 2.0


async def synthesize_hedged(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """synthesize_mp3, but if it hasn't finished within HEDGE_AFTER_SECONDS,
    a second identical request is started and whichever finishes first
    wins. Normally synthesis takes ~1s, but single requests occasionally
    stall -- one took 7s mid-reply on a real call, leaving a dead gap
    between two of her sentences. A duplicate request almost never stalls
    the same way."""
    first = asyncio.create_task(synthesize_mp3(text, voice))
    done, _ = await asyncio.wait({first}, timeout=HEDGE_AFTER_SECONDS)
    if done:
        return first.result()
    pending = {first, asyncio.create_task(synthesize_mp3(text, voice))}
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    return task.result()
        return await first  # both failed: re-raise the original error
    finally:
        for task in pending:
            task.cancel()


async def synthesize_mp3(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    text = clean_for_speech(text)
    if not text:
        return b""
    communicate = edge_tts.Communicate(text, voice)
    chunks = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.extend(chunk["data"])
    return bytes(chunks)

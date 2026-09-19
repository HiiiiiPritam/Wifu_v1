"""
What she says during a call: streamed replies, silence nudges, and the
check for "he probably isn't done talking yet".
"""
import random
import re
from typing import AsyncIterator

from shared import memory

# Picked by replaying a real call moment against each Groq model, 4 tries
# each: gpt-oss-20b (what calls used to use) went wrong 4/4 -- answering its
# own questions, repeating HIS lines back as hers, emoji despite being told
# not to. qwen3.8-27b: 0/4, the most natural phone-style replies, and the
# fastest first word (~0.17s vs ~0.5s). With reasoning off it spends no
# tokens thinking.
CALL_MODEL = "qwen/qwen3.8-27b"
CALL_MODEL_OPTIONS = {"reasoning_effort": "none"}
# Used automatically if the main one errors (outage, rate limit). Also 0/4
# in that test, just slower.
FALLBACK_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL_OPTIONS = {"reasoning_effort": "low"}

REPLY_TEMPERATURE = 0.9
NUDGE_TEMPERATURE = 1.1
# Budget for reasoning (fallback model) plus the spoken reply itself.
MAX_TOKENS = 300


async def _complete(client, messages: list[dict], temperature: float, stream: bool):
    try:
        return await client.chat.completions.create(
            model=CALL_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=MAX_TOKENS,
            stream=stream,
            **CALL_MODEL_OPTIONS,
        )
    except Exception as e:
        print(f"({CALL_MODEL} failed, using {FALLBACK_MODEL}: {type(e).__name__}: {e})")
        return await client.chat.completions.create(
            model=FALLBACK_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=MAX_TOKENS,
            stream=stream,
            **FALLBACK_MODEL_OPTIONS,
        )

# --- "Is he done talking?" ---
# The mic decides your turn is over after a fixed pause. People pause
# mid-thought all the time, though -- "so I went there and... um..." -- and
# answering then is exactly the cutting-off you noticed. If the transcript
# ends like an unfinished sentence, she waits a bit longer; if you carry
# on in that window, the two halves get merged into one message.
UNFINISHED_HOLD_SECONDS = 1.5
_TRAILING_WORDS = {
    "and", "but", "so", "because", "cause", "or", "like", "um", "uh", "umm",
    "uhh", "hmm", "the", "a", "an", "to", "with", "of", "for", "if", "that",
    "which", "then", "my", "i", "i'm", "is", "was", "about", "when", "while",
    "also", "just", "basically", "actually", "maybe", "since", "until",
}


def sounds_unfinished(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    # Whisper marks trailing-off speech with "..." or a dangling comma/dash.
    if t.endswith(("...", "…", ",", "-", "—")):
        return True
    last_word = re.sub(r"[^\w']", "", t.split()[-1])
    return last_word in _TRAILING_WORDS


def build_context(history: list[dict], extra: list[dict] | None = None) -> list[dict]:
    """The short-term window sent to the LLM, with back-to-back messages
    from the same speaker merged into one. Several of her lines in a row
    (a greeting, then a silence nudge, then another) read to the model like
    a script to continue, and it would start answering itself."""
    merged: list[dict] = []
    for m in memory.recent_window(history) + (extra or []):
        if merged and merged[-1]["role"] == m["role"] and m["role"] != "system":
            merged[-1] = {"role": m["role"], "content": f"{merged[-1]['content']} {m['content']}"}
        else:
            merged.append(dict(m))
    return merged


# --- Streaming ---
# Split points: end of a sentence, followed by whitespace -- or directly by
# a capital letter, since the model sometimes drops the space ("stuff?Yeah").
_SENTENCE_END = re.compile(r"([.!?…]+[\"')\]]*)(?:\s+|(?=[A-Z]))")
# The FIRST chunk may also break at a comma/dash once it's this long.
# Synthesis time grows with length, and measured on a real call a long
# opening sentence took ~2s to synthesize after the LLM had it ready in
# 0.5s. A short first chunk gets her talking sooner, while everything
# after it synthesizes in parallel behind it.
FIRST_CHUNK_SOFT_BREAK_CHARS = 30
_SOFT_BREAK = re.compile(r"([,;:—–]|\s-)\s+")


async def stream_sentences(
    client, system_prompt: str, messages: list[dict]
) -> AsyncIterator[str]:
    """Yields her reply in speakable chunks (sentences; the first one
    possibly shorter) as the LLM streams it."""
    stream = await _complete(
        client,
        [{"role": "system", "content": system_prompt}] + messages,
        REPLY_TEMPERATURE,
        stream=True,
    )
    buffer = ""
    emitted = False
    async for chunk in stream:
        if not chunk.choices:
            continue
        # Only the spoken content -- gpt-oss streams its reasoning separately.
        piece = chunk.choices[0].delta.content
        if not piece:
            continue
        buffer += piece
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match and not emitted:
                soft = _SOFT_BREAK.search(buffer, FIRST_CHUNK_SOFT_BREAK_CHARS)
                match = soft
            if not match:
                break
            sentence = buffer[: match.end(1)].strip()
            buffer = buffer[match.end() :]
            if sentence:
                emitted = True
                yield sentence
    if buffer.strip():
        yield buffer.strip()


# --- Silence nudges ---
NUDGE_ANGLES = [
    "check if he's still there, casually, like the line went quiet",
    "just keep talking naturally, like you would if a call went quiet for a moment",
    "ask a quick follow-up about what you two were just talking about",
    "make a small observation to fill the silence, nothing forced",
]
_last_nudge_angle: str | None = None


def word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def is_near_duplicate(a: str, b: str, threshold: float = 0.5) -> bool:
    """Jaccard word overlap -- catches lightly reworded repeats that an
    exact match misses."""
    wa, wb = word_set(a), word_set(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


async def generate_nudge(
    client, system_prompt: str, history: list[dict], recent: list[str]
) -> str:
    """She fills a mid-call silence. The instruction itself is never stored
    in history, only her line. Retries once if she repeats herself -- this
    fires repeatedly during one quiet stretch, and without the check the
    same "did you fall asleep?" came back verbatim."""
    global _last_nudge_angle
    angle = random.choice([a for a in NUDGE_ANGLES if a != _last_nudge_angle] or NUDGE_ANGLES)
    _last_nudge_angle = angle
    recent = recent[-5:]

    reply = ""
    for attempt in range(2):
        avoid = ""
        if recent:
            lines = "\n".join(f"- {r}" for r in recent)
            avoid = (
                f"\n\nYou already said these this call -- say something genuinely different:\n{lines}"
            )
        instruction = (
            "It's gone quiet on the call -- he hasn't said anything for a bit. "
            f"For this line: {angle}. One short sentence, like a real pause-filler."
            f"{avoid}"
        )
        reply = await _directed(client, system_prompt, history, instruction, NUDGE_TEMPERATURE)
        if reply and not any(is_near_duplicate(reply, r) for r in recent):
            break
    return reply


# --- Scheduled calls ---
async def _directed(
    client, system_prompt: str, history: list[dict], instruction: str,
    temperature: float = REPLY_TEMPERATURE,
) -> str:
    """One line from her, steered by an instruction ("it's gone quiet",
    "bring up the medicine").

    The instruction goes INSIDE the system prompt, not after the
    conversation: qwen ignored a trailing instruction and just answered his
    last line (asked to bring up a reminder mid-call, it said "What are you
    watching?"). And qwen's template rejects a request with no user message
    at all, so an opening line gets a stand-in "he picked up" cue."""
    messages = build_context(history)
    if not any(m["role"] == "user" for m in messages):
        messages.append({"role": "user", "content": "(he picks up the phone)"})
    response = await _complete(
        client,
        [{"role": "system", "content": f"{system_prompt}\n\nFor your next line: {instruction}"}]
        + messages,
        temperature,
        stream=False,
    )
    return (response.choices[0].message.content or "").strip()


async def generate_reason_greeting(client, system_prompt: str, reason: str) -> str:
    """Her opening line for a scheduled call, built around why she's
    calling ("reminder for medicine"). Generated while the phone is still
    ringing, so it's ready the moment he picks up."""
    instruction = (
        "You're calling him right now and he just picked up. You called for "
        f"this reason: {reason!r}. Open like a real phone call -- a quick "
        "hello -- then bring up the reason naturally, like a girlfriend "
        "would, not like an alarm. One or two short sentences."
    )
    return await _directed(client, system_prompt, [], instruction)


async def generate_in_call_reminder(
    client, system_prompt: str, history: list[dict], reason: str
) -> str:
    """A scheduled call came due while you're already talking to her, so
    she brings it up in this call instead of ringing you."""
    instruction = (
        "You'd planned to call him right now for this reason: "
        f"{reason!r} -- but you're already on the phone together. Bring it up "
        "naturally, like it just came to mind (\"oh wait, before I forget...\"). "
        "One or two short sentences."
    )
    return await _directed(client, system_prompt, history, instruction)

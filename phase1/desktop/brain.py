"""
Desktop companion's "thinking": replies with emotion tags that drive the
Live2D face, plus unprompted check-ins while you're at the PC.

The phone-call product has its own, separate logic in call/ -- different
prompt, different openers, different pacing. Only memory and the Groq/TTS
plumbing are shared (see shared/).
"""
import random
import re
from datetime import datetime

from groq import Groq

from desktop.persona import SYSTEM_PROMPT
from shared import memory
from shared.llm import LLM_MODEL, REASONING_EFFORT

# --- Proactive check-in ("she calls you first") ---
# If you haven't said anything for this long while she's waiting, she
# speaks up unprompted. Lower this for testing (e.g. 30) so you don't
# have to actually wait 15 minutes to see it happen.
PROACTIVE_CHECKIN_SECONDS = 1 * 60
# Local 24h hours during which she won't proactively check in (start, end).
# (22, 9) = won't initiate between 10pm and 9am. Doesn't stop her from
# replying if you talk to her during quiet hours, just from starting it.
PROACTIVE_QUIET_HOURS = (5, 9)
# After this many proactive check-ins in a row with no real reply from
# you, stop initiating and just wait silently -- don't talk to an empty
# room forever burning API/TTS usage while you're actually away.
MAX_CONSECUTIVE_PROACTIVE = 2

# Maps persona.py's emotion tags to expression files (Settings -> Hotkeys ->
# each hotkey's assigned .exp3.json on your model).
EMOTION_TAG_RE = re.compile(r"^\[(\w+)\]\s*", re.IGNORECASE)
EMOTION_TO_EXPRESSION_FILE = {
    "heart": "Blushing.exp3.json",
    "cry": "Sad.exp3.json",
    "angry": "Angry.exp3.json",
    "shock": "Surprised.exp3.json",
    "neutral": "Normal.exp3.json",
}
ALL_EXPRESSION_FILES = set(EMOTION_TO_EXPRESSION_FILE.values())


def in_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start, end = PROACTIVE_QUIET_HOURS
    if start <= end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


def should_initiate_proactively(consecutive_proactive: int) -> bool:
    """False means: don't have her speak up on her own right now (she's
    already checked in unanswered too many times in a row, or it's quiet
    hours) -- just wait for a real message instead."""
    return consecutive_proactive < MAX_CONSECUTIVE_PROACTIVE and not in_quiet_hours()


def chat_and_parse(
    client: Groq, system_prompt: str, messages: list[dict], temperature: float = 0.9
) -> tuple[str, str, str]:
    """Calls the LLM and splits its reply into (emotion_tag, clean_text,
    raw_text_with_tag_still_on_it)."""
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        temperature=temperature,
        max_tokens=200,
        reasoning_effort=REASONING_EFFORT,
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
    """Returns (emotion_tag, clean_reply_text). Appends both turns to the
    FULL history in place (used later for long-term extraction), but only
    sends memory.recent_window(history) -- the short-term window -- to
    the LLM itself, so a long session doesn't keep growing every call's
    cost/context."""
    history.append({"role": "user", "content": user_text})
    tag, clean_reply, raw_reply = chat_and_parse(
        client, system_prompt, memory.recent_window(history)
    )
    history.append({"role": "assistant", "content": raw_reply})
    return tag, clean_reply


def describe_idle_duration(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 60:
        return f"about {max(1, round(minutes))} minutes"
    hours = minutes / 60
    if hours < 24:
        return f"about {hours:.1f} hours"
    return f"about {hours / 24:.1f} days"


# Randomly sampled each time she initiates, so the underlying instruction
# varies even when the surface prompt boilerplate is the same -- without
# this, proactive openers all sound the same shape ("hey, thinking of you,
# how's X going").
PROACTIVE_ANGLES = [
    "ask a curious, specific question about something from your memory of him",
    "share a random stray thought or observation, like you were just doing "
    "something (anything mundane and specific) and it made you think of him",
    "tease him playfully about something, a little cheeky",
    "tell him you miss him or have been thinking about him -- a bit soft/vulnerable",
    "describe a tiny hypothetical of what you'd be doing together right now "
    "if he were with you",
    "follow up on how something specific from your memory of him turned out",
    "just say something impulsive, like a thought that popped into your head "
    "with no real lead-up",
]
PROACTIVE_TEMPERATURE = 1.15  # higher than normal replies, for more variety
RECENT_OPENERS_TO_AVOID = 5  # how many past openers she's told not to repeat
MAX_REGENERATE_ATTEMPTS = 2  # local safety net if the model repeats itself anyway
_last_proactive_angle: str | None = None  # module state: never repeat the angle back-to-back


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def is_near_duplicate(a: str, b: str, threshold: float = 0.5) -> bool:
    """Jaccard word overlap. Catches lightly-reworded repeats ("I was just
    dusting off the old vinyls..." vs "Hey, I was dusting off some old
    vinyls...") that an exact string match misses -- confirmed in testing
    that the model reworded a near-identical anecdote this way rather than
    repeating it verbatim."""
    wa, wb = _word_set(a), _word_set(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def generate_proactive_opener(
    client: Groq,
    system_prompt: str,
    history: list[dict],
    seconds_idle: float | None,
    recent_openers: list[str] | None = None,
) -> tuple[str, str]:
    """She speaks first, unprompted. Doesn't store the meta-instruction in
    history (only the reply itself), so it never leaks into what
    memory.extract_facts sees or what future turns look back on.

    Repetition is fought on two levels: a randomly picked "angle" (never
    the same one twice in a row) shapes the underlying instruction even
    when the visible prompt is otherwise identical, and a local exact/
    near-exact duplicate check regenerates (with an even stronger warning)
    if the model produces a repeat anyway -- asking nicely in the prompt
    alone was not reliable enough on its own (verified: it repeated a
    prior opener word-for-word in testing without this)."""
    global _last_proactive_angle
    idle_desc = (
        describe_idle_duration(seconds_idle) if seconds_idle is not None else "a while"
    )
    recent_openers = recent_openers or []
    recent_pool = recent_openers[-RECENT_OPENERS_TO_AVOID:]

    available_angles = [a for a in PROACTIVE_ANGLES if a != _last_proactive_angle]
    angle = random.choice(available_angles or PROACTIVE_ANGLES)
    _last_proactive_angle = angle

    for attempt in range(MAX_REGENERATE_ATTEMPTS + 1):
        avoid_block = ""
        if recent_pool:
            recent = "\n".join(f"- {o}" for o in recent_pool)
            strength = (
                "CRITICAL -- you just repeated one of these, that's not allowed. "
                "Say something with genuinely different wording and content, not "
                "just a rephrasing:"
                if attempt > 0
                else "Don't repeat these recent openers of yours, say something new:"
            )
            avoid_block = f"\n\n{strength}\n{recent}"
        instruction = (
            f"He hasn't said anything in {idle_desc}. Reach out to him first, "
            "completely unprompted -- like you're the one calling him, not "
            f"waiting to be talked to. For this message specifically: {angle}. "
            "Still follow all your normal rules (emotion tag, short reply)."
            f"{avoid_block}"
        )
        messages = memory.recent_window(history) + [{"role": "system", "content": instruction}]
        tag, clean_reply, raw_reply = chat_and_parse(
            client, system_prompt, messages, temperature=PROACTIVE_TEMPERATURE
        )
        if not any(is_near_duplicate(clean_reply, o) for o in recent_pool):
            break

    history.append({"role": "assistant", "content": raw_reply})
    return tag, clean_reply

"""
Persistent memory: facts about you that survive across sessions, plus
when you last talked (used by the proactive check-in feature in main.py).

Stored in memory.json next to this file -- personal conversation data,
gitignored, never commit it.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

MEMORY_PATH = Path(__file__).resolve().parent / "memory.json"
MAX_FACTS = 30

DEFAULT_MEMORY = {"facts": [], "last_seen": None}


def load() -> dict:
    if not MEMORY_PATH.exists():
        return dict(DEFAULT_MEMORY)
    with open(MEMORY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {**DEFAULT_MEMORY, **data}


def save(mem: dict) -> None:
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)


def touch_last_seen(mem: dict) -> None:
    mem["last_seen"] = datetime.now(timezone.utc).isoformat()


def seconds_since_last_seen(mem: dict) -> float | None:
    if not mem.get("last_seen"):
        return None
    last = datetime.fromisoformat(mem["last_seen"])
    return (datetime.now(timezone.utc) - last).total_seconds()


def facts_as_context(mem: dict) -> str:
    """Formats stored facts as a block to append to the system prompt."""
    if not mem["facts"]:
        return ""
    bullets = "\n".join(f"- {f}" for f in mem["facts"])
    return f"\n\nThings you remember about him from past conversations:\n{bullets}\n"


_FACT_EXTRACTION_PROMPT = """\
You maintain a girlfriend character's long-term memory of her boyfriend.

Existing memory (facts you already know):
{existing_facts}

Below is a new conversation. Return an UPDATED complete list of facts to \
remember, as a JSON array of short strings (one fact per string). Rules:
- Keep all still-true existing facts.
- Add new noteworthy facts from this conversation (preferences, ongoing \
projects/events, things he mentioned that matter to him, plans he made).
- Do NOT include small talk, one-off pleasantries, or anything not worth \
remembering weeks from now.
- Merge/update facts that changed (e.g. replace an old status with a new \
one), don't just append duplicates.
- Keep at most {max_facts} facts total, dropping the least important ones \
if you exceed that.
- Output ONLY the JSON array, nothing else -- no markdown fences, no prose.

Conversation:
{conversation}
"""


def extract_facts(client, model: str, mem: dict, history: list[dict]) -> None:
    """One LLM call that reviews the conversation and updates mem["facts"]
    in place. Safe no-op on an empty history or if the call/parse fails --
    memory just stays as it was, never crashes the app over this."""
    if not history:
        return

    conversation_text = "\n".join(f"{turn['role']}: {turn['content']}" for turn in history)
    existing = "\n".join(f"- {f}" for f in mem["facts"]) or "(none yet)"
    prompt = _FACT_EXTRACTION_PROMPT.format(
        existing_facts=existing, max_facts=MAX_FACTS, conversation=conversation_text
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
            reasoning_effort="low",
        )
        raw = response.choices[0].message.content.strip()
        # Models sometimes wrap JSON in markdown fences despite instructions.
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        facts = json.loads(raw)
        if isinstance(facts, list) and all(isinstance(f, str) for f in facts):
            mem["facts"] = facts[:MAX_FACTS]
    except Exception as e:
        print(f"(memory update skipped: {e})")

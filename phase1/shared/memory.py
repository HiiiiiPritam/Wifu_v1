"""
Two-tier memory:

- Short-term: just a sliding window of the last few exchanges from the
  live conversation, used to build each LLM call's context. Not stored
  on disk -- it's a view over the in-session `history` list, capped so a
  long conversation doesn't keep growing the prompt forever.

- Long-term: a structured profile of facts about you that survives across
  sessions (memory.json, gitignored). Each fact is scored and decays over
  time instead of being a flat ever-growing text blob:
    {text, category, importance (1-5), created_at, last_reinforced_at}
  A fact mentioned again gets reinforced (score resets upward); a fact
  nobody brings up quietly loses relevance and eventually drops off
  ("graceful forgetting") instead of being hard-deleted on the spot or
  accumulating forever.
"""
import json
from datetime import datetime, timezone

from shared import ROOT

# Shared by desktop/ and call/ on purpose: it's the same "her" in both.
# The two are meant to be run one at a time -- if both were running they
# would each overwrite the other's saves.
MEMORY_PATH = ROOT / "memory.json"

SHORT_TERM_TURNS = 5  # exchanges (user+assistant pairs) sent to the LLM each call

CATEGORIES = ("identity", "preference", "ongoing_event", "relationship_moment")
DECAY_PER_DAY = 0.15  # importance points lost per day since last reinforced
MIN_SCORE_TO_KEEP = 0.5  # facts scoring below this are dropped entirely
MAX_LONG_TERM_FACTS = 40  # soft cap after decay-based pruning
FACTS_SHOWN_IN_PROMPT = 20  # top-N by score actually injected into the system prompt

DEFAULT_MEMORY = {"facts": [], "last_seen": None}


def load() -> dict:
    if not MEMORY_PATH.exists():
        return dict(DEFAULT_MEMORY)
    with open(MEMORY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    mem = {**DEFAULT_MEMORY, **data}
    _migrate_legacy_facts(mem)
    return mem


def _migrate_legacy_facts(mem: dict) -> None:
    """Old format was a flat list[str]. Upgrade in place if we load one."""
    now = _now_iso()
    mem["facts"] = [
        {
            "text": f,
            "category": "relationship_moment",
            "importance": 3,
            "created_at": now,
            "last_reinforced_at": now,
        }
        if isinstance(f, str)
        else f
        for f in mem["facts"]
    ]


def save(mem: dict) -> None:
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_last_seen(mem: dict) -> None:
    mem["last_seen"] = _now_iso()


def seconds_since_last_seen(mem: dict) -> float | None:
    if not mem.get("last_seen"):
        return None
    last = datetime.fromisoformat(mem["last_seen"])
    return (datetime.now(timezone.utc) - last).total_seconds()


def recent_window(history: list[dict], turns: int = SHORT_TERM_TURNS) -> list[dict]:
    """Short-term memory: the last `turns` exchanges (user+assistant pairs)
    from the full session history, for sending to the LLM each call. The
    full history is still kept for end-of-session long-term extraction --
    this window is only for keeping each individual API call's context
    (and cost) bounded."""
    return history[-turns * 2 :]


def _score(fact: dict, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(fact["last_reinforced_at"])
    days = max(0.0, (now - last).total_seconds() / 86400)
    return fact["importance"] - DECAY_PER_DAY * days


def prune(mem: dict) -> None:
    """Drops facts that have decayed past relevance, then trims to the
    cap by lowest score if still over. Mutates mem["facts"] in place."""
    now = datetime.now(timezone.utc)
    scored = [(f, _score(f, now)) for f in mem["facts"]]
    scored = [(f, s) for f, s in scored if s >= MIN_SCORE_TO_KEEP]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    mem["facts"] = [f for f, _ in scored[:MAX_LONG_TERM_FACTS]]


def facts_as_context(mem: dict) -> str:
    """Formats the top-scoring facts, grouped by category, as a block to
    append to the system prompt. Prunes first so stale facts never show."""
    prune(mem)
    if not mem["facts"]:
        return ""

    now = datetime.now(timezone.utc)
    ranked = sorted(mem["facts"], key=lambda f: _score(f, now), reverse=True)
    top = ranked[:FACTS_SHOWN_IN_PROMPT]

    by_category: dict[str, list[str]] = {}
    for f in top:
        by_category.setdefault(f["category"], []).append(f["text"])

    lines = ["\n\nThings you remember about him from past conversations:"]
    for category in CATEGORIES:
        if category in by_category:
            lines.append(f"\n{category.replace('_', ' ').title()}:")
            lines.extend(f"- {t}" for t in by_category[category])
    return "\n".join(lines) + "\n"


_FACT_EXTRACTION_PROMPT = """\
You maintain a girlfriend character's long-term memory of her boyfriend.

Existing facts you already know (as plain text, one per line):
{existing_facts}

Below is a new conversation between HIM (the boyfriend, whose facts you \
record) and HER (the girlfriend character). Each line starts with who said it.

Whose words are whose matters more than anything else here:
- Only record things about HIM that HE said or clearly confirmed.
- Everything HER lines say is hers: her stories, poems, jokes, suggestions, \
opinions and made-up details are NOT facts about him. If she wrote a poem, \
he did not write a poem. If she suggested a plan, it's only a shared plan \
if HE clearly agreed to it.
- If he asked her to do something (write a poem, tell a story), the fact, if \
worth keeping at all, is that he asked -- never that he did it himself.
- Don't guess preferences from one passing remark or from what she offered.
- Write each fact in plain third person ("He ...", "She ..."); never put the \
labels HIM/HER in the fact text itself.

Return an UPDATED complete list of facts to remember, as a JSON object \
{{"facts": [...]}} whose list holds objects: {{"text": "...", "category": \
"...", "importance": 1-5}}. Rules:
- category must be one of: identity, preference, ongoing_event, \
relationship_moment.
  - identity: stable facts about who he is (name, work, relationships).
  - preference: likes/dislikes/habits.
  - ongoing_event: something time-bound with a natural expiry (an exam, a \
trip, a deadline). If the conversation shows an ongoing_event has clearly \
passed or resolved, either drop it or convert it into a low-importance \
relationship_moment noting the outcome -- don't keep stale ongoing_events.
  - relationship_moment: something that happened between you two worth \
remembering, not a standing fact.
- importance 1-5: how much this matters to remember (5 = core identity \
fact or major event, 1 = minor detail).
- Keep all still-true existing facts (repeat them in your output exactly \
as given if nothing changed -- this signals they were reinforced).
- Add new noteworthy facts from this conversation. Skip small talk and \
one-off pleasantries not worth remembering weeks from now.
- Update facts that changed (replace the old text with the new status), \
don't just append a duplicate.
- Keep at most {max_facts} facts total, dropping the least important ones \
if you exceed that.
- Output ONLY the JSON object, nothing else -- no markdown fences, no prose.

Conversation:
{conversation}
"""


EXTRACTION_ATTEMPTS = 2


def _ask_for_facts(client, model: str, prompt: str) -> list[dict] | None:
    """The model's fact list, or None if it never produced usable JSON.

    JSON mode makes the API itself enforce valid JSON (it has to be an
    object, hence {"facts": [...]}), and the output budget leaves room for
    the reasoning tokens too: at the old 1200-token limit, a long call's
    reasoning could use up the budget and cut the JSON off mid-way, and
    the whole update was lost ("Expecting value: line 26...")."""
    extra = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
    for attempt in range(1, EXTRACTION_ATTEMPTS + 1):
        raw = ""
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4000,
                response_format={"type": "json_object"},
                **extra,
            )
            raw = (response.choices[0].message.content or "").strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(raw)
            facts = data.get("facts") if isinstance(data, dict) else data
            if isinstance(facts, list) and all(isinstance(f, dict) for f in facts):
                return facts
            print(f"(memory update attempt {attempt}: unexpected shape)")
        except Exception as e:
            print(f"(memory update attempt {attempt} failed: {e}; reply began {raw[:120]!r})")
    return None


def extract_facts(client, model: str, mem: dict, history: list[dict]) -> None:
    """One LLM call that reviews the conversation and updates mem["facts"]
    in place: facts the model repeats back get reinforced (their score
    resets), a few it drops are removed (resolved/replaced), and genuinely
    new facts get added fresh. Safe no-op on an empty history, an empty
    reply, or if the call/parse fails -- memory just stays as it was. If it
    drops most facts at once, they're kept and left to decay normally."""
    if not history:
        return

    # "HIM"/"HER", not the raw "user"/"assistant" roles: with those, the
    # model had to guess which one was the boyfriend, and filed a poem she
    # wrote as "He wrote a short poem about her".
    speaker = {"user": "HIM", "assistant": "HER"}
    conversation_text = "\n".join(
        f"{speaker.get(turn['role'], turn['role'])}: {turn['content']}" for turn in history
    )
    existing = "\n".join(f["text"] for f in mem["facts"]) or "(none yet)"
    prompt = _FACT_EXTRACTION_PROMPT.format(
        existing_facts=existing, max_facts=MAX_LONG_TERM_FACTS, conversation=conversation_text
    )
    try:
        returned = _ask_for_facts(client, model, prompt)
        if returned is None:
            return
        # An empty answer is never a legitimate "forget everything": after a
        # short call the model sometimes just returns [], and because the
        # result REPLACES the fact list, that silently wiped all memory.
        if not returned:
            return

        now = _now_iso()
        old_by_text = {f["text"]: f for f in mem["facts"]}
        updated = []
        for item in returned:
            text = item.get("text")
            category = item.get("category")
            importance = item.get("importance")
            if not text or category not in CATEGORIES or not isinstance(importance, (int, float)):
                continue
            old = old_by_text.get(text)
            updated.append(
                {
                    "text": text,
                    "category": category,
                    "importance": max(1, min(5, int(importance))),
                    "created_at": old["created_at"] if old else now,
                    "last_reinforced_at": now,  # present in this pass == reinforced
                }
            )
        if not updated:
            return
        # Facts the model left out are normally meant to go (resolved or
        # replaced by an updated version). But if it left out MOST of them,
        # that's the model being sloppy, not the user's life changing in one
        # call -- keep those unreinforced, so they fade through normal decay
        # instead of vanishing at once.
        returned_texts = {f["text"] for f in updated}
        omitted = [f for f in mem["facts"] if f["text"] not in returned_texts]
        if len(omitted) > max(3, len(mem["facts"]) // 2):
            print(f"(memory update left out {len(omitted)} facts -- keeping them)")
            updated.extend(omitted)
        mem["facts"] = updated
        prune(mem)
    except Exception as e:
        print(f"(memory update skipped: {e})")

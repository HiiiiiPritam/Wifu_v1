"""
Her memory of you -- shared by the desktop companion and the phone calls,
since it's the same "her" in both. Stored in phase1/memory.json.

Layers (see the walkthrough that designed this):

  In the call    the last few exchanges word for word, plus a running note
                 of everything earlier in the call (kept by the call server,
                 not stored here).
  Episodes       a short dated summary of every call. Recent ones are shown
                 in full; older ones are compacted into one line per day,
                 then one line per week, then dropped.
  Facts          "About him" (who he is, what he likes) and "About us"
                 (milestones, rituals, inside jokes, promises). Each has a
                 kind that decides whether and how fast it fades.
  Plans          anything with a date ("date on Tue 22 Sep after 6 PM"),
                 stored as a real date, shown relative to now ("tonight"),
                 and expired once it has passed.
  Threads        open questions to follow up on ("how did the exam go?"),
                 closed when answered or after they've gone stale.

Two rules that the first version got wrong and this one exists to fix:
- Time: relative words ("tomorrow", "two weeks ago") are resolved into
  real dates when they're SAID, and turned back into relative words when
  she's SHOWN them. A stored "tomorrow" is wrong the next day, forever.
- Forgetting: a fact's clock only resets when he actually brings it up
  again. The first version reset every fact on every call (the model
  copied the whole list back), so nothing ever faded.

Nothing piles up: everything she's shown is capped per section (SHOW_*),
and every store has a ceiling and a way out (STORE_*, KEEP_*, expiry,
fading, compaction).
"""
import json
import re
import shutil
import uuid
from datetime import date, datetime, timedelta

from shared import ROOT

MEMORY_PATH = ROOT / "memory.json"
VERSION = 2

# ---------------------------------------------------------------- caps
# What she's shown per reply (her prompt is rebuilt every time, so these
# are what keep it the same size after a week or a year).
SHOW_HIM = 16
SHOW_US = 10
SHOW_PLANS = 4
SHOW_THREADS = 3
SHOW_RECENT_CALLS = 3
SHOW_DAYS = 4
SHOW_WEEKS = 3
SHORT_TERM_TURNS = 6  # exchanges shown word for word during a call

# What's kept on disk, and how things leave.
STORE_HIM = 60
STORE_US = 40
STORE_PLANS = 20
STORE_THREADS = 10
KEEP_EPISODES_DAYS = 7  # full call summaries (then only their day line remains)
KEEP_DAY_LINES_DAYS = 35  # one line per day (then only their week line remains)
KEEP_WEEK_LINES = 26  # one line per week, about six months
THREAD_MAX_DAYS = 7  # an open question nobody answered in a week is stale
THREAD_MAX_SHOWN = 3  # ...or once she's been prompted about it 3 calls running
MAX_PENDING = 5  # conversations waiting for a failed memory update to retry
NOTE_MAX_CHARS = 700  # the in-call running note is compressed past this

# How fast each kind of fact fades when it's not mentioned: a half-life in
# days (None = never fades). A fact whose score drops below FORGET_BELOW is
# removed, so "watching skit videos" goes but "his name" stays.
HALF_LIFE = {
    "him": {"identity": None, "preference": 60, "habit": 45, "other": 30},
    "us": {"milestone": None, "promise": None, "ritual": 30, "joke": 30, "moment": 21},
}
FORGET_BELOW = 0.6
KINDS = {section: tuple(kinds) for section, kinds in HALF_LIFE.items()}


# ---------------------------------------------------------------- time
def now_local() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    """Stored timestamps carry their UTC offset; older naive ones are
    taken as local time."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _part_of_day(dt: datetime) -> str:
    h = dt.hour
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening" if h < 21 else "night"


def when_label(dt: datetime, now: datetime) -> str:
    """A past moment the way a person would say it, relative to NOW:
    "earlier today, 2:40 PM", "last night, 9:27 PM", "Mon 21 Sep, 9:27 PM"."""
    dt = dt.astimezone(now.tzinfo)
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"{'this morning' if dt.hour < 12 else 'earlier today'}, {_clock(dt)}"
    if days == 1:
        part = _part_of_day(dt)
        return f"{'last night' if part in ('evening', 'night') else 'yesterday ' + part}, {_clock(dt)}"
    return f"{_day(dt.date())}, {_clock(dt)}"


def plan_label(plan: dict, now: datetime) -> str:
    """An upcoming plan relative to NOW: "tonight at 6 PM (in about 5
    hours)", "tomorrow", "Fri 25 Sep"."""
    when, has_time = _plan_when(plan)
    days = (when.date() - now.date()).days
    if days == 0:
        if not has_time:
            return "today"
        word = "tonight" if _part_of_day(when) in ("evening", "night") else "today"
        hours = (when - now).total_seconds() / 3600
        soon = f" (in about {round(hours)} hours)" if hours >= 1.5 else " (very soon)" if hours > 0 else ""
        return f"{word} at {_clock(when)}{soon}"
    day = "tomorrow" if days == 1 else _day(when.date())
    return f"{day} at {_clock(when)}" if has_time else day


def _plan_when(plan: dict) -> tuple[datetime, bool]:
    value = plan["when"]
    if "T" in value:
        return _parse(value), True
    d = date.fromisoformat(value)
    return datetime(d.year, d.month, d.day).astimezone(), False


def _plan_passed(plan: dict, now: datetime) -> bool:
    when, has_time = _plan_when(plan)
    if has_time:
        return now > when + timedelta(hours=3)  # a date doesn't end the minute it starts
    return now.date() > when.date()


def parse_when(value: str, now: datetime) -> str | None:
    """Validates a plan date from the model: "YYYY-MM-DD" or
    "YYYY-MM-DDTHH:MM". None if unusable or already in the past."""
    value = (value or "").strip()
    try:
        if "T" in value:
            dt = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M").astimezone()
            return None if dt < now - timedelta(hours=1) else dt.strftime("%Y-%m-%dT%H:%M")
        d = date.fromisoformat(value[:10])
        return None if d < now.date() else d.isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- storage
def _empty() -> dict:
    return {
        "version": VERSION,
        "last_seen": None,
        "facts": [],
        "plans": [],
        "threads": [],
        "episodes": [],
        "days": [],
        "weeks": [],
        "pending": [],
    }


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:6]


def load() -> dict:
    if not MEMORY_PATH.exists():
        return _empty()
    with open(MEMORY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != VERSION:
        data = _migrate_v1(data)
    return {**_empty(), **data}


def save(mem: dict) -> None:
    tmp = MEMORY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)
    tmp.replace(MEMORY_PATH)  # never a half-written memory file


def _migrate_v1(data: dict) -> dict:
    """First version: a flat list of {text, category, importance, ...}.
    Converted mechanically: identity/preference facts are about him,
    "relationship moments" are about the two of you, and "ongoing events"
    (mostly momentary states like "he is sleepy now") are dropped -- they
    were the stale "right now" facts this version exists to get rid of.
    The original file is kept as memory.v1.json. Its "last reinforced"
    times were meaningless (every fact was reset on every call), so each
    fact's age restarts from when it was first learned."""
    backup = MEMORY_PATH.with_name("memory.v1.json")
    if MEMORY_PATH.exists() and not backup.exists():
        shutil.copy2(MEMORY_PATH, backup)
    mem = _empty()
    mem["last_seen"] = data.get("last_seen")
    for f in data.get("facts", []):
        if isinstance(f, str):
            f = {"text": f, "category": "relationship_moment", "importance": 3}
        category = f.get("category")
        if category == "ongoing_event" or not f.get("text"):
            continue
        section, kind = {
            "identity": ("him", "identity"),
            "preference": ("him", "preference"),
        }.get(category, ("us", "moment"))
        learned = f.get("created_at") or _iso(now_local())
        mem["facts"].append({
            "id": _new_id("f"),
            "section": section,
            "kind": kind,
            "text": f["text"],
            "importance": int(f.get("importance") or 3),
            "created_at": learned,
            "last_mentioned_at": learned,
        })
    return mem


def touch_last_seen(mem: dict) -> None:
    mem["last_seen"] = _iso(now_local())


def seconds_since_last_seen(mem: dict) -> float | None:
    if not mem.get("last_seen"):
        return None
    return (now_local() - _parse(mem["last_seen"])).total_seconds()


def recent_window(history: list[dict], turns: int = SHORT_TERM_TURNS) -> list[dict]:
    """The last `turns` exchanges of the live conversation, word for word."""
    return history[-turns * 2 :]


# ---------------------------------------------------------------- ranking & eviction
def score(fact: dict, now: datetime) -> float:
    """Importance, faded by how long since he last brought it up."""
    half_life = HALF_LIFE.get(fact["section"], {}).get(fact["kind"], 30)
    if half_life is None:
        return float(fact["importance"])
    age_days = max(0.0, (now - _parse(fact["last_mentioned_at"])).total_seconds() / 86400)
    return fact["importance"] * 0.5 ** (age_days / half_life)


def enforce(mem: dict, now: datetime) -> None:
    """Every store's ceiling and every way out, applied in one place."""
    # Facts: faded ones go; then each section keeps its best STORE_* by score.
    kept = [f for f in mem["facts"] if score(f, now) >= FORGET_BELOW]
    for section, cap in (("him", STORE_HIM), ("us", STORE_US)):
        ranked = sorted((f for f in kept if f["section"] == section), key=lambda f: score(f, now), reverse=True)
        for f in ranked[cap:]:
            kept.remove(f)
    mem["facts"] = kept

    # Plans: passed ones expire (what happened lives in that day's episode).
    plans = [p for p in mem["plans"] if not _plan_passed(p, now)]
    plans.sort(key=lambda p: _plan_when(p)[0])
    mem["plans"] = plans[:STORE_PLANS]

    # Threads: stale after a week, or after she's been prompted 3 times.
    threads = [
        t for t in mem["threads"]
        if (now - _parse(t["created_at"])).days < THREAD_MAX_DAYS and t.get("shown", 0) < THREAD_MAX_SHOWN
    ]
    threads.sort(key=lambda t: t["created_at"], reverse=True)
    mem["threads"] = threads[:STORE_THREADS]

    # Episodes -> day lines -> week lines -> gone. A tier's items only
    # leave once the next tier has summarized them (see compact()).
    day_dates = {d["date"] for d in mem["days"]}
    cutoff = now.date() - timedelta(days=KEEP_EPISODES_DAYS)
    mem["episodes"] = [
        e for e in mem["episodes"]
        if _parse(e["start"]).date() >= cutoff or _parse(e["start"]).date().isoformat() not in day_dates
    ]
    week_starts = {w["start"] for w in mem["weeks"]}
    day_cutoff = now.date() - timedelta(days=KEEP_DAY_LINES_DAYS)
    mem["days"] = [
        d for d in mem["days"]
        if date.fromisoformat(d["date"]) >= day_cutoff or _week_start(date.fromisoformat(d["date"])).isoformat() not in week_starts
    ]
    mem["weeks"] = sorted(mem["weeks"], key=lambda w: w["start"])[-KEEP_WEEK_LINES:]
    mem["pending"] = mem["pending"][-MAX_PENDING:]


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def compact(mem: dict, now: datetime, summarize) -> None:
    """Folds finished days into one line each, and finished weeks into one
    line each. `summarize(kind, lines)` returns one line of text (an LLM
    call in practice); a failure just leaves it for next time."""
    today = now.date()
    have_days = {d["date"] for d in mem["days"]}
    by_day: dict[str, list[dict]] = {}
    for e in mem["episodes"]:
        by_day.setdefault(_parse(e["start"]).date().isoformat(), []).append(e)
    for day_iso, episodes in sorted(by_day.items()):
        if day_iso in have_days or date.fromisoformat(day_iso) >= today:
            continue
        lines = [f"{_clock(_parse(e['start']))} ({e.get('minutes', '?')} min, {e.get('mood', '')}): {e['summary']}" for e in sorted(episodes, key=lambda e: e["start"])]
        try:
            line = summarize("day", lines)
        except Exception as err:
            print(f"(couldn't summarize {day_iso}: {err})")
            continue
        if line:
            mem["days"].append({"date": day_iso, "line": line})

    have_weeks = {w["start"] for w in mem["weeks"]}
    by_week: dict[str, list[dict]] = {}
    for d in mem["days"]:
        by_week.setdefault(_week_start(date.fromisoformat(d["date"])).isoformat(), []).append(d)
    for week_iso, days in sorted(by_week.items()):
        if week_iso in have_weeks or date.fromisoformat(week_iso) + timedelta(days=7) > today:
            continue  # only finished weeks
        lines = [f"{_day(date.fromisoformat(d['date']))}: {d['line']}" for d in sorted(days, key=lambda d: d["date"])]
        try:
            line = summarize("week", lines)
        except Exception as err:
            print(f"(couldn't summarize the week of {week_iso}: {err})")
            continue
        if line:
            mem["weeks"].append({"start": week_iso, "line": line})
    mem["days"].sort(key=lambda d: d["date"])
    mem["weeks"].sort(key=lambda w: w["start"])


def mark_threads_shown(mem: dict, now: datetime) -> None:
    """Called when a call starts: the open threads she's about to see count
    one more showing, so an unanswered one stops nagging after a few calls."""
    for t in _open_threads_to_show(mem):
        t["shown"] = t.get("shown", 0) + 1


def _open_threads_to_show(mem: dict) -> list[dict]:
    return sorted(mem["threads"], key=lambda t: t["created_at"], reverse=True)[:SHOW_THREADS]


# ---------------------------------------------------------------- what she's shown
def render_context(mem: dict, now: datetime | None = None) -> str:
    """Her memories as prompt text, capped per section, with every time
    expressed relative to NOW -- so the same stored date reads "tomorrow"
    on Monday, "tonight" on Tuesday, and isn't shown at all on Wednesday."""
    now = now or now_local()
    sections: list[tuple[str, list[str]]] = []

    def top(section: str, n: int) -> list[str]:
        facts = [f for f in mem["facts"] if f["section"] == section]
        facts.sort(key=lambda f: score(f, now), reverse=True)
        return [f"- {f['text']}" for f in facts[:n]]

    sections.append(("What you know about him", top("him", SHOW_HIM)))
    sections.append(("About the two of you", top("us", SHOW_US)))

    upcoming = [p for p in mem["plans"] if not _plan_passed(p, now)]
    upcoming.sort(key=lambda p: _plan_when(p)[0])
    sections.append(("Coming up", [f"- {plan_label(p, now)}: {p['text']}" for p in upcoming[:SHOW_PLANS]]))

    sections.append((
        "Open threads (follow up on at most one, and only if it fits naturally)",
        [f"- {t['text']} (came up {when_label(_parse(t['created_at']), now)})" for t in _open_threads_to_show(mem)],
    ))

    episodes = sorted(mem["episodes"], key=lambda e: e["start"], reverse=True)[:SHOW_RECENT_CALLS]
    sections.append(("Your recent calls", [
        f"- {when_label(_parse(e['start']), now)} ({e.get('minutes', '?')} min"
        f"{', ' + e['mood'] if e.get('mood') else ''}): {e['summary']}"
        for e in episodes
    ]))

    # Older than what the recent calls cover: day lines, then week lines.
    covered_days = {_parse(e["start"]).date().isoformat() for e in episodes}
    oldest_shown = min((_parse(e["start"]).date() for e in episodes), default=now.date())
    days = [d for d in mem["days"] if d["date"] not in covered_days and date.fromisoformat(d["date"]) < oldest_shown]
    days = sorted(days, key=lambda d: d["date"], reverse=True)[:SHOW_DAYS]
    sections.append(("Earlier days", [f"- {_day(date.fromisoformat(d['date']))}: {d['line']}" for d in days]))
    if days:
        oldest_shown = min(oldest_shown, min(date.fromisoformat(d["date"]) for d in days))
    weeks = [w for w in mem["weeks"] if date.fromisoformat(w["start"]) + timedelta(days=7) <= oldest_shown]
    weeks = sorted(weeks, key=lambda w: w["start"], reverse=True)[:SHOW_WEEKS]
    sections.append(("Earlier weeks", [f"- Week of {date.fromisoformat(w['start']).day} {date.fromisoformat(w['start']):%b}: {w['line']}" for w in weeks]))

    body = "\n\n".join(f"{title}:\n" + "\n".join(lines) for title, lines in sections if lines)
    if not body:
        return ""
    return (
        "\n\nYour memories of him -- use them the way someone who remembers "
        "would, naturally and when relevant; never recite them:\n\n" + body + "\n"
    )


# ---------------------------------------------------------------- updating
_TAG = re.compile(r"^\[\w+\]\s*")  # the desktop companion's [heart]-style emotion tags


def transcript(messages: list[dict]) -> str:
    speaker = {"user": "HIM", "assistant": "HER"}
    return "\n".join(
        f"{speaker.get(m['role'], m['role'])}: {_TAG.sub('', m['content'])}"
        for m in messages if m.get("role") in speaker
    )


_UPDATE_PROMPT = """\
You maintain a girlfriend character's memory of her boyfriend and of their \
relationship. Update it from the new conversation below by returning \
OPERATIONS -- not a rewritten list. Anything you don't mention stays exactly \
as it is.

NOW: {now}

Calendar (use it -- never work out weekdays yourself):
{calendar}

Existing memory (ids in brackets):
{existing}

{context_block}New conversation -- HIM is the boyfriend, HER is the girlfriend character:
{conversation}

Return a JSON object: {{"ops": [...]{episode_field}}}

Operations:
- {{"op": "add", "section": "him" | "us", "kind": ..., "text": ..., "importance": 1-5}}
    section "him": facts about HIM. kind: "identity" (who he is: name, work,
      studies, placement/career, family, where he lives), "preference" (likes/dislikes),
      "habit" (things he regularly does), "other".
    section "us": things that belong to the two of them. kind: "milestone"
      (firsts and big moments: first date, first "I love you"), "ritual"
      (their routines: a nightly call), "joke" (inside jokes, nicknames),
      "promise" (things they said they'd do together someday, with no date),
      "moment" (a meaningful shared moment that isn't a milestone).
- {{"op": "update", "id": ..., "text": ..., "importance": 1-5}}  a fact changed
- {{"op": "mentioned", "id": ...}}  HE brought up an existing fact again
- {{"op": "remove", "id": ...}}  a fact is no longer true
- {{"op": "add_plan", "text": ..., "when": "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM"}}
    Put a time given in words INTO "when" ("Saturday at 7 in the evening" ->
    "2026-09-26T19:00"), not into the text.
- {{"op": "update_plan", "id": ..., "text": ..., "when": ...}}
- {{"op": "remove_plan", "id": ...}}  cancelled
- {{"op": "open_thread", "text": ...}}  something she should follow up on later \
("how did the exam go?"), phrased as what to ask about
- {{"op": "close_thread", "id": ...}}  answered or no longer relevant

Rules:
- Only record what HE said or clearly confirmed. HER lines are hers: her \
stories, suggestions and made-up details are not facts about him. If she \
suggested a plan, it's only a plan if he agreed.
- Turn every relative time into a real date using NOW: "tomorrow", "on \
Friday", "two weeks ago", "next month". Never store the word "tomorrow". \
Write dates inside text like "7 Sep 2026", never "2026-09-07".
- Anything still UPCOMING with a date (an exam, a trip, a date night) is a \
plan, not a fact: {{"op": "add_plan", "text": "His exam", "when": \
"2026-09-25"}}. A PAST event is a fact whose text includes its date ("Got \
placed around 7 Sep 2026").
- Close every open thread this conversation answered: if he said how the \
date went, close "How did the date go?". Open a new thread only for \
something with an outcome she'll want to hear about later.
- Moods and passing states ("he's sleepy", "he feels sad right now", "he's \
eating") are NOT facts{mood_hint}.
- "mentioned" only when HE actually brought the thing up again. Don't \
mention facts that merely exist.
- Prefer "update" over adding a near-duplicate of an existing fact.
- Write every text as she would think it: "he" for him, "you" for her ("Date \
night with you", "He loves the dry noodles you tease him about"). Never \
write "HIM" or "HER" in a text.
- Skip small talk. Importance: 5 = core (his name, their first date), \
1 = minor detail.
- An empty ops list is fine when nothing new was learned.
"""

_EPISODE_FIELD = ', "episode": {"summary": "...", "mood": "..."}'
_EPISODE_RULES = """
Also return "episode": a summary of this WHOLE call for her to remember it \
by -- everything in "Earlier in this call" above PLUS the new conversation, \
not just the last few lines. "summary": 1-3 short sentences, past tense, \
calling him "he" and her "you" (e.g. "He was down about his friends' placements; you cheered him up \
and planned a date."). Include outcomes of anything followed up on. No \
relative days in the summary -- it will be read days later, so write \
"Tuesday evening" or "25 Sep", never "tomorrow" or "yesterday". \
"mood": his mood in a few words, e.g. "low -> better", "playful", "tired".
"""


def _calendar(now: datetime) -> str:
    """Two weeks either side of today with weekday names. Language models
    are unreliable at weekday arithmetic -- asked for "this Saturday" on a
    Tuesday, one answered Thursday -- so they look dates up instead."""
    today = now.date()
    names = {-1: "yesterday", 0: "TODAY", 1: "tomorrow"}
    days = []
    for offset in range(-14, 15):
        d = today + timedelta(days=offset)
        label = f"{d:%a} {d.isoformat()}"
        days.append(f"{label} ({names[offset]})" if offset in names else label)
    return ", ".join(days)


def _existing_block(mem: dict, now: datetime) -> str:
    lines = []
    for f in mem["facts"]:
        lines.append(f"[{f['id']}] {f['section']}/{f['kind']}: {f['text']}")
    for p in mem["plans"]:
        lines.append(f"[{p['id']}] plan on {p['when']}: {p['text']}")
    for t in mem["threads"]:
        lines.append(f"[{t['id']}] open thread: {t['text']}")
    return "\n".join(lines) or "(nothing yet)"


def _ask_json(client, model: str, prompt: str) -> dict | None:
    extra = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
    for attempt in (1, 2):
        raw = ""
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=4000,
                response_format={"type": "json_object"},
                **extra,
            )
            raw = (response.choices[0].message.content or "").strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"(memory update attempt {attempt} failed: {e}; reply began {raw[:120]!r})")
    return None


def update_from_conversation(
    client,
    model: str,
    mem: dict,
    messages: list[dict],
    now: datetime | None = None,
    *,
    final: bool = False,
    earlier_note: str = "",
    call_start: datetime | None = None,
    minutes: float | None = None,
) -> bool:
    """One LLM call: turns the conversation into memory operations (and,
    when `final`, the call's episode). `earlier_note` is the running note of
    the call's earlier part, already processed -- given as context so the
    episode covers the whole call. False if the model never produced usable
    JSON; the caller keeps the conversation for a retry."""
    now = now or now_local()
    conversation = transcript(messages)
    if not conversation and not final:
        return True
    context_block = f"Earlier in this call (already processed, context only):\n{earlier_note}\n\n" if earlier_note else ""
    prompt = _UPDATE_PROMPT.format(
        now=f"{now:%A %d %B %Y, %I:%M %p}",
        calendar=_calendar(now),
        existing=_existing_block(mem, now),
        context_block=context_block,
        conversation=conversation or "(nothing new)",
        episode_field=_EPISODE_FIELD if final else "",
        mood_hint=" -- they belong in the episode's mood and summary" if final else "",
    )
    if final:
        prompt += _EPISODE_RULES
    data = _ask_json(client, model, prompt)
    if data is None:
        return False
    apply_ops(mem, data.get("ops") or [], now)
    episode = data.get("episode")
    if final and isinstance(episode, dict) and str(episode.get("summary") or "").strip():
        start = call_start or now
        mem["episodes"].append({
            "id": _new_id("e"),
            "start": _iso(start),
            "minutes": max(1, round(minutes if minutes is not None else (now - start).total_seconds() / 60)),
            "mood": str(episode.get("mood") or "").strip()[:40],
            "summary": str(episode["summary"]).strip()[:400],
        })
    enforce(mem, now)
    return True


def apply_ops(mem: dict, ops: list, now: datetime) -> dict:
    """Applies the model's operations, ignoring any that don't validate
    (unknown ids, missing text, dates in the past). Returns counts, for
    logging."""
    counts: dict[str, int] = {}
    facts = {f["id"]: f for f in mem["facts"]}
    plans = {p["id"]: p for p in mem["plans"]}
    threads = {t["id"]: t for t in mem["threads"]}
    stamp = _iso(now)

    def count(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        kind_of_op = op.get("op")
        text = str(op.get("text") or "").strip()[:200]
        if kind_of_op == "add":
            section = op.get("section") if op.get("section") in KINDS else "him"
            kind = op.get("kind") if op.get("kind") in KINDS[section] else KINDS[section][-1]
            if not text:
                continue
            same = next((f for f in mem["facts"] if f["text"].casefold() == text.casefold()), None)
            if same:
                same["last_mentioned_at"] = stamp
                count("mentioned")
                continue
            fact = {
                "id": _new_id("f"), "section": section, "kind": kind, "text": text,
                "importance": _importance(op.get("importance")),
                "created_at": stamp, "last_mentioned_at": stamp,
            }
            mem["facts"].append(fact)
            facts[fact["id"]] = fact
            count("add")
        elif kind_of_op in ("update", "mentioned") and op.get("id") in facts:
            fact = facts[op["id"]]
            if kind_of_op == "update":
                if text:
                    fact["text"] = text
                if op.get("importance") is not None:
                    fact["importance"] = _importance(op.get("importance"))
            fact["last_mentioned_at"] = stamp
            count(kind_of_op)
        elif kind_of_op == "remove" and op.get("id") in facts:
            mem["facts"].remove(facts.pop(op["id"]))
            count("remove")
        elif kind_of_op == "add_plan":
            when = parse_when(str(op.get("when") or ""), now)
            if text and when:
                plan = {"id": _new_id("p"), "text": text, "when": when, "created_at": stamp}
                mem["plans"].append(plan)
                plans[plan["id"]] = plan
                count("add_plan")
        elif kind_of_op == "update_plan" and op.get("id") in plans:
            plan = plans[op["id"]]
            if text:
                plan["text"] = text
            when = parse_when(str(op.get("when") or ""), now)
            if when:
                plan["when"] = when
            count("update_plan")
        elif kind_of_op == "remove_plan" and op.get("id") in plans:
            mem["plans"].remove(plans.pop(op["id"]))
            count("remove_plan")
        elif kind_of_op == "open_thread" and text:
            if not any(t["text"].casefold() == text.casefold() for t in mem["threads"]):
                thread = {"id": _new_id("t"), "text": text, "created_at": stamp, "shown": 0}
                mem["threads"].append(thread)
                threads[thread["id"]] = thread
                count("open_thread")
        elif kind_of_op == "close_thread" and op.get("id") in threads:
            mem["threads"].remove(threads.pop(op["id"]))
            count("close_thread")
    return counts


def _importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def summarizer(client, model: str):
    """The compact() summarizer, backed by an LLM."""
    def summarize(kind: str, lines: list[str]) -> str:
        what = "one day of calls" if kind == "day" else "one week, day by day"
        prompt = (
            f"These are a girlfriend character's notes on {what} with her boyfriend. "
            "Compress them into ONE line (max 25 words), past tense, calling him \"he\": "
            "keep what mattered (events, how he felt, anything they planned or shared), "
            "drop small talk.\n\n" + "\n".join(lines) + "\n\nReturn only the line."
        )
        extra = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=800, **extra,
        )
        return (response.choices[0].message.content or "").strip().strip('"')[:220]
    return summarize


# ---------------------------------------------------------------- in-call note
def update_call_note(client, model: str, note: str, messages: list[dict], compress_model: str | None = None) -> str:
    """The running note of the earlier part of a call (Layer 1). Each new
    part is summarized on its own and APPENDED, so nothing already in the
    note can be lost -- a model asked to rewrite the whole note tended to
    keep only the newest part. Only when the note gets long is it
    compressed, with instructions to keep every concrete detail."""
    def extra(m: str) -> dict:
        return {"reasoning_effort": "low"} if m.startswith("openai/gpt-oss") else {}

    prompt = (
        "Summarize this part of a phone call between a girlfriend character (HER) and her "
        'boyfriend (HIM) in 1-2 short sentences, past tense, calling him "he" and her '
        '"you". Keep concrete details (names, dates, plans, how he felt); drop small talk.\n\n'
        f"{transcript(messages)}\n\nReturn only the summary."
    )
    response = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=600, **extra(model),
    )
    part = (response.choices[0].message.content or "").strip()
    if not part:
        return note
    combined = f"{note} {part}".strip()
    if len(combined) <= NOTE_MAX_CHARS:
        return combined
    compress_model = compress_model or model
    prompt = (
        "Shorten this running note of a phone call to at most 90 words. Keep EVERY "
        "concrete detail -- names, dates, plans, what he told you, how he felt -- and "
        'drop only repetition and filler. Past tense, "he" and "you".\n\n'
        f"{combined}\n\nReturn only the shortened note."
    )
    response = client.chat.completions.create(
        model=compress_model, messages=[{"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=900, **extra(compress_model),
    )
    return (response.choices[0].message.content or "").strip() or combined[-NOTE_MAX_CHARS:]

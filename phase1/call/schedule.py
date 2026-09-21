"""
One-time scheduled calls: "call me at 9pm tomorrow -- reminder for
medicine". Stored in phase1/schedule.json (gitignored), managed from the
app's Settings tab via GET/POST/DELETE /schedule.

Rules (see server._ring_loop for where they're applied):
- A scheduled call rings at its time even inside a quiet window: you set
  it up for exactly that time, so it beats the general "don't call" rule.
- If a periodic call and a scheduled one would land close together, the
  scheduled one wins and the periodic call is skipped (OVERLAP_MINUTES).
- If you're already on a call when one comes due, she brings it up in
  that call instead of ringing you.
- Unanswered: retried RETRY_MINUTES later, up to MAX_ATTEMPTS rings, then
  marked missed.
- One call per minute. Any that still come due together (e.g. a retry
  landing on another call's time) are merged into one call.
"""
import json
import uuid
from datetime import datetime, timedelta

from shared import ROOT

SCHEDULE_PATH = ROOT / "schedule.json"

OVERLAP_MINUTES = 30
RETRY_MINUTES = 5
MAX_ATTEMPTS = 3
# Entries this far past their time without ringing (the server was off)
# are marked missed instead of ringing hours late.
STALE_AFTER_MINUTES = 60
KEEP_FINISHED = 20  # done/missed entries kept for the app's history view

FORMAT = "%Y-%m-%dT%H:%M"


def load() -> list[dict]:
    if not SCHEDULE_PATH.exists():
        return []
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict) and "id" in e and "at" in e]
    except Exception as e:
        print(f"(schedule.json unreadable, ignoring it: {e})")
        return []


def save(entries: list[dict]) -> None:
    pending = sorted((e for e in entries if e["status"] == "pending"), key=lambda e: e["at"])
    finished = sorted((e for e in entries if e["status"] != "pending"), key=lambda e: e["at"])
    entries = pending + finished[-KEEP_FINISHED:]
    SCHEDULE_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def parse_at(value: str) -> datetime:
    return datetime.strptime(value, FORMAT)


def add(at: str, note: str) -> dict:
    when = parse_at(at)  # raises ValueError on a bad timestamp
    if when < datetime.now() - timedelta(minutes=1):
        raise ValueError("that time has already passed")
    entries = load()
    # One call per minute: two set for the same time ring back to back (or
    # one rings and the other gets mentioned mid-call), which just feels
    # like a glitch. due() merges any that still coincide.
    if any(e["status"] == "pending" and e["at"] == when.strftime(FORMAT) for e in entries):
        raise ValueError("there's already a call scheduled at that time")
    entry = {
        "id": uuid.uuid4().hex[:10],
        "at": when.strftime(FORMAT),
        "note": str(note or "").strip()[:200],
        "status": "pending",  # pending -> done | missed
        "attempts": 0,
        "next_try": when.strftime(FORMAT),
    }
    entries.append(entry)
    save(entries)
    return entry


def remove(entry_id: str) -> bool:
    entries = load()
    kept = [e for e in entries if e["id"] != entry_id]
    save(kept)
    return len(kept) != len(entries)


def update(entry_id: str, **changes) -> None:
    entries = load()
    for e in entries:
        if e["id"] == entry_id:
            e.update(changes)
    save(entries)


def due(now: datetime) -> dict | None:
    """The scheduled call that should ring now, if any -- with any others
    due at the same moment merged into it. Marks long-overdue entries
    missed on the way."""
    entries = load()
    changed = False
    ready = []
    for e in entries:
        if e["status"] != "pending":
            continue
        if now - parse_at(e["at"]) > timedelta(minutes=STALE_AFTER_MINUTES):
            e["status"] = "missed"
            changed = True
            continue
        if parse_at(e["next_try"]) <= now:
            ready.append(e)
    if len(ready) > 1:
        # Several calls due at once (set before the one-per-minute rule
        # existed, or a retry landing on another call's time): ONE call
        # carrying all their reasons, not a ring right after a ring.
        first, rest = ready[0], ready[1:]
        notes = [n for n in dict.fromkeys(e["note"] for e in ready) if n]
        first["note"] = "; ".join(notes)
        for e in rest:
            e["status"] = "done"
            e["note"] = f"{e['note']} (merged into another call)".strip()
        changed = True
    if changed:
        save(entries)
    return ready[0] if ready else None


def blocks_periodic(now: datetime) -> bool:
    """True if a scheduled call is due soon or rang recently -- the
    periodic call is skipped so the two don't land on top of each other."""
    window = timedelta(minutes=OVERLAP_MINUTES)
    for e in load():
        at = parse_at(e["at"])
        if e["status"] == "pending" and now <= at <= now + window:
            return True
        if e["status"] == "done" and now - window <= at <= now:
            return True
    return False


def record_ring(entry_id: str) -> None:
    entries = load()
    for e in entries:
        if e["id"] == entry_id:
            e["attempts"] += 1
            if e["attempts"] >= MAX_ATTEMPTS:
                # Stays pending until this last ring times out; the
                # server marks it missed then (see mark_unanswered).
                e["next_try"] = "9999-12-31T23:59"
            else:
                retry = datetime.now() + timedelta(minutes=RETRY_MINUTES)
                e["next_try"] = retry.strftime(FORMAT)
    save(entries)


def mark_unanswered(entry_id: str) -> None:
    for e in load():
        if e["id"] == entry_id and e["attempts"] >= MAX_ATTEMPTS:
            update(entry_id, status="missed")

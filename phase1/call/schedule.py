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
    entry = {
        "id": uuid.uuid4().hex[:10],
        "at": when.strftime(FORMAT),
        "note": str(note or "").strip()[:200],
        "status": "pending",  # pending -> done | missed
        "attempts": 0,
        "next_try": when.strftime(FORMAT),
    }
    entries = load()
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
    """The scheduled call that should ring now, if any. Marks long-overdue
    entries missed on the way."""
    entries = load()
    changed = False
    result = None
    for e in entries:
        if e["status"] != "pending":
            continue
        if now - parse_at(e["at"]) > timedelta(minutes=STALE_AFTER_MINUTES):
            e["status"] = "missed"
            changed = True
            continue
        if parse_at(e["next_try"]) <= now and result is None:
            result = e
    if changed:
        save(entries)
    return result


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

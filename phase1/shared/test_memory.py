"""
Memory logic with fixed dates and no API calls -- relative times, expiry,
fading, caps, compaction and the v1 migration:

    python -m shared.test_memory
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared import memory as m

IST = timezone(timedelta(hours=5, minutes=30))


def at(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


failed = 0


def check(label, ok, detail=""):
    global failed
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""))
    failed += not ok


def fake_summarizer(kind, lines):
    return f"{kind} summary of {len(lines)}"


def main():
    # Walkthrough scenario ------------------------------------------------
    print("1) relative time: the same stored date reads differently each day")
    mem = m._empty()
    mon_night = at(2026, 9, 21, 22, 2)
    m.apply_ops(mem, [
        {"op": "add_plan", "text": "Date at a traditional Indian restaurant", "when": "2026-09-22T18:00"},
        {"op": "add", "section": "him", "kind": "identity", "text": "Got placed around 7 Sep 2026", "importance": 4},
        {"op": "add", "section": "him", "kind": "habit", "text": "Goes for post-dinner walks", "importance": 2},
        {"op": "open_thread", "text": "How did the date go?"},
    ], mon_night)
    mem["episodes"].append({"id": "e1", "start": m._iso(at(2026, 9, 21, 21, 27)), "minutes": 35,
                            "mood": "low -> better", "summary": "He was down about friends' placements; you cheered him up."})
    mon = m.render_context(mem, at(2026, 9, 21, 22, 30))
    tue = m.render_context(mem, at(2026, 9, 22, 12, 5))
    wed = m.render_context(mem, at(2026, 9, 23, 21, 0))
    check("Monday night: the date is 'tomorrow'", "tomorrow at 6:00 PM" in mon, mon)
    check("Tuesday noon: the date is 'tonight', with hours to go", "tonight at 6:00 PM (in about 6 hours)" in tue, tue)
    check("Tuesday: Monday's call reads 'last night'", "last night, 9:27 PM" in tue, tue)
    check("Wednesday: the passed date is gone from 'Coming up'", "Coming up" not in wed, wed)
    check("Wednesday: Monday's call reads with its date", "Mon 21 Sep, 9:27 PM" in wed, wed)
    check("the word 'tomorrow' is never stored", "tomorrow" not in json.dumps(mem).lower())

    print("2) plans in the past are rejected; bad ops are ignored")
    counts = m.apply_ops(mem, [
        {"op": "add_plan", "text": "Yesterday's thing", "when": "2026-09-20"},
        {"op": "mentioned", "id": "f-nope"},
        {"op": "remove", "id": "f-nope"},
        "garbage",
        {"op": "add", "section": "him", "kind": "identity", "text": "  "},
    ], mon_night)
    check("nothing applied", counts == {}, str(counts))

    print("3) forgetting: only real mentions reset the clock")
    mem = m._empty()
    old = at(2026, 3, 1)
    m.apply_ops(mem, [
        {"op": "add", "section": "him", "kind": "identity", "text": "His name is Pritam", "importance": 5},
        {"op": "add", "section": "him", "kind": "preference", "text": "Watches skit videos", "importance": 1},
        {"op": "add", "section": "us", "kind": "milestone", "text": "First date on 22 Sep 2026", "importance": 5},
        {"op": "add", "section": "us", "kind": "joke", "text": "The dry noodles joke", "importance": 2},
    ], old)
    later = at(2026, 9, 1)
    m.enforce(mem, later)
    texts = {f["text"] for f in mem["facts"]}
    check("identity survives six months unmentioned", "His name is Pritam" in texts)
    check("milestone survives six months unmentioned", "First date on 22 Sep 2026" in texts)
    check("a minor preference fades away", "Watches skit videos" not in texts)
    check("an unused inside joke fades away", "The dry noodles joke" not in texts)
    fresh = m._empty()
    m.apply_ops(fresh, [{"op": "add", "section": "us", "kind": "joke", "text": "The dry noodles joke", "importance": 2}], old)
    joke_id = fresh["facts"][0]["id"]
    m.apply_ops(fresh, [{"op": "mentioned", "id": joke_id}], at(2026, 8, 30))
    m.enforce(fresh, later)
    check("...unless he brings it up again", len(fresh["facts"]) == 1)
    dup = m.apply_ops(fresh, [{"op": "add", "section": "us", "kind": "joke", "text": "the DRY noodles joke"}], later)
    check("re-adding an existing fact counts as a mention, not a duplicate", dup == {"mentioned": 1} and len(fresh["facts"]) == 1)

    print("4) storage ceilings and prompt caps")
    mem = m._empty()
    base = at(2026, 9, 1)
    m.apply_ops(mem, [{"op": "add", "section": "him", "kind": "identity", "text": f"Fact {i}", "importance": 1 + i % 5} for i in range(100)], base)
    m.apply_ops(mem, [{"op": "add", "section": "us", "kind": "milestone", "text": f"Us {i}", "importance": 3} for i in range(70)], base)
    m.enforce(mem, base)
    him = [f for f in mem["facts"] if f["section"] == "him"]
    us = [f for f in mem["facts"] if f["section"] == "us"]
    check(f"'About him' storage capped at {m.STORE_HIM}", len(him) == m.STORE_HIM, str(len(him)))
    check("...keeping the most important ones", min(f["importance"] for f in him) >= 3)
    check(f"'About us' storage capped at {m.STORE_US}", len(us) == m.STORE_US, str(len(us)))
    text = m.render_context(mem, base)
    shown_him = text.split("What you know about him:\n")[1].split("\n\n")[0].count("\n- ") + 1
    check(f"only {m.SHOW_HIM} 'About him' lines shown", shown_him == m.SHOW_HIM, str(shown_him))

    print("5) open threads go stale")
    mem = m._empty()
    m.apply_ops(mem, [{"op": "open_thread", "text": "How did the exam go?"}], at(2026, 9, 1))
    m.enforce(mem, at(2026, 9, 9))
    check("unanswered for over a week: closed", mem["threads"] == [])
    m.apply_ops(mem, [{"op": "open_thread", "text": "How was the movie?"}], at(2026, 9, 10))
    for day in (10, 11, 12):
        m.mark_threads_shown(mem, at(2026, 9, day))
        m.enforce(mem, at(2026, 9, day))
    check("prompted three calls running: dropped so she doesn't nag", mem["threads"] == [])

    print("6) compaction: episodes -> days -> weeks, nothing piles up")
    mem = m._empty()
    start = at(2026, 8, 3, 21, 0)  # a Monday
    lengths = []
    for i in range(60):  # 60 days of two calls a day
        day = start + timedelta(days=i)
        for hour in (13, 21):
            call = day.replace(hour=hour)
            mem["episodes"].append({"id": f"e{i}-{hour}", "start": m._iso(call), "minutes": 10,
                                    "mood": "fine", "summary": f"Call on day {i} at {hour}h with some words in it."})
        now = day.replace(hour=23)
        m.compact(mem, now, fake_summarizer)
        m.enforce(mem, now)
        lengths.append(len(m.render_context(mem, now)))
    check(f"full episodes kept only ~{m.KEEP_EPISODES_DAYS} days", len(mem["episodes"]) <= 2 * (m.KEEP_EPISODES_DAYS + 1), str(len(mem["episodes"])))
    check(f"day lines kept only ~{m.KEEP_DAY_LINES_DAYS} days", len(mem["days"]) <= m.KEEP_DAY_LINES_DAYS + 7, str(len(mem["days"])))
    check("finished weeks got a line", len(mem["weeks"]) >= 6, str(len(mem["weeks"])))
    final = m.render_context(mem, now)
    check("prompt shows 3 calls + 4 days + 3 weeks", final.count("\n- ") >= 9 and "Earlier weeks" in final, final)
    check("prompt size flat from day 10 to day 60", max(lengths[10:]) - min(lengths[10:]) < 200, f"{min(lengths[10:])}..{max(lengths[10:])}")
    failing = m._empty()
    failing["episodes"] = [{"id": "x", "start": m._iso(at(2026, 9, 1, 20)), "minutes": 5, "mood": "", "summary": "s"}]

    def broken(kind, lines):
        raise RuntimeError("model down")

    m.compact(failing, at(2026, 9, 20), broken)
    m.enforce(failing, at(2026, 9, 20))
    check("a failed summary never loses the episode", len(failing["episodes"]) == 1)

    print("7) migration from the first version")
    with tempfile.TemporaryDirectory() as tmp:
        m.MEMORY_PATH = Path(tmp) / "memory.json"
        v1 = {"facts": [
            {"text": "His name is Pritam", "category": "identity", "importance": 5, "created_at": "2026-09-19T18:56:59+00:00", "last_reinforced_at": "2026-09-21T21:37:50+00:00"},
            {"text": "He likes pizza", "category": "preference", "importance": 3, "created_at": "2026-09-19T17:21:44+00:00", "last_reinforced_at": "2026-09-21T21:37:50+00:00"},
            {"text": "They met tonight for pizza", "category": "relationship_moment", "importance": 4, "created_at": "2026-09-19T17:56:30+00:00", "last_reinforced_at": "2026-09-21T21:37:50+00:00"},
            {"text": "He is very sleepy and wants to sleep now", "category": "ongoing_event", "importance": 2, "created_at": "2026-09-19T18:58:55+00:00", "last_reinforced_at": "2026-09-21T21:37:50+00:00"},
        ], "last_seen": "2026-09-21T21:37:50+00:00"}
        m.MEMORY_PATH.write_text(json.dumps(v1), encoding="utf-8")
        mem = m.load()
        sections = {f["text"]: (f["section"], f["kind"]) for f in mem["facts"]}
        check("identity -> about him", sections.get("His name is Pritam") == ("him", "identity"))
        check("relationship moment -> about us", sections.get("They met tonight for pizza") == ("us", "moment"))
        check("momentary state dropped", "He is very sleepy and wants to sleep now" not in sections)
        check("fake 'reinforced' time discarded", all(f["last_mentioned_at"] == f["created_at"] for f in mem["facts"]))
        check("original kept as memory.v1.json", (Path(tmp) / "memory.v1.json").exists())
        m.save(mem)
        check("saves as version 2", json.loads(m.MEMORY_PATH.read_text(encoding="utf-8"))["version"] == 2)

    print("\nALL PASSED" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

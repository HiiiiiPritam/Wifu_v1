"""
Memory extraction against the real model: does it follow the rules?
Makes real Groq calls; touches no files.

    python -m shared.test_memory_llm
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from shared import llm
from shared import memory as m

IST = timezone(timedelta(hours=5, minutes=30))
failed = 0


def check(label, ok, detail=""):
    global failed
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"\n          {detail}" if not ok and detail else ""))
    failed += not ok


def convo(*lines):
    return [{"role": "user" if who == "HIM" else "assistant", "content": text} for who, text in lines]


def main():
    client = llm.sync_client()
    mem = m._empty()

    print("Monday 21 Sep, 9:30 PM")
    monday = datetime(2026, 9, 21, 21, 58, tzinfo=IST)
    ok = m.update_from_conversation(client, llm.MEMORY_MODEL, mem, convo(
        ("HER", "Hey you, how was your day?"),
        ("HIM", "Honestly not great. I'm feeling kind of low right now."),
        ("HER", "Aww, what happened?"),
        ("HIM", "My friends might get into better companies than me. I got placed two weeks ago but still."),
        ("HER", "You got placed first! Want to go on a date to cheer you up?"),
        ("HIM", "Yeah, let's go on a date tomorrow evening, after 6, at that traditional Indian restaurant."),
        ("HER", "Yay! And someday we should go to Goa together."),
        ("HIM", "haha maybe, we'll see."),
        ("HIM", "Also I'm so sleepy right now."),
        ("HER", "Go sleep, silly."),
    ), monday, final=True, call_start=datetime(2026, 9, 21, 21, 27, tzinfo=IST), minutes=31)
    print(json.dumps({k: mem[k] for k in ("facts", "plans", "threads", "episodes")}, indent=1, ensure_ascii=False)[:3000])
    check("update succeeded", ok)
    plans = mem["plans"]
    check("the date became a plan on Tue 22 Sep", any(p["when"].startswith("2026-09-22") for p in plans), str(plans))
    blob = json.dumps(mem["facts"]).lower()
    check("'sleepy right now' is not a fact", "sleepy" not in blob, blob)
    check("'feeling low right now' is not a fact", "low right now" not in blob and "feels low" not in blob, blob)
    placed = [f for f in mem["facts"] if "placed" in f["text"].lower()]
    check("placement stored as a fact", bool(placed), blob)
    check("...with a real date, not 'two weeks ago'", placed and "two weeks" not in placed[0]["text"].lower()
          and any(x in placed[0]["text"] for x in ("Sep", "September", "2026")), str(placed))
    check("Goa ('haha maybe') is not a plan", not any("goa" in p["text"].lower() for p in plans), str(plans))
    check("an episode was written", len(mem["episodes"]) == 1 and len(mem["episodes"][0]["summary"]) > 20)

    print("\nWednesday 23 Sep, 9:00 PM")
    m.enforce(mem, datetime(2026, 9, 23, 20, 59, tzinfo=IST))
    mem["threads"].append({"id": "t1", "text": "How did the date go?", "created_at": "2026-09-21T22:00:00+05:30", "shown": 1})
    wednesday = datetime(2026, 9, 23, 21, 20, tzinfo=IST)
    ok = m.update_from_conversation(client, llm.MEMORY_MODEL, mem, convo(
        ("HER", "Okay, spill. How was last night?"),
        ("HIM", "It was really nice honestly. Our first proper date, I loved it."),
        ("HER", "Aww, me too! What's next for you this week?"),
        ("HIM", "I have an exam on Friday, kind of nervous."),
        ("HER", "You'll do great. I'll call you after it."),
    ), wednesday, final=True, call_start=datetime(2026, 9, 23, 21, 0, tzinfo=IST), minutes=20)
    print(json.dumps({k: mem[k] for k in ("facts", "plans", "threads")}, indent=1, ensure_ascii=False)[:2500])
    check("update succeeded", ok)
    us = [f for f in mem["facts"] if f["section"] == "us"]
    check("the first date became an 'about us' fact", any("date" in f["text"].lower() for f in us), str(us))
    check("the exam became a plan on Fri 25 Sep", any(p["when"].startswith("2026-09-25") for p in mem["plans"]), str(mem["plans"]))
    check("the answered thread was closed", not any(t["id"] == "t1" for t in mem["threads"]), str(mem["threads"]))
    print("\nWhat she'd be shown on Thursday morning:")
    print(m.render_context(mem, datetime(2026, 9, 24, 9, 30, tzinfo=IST)))

    print("ALL PASSED" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

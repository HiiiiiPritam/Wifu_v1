"""
The memory flow of a whole call, in-process with real model calls: the
running note, a mid-call checkpoint, and the end-of-call episode. Thresholds
are shrunk so a six-line call exercises everything. All files go to a temp
folder; nothing real is touched and nothing rings.

    python -m call.test_memory_flow
"""
import asyncio
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from call import push, schedule, server
from call import settings as settings_module
from shared import llm, memory

failed = 0


def check(label, ok, detail=""):
    global failed
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"\n          {detail}" if not ok and detail else ""))
    failed += not ok


class FakeWS:
    async def send_json(self, m):
        pass

    async def send_bytes(self, b):
        pass


async def drain():
    """Waits for the server's background memory work to finish."""
    for _ in range(120):
        pending = [t for t in server._background_tasks if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.5)


async def main():
    tmp = Path(tempfile.mkdtemp())
    memory.MEMORY_PATH = tmp / "memory.json"
    settings_module.SETTINGS_PATH = tmp / "settings.json"
    schedule.SCHEDULE_PATH = tmp / "schedule.json"
    server.LOG_DIR = tmp / "logs"
    server.AVATAR_PATH = tmp / "avatar.img"
    push.send_ring = lambda *a: True
    settings_module.save({**settings_module.DEFAULTS, "calling_enabled": False, "fillers_enabled": False})

    # Shrunk so a short call triggers everything.
    memory.SHORT_TERM_TURNS = 2
    server.NOTE_REFRESH_EXCHANGES = 1
    server.CHECKPOINT_EVERY = 3

    server.aclient = llm.async_client()
    server.sclient = llm.sync_client()
    server.state["mem"] = memory.load()
    ws = FakeWS()
    s = settings_module.load()

    await server._start_call(ws)
    lines = [
        "Hey! Guess what, I got placed at Infosys two weeks ago.",
        "We should celebrate. Let's go on a date this Saturday at 7 in the evening.",
        "Also, my exam is on Friday, so wish me luck.",
        "I'm kind of tired right now though.",
        "You know I still love those dry noodles you always tease me about.",
        "Okay, I have to go now. Talk later!",
    ]
    for text in lines:
        await server._respond(ws, text, s, 0)
        await asyncio.sleep(1)
    note = server.state["call_note"]
    print(f"   running note: {note!r}")
    check("the running note covers the early part of the call", len(note) > 20 and "infosys" in note.lower(), note)
    await drain()
    mid = memory.load()
    check("a checkpoint wrote memory before the call ended", len(mid["facts"]) + len(mid["plans"]) > 0)

    await server._end_call()
    await drain()
    mem = memory.load()
    now = memory.now_local()
    saturday = now.date() + timedelta(days=(5 - now.weekday()) % 7 or 7)
    friday = now.date() + timedelta(days=(4 - now.weekday()) % 7 or 7)
    print(memory.render_context(mem))
    check("the call became an episode", len(mem["episodes"]) == 1, str(mem["episodes"]))
    summary = mem["episodes"][0]["summary"].lower() if mem["episodes"] else ""
    check("...covering the WHOLE call, not just its last lines",
          "infosys" in summary or "placed" in summary, summary)
    check(f"the date is a plan on {saturday} at 19:00",
          any(p["when"].startswith(f"{saturday.isoformat()}T19") for p in mem["plans"]), str(mem["plans"]))
    check(f"the exam is a plan on {friday}",
          any(p["when"].startswith(friday.isoformat()) and "exam" in p["text"].lower() for p in mem["plans"]), str(mem["plans"]))
    check("the placement is a fact with a real date",
          any("infosys" in f["text"].lower() and "two weeks" not in f["text"].lower() for f in mem["facts"]), str(mem["facts"]))
    check("'tired right now' is not a fact", not any("tired" in f["text"].lower() for f in mem["facts"]))
    check("nothing stuck in the retry queue", mem["pending"] == [])
    check("last seen recorded", mem["last_seen"] is not None)

    print("\nALL PASSED" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

"""
Checks the calling rules without a phone: scheduled calls, the
scheduled-beats-periodic overlap rule, quiet windows, ring timeouts with
missed calls, and the incoming-screen actions. Push notifications are
faked and every file is redirected to a temp folder, so this never rings
your phone or touches your real schedule/settings/memory. The scheduled
greeting and in-call reminder make real LLM + TTS calls.

From the phase1 folder:
    python -m call.test_scheduling
"""
import asyncio
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from call import push, schedule, server
from call import settings as settings_module
from shared import llm, memory

pushes: list[dict] = []


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, m):
        self.sent.append(m)

    async def send_bytes(self, b):
        self.sent.append({"type": "AUDIO", "bytes": len(b)})


def check(label: str, ok: bool) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        check.failed += 1


check.failed = 0


def at(minutes_from_now: float) -> str:
    return (datetime.now() + timedelta(minutes=minutes_from_now)).strftime(schedule.FORMAT)


def reset(**settings_overrides) -> None:
    schedule.SCHEDULE_PATH.unlink(missing_ok=True)
    settings_module.save({**settings_module.DEFAULTS, **settings_overrides})
    server.state.update(ring=None, in_call=False, consecutive_proactive=0, snooze_until=0.0)
    pushes.clear()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    schedule.SCHEDULE_PATH = tmp / "schedule.json"
    settings_module.SETTINGS_PATH = tmp / "settings.json"
    memory.MEMORY_PATH = tmp / "memory.json"
    server.LOG_DIR = tmp / "logs"
    server.AVATAR_PATH = tmp / "avatar.img"
    push.send_ring = lambda *a: pushes.append({"type": "ring", "args": a}) or True
    push.send_cancel = lambda: pushes.append({"type": "cancel"}) or True
    push.send_missed = lambda *a: pushes.append({"type": "missed"}) or True
    server.RING_SECONDS = 1
    server.aclient = llm.async_client()
    server.state["mem"] = memory.load()
    now_minute = datetime.now().hour * 60 + datetime.now().minute
    all_day_quiet = [{"label": "All day", "start": "00:00", "end": "23:59"}]

    print("1) periodic call, nothing scheduled")
    reset(checkin_minutes=1, quiet_windows=[])
    await server._ring_tick()
    check("she rings", server.state["ring"] is not None and pushes[0]["type"] == "ring")
    server.state["ring"]["timeout"].cancel()

    print("2) quiet window blocks periodic calls...")
    reset(checkin_minutes=1, quiet_windows=all_day_quiet)
    await server._ring_tick()
    check("no ring inside a quiet window", server.state["ring"] is None and not pushes)
    print("   ...but not scheduled ones")
    entry = schedule.add(at(0), "reminder for medicine")
    await server._ring_tick()
    ring = server.state["ring"]
    check("scheduled call rings anyway", ring is not None and ring["schedule_id"] == entry["id"])
    check("ring carries the note", pushes and pushes[-1]["args"][2] == "reminder for medicine")

    print("3) answering a scheduled call opens with its reason")
    ws = FakeWS()
    await server._start_call(ws)
    lines = [m["text"] for m in ws.sent if m.get("value") == "speaking"]
    print(f"   she said: {lines[0] if lines else None!r}")
    # Any natural phrasing counts -- "meds", "pills" -- not just "medicine".
    check("greeting mentions the medicine", bool(lines) and any(
        w in lines[0].lower() for w in ("medic", "meds", "pill", "tablet")))
    check("entry marked done", schedule.load()[0]["status"] == "done")
    for key in ("silence_watcher_task", "duration_timer_task"):
        server.state[key].cancel()
    server.state.update(in_call=False, call_ws=None, history=[])

    print("4) scheduled call soon -> periodic call skipped (scheduled wins)")
    reset(checkin_minutes=1, quiet_windows=[])
    schedule.add(at(20), "movie night")
    await server._ring_tick()
    check("no periodic ring 20 min before a scheduled one", server.state["ring"] is None)

    print("5) unanswered ring -> missed call, retried later")
    reset(checkin_minutes=1, quiet_windows=all_day_quiet)
    entry = schedule.add(at(0), "call mom")
    await server._ring_tick()
    await asyncio.sleep(1.3)
    kinds = [p["type"] for p in pushes]
    check("phone told to stop ringing + missed call", kinds == ["ring", "cancel", "missed"])
    e = schedule.load()[0]
    check("still pending, retry set 5 min later", e["status"] == "pending" and e["attempts"] == 1
          and schedule.parse_at(e["next_try"]) > datetime.now() + timedelta(minutes=4))

    print("6) 'Remind me' from the incoming screen")
    reset(checkin_minutes=1, quiet_windows=[])
    await server._ring_tick()
    server._end_ring("remind", 10)
    entries = schedule.load()
    check("a call-back is scheduled ~10 min out", len(entries) == 1
          and 9 <= (schedule.parse_at(entries[0]["at"]) - datetime.now()).total_seconds() / 60 <= 10)

    print("7) 'Message: can't talk' snoozes check-in calls")
    reset(checkin_minutes=1, quiet_windows=[])
    await server._ring_tick()
    server._end_ring("busy")
    await server._ring_tick()
    check("no ring while snoozed", server.state["ring"] is None
          and server.state["snooze_until"] > time.monotonic() + 50 * 60)

    print("8) scheduled call comes due during a call -> she brings it up")
    reset(checkin_minutes=1, quiet_windows=[])
    ws = FakeWS()
    server.state.update(in_call=True, call_ws=ws, history=[
        {"role": "assistant", "content": "Hey! How are you?"},
        {"role": "user", "content": "Good, just watching TV."},
    ])
    schedule.add(at(0), "remind him to drink water")
    await server._ring_tick()
    lines = [m["text"] for m in ws.sent if m.get("value") == "speaking"]
    print(f"   she said: {lines[0] if lines else None!r}")
    check("no ring during a call", not pushes)
    check("she mentions water mid-call", bool(lines) and "water" in lines[0].lower())
    server.state.update(in_call=False, call_ws=None, history=[])

    print("9) only one scheduled call per minute")
    reset(checkin_minutes=1, quiet_windows=[])
    schedule.add(at(60), "wake up")
    try:
        schedule.add(at(60), "wake up again")
        rejected = False
    except ValueError:
        rejected = True
    check("a second call in the same minute is refused", rejected)
    schedule.add(at(70), "are you up?")
    check("a call ten minutes later is fine", len(schedule.load()) == 2)

    print("10) two calls already due at once (like the 5:00 pair) -> one call")
    reset(checkin_minutes=1, quiet_windows=[])
    a = schedule.add(at(60), "wake up early")
    b = schedule.add(at(61), "she's calling to wake me up")
    entries = schedule.load()
    for e in entries:  # both due right now, as if set before the rule existed
        e["at"] = e["next_try"] = at(0)
    schedule.save(entries)
    await server._ring_tick()
    rings = [p for p in pushes if p["type"] == "ring"]
    check("exactly one ring", len(rings) == 1)
    check("it carries both reasons", bool(rings) and "wake up early" in rings[0]["args"][2]
          and "she's calling to wake me up" in rings[0]["args"][2])
    statuses = {e["id"]: e["status"] for e in schedule.load()}
    check("the other one is merged, not left to ring later", sorted(statuses.values()) == ["done", "pending"])
    server.state["ring"]["timeout"].cancel()
    del a, b

    print("\nALL PASSED" if not check.failed else f"\n{check.failed} FAILED")
    del now_minute


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

"""
The phantom-call / dead-line bugs seen on the deployed server, replayed
against a running server (python -m call first, then from phase1):

    python -m call.test_reconnect

1. The call screen is rebuilt mid-call (a new page connects and asks for a
   call while the old connection is still open): the SAME call must carry
   on -- no second greeting, and the new page must actually work.
2. The connection drops outright and comes back within the grace period:
   "resume" picks the call back up.
3. Right after hanging up, a replayed "call her" must be refused, not dial.

Makes real Groq/edge-tts calls for the spoken turn.
"""
import asyncio
import json
import ssl
import sys

import websockets

from call import access
from call.test_call_client import collect_until_listening, speech_webm

URL = f"wss://localhost:8765{access.mount_path()}/ws"


def ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def connect():
    return await websockets.connect(URL, ssl=ssl_ctx(), max_size=None)


async def next_control(ws, timeout=15, want: str | None = None) -> dict:
    """Next control message -- or, with want=, the next one of that type
    (status updates like "listening" can arrive in between)."""
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(msg, str):
            data = json.loads(msg)
            if want is None or data.get("type") == want:
                return data


def check(label: str, ok: bool) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        check.failed += 1


check.failed = 0


async def main() -> None:
    utterance = await speech_webm("Still there? Tell me something short.")

    print("1) call screen rebuilt mid-call")
    ws1 = await connect()
    await ws1.send(json.dumps({"type": "call_me_now"}))
    controls, clips, _ = await collect_until_listening(ws1)
    check("first page gets a call with a greeting", clips >= 1)

    ws2 = await connect()  # the rebuilt page -- ws1 is still open
    await ws2.send(json.dumps({"type": "call_me_now"}))
    started = await next_control(ws2)
    check("rebuilt page RESUMES the call", started.get("type") == "call_started" and started.get("resumed") is True)
    listening = await next_control(ws2)
    check("...without a second greeting", listening.get("type") == "state" and listening.get("value") == "listening")
    try:
        await asyncio.wait_for(ws1.recv(), timeout=3)
        old_closed = False
    except websockets.ConnectionClosed:
        old_closed = True
    except asyncio.TimeoutError:
        old_closed = False
    check("old page's connection is closed", old_closed)

    await ws2.send(utterance)
    controls, clips, _ = await collect_until_listening(ws2, timeout=30)
    check("she answers on the rebuilt page", clips >= 1)

    print("2) connection drops, comes back within the grace period")
    await ws2.close()
    await asyncio.sleep(2)
    ws3 = await connect()
    await ws3.send(json.dumps({"type": "resume"}))
    started = await next_control(ws3)
    check("resume picks the call back up", started.get("type") == "call_started" and started.get("resumed") is True)

    print("3) replayed 'call her' right after hanging up")
    await ws3.send(json.dumps({"type": "hangup"}))
    ended = await next_control(ws3, want="call_ended")
    check("hang-up ends the call", ended.get("type") == "call_ended")
    await ws3.close()
    ws4 = await connect()
    await ws4.send(json.dumps({"type": "call_me_now"}))
    reply = await next_control(ws4)
    check("replayed request is refused, not dialed",
          reply.get("type") == "call_ended" and reply.get("replay") is True)
    await ws4.close()

    print("\nALL PASSED" if not check.failed else f"\n{check.failed} FAILED")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

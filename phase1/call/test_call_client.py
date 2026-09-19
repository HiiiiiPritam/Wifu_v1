"""
End-to-end check of the call server without a phone: plays the part of the
page over the real websocket, with synthesized speech as "your" voice.

Checks: the greeting is instant, a reply streams back, and a sentence cut
off mid-thought gets merged with its continuation instead of answered.

Makes real Groq/edge-tts calls. Start the server first (python -m call),
then from the phase1 folder:
    python -m call.test_call_client
"""
import asyncio
import json
import ssl
import sys
import tempfile
import time
from pathlib import Path

import av
import edge_tts
import websockets

URL = "wss://localhost:8765/ws"
TEST_VOICE = "en-US-GuyNeural"  # a different voice from hers, playing "you"


async def speech_webm(text: str) -> bytes:
    """Synthesize text and re-encode it as webm/opus, like the page records."""
    with tempfile.TemporaryDirectory() as tmp:
        mp3, webm = Path(tmp) / "a.mp3", Path(tmp) / "a.webm"
        await edge_tts.Communicate(text, TEST_VOICE).save(str(mp3))
        src = av.open(str(mp3))
        dst = av.open(str(webm), mode="w")
        out = dst.add_stream("libopus", rate=48000)
        for frame in src.decode(src.streams.audio[0]):
            for packet in out.encode(frame):
                dst.mux(packet)
        for packet in out.encode(None):
            dst.mux(packet)
        dst.close()
        src.close()
        return webm.read_bytes()


async def collect_until_listening(ws, timeout: float = 30) -> tuple[list[dict], int, float | None]:
    """Reads until the server says 'listening'. Returns (control messages,
    audio clip count, seconds until the first audio clip)."""
    t0 = time.monotonic()
    controls, clips, first_audio = [], 0, None
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(msg, bytes):
            clips += 1
            if first_audio is None:
                first_audio = time.monotonic() - t0
            continue
        data = json.loads(msg)
        controls.append(data)
        print("   ", data)
        if data.get("type") == "state" and data.get("value") == "listening":
            return controls, clips, first_audio


async def main() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the server's cert is self-signed

    print("preparing test speech...")
    whole = await speech_webm("Hey, I just got back from the gym. What have you been up to today?")
    part1 = await speech_webm("So yesterday I went to the market and")
    part2 = await speech_webm("I ended up buying a really cheap guitar.")

    async with websockets.connect(URL, ssl=ctx, max_size=None) as ws:
        print("\n1) calling her")
        await ws.send(json.dumps({"type": "call_me_now"}))
        controls, clips, first = await collect_until_listening(ws)
        assert any(c.get("type") == "call_started" for c in controls), "no call_started"
        assert clips >= 1, "no greeting audio"
        print(f"   greeting audio after {first:.2f}s")

        print("\n2) a normal turn")
        t0 = time.monotonic()
        await ws.send(whole)
        controls, clips, first = await collect_until_listening(ws)
        said = [c["text"] for c in controls if c.get("type") == "you_said"]
        assert said, "never transcribed"
        assert clips >= 1, "no reply audio"
        print(f"   heard: {said[-1]!r}")
        print(f"   {clips} audio clip(s); first after {first:.2f}s (total {time.monotonic() - t0:.2f}s)")

        print("\n2b) her own voice leaking back into the mic")
        her_reply = " ".join(
            c["text"] for c in controls if c.get("type") in ("state", "her_more") and c.get("text")
        )
        await ws.send(json.dumps({"type": "activity"}))  # must be accepted silently
        await ws.send(await speech_webm(her_reply))
        controls, clips, _ = await collect_until_listening(ws)
        assert not any(c.get("type") == "you_said" for c in controls), "her echo was taken as you"
        assert clips == 0, "she replied to her own echo"
        print("   ignored, as it should be")

        print("\n3) pausing mid-sentence, then carrying on")
        await ws.send(part1)
        await asyncio.sleep(0.4)  # a short pause, like a breath
        await ws.send(part2)
        controls, clips, _ = await collect_until_listening(ws)
        merged = [c for c in controls if c.get("type") == "you_said" and c.get("merge")]
        speaking = [c for c in controls if c.get("type") == "state" and c.get("value") == "speaking"]
        assert merged, "the two parts were NOT merged"
        assert len(speaking) == 1, f"she answered {len(speaking)} times, expected once"
        print(f"   merged: {merged[-1]['text']!r}")

        print("\n4) hanging up")
        await ws.send(json.dumps({"type": "hangup"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            if isinstance(msg, str) and json.loads(msg).get("type") == "call_ended":
                break

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

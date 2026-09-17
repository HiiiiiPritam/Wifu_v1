"""
Connection + control helpers for VTube Studio (the Live2D avatar renderer).

VTube Studio must already be running with its API enabled
(Settings -> plug icon -> "Start API") before any of this works.
On first connect, VTube Studio will show an on-screen "Allow plugin?"
popup — you must click Allow there once; after that a token is cached
to vts_token.txt and it won't ask again.

We drive mouth movement ourselves (from the TTS audio's volume envelope)
rather than relying on VTube Studio's built-in mic-based lip sync — that
would require routing our speaker output back into a virtual microphone.
Driving the "MouthOpen" parameter directly is simpler and needs no extra
software.
"""
import time
from pathlib import Path

import numpy as np
import pyvts
import sounddevice as sd

TOKEN_PATH = str(Path(__file__).resolve().parent / "vts_token.txt")

PLUGIN_INFO = {
    "plugin_name": "AriaCompanion",
    "developer": "you",
    "authentication_token_path": TOKEN_PATH,
}


async def connect() -> pyvts.vts:
    """Connect to and authenticate with VTube Studio. Call once at startup."""
    vts = pyvts.vts(plugin_info=PLUGIN_INFO)
    await vts.connect()
    await vts.request_authenticate_token()  # first run: triggers the Allow popup
    await vts.request_authenticate()
    return vts


async def set_mouth_open(vts: pyvts.vts, value: float) -> None:
    value = max(0.0, min(1.0, value))
    await vts.request(
        vts.vts_request.requestSetParameterValue(parameter="MouthOpen", value=value)
    )


async def list_hotkeys(vts: pyvts.vts) -> list[dict]:
    """Returns the hotkeys configured on the currently loaded model in VTube
    Studio (Settings -> Hotkeys). Kept around for test_vts_connection.py's
    diagnostic output only -- expressions are no longer driven through
    hotkeys (see set_expression below)."""
    response = await vts.request(vts.vts_request.requestHotKeyList())
    return response.get("data", {}).get("availableHotkeys", [])


async def set_expression(vts: pyvts.vts, expression_file: str, active: bool) -> None:
    """Explicitly activates/deactivates one expression (.exp3.json) by file
    name. Deliberately NOT using VTube Studio's hotkey system for this --
    hotkeys of type "Set/Unset Expression" actually TOGGLE, and toggling
    across a multi-turn conversation desyncs badly (an expression can get
    stuck active with nothing left to turn it off). This call is a real
    set, so it's safe to call every turn regardless of prior state."""
    await vts.request(
        vts.vts_request.BaseRequest(
            "ExpressionActivationRequest",
            data={"expressionFile": expression_file, "active": active},
        )
    )


def _rms_envelope(audio: np.ndarray, sr: int, hop_ms: int = 50) -> np.ndarray:
    hop = max(1, int(sr * hop_ms / 1000))
    n_frames = max(1, len(audio) // hop)
    levels = np.zeros(n_frames)
    for i in range(n_frames):
        chunk = audio[i * hop : (i + 1) * hop]
        levels[i] = np.sqrt(np.mean(chunk.astype(np.float64) ** 2)) if len(chunk) else 0.0
    peak = levels.max() or 1.0
    return np.clip(levels / peak, 0.0, 1.0)


async def speak_with_lipsync(
    vts: pyvts.vts, audio: np.ndarray, samplerate: int, hop_ms: int = 50
) -> None:
    """Plays audio through speakers while moving MouthOpen in time with its
    volume envelope. Not phoneme-accurate, but reads as talking, not static."""
    import asyncio

    envelope = _rms_envelope(audio, samplerate, hop_ms)
    sd.play(audio, samplerate=samplerate)
    start = time.monotonic()
    for i, level in enumerate(envelope):
        target = start + i * hop_ms / 1000
        now = time.monotonic()
        if target > now:
            await asyncio.sleep(target - now)
        await set_mouth_open(vts, float(level))
    sd.wait()
    await set_mouth_open(vts, 0.0)

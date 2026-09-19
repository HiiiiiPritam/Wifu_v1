"""
Run this AFTER VTube Studio is open with the API enabled
(Settings -> plug icon -> "Start API").

First run: VTube Studio will pop up an "Allow plugin?" prompt on screen
-- click Allow. This proves the connection works before we wire it into
the full voice loop, and lists your model's hotkeys so we know what
expression names are available to trigger later.
"""
import asyncio
import sys

from desktop.vts_face import connect, list_hotkeys, set_mouth_open


async def main() -> None:
    print("Connecting to VTube Studio...")
    print("(If a popup appears in VTube Studio asking to allow a plugin, click Allow)")
    try:
        vts = await connect()
    except OSError:
        sys.exit(
            "Could not reach VTube Studio on ws://localhost:8001\n"
            "Make sure VTube Studio is open AND its API is enabled:\n"
            "  Settings -> plug icon (left icon rail) -> 'Start API'"
        )
    print("Connected and authenticated!")

    hotkeys = await list_hotkeys(vts)
    if hotkeys:
        print("\nHotkeys configured on your current model:")
        for hk in hotkeys:
            print(f"  - {hk.get('name')!r}  (id: {hk.get('hotkeyID')})")
    else:
        print("\nNo hotkeys configured on this model yet (that's fine for now).")

    print("\nFlapping the mouth open/closed a few times, watch the model...")
    for _ in range(4):
        await set_mouth_open(vts, 1.0)
        await asyncio.sleep(0.3)
        await set_mouth_open(vts, 0.0)
        await asyncio.sleep(0.3)

    await vts.close()
    print("\nDone. If the mouth moved, the connection works end to end.")


if __name__ == "__main__":
    asyncio.run(main())

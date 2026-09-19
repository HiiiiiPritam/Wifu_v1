"""
Sends the "ring" push notification to the Android app via Firebase Cloud
Messaging -- this is what wakes the phone even when it's locked or the
app is closed, which the WebSocket-only ring (still used when the page
happens to be open) can't do on its own.

Needs a Firebase service account key (Firebase Console -> Project
Settings -> Service accounts -> Generate new private key) referenced by
FIREBASE_SERVICE_ACCOUNT_JSON in .env. Everything here is a no-op (prints
a note once, then quietly skips) if that's not configured yet -- the
WebSocket ring still works fine without it, this is additive.
"""
import json
import os
from pathlib import Path

from shared import ROOT

DEVICE_TOKEN_PATH = ROOT / "device_token.json"

_firebase_app = None
_warned_not_configured = False


def _get_firebase_app():
    global _firebase_app, _warned_not_configured
    if _firebase_app is not None:
        return _firebase_app

    key_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    # A relative path in .env means relative to phase1/, not to wherever
    # the server happened to be started from.
    if key_path and not Path(key_path).is_absolute():
        key_path = str(ROOT / key_path)
    if not key_path or not Path(key_path).exists():
        if not _warned_not_configured:
            print(
                "(FIREBASE_SERVICE_ACCOUNT_JSON not set/found -- phone won't ring when "
                "locked/closed yet. WebSocket ring still works while the page is open.)"
            )
            _warned_not_configured = True
        return None

    import firebase_admin
    from firebase_admin import credentials

    cred = credentials.Certificate(key_path)
    _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


def save_device_token(token: str) -> None:
    DEVICE_TOKEN_PATH.write_text(json.dumps({"token": token}), encoding="utf-8")


def load_device_token() -> str | None:
    if not DEVICE_TOKEN_PATH.exists():
        return None
    try:
        return json.loads(DEVICE_TOKEN_PATH.read_text(encoding="utf-8")).get("token")
    except Exception:
        return None


def _send(data: dict[str, str], ttl: int = 60) -> bool:
    """Returns True if a push was actually sent (app registered + Firebase
    configured), False otherwise -- caller decides whether that matters.
    FCM data values must all be strings."""
    app = _get_firebase_app()
    token = load_device_token()
    if app is None or token is None:
        return False

    from firebase_admin import messaging

    message = messaging.Message(
        data={k: str(v) for k, v in data.items()},
        token=token,
        android=messaging.AndroidConfig(
            priority="high",
            # Without a TTL, FCM queues an undeliverable push (phone off/no
            # signal/doze) and can deliver it hours later once reconnected --
            # a "call" notification arriving stale like that is confusing
            # and, since the server may be down by then anyway, useless.
            ttl=ttl,
        ),
    )
    try:
        messaging.send(message)
        return True
    except Exception as e:
        print(f"(FCM push failed: {e})")
        return False


def send_ring(name: str, number: str, reason: str = "", call_id: str = "") -> bool:
    """Everything the phone's incoming-call screen shows comes in the push
    itself, so it renders correctly even with the app closed and no
    connection to the server yet. number is "" when hidden in settings."""
    return _send(
        {"type": "ring", "name": name, "number": number, "reason": reason, "call_id": call_id}
    )


def send_cancel() -> bool:
    """Stops the phone ringing (she "hung up" -- the ring timed out)."""
    return _send({"type": "cancel"})


def send_missed(name: str, number: str) -> bool:
    """A "Missed call from ..." notification. Longer TTL: unlike a ring,
    it's still worth showing if it arrives a while late."""
    return _send({"type": "missed", "name": name, "number": number}, ttl=3600)

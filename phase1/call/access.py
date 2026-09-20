"""
The server's secret path.

Everything is served under /s/<token>/ instead of /, so a server on a
public address isn't wide open: without the token you get a 404, the same
as any wrong URL. That matters because ANYONE who can reach this server
can otherwise call you, read her memory of you and change your settings.

A path secret (rather than a header or a password form) is deliberate: the
phone app, the call page, the native call screens and the FCM registration
all just append paths to one stored server URL, so putting the secret in
that URL secures every one of them without touching the app.

It travels inside the encrypted part of an HTTPS request, so it isn't
visible on the network -- but it does end up in server logs and browser
history, so treat it like a password: don't paste the URL anywhere public.
Delete phase1/.call_token to roll a new one (then re-paste the new URL in
the app).
"""
import os
import secrets

from shared import ROOT

TOKEN_PATH = ROOT / ".call_token"


def token() -> str:
    """From CALL_TOKEN if set (handy on a server), else phase1/.call_token,
    created on first run."""
    from_env = os.getenv("CALL_TOKEN", "").strip()
    if from_env:
        return from_env
    if TOKEN_PATH.exists():
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    fresh = secrets.token_urlsafe(18)
    TOKEN_PATH.write_text(fresh, encoding="utf-8")
    return fresh


def mount_path() -> str:
    return f"/s/{token()}"

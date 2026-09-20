"""
python -m call   (run from the phase1 folder)

Serves the call server under a secret path (see access.py), so it's safe
to expose to the internet.

Environment:
  CALL_TOKEN   the secret path segment (default: phase1/.call_token)
  CALL_TLS     "0" when something else terminates HTTPS in front of this
               (Caddy/nginx on a server). Default: on, with the
               self-signed certificate used on a home network.
  PUBLIC_URL   how the phone reaches this server, for the startup banner
               (e.g. https://yourname.duckdns.org)
  PORT         default 8765
"""
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from call import access, certs
from call.server import PORT, app, local_lan_ip

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

port = int(os.getenv("PORT", PORT))
use_tls = os.getenv("CALL_TLS", "1") != "0"
mount = access.mount_path()

@asynccontextmanager
async def lifespan(_):
    """Runs the mounted app's own startup (Groq clients, memory, the ring
    loop). A mounted sub-app does NOT get its startup run automatically --
    without this the server came up but she had no way to think: her
    greeting played from the audio cache and then every reply failed."""
    async with app.router.lifespan_context(app):
        yield


# The real server lives under the secret path; anything else 404s.
outer = FastAPI(lifespan=lifespan)


@outer.get("/healthz")
async def healthz() -> JSONResponse:
    """Unauthenticated liveness check -- says nothing about you."""
    return JSONResponse({"status": "ok"})


outer.mount(mount, app)

scheme = "https" if use_tls else "http"
public = os.getenv("PUBLIC_URL", "").rstrip("/")
print("\n" + "=" * 68)
print("Paste THIS into the app (Settings -> Connection -> Server address):")
if public:
    print(f"    {public}{mount}")
else:
    print(f"    {scheme}://<this machine's address>:{port}{mount}")
    print(f"    on this PC: {scheme}://localhost:{port}{mount}")
print("Treat it like a password -- it's the only thing protecting the server.")
print("=" * 68)

if use_tls:
    keyfile, certfile = certs.ensure_certificate(local_lan_ip())
    uvicorn.run(outer, host="0.0.0.0", port=port, ssl_keyfile=keyfile, ssl_certfile=certfile)
else:
    # Behind a reverse proxy that handles HTTPS. proxy_headers makes
    # uvicorn trust its X-Forwarded-* so redirects and logs stay correct.
    uvicorn.run(outer, host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="*")

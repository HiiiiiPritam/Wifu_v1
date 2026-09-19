"""python -m call  (run from the phase1 folder)"""
import sys

import uvicorn

from call import certs
from call.server import PORT, app, local_lan_ip

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

keyfile, certfile = certs.ensure_certificate(local_lan_ip())
uvicorn.run(app, host="0.0.0.0", port=PORT, ssl_keyfile=keyfile, ssl_certfile=certfile)

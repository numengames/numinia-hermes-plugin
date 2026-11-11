# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A node-less STAND-IN for the engine entry file, run by the tests with the Python interpreter as the "node".

It serves ``GET /health`` on ``HOST:PORT`` like the real engine, behaves as the file ``mode`` next to it says
(ok | crash | silent), and exits when stdin closes if ``MOTOR_SALIR_SI_STDIN_CIERRA=1``. It records which variables it got in
``seen.json`` (names only for secrets) so a test can check what the child received without ever printing a value.
"""

import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

here = Path(__file__).resolve().parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "ok"
(here / "seen.json").write_text(
    json.dumps(
        {
            "argv": sys.argv,
            "has": sorted(k for k in os.environ if k.startswith("MOTOR_") or k in ("PORT", "HOST", "WWW_DIR")),
            "exit_on_eof": os.environ.get("MOTOR_SALIR_SI_STDIN_CIERRA"),
            "secret_len": len(os.environ.get("MOTOR_SECRET", "")),
            "admin_len": len(os.environ.get("MOTOR_ADMIN", "")),
            "path": os.environ.get("PATH", ""),
            "leaked": [k for k in os.environ if "TOKEN" in k or "API_KEY" in k],
        }
    )
)
world = Path(os.environ.get("MOTOR_MUNDO", ""))
if (here / "seed").exists() and os.environ.get("MOTOR_MUNDO") and not (world / "mundo.json").exists():
    # like the real engine: a world folder with no mundo.json is born now from the template (embed OFF, mirror on)
    (world / "entorno").mkdir(parents=True, exist_ok=True)
    (world / "entorno" / "espejo.json").write_text(
        json.dumps({"schema": "numinia-engine/espejo@1", "activa": True, "fuente": {"tipo": "enlace-local", "puerto": 8791}})
    )
    (world / "mundo.json").write_text(json.dumps({"esquema": "numinia-engine/mundo@2"}))
if (here / "seed").exists():
    import socket

    _link = socket.socket()  # the real engine opens its link port when the mirror is on
    _link.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _link.bind(("127.0.0.1", int(os.environ["MOTOR_ENLACE_PUERTO"])))
    _link.listen(4)


def embed_on():
    try:
        cfg = json.loads((world / "entorno" / "espejo.json").read_text())
    except (OSError, ValueError):
        return mode != "noembed"
    return cfg.get("embed", {}).get("activa") is True


if mode == "crash":
    print("boom: cannot open the world (stub)", file=sys.stderr)
    sys.exit(3)
if os.environ.get("MOTOR_SALIR_SI_STDIN_CIERRA") == "1":

    def watch():
        sys.stdin.buffer.read()  # blocks until the plugin side closes
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
if mode == "silent":
    time.sleep(600)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = {"/health": b'{"ok":true}', "/status": json.dumps({"espejo": {"embed": embed_on()}}).encode()}.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(body or b"")


http.server.ThreadingHTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()

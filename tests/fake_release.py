# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A FAKE GitHub release for tests: a local http server on a random loopback port serving one tiny asset and its SHA256SUMS.

Tests never download from the internet. The asset has the SHAPE of the engine's real one (``instalar-motor.sh`` = a bash header, the
line ``__CARGA_DEBAJO__`` and a base64 ``tar.gz`` of ``app/`` and ``www/``); helpers build hostile variants too.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import tarfile
import threading
import zipfile
from pathlib import Path

LOOP = ".".join(("127", "0", "0", "1"))
ENTRY = "packages/server/src/entrada.ts"
TAG = "motor-test1"
ASSET = "instalar-motor.sh"


def tar_gz(members: list[tuple[str, bytes | None, dict]]) -> bytes:
    """``members``: (name, data or None for a dir, extra TarInfo fields: type/linkname/mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, extra in members:
            info = tarfile.TarInfo(name)
            info.mode = extra.get("mode", 0o755 if data is None else 0o644)
            if "type" in extra:
                info.type = extra["type"]
                info.linkname = extra.get("linkname", "")
                tf.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def good_members(extra: list | None = None) -> list:
    return [
        ("app/", None, {}),
        ("app/package.json", b'{"name":"fake-engine"}\n', {}),
        (f"app/{ENTRY}", b"// the fake engine is engine_stub.py in the tests\n", {}),
        ("app/node_modules/ws/index.js", b"module.exports = {}\n", {}),
        ("app/bin/run.sh", b"#!/bin/sh\n", {"mode": 0o4755}),  # a setuid bit in the archive must not survive
        ("www/", None, {}),
        ("www/index.html", b"<html>fake plaza</html>\n", {}),
        ("actualizar.sh", b"#!/bin/sh\nexit 1\n", {}),  # outside app/ and www/: skipped
        *(extra or []),
    ]


def as_script(payload: bytes) -> bytes:
    b64 = base64.b64encode(payload)
    lines = b"\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
    return b"#!/usr/bin/env bash\n# fake instalar-motor.sh\nexit 1\n__CARGA_DEBAJO__\n" + lines + b"\n"


def good_asset(extra: list | None = None) -> bytes:
    return as_script(tar_gz(good_members(extra)))


def zip_asset(names: dict[str, bytes], symlink: tuple[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in names.items():
            zf.writestr(n, d)
        if symlink:
            zi = zipfile.ZipInfo(symlink[0])
            zi.external_attr = (0o120777) << 16
            zf.writestr(zi, symlink[1])
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeRelease:
    """Serves ``/<tag>/<asset>`` and ``/<tag>/SHA256SUMS``. ``truncate_at`` sends a Content-Length of the whole body then stops early."""

    def __init__(
        self,
        asset: bytes,
        tag: str = TAG,
        name: str = ASSET,
        truncate_at: int | None = None,
        sums: bytes | None = None,
        sums_status: int = 200,
    ) -> None:
        self.sums, self.sums_status = sums, sums_status
        self.asset, self.tag, self.name, self.truncate_at = asset, tag, name, truncate_at
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                if self.path == f"/{outer.tag}/{outer.name}":
                    body = outer.asset
                elif self.path == f"/{outer.tag}/SHA256SUMS":
                    if outer.sums_status != 200:
                        self.send_response(outer.sums_status)
                        self.end_headers()
                        return
                    body = outer.sums if outer.sums is not None else f"{sha(outer.asset)}  {outer.name}\n".encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut = outer.truncate_at
                self.wfile.write(body if cut is None else body[:cut])
                self.wfile.flush()
                if cut is not None:
                    self.close_connection = True

        self.httpd = http.server.ThreadingHTTPServer((LOOP, 0), Handler)
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self) -> FakeRelease:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://{LOOP}:{self.httpd.server_address[1]}/{self.tag}/{self.name}"

    @property
    def sha256(self) -> str:
        return sha(self.asset)

    def lock(self, directory: Path, **over) -> Path:
        data = {
            "source": "https://github.com/numengames/numinia-engine",
            "tag": self.tag,
            "asset": self.name,
            "url": f"http://{LOOP}:{self.httpd.server_address[1]}/{{tag}}/{{asset}}",
            "sha256": self.sha256,
            "size_bytes": len(self.asset),
            "entry": ENTRY,
            "node": ">=22.18",
            **over,
        }
        path = directory / "engine.lock.json"
        path.write_text(json.dumps(data))
        return path

# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The loopback face as an attack surface: rebinding, browsers, slowloris, request smuggling, size caps, logs.

Every test speaks raw bytes on a socket so that nothing a polite HTTP client would normalise can hide a hole.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import stat

import pytest
from conftest import LOOP, run, until

from hermes_3d_lib import hub as hub_mod
from hermes_3d_lib import logsafe
from hermes_3d_lib.hub import Hub

TOKEN = "tok-0123456789abcdef"


def make_hub(**kw) -> Hub:
    return Hub(token=TOKEN, engine_url=None, link_key=None, switches={"salud": True}, **kw)


async def raw(port: int, payload: bytes, *, read_timeout: float = 3.0) -> tuple[int, bytes]:
    """Send ``payload`` verbatim, return (status, body); status 0 when the server closed without answering."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    try:
        data = await asyncio.wait_for(reader.read(), read_timeout)
    except (TimeoutError, ConnectionError):
        data = b""
    writer.close()
    if not data.startswith(b"HTTP/1.1 "):
        return 0, data
    return int(data.split(b" ", 2)[1]), data.split(b"\r\n\r\n", 1)[-1]


def request(*, method="GET", path="/status", host="auto", port=0, headers=(), body=b"", auth=True, length=True) -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    if host == "auto":
        host = f"127.0.0.1:{port}"
    if host is not None:
        lines.append(f"Host: {host}")
    if auth:
        lines.append(f"Authorization: Bearer {TOKEN}")
    if length:
        lines.append(f"Content-Length: {len(body)}")
    lines += list(headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


async def with_hub(fn, **kw):
    h = make_hub(**kw)
    port = await h.start()
    try:
        return await fn(h, port)
    finally:
        await h.stop()


def test_baseline_valid_request_works():
    async def main(h, port):
        assert (await raw(port, request(port=port)))[0] == 200
        assert (await raw(port, request(host="localhost", port=port)))[0] == 200
        assert (await raw(port, request(host="127.0.0.1")))[0] == 200  # a client that omits the port

    run(with_hub(main))


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "evil.example.com:80",
        "127.0.0.1.evil.com",
        "localhost.evil.com",
        f"{LOOP}:1",
        "0.0.0.0",
        "[::1]x",
        "127.0.0.1@evil.com",
        "",
    ],
)
def test_host_header_must_be_loopback_defeating_dns_rebinding(host):
    async def main(h, port):
        status, _ = await raw(port, request(host=host))
        assert status == 421, host
        assert h.rejected_local >= 1

    run(with_hub(main))


def test_missing_or_duplicate_host_is_refused():
    async def main(h, port):
        assert (await raw(port, request(host=None)))[0] == 400
        assert (await raw(port, request(port=port, headers=["Host: evil.example"])))[0] == 400

    run(with_hub(main))


@pytest.mark.parametrize("value", ["cross-site", "same-site", "same-origin", "bogus"])
def test_browser_initiated_requests_are_refused_by_fetch_metadata(value):
    async def main(h, port):
        assert (await raw(port, request(port=port, headers=[f"Sec-Fetch-Site: {value}"])))[0] == 403

    run(with_hub(main))


def test_sec_fetch_site_none_is_a_user_typed_navigation_and_is_not_a_page():
    async def main(h, port):
        assert (await raw(port, request(port=port, headers=["Sec-Fetch-Site: none"])))[0] == 200

    run(with_hub(main))


@pytest.mark.parametrize("origin", ["http://evil.example", "null", f"http://{LOOP}:1", "http://localhost"])
def test_any_origin_is_refused_there_is_no_allowed_origin(origin):
    async def main(h, port):
        assert (await raw(port, request(port=port, headers=[f"Origin: {origin}"])))[0] == 403

    run(with_hub(main))


def test_post_requires_json_content_type():
    async def main(h, port):
        body = json.dumps({"kind": "x"}).encode()
        assert (await raw(port, request(method="POST", path="/event", port=port, body=body)))[0] == 415
        ok = request(method="POST", path="/event", port=port, body=body, headers=["Content-Type: application/json"])
        assert (await raw(port, ok))[0] == 200

    run(with_hub(main))


def test_duplicate_content_length_is_refused_not_guessed():
    async def main(h, port):
        req = request(
            method="POST",
            path="/event",
            port=port,
            length=False,
            headers=["Content-Type: application/json", "Content-Length: 2", "Content-Length: 50"],
            body=b"{}",
        )
        status, _ = await raw(port, req, read_timeout=2)
        assert status == 400

    run(with_hub(main))


@pytest.mark.parametrize("value", ["-1", "+5", "1_0", "0x10", "1.5", " ", "5, 5", "99999999999999999999"])
def test_malformed_content_length_is_refused(value):
    async def main(h, port):
        req = request(
            method="POST",
            path="/event",
            port=port,
            length=False,
            headers=["Content-Type: application/json", f"Content-Length: {value}"],
            body=b"{}",
        )
        assert (await raw(port, req, read_timeout=2))[0] in (400, 413)

    run(with_hub(main))


def test_transfer_encoding_is_refused_so_content_length_and_chunked_cannot_be_smuggled():
    async def main(h, port):
        smuggled = (
            b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"0\r\n\r\nGET /stop HTTP/1.1\r\n\r\n"
        )
        assert (await raw(port, smuggled, read_timeout=2))[0] == 400
        assert not h._stop.is_set()
        only_te = request(
            method="POST",
            path="/event",
            port=port,
            length=False,
            headers=["Content-Type: application/json", "Transfer-Encoding: chunked"],
            body=b"2\r\n{}\r\n0\r\n\r\n",
        )
        assert (await raw(port, only_te, read_timeout=2))[0] == 400

    run(with_hub(main))


def test_oversized_declared_body_is_refused_without_reading_it():
    async def main(h, port):
        req = request(
            method="POST",
            path="/event",
            port=port,
            length=False,
            headers=["Content-Type: application/json", f"Content-Length: {hub_mod.MAX_BODY + 1}"],
        )
        assert (await raw(port, req, read_timeout=2))[0] == 413

    run(with_hub(main))


def test_header_block_size_and_count_are_capped():
    async def main(h, port):
        assert (await raw(port, request(port=port, headers=["X-Pad: " + "a" * 20000])))[0] == 431
        many = [f"X-H{i}: v" for i in range(hub_mod.MAX_HEADERS + 5)]
        assert (await raw(port, request(port=port, headers=many)))[0] == 431
        assert (await raw(port, request(port=port, path="/" + "a" * 3000)))[0] in (414, 431)

    run(with_hub(main))


def test_only_get_and_post_and_known_http_version():
    async def main(h, port):
        assert (await raw(port, request(method="DELETE", port=port)))[0] == 405
        assert (await raw(port, b"GET /status HTTP/0.9\r\nHost: 127.0.0.1\r\n\r\n"))[0] == 400
        assert (await raw(port, b"garbage\r\n\r\n"))[0] == 400

    run(with_hub(main))


def test_connection_cap_answers_503_and_recovers(monkeypatch):
    monkeypatch.setattr(hub_mod, "MAX_CONNECTIONS", 4)

    async def main(h, port):
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(4)]
        await until(lambda: h.open_connections == 4)
        assert (await raw(port, request(port=port)))[0] == 503
        for _r, w in idle:
            w.close()
        await until(lambda: h.open_connections == 0)
        assert (await raw(port, request(port=port)))[0] == 200

    run(with_hub(main))


def test_idle_connection_is_dropped_after_the_read_deadline(monkeypatch):
    monkeypatch.setattr(hub_mod, "READ_DEADLINE", 0.3)

    async def main(h, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        data = await asyncio.wait_for(reader.read(), 2)  # we send nothing at all
        assert data == b"" or data.startswith(b"HTTP/1.1 408")
        await until(lambda: h.open_connections == 0)
        writer.close()

    run(with_hub(main))


def test_slowloris_drip_feed_cannot_outlive_the_overall_deadline(monkeypatch):
    monkeypatch.setattr(hub_mod, "READ_DEADLINE", 0.5)

    async def main(h, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        t0 = asyncio.get_running_loop().time()
        closed = False
        for ch in b"GET /status HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: " + b"a" * 200:
            try:
                writer.write(bytes([ch]))
                await writer.drain()
            except ConnectionError:
                closed = True
                break
            await asyncio.sleep(0.05)  # every gap is shorter than any per-read timeout
            if reader.at_eof():
                closed = True
                break
        assert closed or asyncio.get_running_loop().time() - t0 < 2.0
        assert closed, "a drip-fed request must be cut off at the overall deadline"
        writer.close()

    run(with_hub(main))


def test_token_is_compared_in_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hub_mod.hmac, "compare_digest", lambda a, b: calls.append((type(a), type(b))) or real(a, b))

    async def main(h, port):
        assert (await raw(port, request(port=port, auth=False, headers=["Authorization: Bearer nope"])))[0] == 401
        assert (await raw(port, request(port=port)))[0] == 200

    run(with_hub(main))
    assert len(calls) >= 2


def test_unauthorized_never_runs_a_route():
    hits = []

    async def route(method, path, raw_body):
        hits.append(path)
        return 200, {}

    async def main(h, port):
        assert (await raw(port, request(port=port, path="/action/pending", auth=False)))[0] == 401
        assert hits == []

    run(with_hub(main, on_route=route))


# ---- secrets never reach a log line, and log files are private -------------------------------------------------
def test_redact_covers_known_values_and_the_shapes_secrets_travel_in():
    secret = "S3cr3t-value-0123456789"
    text = (
        f"Authorization: Bearer {secret} MOTOR_ENLACE_CLAVE={secret} token={secret} HERMES_3D_LINK_KEY: {secret} plain {secret}"
    )
    out = logsafe.redact(text, {secret})
    assert secret not in out and out.count(logsafe.REDACTED) >= 4
    assert logsafe.redact("nothing to hide here") == "nothing to hide here"


def test_every_log_line_is_redacted_and_the_file_is_private(tmp_path):
    path = tmp_path / "bridge.log"
    old_umask = os.umask(0)
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        flt = logsafe.install(path)
        flt.add_secret(TOKEN)

        async def boom(method, p, b):
            raise RuntimeError(f"internal failure with {TOKEN} inside")

        async def main(h, port):
            req = request(port=port, path="/action/x")
            assert (await raw(port, req))[0] == 500

        run(with_hub(main, on_route=boom))
        logging.getLogger("hermes_3d").warning("link key is %s and header Authorization: Bearer %s", "abcdef0123456789xyz", TOKEN)
        logging.getLogger("hermes_3d").info("tuple arg %s", ("k=" + TOKEN,))
        for handler in root.handlers:
            handler.flush()
    finally:
        os.umask(old_umask)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)
    text = path.read_text()
    assert "internal failure" in text and "Traceback" in text, "errors are still logged with a traceback"
    assert TOKEN not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

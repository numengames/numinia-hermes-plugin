# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""N-S4: a per-client token bucket on the bridge's loopback face (429 when a client floods it)."""

from __future__ import annotations

import asyncio

from conftest import run
from test_hub_security import raw, request, with_hub

from hermes_3d_lib import hub as hub_mod


async def raw_from(source: str, port: int, payload: bytes) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/1.1 ") else 0


def test_a_flooding_client_gets_429_with_retry_after_and_others_are_unaffected(monkeypatch):
    monkeypatch.setattr(hub_mod, "RATE_BURST", 3)
    monkeypatch.setattr(hub_mod, "RATE_PER_SECOND", 0.01)

    async def main(h, port):
        codes = [await raw_from("127.0.0.1", port, request(port=port)) for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
        assert await raw_from("127.0.0.2", port, request(port=port)) == 200, "another client has its own bucket"
        assert h.rejected_local >= 2

    run(with_hub(main))


def test_429_carries_retry_after(monkeypatch):
    monkeypatch.setattr(hub_mod, "RATE_BURST", 1)
    monkeypatch.setattr(hub_mod, "RATE_PER_SECOND", 0.01)

    async def main(h, port):
        await raw(port, request(port=port))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request(port=port))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        assert data.startswith(b"HTTP/1.1 429") and b"Retry-After: " in data

    run(with_hub(main))


def test_the_bucket_refills_over_time(monkeypatch):
    monkeypatch.setattr(hub_mod, "RATE_BURST", 1)
    monkeypatch.setattr(hub_mod, "RATE_PER_SECOND", 20.0)

    async def main(h, port):
        assert (await raw(port, request(port=port)))[0] == 200
        await asyncio.sleep(0.15)
        assert (await raw(port, request(port=port)))[0] == 200

    run(with_hub(main))


def test_the_default_limit_does_not_bother_normal_use():
    assert hub_mod.RATE_PER_SECOND >= 20 and hub_mod.RATE_BURST >= 50

    async def main(h, port):
        codes = [(await raw(port, request(port=port)))[0] for _ in range(10)]
        assert set(codes) == {200}

    run(with_hub(main))


def test_the_limiter_forgets_old_clients_and_stays_bounded():
    lim = hub_mod._ClientLimiter()
    for i in range(hub_mod.MAX_TRACKED_CLIENTS + 50):
        assert lim.allow(f"10.0.0.{i}", now=float(i))
    assert len(lim.buckets) <= hub_mod.MAX_TRACKED_CLIENTS

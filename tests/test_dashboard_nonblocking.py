# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""The dashboard routes run inside Hermes' web server: a slow bridge call must not freeze its event loop."""

from __future__ import annotations

import asyncio
import time

from test_entry import load_router

from hermes_3d_lib import client


def test_a_slow_bridge_call_does_not_block_the_event_loop(hermes_home, monkeypatch):
    api = load_router()

    def slow_call(*_a, **_k):
        time.sleep(0.6)  # what a pending world action costs (the real wait can reach 25-35 s)
        return {"ok": False, "error": "bridge down"}

    monkeypatch.setattr(client, "call", slow_call)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        t = asyncio.create_task(ticker())
        await api.status()
        t.cancel()
        return ticks

    assert asyncio.run(main()) >= 5  # the loop kept serving while the call waited

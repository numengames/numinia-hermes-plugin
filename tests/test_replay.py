"""A world request id is honoured once: a replayed ``peticion`` frame is refused (link threat model, replay)."""

from __future__ import annotations

import asyncio

from hermes_3d_lib import actions


def _queue(ttl_known=True):
    return actions.ActionQueue(enabled=lambda: True, known_job=lambda j: ttl_known)


def _req(i="req-1", job="8bc0eecea9b5"):
    return {"id": i, "objeto": f"cron/{job}", "verbo": "cron.pausar", "quien": {"id": "u1", "nombre": "Ana"}}


def test_a_replayed_request_id_is_refused_without_a_second_approval(monkeypatch):
    monkeypatch.setattr(actions, "TTL_SECONDS", 0.05)

    async def main():
        q = _queue()
        first = await q.submit(_req())
        assert first["code"] == "no_approver"  # nobody approved in the test; it was parked and expired
        again = await q.submit(_req())
        assert again["code"] == "replayed" and again["ok"] is False
        assert q.pending() == []
        other = await q.submit(_req("req-2"))
        assert other["code"] == "no_approver"

    asyncio.run(main())


def test_a_request_without_an_id_is_refused():
    res = asyncio.run(_queue().submit({"objeto": "cron/8bc0eecea9b5", "verbo": "cron.pausar"}))
    assert res["code"] == "bad_request"


def test_the_seen_window_is_bounded_and_expires(monkeypatch):
    q = _queue()
    for i in range(actions.SEEN_MAX + 50):
        q._remember(f"id-{i}")
    assert len(q._seen) <= actions.SEEN_MAX
    assert not q._is_replay("id-0") and q._is_replay(f"id-{actions.SEEN_MAX + 49}")
    real = actions.time.monotonic()
    monkeypatch.setattr(actions.time, "monotonic", lambda: real + actions.SEEN_TTL + 1)
    assert not q._is_replay(f"id-{actions.SEEN_MAX + 49}")


def test_read_only_and_cron_off_answers_do_not_burn_the_id():
    q = actions.ActionQueue(enabled=lambda: False, known_job=lambda j: True)
    assert asyncio.run(q.submit(_req()))["code"] == "cron_off"
    assert not q._is_replay("req-1")

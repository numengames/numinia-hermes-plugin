"""C12: delivery clock, requirements and official links (``plazo``). Pure: the clock is passed in."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from conftest import LOOP

from hermes_3d_lib import plazo
from hermes_3d_lib import settings as S
from hermes_3d_lib.protocol import MAX_TITLE, MAX_URL, OBJECT_KEYS

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
D = plazo.DEADLINE


def test_deadline_is_nov_11_22_utc():
    assert datetime(2026, 11, 11, 22, 0, tzinfo=UTC) == D


def test_left_text_rounds_down_to_the_minute():
    assert plazo.left_text(4 * 86400 + 3 * 3600 + 12 * 60 + 59) == "4d 03h 12m"
    assert plazo.left_text(3 * 3600 + 5 * 60) == "03h 05m"
    assert plazo.left_text(59) == "0m"
    assert plazo.left_text(600) == "10m"


def test_clock_counts_down_and_changes_once_a_minute():
    t0 = D - timedelta(days=5, seconds=100)  # 5d 00h 01m 40s left
    a = plazo.clock_object(t0)
    b = plazo.clock_object(t0 + timedelta(seconds=30))  # 1m 10s left: same minute
    c = plazo.clock_object(t0 + timedelta(seconds=41))  # 59 s left: next minute
    assert a["titulo"] == "Deadline · 5d 00h 01m" and a == b  # same minute -> same card -> no 'cambio' is sent
    assert c["titulo"] == "Deadline · 5d 00h 00m" and c != a
    assert a["estado"] == "activo"


def test_clock_goes_programmed_in_the_last_day_and_closed_after():
    assert plazo.clock_object(D - timedelta(hours=3))["estado"] == "programado"
    closed = plazo.clock_object(D + timedelta(seconds=1))
    assert closed["titulo"] == "Deadline · closed" and closed["estado"] == "fallo"
    assert plazo.clock_object(D + timedelta(days=9)) == closed  # stops changing
    assert plazo.remaining(D + timedelta(days=1)) == 0


def test_naive_now_is_taken_as_utc():
    assert plazo.remaining(datetime(2026, 11, 11, 21, 0)) == 3600


def test_snapshot_shape_limits_and_zone():
    snap = plazo.snapshot(D - timedelta(hours=1))
    assert len(snap) == 6 and "plazo-reloj" in snap
    for id_, o in snap.items():
        assert ID_RE.match(id_) and o["tipo"] == "documento" and o["zona"] == "z1" and o["acciones"] == []
        assert set(o) <= set(OBJECT_KEYS)
        assert len(o["titulo"].encode()) <= MAX_TITLE and len(o["subtitulo"].encode()) <= 120
        assert len(o["descripcion"].encode()) <= 200 and len(o["url"].encode()) <= MAX_URL


def test_links_are_the_official_https_pages_with_listed_origins():
    snap = plazo.snapshot()
    urls = {o["url"] for o in snap.values()}
    assert plazo.URL_SUBMISSION in urls and plazo.URL_DEVGUIDE in urls and plazo.URL_FORM in urls and plazo.URL_DISCORD in urls
    assert plazo.URL_FORM.endswith("/FwJLvOln")
    for u in urls:
        p = urlparse(u)
        assert p.scheme == "https" and not p.username and f"{p.scheme}://{p.hostname}" in plazo.ORIGINS
        assert LOOP not in u


def test_documentos_switch_gates_the_clock(fake_root, tmp_path):
    from hermes_3d_lib import bridge_main

    def bridge(on):
        b = bridge_main.Bridge(
            tmp_path / "home", engine_url=f"ws://{LOOP}:1", link_key="k", scan_root=fake_root, allow_direct_db=True
        )
        b.hub.engine_functions = {n: (n in on) for n in S.FUNCTIONS}
        b.hub.switches = {n: (n in on) for n in S.FUNCTIONS}
        return b

    assert not [k for k in bridge({"salud"}).mirror_objects() if k.startswith("documento/plazo")]
    b = bridge({"documentos"})
    b.now = lambda: D - timedelta(minutes=90)
    got = {k for k in b.mirror_objects() if k.startswith("documento/plazo")}
    assert len(got) == 6 and "documento/plazo-reloj" in got


def test_a_tick_publishes_only_the_clock(fake_root, tmp_path):
    from hermes_3d_lib import bridge_main

    b = bridge_main.Bridge(
        tmp_path / "home", engine_url=f"ws://{LOOP}:1", link_key="k", scan_root=fake_root, allow_direct_db=True
    )
    b.hub.engine_functions = {n: (n == "documentos") for n in S.FUNCTIONS}
    b.hub.switches = {n: (n == "documentos") for n in S.FUNCTIONS}
    b.hub.connected = True
    sent: list[dict] = []
    b.hub.publish = lambda m: sent.append(m) or True
    t0 = D - timedelta(days=2, minutes=5)
    b.now = lambda: t0
    b._mirror_published = b.mirror_objects()
    b.publish_mirrors()
    assert sent == []  # same minute: nothing
    b.now = lambda: t0 + timedelta(seconds=61)
    b.publish_mirrors()
    assert len(sent) == 1 and sent[0]["t"] == "cambio" and sent[0]["objeto"]["id"] == "plazo-reloj"
    assert sent[0]["objeto"]["titulo"] == "Deadline · 2d 00h 03m"

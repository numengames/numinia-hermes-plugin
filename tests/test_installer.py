# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``install-engine`` against a FAKE release server on the loopback: nothing here touches the internet or a real Node."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import LOOP
from fake_release import ASSET, ENTRY, FakeRelease, as_script, good_asset, good_members, tar_gz, zip_asset
from test_entry import load_entry

from hermes_3d_lib import installer
from hermes_3d_lib import settings as S
from hermes_3d_lib.errors import Hermes3DError

REAL_WHICH = installer.shutil.which


@pytest.fixture(autouse=True)
def no_real_hermes(monkeypatch):
    """Never let a test reach a `hermes` found on the developer's PATH (it could be a real install)."""
    monkeypatch.setattr(installer.shutil, "which", lambda n: None if n == "hermes" else REAL_WHICH(n))


def run_install(home, lock, rel, **kw):
    kw.setdefault("yes", True)
    kw.setdefault("no_shortcut", True)
    kw.setdefault("out", lambda *a, **k: None)
    kw.setdefault("euid", 1000)
    return installer.install_engine(home, lock_path=lock, url=rel.url, **kw)


def data_dir(home: Path) -> Path:
    return home / "plugin-data" / "hermes-3d"


def code_of(exc: pytest.ExceptionInfo) -> str:
    assert isinstance(exc.value, Hermes3DError)
    return exc.value.code


def assert_nothing_left(home: Path):
    root = data_dir(home) / "engine"
    assert not root.exists() or not [
        p for p in root.iterdir() if p.name.endswith(".partial") or (p.is_dir() and not (p / installer.MARKER).exists())
    ]


# ---- the shipped lock --------------------------------------------------------------------------------------------
def unpinned_lock(tmp_path):
    p = tmp_path / "unpinned.lock.json"
    p.write_text(json.dumps({"tag": installer.UNPINNED, "sha256": installer.UNPINNED, "asset": "instalar-motor.sh"}))
    return p


def test_the_shipped_lock_pins_the_real_release(hermes_home):
    lock = installer.load_lock()
    assert lock.pinned and lock.tag == "motor-7c6c9a2" and len(lock.sha256) == 64 and lock.size_bytes == 14393001
    assert lock.entry == "packages/server/src/entrada.ts" and lock.asset == "instalar-motor.sh" and lock.node_min == (22, 18, 0)
    assert "{tag}" in lock.url_template and lock.url_template.startswith("https://github.com/numengames/")


def test_an_unpinned_lock_refuses_with_a_clear_message_and_downloads_nothing(hermes_home, tmp_path):
    with pytest.raises(installer.InstallError, match="not pinned yet") as e:
        installer.install_engine(hermes_home, lock_path=unpinned_lock(tmp_path), yes=True, euid=1000)
    assert code_of(e) == "ENGINE_NOT_PUBLISHED" and e.value.retryable is False
    assert not (data_dir(hermes_home) / "engine").exists()


@pytest.mark.parametrize(
    "over",
    [
        {"sha256": "abc"},
        {"tag": "bad tag!"},
        {"sha256": "A" * 63},
        {"asset": "../x"},
        {"entry": "../../etc/passwd"},
        {"entry": "/abs"},
    ],
)
def test_lock_with_a_bad_pin_is_refused(hermes_home, tmp_path, over):
    with FakeRelease(good_asset()) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path, **over), rel)
        assert code_of(e) == "LOCK_INVALID" and rel.hits == []


def test_missing_or_broken_lock_file(hermes_home, tmp_path):
    for body in (None, "{not json", "[]"):
        p = tmp_path / "x.json"
        p.unlink(missing_ok=True)
        if body is not None:
            p.write_text(body)
        with pytest.raises(installer.InstallError, match=r"engine.lock.json"):
            installer.install_engine(hermes_home, lock_path=p, yes=True, euid=1000)


def test_url_must_be_https_or_loopback_http(hermes_home, tmp_path):
    for bad in ("http://example.org/a", "file:///etc/passwd", "ftp://x/y", "https:///nohost"):
        with pytest.raises(installer.InstallError) as e:
            installer.check_url(bad)
        assert code_of(e) == "URL_REFUSED"
    installer.check_url("https://github.com/numengames/numinia-engine/releases/download/t/a")
    installer.check_url(f"http://{LOOP}:9/a")


# ---- the good path -----------------------------------------------------------------------------------------------------
def test_full_flow_downloads_verifies_unpacks_and_saves_engine_path(hermes_home, tmp_path):
    lines: list[str] = []
    with FakeRelease(good_asset()) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel, out=lambda *a, **k: lines.append(" ".join(map(str, a))))
        assert rel.hits == [f"/{rel.tag}/{rel.name}", f"/{rel.tag}/SHA256SUMS"]  # the asset, then the second check
    dest = Path(res["engine_path"])
    assert dest == data_dir(hermes_home) / "engine" / rel.sha256[:12] and res["reused"] is False
    assert (dest / "app" / ENTRY).is_file() and (dest / "www" / "index.html").is_file()
    assert (dest / "app/node_modules/ws/index.js").is_file()
    assert (
        not (dest / "actualizar.sh").exists() and not (dest / "asset").exists()
    )  # outside app/ www/ is skipped; the asset is deleted
    marker = json.loads((dest / installer.MARKER).read_text())
    assert marker["sha256"] == rel.sha256 and marker["entry"] == ENTRY
    assert stat.S_IMODE((dest / "app/bin/run.sh").stat().st_mode) == 0o755  # setuid stripped, exec kept
    assert stat.S_IMODE((dest / "app/package.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((data_dir(hermes_home) / "engine").stat().st_mode) == 0o700
    text = "\n".join(lines)
    assert rel.url in text and "MB" in text and rel.sha256 in text and "sha256 matches" in text
    cfg = S.Hermes3DSettings.from_yaml(hermes_home)
    assert cfg.engine_path == dest and cfg.engine_main() == dest / "app" / ENTRY and cfg.www_dir() == dest / "www"


def test_a_plain_tar_gz_and_a_zip_with_app_and_www_work_too(hermes_home, tmp_path):
    for n, asset in enumerate(
        (
            tar_gz(good_members()),
            zip_asset({"app/" + ENTRY: b"//x", "app/package.json": b"{}", "www/index.html": b"<html/>"}),
        )
    ):
        home = hermes_home / f"p{n}"
        home.mkdir()
        with FakeRelease(asset, name="engine.pkg") as rel:
            res = run_install(home, rel.lock(tmp_path), rel)
        assert (Path(res["engine_path"]) / "app" / ENTRY).is_file()


def test_rerun_is_idempotent_and_downloads_nothing(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path)
        first = run_install(hermes_home, lock, rel)
        marker = Path(first["engine_path"]) / installer.MARKER
        before = marker.stat().st_mtime_ns
        rel.hits.clear()
        again = run_install(
            hermes_home, lock, rel, yes=False, confirm=lambda q: pytest.fail("must not ask when already installed")
        )
        assert rel.hits == [] and again["reused"] is True and again["engine_path"] == first["engine_path"]
        assert marker.stat().st_mtime_ns == before


def test_same_dir_but_other_sha_is_not_reused(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path)
        dest = installer.engine_dir(hermes_home, installer.load_lock(lock))
        dest.mkdir(parents=True)
        (dest / installer.MARKER).write_text(json.dumps({"sha256": "0" * 64}))
        assert installer.is_installed(dest, installer.load_lock(lock)) is False
        res = run_install(hermes_home, lock, rel)
        assert res["reused"] is False and json.loads((dest / installer.MARKER).read_text())["sha256"] == rel.sha256


def test_the_confirmation_gate(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path)
        asked: list[str] = []
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, lock, rel, yes=False, confirm=lambda q: asked.append(q) or False)
        assert code_of(e) == "NOT_CONFIRMED" and rel.hits == [] and "MB" in asked[0] and f"{LOOP}:" in asked[0]
        with pytest.raises(installer.InstallError, match="Not confirmed"):
            run_install(hermes_home, lock, rel, yes=False, confirm=None)
        assert rel.hits == []
        run_install(hermes_home, lock, rel, yes=False, confirm=lambda q: True)
        assert rel.hits


# ---- refusals: integrity ---------------------------------------------------------------------------------------------------
def test_bad_sha256_is_refused_and_nothing_is_left(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path, sha256="1" * 64)
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, lock, rel)
    assert code_of(e) == "SHA256_MISMATCH" and "nothing was unpacked" in str(e.value)
    assert_nothing_left(hermes_home)
    assert not any((data_dir(hermes_home) / "engine").rglob("entrada.ts"))


def test_verification_happens_before_the_asset_is_opened_or_extracted(hermes_home, tmp_path, monkeypatch):
    """Bad sum, bad length: extract_asset / tarfile / zipfile are never reached and the downloaded file is deleted."""
    asset = good_asset()
    opened: list = []
    monkeypatch.setattr(installer, "extract_asset", lambda *a, **k: opened.append("extract") or {})
    for name in ("tarfile", "zipfile"):
        mod = getattr(installer, name)
        monkeypatch.setattr(
            mod, "open" if name == "tarfile" else "ZipFile", lambda *a, **k: opened.append("open") or pytest.fail("opened")
        )
    for over in ({"sha256": "1" * 64}, {"size_bytes": len(asset) - 1}, {"size_bytes": len(asset) + 1}):
        with FakeRelease(asset) as rel:
            lock = rel.lock(tmp_path, **over)
            with pytest.raises(installer.InstallError) as e:
                run_install(hermes_home, lock, rel)
        assert code_of(e) in ("SHA256_MISMATCH", "DOWNLOAD_TOO_LARGE", "DOWNLOAD_TRUNCATED")
        assert not list((data_dir(hermes_home) / "engine").rglob("asset"))  # the downloaded file is gone
    assert opened == []
    assert_nothing_left(hermes_home)


def test_setuid_and_setgid_bits_never_survive_unpacking(hermes_home, tmp_path):
    extra = [("app/bin/sg.sh", b"#!/bin/sh\n", {"mode": 0o2755}), ("app/bin/st.sh", b"#!/bin/sh\n", {"mode": 0o7777})]
    with FakeRelease(good_asset(extra)) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel)
    for n in ("run.sh", "sg.sh", "st.sh"):
        assert stat.S_IMODE((Path(res["engine_path"]) / "app/bin" / n).stat().st_mode) & 0o7000 == 0


# ---- the second check: SHA256SUMS next to the asset --------------------------------------------------------------------------
def test_sums_file_that_agrees_is_used_and_reported(hermes_home, tmp_path):
    lines: list[str] = []
    with FakeRelease(good_asset()) as rel:
        run_install(hermes_home, rel.lock(tmp_path), rel, out=lambda *a, **k: lines.append(" ".join(map(str, a))))
        assert any(h.endswith("/SHA256SUMS") for h in rel.hits)
    assert any("SHA256SUMS agrees" in line for line in lines)


def test_sums_file_that_disagrees_is_a_hard_failure_and_nothing_is_unpacked(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "extract_asset", lambda *a, **k: pytest.fail("extracted"))
    with FakeRelease(good_asset(), sums=(f"{'2' * 64}  {ASSET}\n").encode()) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert code_of(e) == "SUMS_MISMATCH"
    assert_nothing_left(hermes_home)
    assert not list((data_dir(hermes_home) / "engine").rglob("asset"))


@pytest.mark.parametrize(
    "kw",
    [{"sums_status": 404}, {"sums": b"garbage\n"}, {"sums": ("f" * 64 + "  other-file.sh\n").encode()}, {"sums": b"\xff\xfe"}],
)
def test_unreachable_or_unhelpful_sums_file_is_skipped_silently(hermes_home, tmp_path, kw):
    with FakeRelease(good_asset(), **kw) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel)
    assert Path(res["engine_path"]).is_dir()


def test_sums_file_unreachable_server_is_skipped(monkeypatch):
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert installer.check_against_sums("http://127.0.0.1:9/t/a", "a", "0" * 64) is False


def test_truncated_download_is_refused_and_deleted(hermes_home, tmp_path):
    asset = good_asset()
    with FakeRelease(asset, truncate_at=len(asset) // 2) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert code_of(e) in ("DOWNLOAD_TRUNCATED", "DOWNLOAD_FAILED") and e.value.retryable is True
    assert_nothing_left(hermes_home)


def test_a_server_that_sends_more_than_pinned_is_stopped(hermes_home, tmp_path):
    asset = good_asset()
    with FakeRelease(asset + b"X" * 5000) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path, size_bytes=len(asset), sha256=rel.sha256), rel)
    assert code_of(e) == "DOWNLOAD_TOO_LARGE"
    assert_nothing_left(hermes_home)


def test_unpinned_size_still_has_a_cap(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "MAX_DOWNLOAD", 100)
    with FakeRelease(good_asset()) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path, size_bytes=0), rel)
    assert code_of(e) == "DOWNLOAD_TOO_LARGE"


def test_http_error_is_reported(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path, tag="other")
        with pytest.raises(installer.InstallError, match="HTTP 404") as e:
            installer.install_engine(
                hermes_home, lock_path=lock, yes=True, euid=1000, url=rel.url.replace(rel.tag, "other"), out=lambda *a: None
            )
    assert code_of(e) == "DOWNLOAD_FAILED"
    assert_nothing_left(hermes_home)


def test_connection_refused_is_a_clean_retryable_error(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        lock = rel.lock(tmp_path)
        url = rel.url
    with pytest.raises(installer.InstallError) as e:
        installer.install_engine(hermes_home, lock_path=lock, yes=True, euid=1000, url=url, out=lambda *a: None)
    assert code_of(e) == "DOWNLOAD_FAILED" and e.value.retryable is True


# ---- refusals: hostile archives (sha matches: the asset itself is the attack) ----------------------------------------------
HOSTILE = {
    "traversal": [("app/../../outside.txt", b"pwned", {})],
    "traversal-deep": [("www/a/../../../outside.txt", b"pwned", {})],
    "absolute": [("/etc/hermes-3d-pwned", b"pwned", {})],
    "backslash": [("app\\..\\x", b"p", {})],
    "symlink-absolute": [("app/evil", None, {"type": tarfile.SYMTYPE, "linkname": "/etc"})],
    "symlink-escape": [("app/evil", None, {"type": tarfile.SYMTYPE, "linkname": "../../../etc"})],
    "symlink-to-www-parent": [("www/evil", None, {"type": tarfile.SYMTYPE, "linkname": "../.."})],
    "hardlink": [("app/hard", None, {"type": tarfile.LNKTYPE, "linkname": "app/package.json"})],
    "hardlink-escape": [("app/hard2", None, {"type": tarfile.LNKTYPE, "linkname": "../../etc/passwd"})],
    "hardlink-absolute": [("app/hard3", None, {"type": tarfile.LNKTYPE, "linkname": "/etc/passwd"})],
    "block-device": [("app/blk", None, {"type": tarfile.BLKTYPE})],
    "device": [("app/dev", None, {"type": tarfile.CHRTYPE})],
    "fifo": [("app/fifo", None, {"type": tarfile.FIFOTYPE})],
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_archive_entries_are_refused_and_nothing_escapes(hermes_home, tmp_path, name):
    asset = as_script(tar_gz(good_members(HOSTILE[name])))
    with FakeRelease(asset) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert code_of(e) == "UNSAFE_ARCHIVE"
    assert_nothing_left(hermes_home)
    for stray in (tmp_path / "outside.txt", data_dir(hermes_home) / "outside.txt", data_dir(hermes_home).parent / "outside.txt"):
        assert not stray.exists()


def test_a_file_cannot_be_written_through_an_earlier_symlink(hermes_home, tmp_path):
    target = tmp_path / "victim"
    target.mkdir()
    members = [("app/link", None, {"type": tarfile.SYMTYPE, "linkname": "../www"}), ("app/link/x.txt", b"data", {})]
    with FakeRelease(as_script(tar_gz(good_members(members)))) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert code_of(e) == "UNSAFE_ARCHIVE" and list(target.iterdir()) == []


def test_a_safe_relative_symlink_inside_the_folder_is_kept(hermes_home, tmp_path):
    members = [
        ("app/node_modules/.bin", None, {}),
        ("app/node_modules/.bin/tool", None, {"type": tarfile.SYMTYPE, "linkname": "../ws/index.js"}),
    ]
    with FakeRelease(as_script(tar_gz(good_members(members)))) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel)
    link = Path(res["engine_path"]) / "app/node_modules/.bin/tool"
    assert link.is_symlink() and link.resolve().is_file()


def test_zip_symlink_and_zip_traversal_are_refused(hermes_home, tmp_path):
    base = {"app/" + ENTRY: b"//x", "www/index.html": b"<html/>"}
    for asset in (zip_asset(base, symlink=("app/evil", "/etc")), zip_asset({**base, "app/../../x": b"p"})):
        with FakeRelease(asset) as rel:
            with pytest.raises(installer.InstallError) as e:
                run_install(hermes_home, rel.lock(tmp_path), rel)
        assert code_of(e) == "UNSAFE_ARCHIVE"
    assert_nothing_left(hermes_home)


def test_oversize_unpacked_asset_is_refused(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "MAX_UNPACKED", 1000)
    for asset in (good_asset([("app/big.bin", b"0" * 5000, {})]), zip_asset({"app/big.bin": b"0" * 5000})):
        with FakeRelease(asset) as rel:
            with pytest.raises(installer.InstallError) as e:
                run_install(hermes_home, rel.lock(tmp_path), rel)
        assert code_of(e) == "ARCHIVE_TOO_LARGE"
    assert_nothing_left(hermes_home)


def test_too_many_members_is_refused(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "MAX_MEMBERS", 5)
    with FakeRelease(good_asset()) as rel:
        with pytest.raises(installer.InstallError) as e:
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert code_of(e) == "ARCHIVE_TOO_LARGE"


@pytest.mark.parametrize(
    ("asset", "why"),
    [
        (b"plain text, not an archive", "neither"),
        (b"#!/bin/sh\nno marker here\n", "payload marker"),
        (b"#!/bin/sh\n__CARGA_DEBAJO__\n!!!not base64!!!\n", "base64"),
        (tar_gz([("app/package.json", b"{}", {})]), "not the engine"),
    ],
)
def test_assets_that_are_not_the_engine(hermes_home, tmp_path, asset, why):
    with FakeRelease(asset) as rel:
        with pytest.raises(installer.InstallError, match=why):
            run_install(hermes_home, rel.lock(tmp_path), rel)
    assert_nothing_left(hermes_home)


def test_a_tar_member_shorter_than_its_header_is_refused(tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo("app/a.txt")
        info.size = 100
        tf.addfile(info, io.BytesIO(b"x" * 100))
    blob = raw.getvalue()[:700]  # cut inside the data block
    import gzip

    asset = tmp_path / "a.tgz"
    asset.write_bytes(gzip.compress(blob))
    with pytest.raises((installer.InstallError, tarfile.TarError, EOFError)):
        (tmp_path / "out").mkdir()
        installer.extract_asset(asset, tmp_path / "out")


# ---- root ----------------------------------------------------------------------------------------------------------------
def test_root_is_refused_before_anything_else(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        with pytest.raises(installer.InstallError, match="root") as e:
            run_install(hermes_home, rel.lock(tmp_path), rel, euid=0)
        assert code_of(e) == "ROOT_REFUSED" and e.value.retryable is False and rel.hits == []
    assert not data_dir(hermes_home).exists()
    with pytest.raises(installer.InstallError):
        installer.refuse_root(0)
    installer.refuse_root(1000)
    if os.geteuid() != 0:
        installer.refuse_root()  # the real euid of the test run


# ---- settings written -----------------------------------------------------------------------------------------------------
def test_config_yaml_keeps_other_settings_and_gets_a_backup(hermes_home, tmp_path):
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: x\nplugins:\n  entries:\n    hermes-3d:\n      settings:\n        publish_cron: true\n    other:\n      enabled: true\n"
    )
    with FakeRelease(good_asset()) as rel:
        run_install(hermes_home, rel.lock(tmp_path), rel)
    data = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert data["model"] == {"default": "x"} and data["plugins"]["entries"]["other"] == {"enabled": True}
    st = data["plugins"]["entries"]["hermes-3d"]["settings"]
    assert st["publish_cron"] is True and st["engine_path"].endswith(rel.sha256[:12])
    assert (hermes_home / "config.yaml.hermes-3d-backup").is_file()


def test_broken_config_yaml_is_found_before_downloading(hermes_home, tmp_path):
    (hermes_home / "config.yaml").write_text("a: [unclosed")
    with FakeRelease(good_asset()) as rel:
        with pytest.raises(installer.InstallError, match="not valid YAML"):
            run_install(hermes_home, rel.lock(tmp_path), rel)
        assert rel.hits == []
    (hermes_home / "config.yaml").write_text("- a list\n")
    with pytest.raises(installer.InstallError, match="not a YAML mapping"):
        installer._preflight_config(hermes_home)


def test_engine_path_goes_through_hermes_config_set_when_hermes_is_on_the_path(hermes_home, tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(installer.shutil, "which", lambda n: "/fake/hermes" if n == "hermes" else REAL_WHICH(n))
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    with FakeRelease(good_asset()) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel)
    assert calls[0][:3] == ["/fake/hermes", "config", "set"] and calls[0][4] == res["engine_path"]
    assert calls[0][3] == "plugins.entries.hermes-3d.settings.engine_path"


def test_hermes_config_set_failing_falls_back_to_the_yaml_edit(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/fake/hermes" if n == "hermes" else REAL_WHICH(n))
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", "boom"))
    with FakeRelease(good_asset()) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel)
    assert S.Hermes3DSettings.from_yaml(hermes_home).engine_path == Path(res["engine_path"])


def test_save_setting_false_leaves_config_alone(hermes_home, tmp_path):
    with FakeRelease(good_asset()) as rel:
        run_install(hermes_home, rel.lock(tmp_path), rel, save_setting=False)
    assert not (hermes_home / "config.yaml").exists()


def test_a_shortcut_is_written_unless_asked_not_to(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    with FakeRelease(good_asset()) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel, no_shortcut=False, platform="linux")
    assert res["shortcut"] and Path(res["shortcut"]).is_file()
    with FakeRelease(good_asset()) as rel:
        res = run_install(hermes_home, rel.lock(tmp_path), rel, no_shortcut=False, platform="plan9")
    assert res["shortcut"] is None


# ---- shortcuts ---------------------------------------------------------------------------------------------------------------
def test_shortcut_contents_for_each_platform(tmp_path):
    url = f"http://{LOOP}:8790/"
    d = installer.shortcut_text("linux", "/usr/bin/hermes", url, None)
    assert d.startswith("[Desktop Entry]") and "Terminal=true" in d and "Exec=" in d and url in d
    m = installer.shortcut_text("darwin", "/usr/local/bin/hermes", url, None)
    assert m.startswith("#!/bin/bash") and "open " in m and url in m
    w = installer.shortcut_text("win32", "hermes", url, None)
    assert "@echo off" in w and "3d up" in w and f"start {url}" in w
    # a custom home is exported, and a hostile path cannot break out of the quoting
    nasty = "/tmp/a b'; touch /tmp/pwned #"
    m2 = installer.shortcut_text("darwin", "/usr/local/bin/hermes", url, nasty)
    assert "touch /tmp/pwned" in m2 and "'/tmp/a b'\"'\"'; touch /tmp/pwned #'" in m2
    assert installer.shortcut_text("linux", "/x/hermes", url, nasty).count("\n") < 12


def test_shortcut_files_per_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    home = tmp_path / "userhome"
    p1 = installer.write_shortcut("linux", "hermes", f"http://{LOOP}:8790/", None, home=home)
    assert p1 == tmp_path / "share" / "applications" / "hermes-3d.desktop" and p1.stat().st_mode & 0o111
    p2 = installer.write_shortcut("darwin", "hermes", f"http://{LOOP}:8790/", None, home=home)
    assert p2 == home / "Desktop" / "Hermes 3D.command" and p2.stat().st_mode & 0o100
    p3 = installer.write_shortcut("win32", "hermes", f"http://{LOOP}:8790/", None, home=home)
    assert p3 == home / "Desktop" / "Hermes 3D.bat"
    monkeypatch.delenv("XDG_DATA_HOME")
    p4 = installer.write_shortcut("linux", "hermes", f"http://{LOOP}:8790/", None, home=home)
    assert p4 == home / ".local/share/applications/hermes-3d.desktop"
    assert installer.write_shortcut("plan9", "hermes", "u", None, home=home) is None


# ---- the CLI ---------------------------------------------------------------------------------------------------------------------
def cli_args(**kw):
    base = {"hermes3d_cmd": "install-engine", "yes": True, "no_shortcut": True, "url": None}
    return type("A", (), {**base, **kw})()


def test_cli_subcommand_parses_the_options(hermes_home):
    import argparse

    mod = load_entry()
    p = argparse.ArgumentParser()
    mod._setup_cli(p)
    a = p.parse_args(["install-engine", "--yes", "--no-shortcut", "--url", "http://x"])
    assert a.hermes3d_cmd == "install-engine" and a.yes and a.no_shortcut and a.url == "http://x"
    assert p.parse_args(["install-engine"]).yes is False


def test_cli_refuses_with_the_unpinned_lock_and_exit_code_1(hermes_home, capsys, tmp_path, monkeypatch):
    mod = load_entry()
    monkeypatch.setattr(installer, "default_lock_path", lambda: unpinned_lock(tmp_path))
    assert mod._cli(cli_args()) == 1
    err = capsys.readouterr().err
    assert "ENGINE_NOT_PUBLISHED" in err and "TODO-PUBLISH" in err


def test_cli_asks_on_the_terminal_and_stops_on_no(hermes_home, tmp_path, monkeypatch, capsys):
    mod = load_entry()
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with FakeRelease(good_asset()) as rel:
        monkeypatch.setattr(installer, "default_lock_path", lambda: rel.lock(tmp_path))
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert mod._cli(cli_args(yes=False, url=rel.url)) == 1
        assert rel.hits == []
    assert "Not confirmed" in capsys.readouterr().err


def test_cli_without_a_terminal_refuses_instead_of_assuming_yes(hermes_home, tmp_path, monkeypatch, capsys):
    mod = load_entry()
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with FakeRelease(good_asset()) as rel:
        monkeypatch.setattr(installer, "default_lock_path", lambda: rel.lock(tmp_path))

        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert mod._cli(cli_args(yes=False, url=rel.url)) == 1
        assert rel.hits == []
    assert "--yes" in capsys.readouterr().err


def test_cli_happy_path_end_to_end(hermes_home, tmp_path, monkeypatch, capsys):
    mod = load_entry()
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with FakeRelease(good_asset()) as rel:
        monkeypatch.setattr(installer, "default_lock_path", lambda: rel.lock(tmp_path))
        assert mod._cli(cli_args(url=rel.url)) == 0
    out = capsys.readouterr().out
    assert "engine_path" in out and "hermes 3d up" in out and "sha256 matches" in out

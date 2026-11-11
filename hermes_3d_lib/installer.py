# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d install-engine``: a USER-initiated, pinned, checksum-verified download of the Numinia Engine release.

The plugin catalog forbids bundling the engine, so nothing here runs unless the user types the command (or answers yes inside
``hermes 3d setup``). The flow:

1. refuse to run as root (the engine refuses too; an install under root would also leave root-owned files in the user's home);
2. read ``engine.lock.json``: release ``tag``, ``asset`` name, URL template, ``sha256`` of the asset, expected ``size_bytes`` and the
   ``entry`` file. While ``tag``/``sha256`` say ``TODO-PUBLISH`` the command refuses, never downloading an unpinned thing;
3. already installed with the same sha256 -> nothing is downloaded, nothing asked (idempotent);
4. print URL, size and destination and ask (``--yes`` skips the question and nothing else);
5. download to ``<plugin data dir>/engine/<sha12>.partial/`` (0700) with a size cap, hashing while reading; a short read, a wrong
   length or a wrong sha256 deletes the partial folder and fails BEFORE a single byte is unpacked;
6. unpack IN PYTHON (never by running the downloaded script): see :func:`extract_asset`. The release asset is the engine's own
   ``instalar-motor.sh``, whose payload (a base64 ``tar.gz`` of ``app/`` and ``www/``) sits under the marker line
   ``__CARGA_DEBAJO__``; a plain ``.tar.gz`` or ``.zip`` with the same two folders works too. Only ``app/`` and ``www/`` are
   written, as the current user, with no setuid/setgid bit, no device, no path outside the folder and no link that leaves it;
7. check ``app/<entry>``, write the marker ``.hermes-3d-engine.json`` and move the folder into place; ``engine_path`` is saved.

Why not run ``instalar-motor.sh --solo-extraer``: it is a root-oriented bash script; executing a 14 MB script just to unpack a tar
adds an interpreter, an environment and a privilege question for nothing. The payload format is one line marker + base64 + tar.gz,
which we read with a bounded, link-aware extractor. (Verified on the engine's own generated ``deploy/instalar-motor.sh``.)
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from . import config
from . import settings as S
from .errors import Hermes3DError

LOCK_NAME = "engine.lock.json"
UNPINNED = "TODO-PUBLISH"
MARKER = ".hermes-3d-engine.json"
PAYLOAD_MARK = b"__CARGA_DEBAJO__\n"
KEEP_TOP = ("app", "www")  # the only folders of the asset that are written
DEFAULT_URL = "https://github.com/numengames/numinia-engine/releases/download/{tag}/{asset}"
MAX_DOWNLOAD = 120 * 1024 * 1024  # bytes; the real asset is ~14 MB
MAX_UNPACKED = 400 * 1024 * 1024  # bytes written under app/ + www/ (the real ones are ~24 MB)
MAX_MEMBERS = 20000
CONNECT_TIMEOUT = 30.0
SETTING_KEY = f"plugins.entries.{S.PLUGIN_ID}.settings.engine_path"
_SHA = re.compile(r"[0-9a-f]{64}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENTRY = re.compile(r"[A-Za-z0-9_.@-]+(/[A-Za-z0-9_.@-]+)*")
_ENV_KEEP = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

Out = Callable[..., None]


class InstallError(Hermes3DError):
    """Something the user can act on; the message says what. ``code`` tells a machine which kind."""

    code = "INSTALL_FAILED"


def fail(code: str, message: str, *, retryable: bool = False) -> InstallError:
    return InstallError(message, code=code, retryable=retryable)


@dataclass(frozen=True)
class Lock:
    source: str
    tag: str
    asset: str
    url_template: str
    sha256: str
    size_bytes: int
    entry: str
    node_min: tuple[int, int, int]

    @property
    def pinned(self) -> bool:
        return self.tag != UNPINNED and bool(_TAG.fullmatch(self.tag)) and bool(_SHA.fullmatch(self.sha256))

    @property
    def url(self) -> str:
        return self.url_template.replace("{tag}", self.tag).replace("{asset}", self.asset)

    @property
    def dir_name(self) -> str:
        return self.sha256[:12]

    @property
    def size_text(self) -> str:
        return f"{self.size_bytes / 1_000_000:.1f} MB" if self.size_bytes else "size not pinned"


def default_lock_path() -> Path:
    return Path(__file__).resolve().parents[1] / LOCK_NAME


def _int(raw: Any, default: int = 0) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else default


def _sha_text(raw: Any) -> str:
    text = str(raw or "").strip()
    return text if text == UNPINNED else text.lower()


def load_lock(path: Path | None = None) -> Lock:
    path = path or default_lock_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise fail("LOCK_UNREADABLE", f"cannot read {LOCK_NAME} ({path}): {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise fail("LOCK_UNREADABLE", f"{LOCK_NAME} must be a JSON object")
    node = re.findall(r"\d+", str(data.get("node", ">=22.18")))
    node_min = tuple(int(x) for x in [*node, "0", "0", "0"][:3])
    return Lock(
        source=str(data.get("source", "")),
        tag=str(data.get("tag", "")),
        asset=str(data.get("asset", "")),
        url_template=str(data.get("url", DEFAULT_URL)),
        sha256=_sha_text(data.get("sha256")),
        size_bytes=_int(data.get("size_bytes")),
        entry=str(data.get("entry", "")),
        node_min=(node_min[0], node_min[1], node_min[2]),
    )


def validate_lock(lock: Lock) -> None:
    if lock.tag == UNPINNED or lock.sha256 == UNPINNED:
        raise fail(
            "ENGINE_NOT_PUBLISHED",
            f"the engine release is not pinned yet: {LOCK_NAME} still says {UNPINNED}, so there is no checksum to verify. "
            "Refusing to download an unpinned file. Point engine_path at an engine you already have, or wait for a release.",
        )
    if not lock.pinned:
        raise fail(
            "LOCK_INVALID", f"{LOCK_NAME} has no valid pin: `tag` must be a release tag and `sha256` 64 lowercase hex characters"
        )
    if not _ASSET.fullmatch(lock.asset):
        raise fail("LOCK_INVALID", f"{LOCK_NAME}: `asset` is not a plain file name")
    if not _ENTRY.fullmatch(lock.entry) or ".." in PurePosixPath(lock.entry).parts:
        raise fail("LOCK_INVALID", f"{LOCK_NAME}: `entry` must be a relative path inside app/ without '..'")


def check_url(url: str) -> None:
    """https only; plain http only to the loopback (the tests' fake release server)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return
    raise fail("URL_REFUSED", f"engine URL must be https:// (or http:// to the loopback), got {url[:80]!r}")


def refuse_root(euid: int | None = None) -> None:
    uid = os.geteuid() if euid is None and hasattr(os, "geteuid") else euid
    if uid == 0:
        raise fail(
            "ROOT_REFUSED",
            "refusing to run as root: the engine runs as your own user and refuses to start as root. "
            "Run `hermes 3d setup` as the user that owns your Hermes home.",
        )


# ---- download ------------------------------------------------------------------------------------------------------------
def download(
    url: str,
    dest: Path,
    *,
    expect_size: int,
    expect_sha: str,
    out: Out = print,
    timeout: float = CONNECT_TIMEOUT,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Fetch ``url`` into ``dest`` (0600), hashing while reading. Deletes ``dest`` and raises on ANY problem. Returns the size."""
    check_url(url)
    cap = expect_size if expect_size else MAX_DOWNLOAD
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-3d-installer"})  # noqa: S310 -- scheme checked above
    digest = hashlib.sha256()
    total = 0
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise fail("DOWNLOAD_TOO_LARGE", f"the server announces {declared} bytes, more than the {cap} allowed; refusing")
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise fail("DOWNLOAD_TOO_LARGE", f"the download is larger than the {cap} bytes allowed; stopped and deleted")
                digest.update(chunk)
                fh.write(chunk)
                if progress is not None:
                    progress(total)
        if expect_size and total != expect_size:
            raise fail(
                "DOWNLOAD_TRUNCATED",
                f"downloaded {total} bytes, expected {expect_size}; the file is incomplete and was deleted",
                retryable=True,
            )
        if digest.hexdigest() != expect_sha:
            raise fail(
                "SHA256_MISMATCH",
                f"sha256 of the download is {digest.hexdigest()[:16]}…, the pinned one is {expect_sha[:16]}…; "
                "the file was deleted and nothing was unpacked",
            )
    except InstallError:
        dest.unlink(missing_ok=True)
        raise
    except http.client.IncompleteRead:
        dest.unlink(missing_ok=True)
        raise fail("DOWNLOAD_TRUNCATED", "the connection closed before the whole file arrived; deleted", retryable=True) from None
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise fail("DOWNLOAD_FAILED", f"the server answered HTTP {exc.code} for {url}", retryable=exc.code >= 500) from None
    except (OSError, http.client.HTTPException) as exc:
        dest.unlink(missing_ok=True)
        raise fail("DOWNLOAD_FAILED", f"download failed ({type(exc).__name__}); nothing was installed", retryable=True) from None
    out(f"  Downloaded {total / 1_000_000:.1f} MB. sha256 matches.")
    return total


Step = Callable[..., None]  # on_step(name, **facts): download_start, downloaded, verified, unpack_start, unpacked


SUMS_NAME = "SHA256SUMS"
SUMS_MAX = 65536


def check_against_sums(url: str, asset_name: str, sha: str, *, timeout: float = 10.0, out: Out = print) -> bool:
    """Second check: if ``SHA256SUMS`` sits next to the asset, the pinned sha must match its line for the asset.

    Soft when the file cannot be fetched or does not list the asset (returns False, silently); a mismatch raises
    ``SUMS_MISMATCH``. Returns True when it was fetched and agrees.
    """
    sums_url = url.rsplit("/", 1)[0] + "/" + SUMS_NAME
    try:
        check_url(sums_url)
        req = urllib.request.Request(sums_url, headers={"User-Agent": "hermes-3d-installer"})  # noqa: S310 -- scheme checked
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            text = resp.read(SUMS_MAX).decode("utf-8", "replace")
    except (InstallError, OSError, http.client.HTTPException, ValueError):
        return False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset_name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            if parts[0].lower() != sha:
                raise fail(
                    "SUMS_MISMATCH",
                    f"the release's {SUMS_NAME} lists {parts[0][:16]}… for {asset_name}, the pin is {sha[:16]}…; nothing was unpacked",
                )
            out(f"  {SUMS_NAME} agrees with the pin.")
            return True
    return False


# ---- safe unpacking ------------------------------------------------------------------------------------------------------
class _Budget:
    def __init__(self) -> None:
        self.bytes = 0
        self.members = 0

    def member(self) -> None:
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise fail("ARCHIVE_TOO_LARGE", f"the asset has more than {MAX_MEMBERS} entries; refusing")

    def add(self, n: int) -> None:
        self.bytes += n
        if self.bytes > MAX_UNPACKED:
            raise fail("ARCHIVE_TOO_LARGE", f"the asset unpacks to more than {MAX_UNPACKED // 1_000_000} MB; refusing")


def _clean_name(name: str) -> PurePosixPath | None:
    """The member name as a relative posix path under app/ or www/, or None when it is outside what we keep. Unsafe -> raises."""
    if "\\" in name or "\0" in name:
        raise fail("UNSAFE_ARCHIVE", f"entry {name[:60]!r} has a backslash or NUL in its name; refusing the whole asset")
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        raise fail("UNSAFE_ARCHIVE", f"entry {name[:60]!r} points outside the install folder; refusing the whole asset")
    parts = [x for x in p.parts if x != "."]
    if not parts or parts[0] not in KEEP_TOP:
        return None
    return PurePosixPath(*parts)


def _real_parent(root: Path, rel: PurePosixPath) -> Path:
    """Create the parent folders of ``rel`` under ``root`` one by one, refusing to pass through a link."""
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        try:
            st = cur.lstat()
        except FileNotFoundError:
            cur.mkdir(mode=0o755)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise fail("UNSAFE_ARCHIVE", f"entry {rel} goes through a link or a file; refusing the whole asset")
    return cur / rel.parts[-1]


def _link_ok(rel: PurePosixPath, target: str) -> bool:
    """A symlink is kept only if it is relative and stays inside the folder it is in once resolved."""
    if not target or target.startswith("/") or "\\" in target or "\0" in target:
        return False
    parts: list[str] = list(rel.parts[:-1])
    for seg in target.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if len(parts) <= 1:  # would climb out of app/ or www/
                return False
            parts.pop()
        else:
            parts.append(seg)
    return len(parts) >= 1 and parts[0] in KEEP_TOP


def _write_file(path: Path, src: Any, size: int, executable: bool, budget: _Budget) -> None:
    budget.add(size)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if executable else 0o644)
    left = size
    with os.fdopen(fd, "wb") as fh:
        while left > 0:
            chunk = src.read(min(1 << 20, left))
            if not chunk:
                raise fail("UNSAFE_ARCHIVE", f"entry {path.name} is shorter than its header says; refusing")
            left -= len(chunk)
            fh.write(chunk)
    os.chmod(path, 0o755 if executable else 0o644)  # umask-independent; setuid/setgid/sticky can never be set


def _extract_tar(fileobj: Any, root: Path, mode: str) -> _Budget:
    budget = _Budget()
    links: list[tuple[Path, str]] = []
    with tarfile.open(fileobj=fileobj, mode=mode) as tf:  # type: ignore[call-overload]
        for m in tf:
            budget.member()
            rel = _clean_name(m.name)
            if rel is None:
                continue
            if m.isdir():
                (root / rel).mkdir(mode=0o755, parents=True, exist_ok=True)
            elif m.isreg():
                target = _real_parent(root, rel)
                src = tf.extractfile(m)
                if src is None:
                    raise fail("UNSAFE_ARCHIVE", f"entry {rel} cannot be read")
                _write_file(target, src, m.size, bool(m.mode & 0o111), budget)
            elif m.issym():
                if not _link_ok(rel, m.linkname):
                    raise fail(
                        "UNSAFE_ARCHIVE",
                        f"symlink {rel} -> {m.linkname[:60]!r} leaves the install folder; refusing the whole asset",
                    )
                links.append((_real_parent(root, rel), m.linkname))
            else:  # hard links, devices, fifos
                raise fail("UNSAFE_ARCHIVE", f"entry {rel} is a hard link, device or fifo; refusing the whole asset")
    for path, linkname in links:  # after the files, so no file could be written through a link made earlier
        os.symlink(linkname, path)
    return budget


def _extract_zip(fileobj: Any, root: Path) -> _Budget:
    budget = _Budget()
    with zipfile.ZipFile(fileobj) as zf:
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_UNPACKED or len(zf.infolist()) > MAX_MEMBERS:
            raise fail("ARCHIVE_TOO_LARGE", "the asset is too large once unpacked; refusing")
        for info in zf.infolist():
            budget.member()
            rel = _clean_name(info.filename)
            if rel is None:
                continue
            unix = info.external_attr >> 16
            if stat.S_ISLNK(unix):
                raise fail("UNSAFE_ARCHIVE", f"zip entry {rel} is a symlink; refusing the whole asset")
            if info.is_dir():
                (root / rel).mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if stat.S_IFMT(unix) not in (0, stat.S_IFREG):  # some zip tools write no file-type bits at all
                raise fail("UNSAFE_ARCHIVE", f"zip entry {rel} is not a regular file; refusing the whole asset")
            with zf.open(info) as src:
                _write_file(_real_parent(root, rel), src, info.file_size, bool(unix & 0o111), budget)
    return budget


class _Base64Lines(io.RawIOBase):
    """A readable stream of the bytes behind base64 lines (the installer payload), without loading it all."""

    def __init__(self, lines: Iterator[bytes]) -> None:
        self._lines = lines
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while len(self._buf) < len(b):
            line = next(self._lines, None)
            if line is None:
                break
            line = line.strip()
            if line:
                try:
                    self._buf += base64.b64decode(line, validate=True)
                except (binascii.Error, ValueError):
                    raise fail("UNSAFE_ARCHIVE", "the installer payload is not valid base64; refusing") from None
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def extract_asset(asset: Path, root: Path) -> dict[str, int]:
    """Unpack ``app/`` and ``www/`` of ``asset`` into ``root``. Returns ``{"files": n, "bytes": n}``; raises on anything unsafe."""
    with asset.open("rb") as fh:
        head = fh.read(4)
        fh.seek(0)
        if head[:2] == b"#!":  # the engine's installer script: payload after the marker line
            for line in fh:
                if line == PAYLOAD_MARK:
                    break
            else:
                raise fail("UNSAFE_ARCHIVE", "the installer has no payload marker (__CARGA_DEBAJO__); not an engine asset")
            budget = _extract_tar(io.BufferedReader(_Base64Lines(iter(fh))), root, "r|gz")
        elif head[:2] == b"\x1f\x8b":
            budget = _extract_tar(fh, root, "r|gz")
        elif head[:2] == b"PK":
            budget = _extract_zip(fh, root)
        else:
            raise fail("UNSAFE_ARCHIVE", "the asset is neither an installer script, a tar.gz nor a zip; refusing")
    return {"files": budget.members, "bytes": budget.bytes}


# ---- settings ----------------------------------------------------------------------------------------------------------
def _preflight_config(home: Path) -> None:
    """A broken config.yaml is found BEFORE downloading anything."""
    import yaml

    path = home / "config.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, yaml.YAMLError):
            raise InstallError(f"{path} is not valid YAML; fix it first (nothing was downloaded)") from None
        if not isinstance(data, dict):
            raise InstallError(f"{path} is not a YAML mapping; fix it first (nothing was downloaded)")


def set_engine_path(home: Path, engine_path: Path) -> str:
    """Write ``engine_path`` into the plugin settings. Returns how: ``hermes config set`` or ``yaml``."""
    hermes = shutil.which("hermes")
    if hermes:
        res = subprocess.run(
            [hermes, "config", "set", SETTING_KEY, str(engine_path)],
            env=_env({"HERMES_HOME": str(home)}),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if res.returncode == 0:
            return "hermes config set"
    import yaml

    path = home / "config.yaml"
    data: dict[str, Any] = {}
    mode = 0o600
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        mode = path.stat().st_mode & 0o777
        shutil.copy2(path, home / "config.yaml.hermes-3d-backup")
    entry = data.setdefault("plugins", {}).setdefault("entries", {}).setdefault(S.PLUGIN_ID, {})
    entry.setdefault("settings", {})["engine_path"] = str(engine_path)
    tmp = path.with_name("config.yaml.hermes-3d-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    return "yaml"


# ---- shortcuts ------------------------------------------------------------------------------------------------------------
def _desktop_escape(command: str) -> str:
    """Escape a shell command for the quoted argument of ``Exec=sh -c "..."`` in a .desktop file."""
    out = command.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`").replace("%", "%%")
    return out.replace("\\", "\\\\")  # the .desktop string layer unescapes once more


def shortcut_text(platform: str, hermes_bin: str, url: str, hermes_home: str | None) -> str:
    env = f"HERMES_HOME={shlex.quote(hermes_home)} " if hermes_home else ""
    if platform.startswith("win"):
        setter = f'set "HERMES_HOME={hermes_home}"\r\n' if hermes_home and not re.search(r'["%^&|<>]', hermes_home) else ""
        return f'@echo off\r\n{setter}"{hermes_bin}" 3d up\r\nstart {url}\r\n'
    if platform == "darwin":
        export = f"export HERMES_HOME={shlex.quote(hermes_home)}\n" if hermes_home else ""
        return f"#!/bin/bash\n{export}{shlex.quote(hermes_bin)} 3d up\nopen {shlex.quote(url)}\n"
    command = f"{env}{shlex.quote(hermes_bin)} 3d up && xdg-open {shlex.quote(url)}; read -r -p 'Press Enter to close ' _"
    return (
        "[Desktop Entry]\nType=Application\nName=Hermes 3D\n"
        "Comment=Start the Hermes world and open it in the browser\n"
        f'Exec=sh -c "{_desktop_escape(command)}"\nTerminal=true\nCategories=Utility;\n'
    )


def write_shortcut(platform: str, hermes_bin: str, url: str, hermes_home: str | None, home: Path | None = None) -> Path | None:
    """Linux: a .desktop file in the applications folder; macOS: a .command and Windows: a .bat on the Desktop."""
    home = home or Path.home()
    if platform.startswith("linux"):
        data = os.environ.get("XDG_DATA_HOME")
        target = (Path(data) if data else home / ".local" / "share") / "applications" / "hermes-3d.desktop"
        mode = 0o755
        platform = "linux"
    elif platform == "darwin":
        target, mode = home / "Desktop" / "Hermes 3D.command", 0o755
    elif platform.startswith("win"):
        target, mode = home / "Desktop" / "Hermes 3D.bat", 0o644
    else:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(shortcut_text(platform, hermes_bin, url, hermes_home), encoding="utf-8")
    target.chmod(mode)
    return target


# ---- the install -----------------------------------------------------------------------------------------------------------
def engine_dir(home: Path, lock: Lock) -> Path:
    return config.plugin_data_dir(home) / "engine" / lock.dir_name


def installed_marker(dest: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((dest / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_installed(dest: Path, lock: Lock) -> bool:
    """True when ``dest`` holds THIS pinned asset (same sha256) and its entry file is there."""
    data = installed_marker(dest)
    return bool(data and data.get("sha256") == lock.sha256 and (dest / "app" / lock.entry).is_file())


def install_engine(
    home: Path,
    *,
    lock_path: Path | None = None,
    url: str | None = None,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    out: Out = print,
    euid: int | None = None,
    platform: str | None = None,
    no_shortcut: bool = False,
    save_setting: bool = True,
    on_step: Step | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    refuse_root(euid)
    lock = load_lock(lock_path)
    validate_lock(lock)
    url = url or lock.url
    check_url(url)
    _preflight_config(home)

    dest = engine_dir(home, lock)
    root = dest.parent
    out(f"Release : {lock.tag} ({lock.asset})")
    out(f"From    : {url}")
    out(f"Size    : {lock.size_text}")
    out(f"sha256  : {lock.sha256}  (checked before anything is unpacked)")
    out(f"Into    : {dest}")
    reused = is_installed(dest, lock)
    t0 = time.monotonic()
    if reused:
        out("Already installed with that sha256. Nothing to download.")
    else:
        if not yes and (
            confirm is None or not confirm(f"Download {lock.size_text} from {urlsplit(url).netloc} and unpack it? [y/N] ")
        ):
            raise fail("NOT_CONFIRMED", "Not confirmed, so nothing was downloaded.")
        _fetch_and_unpack(lock, url, root, dest, out, on_step, progress)
    seconds = round(time.monotonic() - t0, 1)
    how = set_engine_path(home, dest) if save_setting else "not saved"
    out(f"Saved engine_path: {dest} (via {how})." if save_setting else f"Engine at {dest}.")
    shortcut = None
    if not no_shortcut:
        hermes_bin = shutil.which("hermes") or "hermes"
        page = S.Hermes3DSettings.from_yaml(home).page_url()
        shortcut = write_shortcut(platform or sys.platform, hermes_bin, page, os.environ.get("HERMES_HOME"))
        out(f"Shortcut: {shortcut}" if shortcut else "No shortcut created (this platform is not supported).")
    return {
        "engine_path": str(dest),
        "tag": lock.tag,
        "sha256": lock.sha256,
        "reused": reused,
        "seconds": seconds,
        "size_mb": _size_mb(dest),
        "shortcut": str(shortcut) if shortcut else None,
    }


def _size_mb(path: Path) -> float:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(base, name)).st_size
    return round(total / 1_000_000, 1)


def _fetch_and_unpack(
    lock: Lock,
    url: str,
    root: Path,
    dest: Path,
    out: Out,
    on_step: Step | None = None,
    progress: Callable[[int], None] | None = None,
) -> None:
    def step(name: str, **facts: Any) -> None:
        if on_step is not None:
            on_step(name, **facts)

    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    stage = root / f"{lock.dir_name}.partial"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(mode=0o700)
    try:
        out("Downloading ...")
        step("download_start")
        asset = stage / "asset"
        got = download(url, asset, expect_size=lock.size_bytes, expect_sha=lock.sha256, out=out, progress=progress)
        step("downloaded", bytes=got)
        try:
            sums = check_against_sums(url, lock.asset, lock.sha256, out=out)
        except InstallError:
            asset.unlink(missing_ok=True)
            raise
        step("verified", sums=sums)
        out("Unpacking app/ and www/ ...")
        step("unpack_start")
        stats = extract_asset(asset, stage)
        asset.unlink()
        entry = stage / "app" / lock.entry
        if not entry.is_file():
            raise fail("NOT_AN_ENGINE", f"the asset has no app/{lock.entry}; this is not the engine the lock expects")
        marker = {"schema": 1, "tag": lock.tag, "sha256": lock.sha256, "entry": lock.entry, "app": "app", "www": "www"}
        fd = os.open(stage / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        stage.chmod(0o700)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(stage, dest)
        out(f"  Unpacked {stats['files']} entries, {stats['bytes'] / 1_000_000:.1f} MB.")
        step("unpacked", files=stats["files"], bytes=stats["bytes"], path=str(dest))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    env.update(extra or {})
    return env

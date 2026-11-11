# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Installed-plugins reader (C9): the plugins found in the ``plugins/`` folder of every Hermes profile.

Source, per profile (the default home and every ``profiles/<name>``):

* ``<home>/plugins/<dir>/plugin.yaml`` (or ``plugin.json``): ``name``, ``version``, ``description``, ``kind``,
  and only the NUMBER of ``provides_tools`` / ``provides_hooks``;
* ``<home>/config.yaml``: only the lists ``plugins.enabled`` and ``plugins.disabled`` (state).

Strictly READ-ONLY and state only: no plugin code (``__init__.py`` etc.) is ever opened, imported or run, no
other file of the plugin folder is read (``.env``, ``mcp.json``, ``config``), no path leaves this module, and
``hermes plugins`` is not invoked. Manifests over 64 kB are ignored. Every string is cleaned and cut like the
rest of the plugin. Player-visible words are English.

Hermes loads a user plugin only when its name is in ``plugins.enabled`` (and not in ``plugins.disabled``); that
is what ``enabled`` means here. A plugin installed in several profiles is ONE entry (one plug) with one state
per profile.

``read_plugins(root)`` returns a plain, JSON-serialisable dict::

    {"schema": "hermes-3d/plugins@1", "label": "Plugins", "count": n, "installed": m, "enabled_somewhere": k,
     "plugins": [{"name", "version", "mixed_versions", "description", "kind", "tools", "hooks",
                  "profiles": [{"profile", "enabled"}], "enabled_in": j}],
     "error": ""}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .crontable import clean_text
from .scan import _EXPECTED, _NAME, _read_yaml, profile_dirs

_EXPECTED_VALUE: tuple[type[BaseException], ...] = (*_EXPECTED, ValueError)
SCHEMA = "hermes-3d/plugins@1"
LABEL = "Plugins"
MAX_PLUGINS = 200  # the world admits 200 things per zone
MAX_MANIFEST = 64 * 1024
NAME_CARD = 40
DESCRIPTION_CARD = 140
KINDS = ("standalone", "backend", "platform", "exclusive")


def _manifest(pdir: Path) -> dict[str, Any] | None:
    """The parsed manifest of one plugin folder (plugin.yaml, else plugin.json); ``None`` if there is none."""
    yml, js = pdir / "plugin.yaml", pdir / "plugin.json"
    try:
        if yml.is_file() and yml.stat().st_size <= MAX_MANIFEST:
            return _read_yaml(yml)
        if js.is_file() and js.stat().st_size <= MAX_MANIFEST:
            data = json.loads(js.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except _EXPECTED_VALUE:  # unreadable or malformed manifest: the plugin is still listed, without details
        return {}
    return None


def _names(cfg: dict[str, Any], key: str) -> set[str]:
    block = cfg.get("plugins")
    items = block.get(key) if isinstance(block, dict) else None
    return {str(x) for x in items} if isinstance(items, list) else set()


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def read_plugins(root: Path) -> dict[str, Any]:
    """Every plugin installed under ``<profile>/plugins/`` for every profile under ``root``. Never raises for odd files."""
    by_name: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for pname, pdir in profile_dirs(Path(root)):
        base = pdir / "plugins"
        if not base.is_dir():
            continue
        cfg = _read_yaml(pdir / "config.yaml")
        on, off = _names(cfg, "enabled"), _names(cfg, "disabled")
        try:
            entries = sorted(base.iterdir())
        except _EXPECTED as exc:
            errors.append(f"{pname}: {type(exc).__name__}")
            continue
        for d in entries:
            if not d.is_dir() or not _NAME.match(d.name):  # skips __pycache__, dotfiles, odd names
                continue
            man = _manifest(d)
            if man is None:  # a folder with no manifest is not a plugin
                continue
            name = clean_text(man.get("name") or d.name, NAME_CARD) or d.name
            version = clean_text(man.get("version") or "", 20)
            kind = clean_text(man.get("kind") or "standalone", 20).lower()
            entry = by_name.get(name)
            if entry is None:
                entry = by_name[name] = {
                    "name": name,
                    "version": version,
                    "mixed_versions": False,
                    "description": clean_text(man.get("description") or "", DESCRIPTION_CARD),
                    "kind": kind if kind in KINDS else "standalone",
                    "tools": _count(man.get("provides_tools")),
                    "hooks": _count(man.get("provides_hooks")),
                    "profiles": [],
                }
            elif version and entry["version"] and version != entry["version"]:
                entry["mixed_versions"] = True
            entry["profiles"].append(
                {"profile": pname, "enabled": (d.name in on or name in on) and not (d.name in off or name in off)}
            )
    plugins = sorted(by_name.values(), key=lambda p: p["name"])[:MAX_PLUGINS]
    for p in plugins:
        p["enabled_in"] = sum(1 for x in p["profiles"] if x["enabled"])
    return {
        "schema": SCHEMA,
        "label": LABEL,
        "count": len(plugins),
        "installed": sum(len(p["profiles"]) for p in plugins),
        "enabled_somewhere": sum(1 for p in plugins if p["enabled_in"]),
        "plugins": plugins,
        "error": "; ".join(errors)[:200],
    }

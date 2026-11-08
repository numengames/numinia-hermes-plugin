"""C9: the installed-plugins reader. Synthetic homes only; the run against the real Hermes is C9-evidence/run_c9.py."""

from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_palace_lib import enchufes, scan


def _plugin(home: Path, folder: str, manifest: str, extra: dict[str, str] | None = None) -> None:
    d = home / "plugins" / folder
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(manifest)
    (d / "__init__.py").write_text("raise RuntimeError('plugin code must never run')\n")
    for k, v in (extra or {}).items():
        (d / k).write_text(v)


def _home(tmp_path: Path) -> Path:
    root = tmp_path / ".hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "config.yaml").write_text("plugins:\n  enabled: [portero]\n  disabled: [homeassistant]\n")
    (root / "profiles" / "alpha" / "config.yaml").write_text("plugins:\n  enabled: [portero, blender, homeassistant]\n")
    _plugin(
        root, "portero", 'name: portero\nversion: "0.3.0"\ndescription: "Brakes\\nfor the dispatcher"\nprovides_hooks: [a, b]\n'
    )
    _plugin(root, "homeassistant", 'name: homeassistant\nversion: "2.0.1"\nkind: platform\n')
    _plugin(root / "profiles" / "alpha", "portero", 'name: portero\nversion: "0.4.0"\nprovides_tools: [t]\n')
    _plugin(root / "profiles" / "alpha", "homeassistant", 'name: homeassistant\nversion: "2.0.1"\nkind: platform\n')
    _plugin(root / "profiles" / "alpha", "blender", "x", {".env": "SECRET=1"})
    (root / "profiles" / "alpha" / "plugins" / "blender" / "plugin.yaml").unlink()
    (root / "profiles" / "alpha" / "plugins" / "blender" / "plugin.json").write_text(
        '{"name": "blender", "version": "0.4.1", "description": "Blender"}'
    )
    (root / "profiles" / "alpha" / "plugins" / "__pycache__").mkdir()
    (root / "profiles" / "alpha" / "plugins" / "nomanifest").mkdir()
    return root


def test_one_plug_per_plugin_with_a_state_per_profile(tmp_path):
    t = enchufes.read_plugins(_home(tmp_path))
    by = {p["name"]: p for p in t["plugins"]}
    assert set(by) == {"blender", "homeassistant", "portero"}  # no __pycache__, no folder without manifest
    assert t["count"] == 3 and t["installed"] == 5 and t["error"] == ""
    assert by["portero"]["profiles"] == [{"profile": "default", "enabled": True}, {"profile": "alpha", "enabled": True}]
    assert by["portero"]["mixed_versions"] is True and by["portero"]["enabled_in"] == 2
    assert by["portero"]["description"] == "Brakes for the dispatcher" and by["portero"]["hooks"] == 2
    assert by["homeassistant"]["kind"] == "platform"
    # disabled wins over enabled in the default home; alpha lists it as enabled
    assert [x["enabled"] for x in by["homeassistant"]["profiles"]] == [False, True]
    assert by["blender"]["version"] == "0.4.1" and by["blender"]["profiles"] == [{"profile": "alpha", "enabled": True}]
    assert t["enabled_somewhere"] == 3


def test_not_listed_means_not_enabled(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n")
    _plugin(root, "solo", "name: solo\nversion: 1\n")
    t = enchufes.read_plugins(root)
    assert t["plugins"][0]["profiles"] == [{"profile": "default", "enabled": False}] and t["enabled_somewhere"] == 0


def test_reads_state_only_never_runs_code_and_leaves_files_untouched(tmp_path):
    root = _home(tmp_path)

    def snap():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}

    before = snap()
    t = enchufes.read_plugins(root)  # would raise if an __init__.py were imported
    assert snap() == before
    text = repr(t)
    assert "SECRET" not in text and str(tmp_path) not in text and "RuntimeError" not in text


def test_no_plugins_dir_and_broken_manifest_do_not_raise(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("plugins: [not, a, dict]\n")
    assert enchufes.read_plugins(root)["count"] == 0
    _plugin(root, "bad", ":\n  - [unclosed\n")
    t = enchufes.read_plugins(root)
    assert [p["name"] for p in t["plugins"]] == ["bad"] and t["plugins"][0]["version"] == ""


def test_scan_has_the_enchufes_section_without_a_database(tmp_path):
    snap = scan.scan_hermes(_home(tmp_path), only={"enchufes"})
    sec = snap["sections"]["enchufes"]
    assert sec["ok"] and sec["count"] == 3 and "enchufes" not in scan.DB_SECTIONS

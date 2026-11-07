"""Plugin settings: config.yaml section -> typed values, precedence, validation, no env vars."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import LOOP, ROOT

from hermes_palace_lib import settings as S


def write_config(home: Path, settings: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"hermes-palace": {"settings": settings}}}}))


def test_defaults_when_nothing_is_configured(hermes_home):
    cfg = S.PalaceSettings.from_yaml(hermes_home)
    assert cfg.engine_path is None and cfg.engine_main() is None and cfg.www_dir() is None
    assert cfg.world_path == hermes_home / "plugin-data" / "hermes-palace" / "mundo"
    assert (cfg.engine_port, cfg.link_port, cfg.world_url) == (8790, 0, "")
    assert cfg.page_url() == f"http://{LOOP}:8790/"


def test_config_yaml_round_trip(hermes_home, tmp_path):
    engine = tmp_path / "engine"
    (engine / "packages/server/src").mkdir(parents=True)
    (engine / "packages/server/src/main.ts").write_text("//")
    (engine / "dist/plaza").mkdir(parents=True)
    (engine / "dist/plaza/index.html").write_text("<html>")
    write_config(
        hermes_home,
        {
            "engine_path": str(engine),
            "engine_port": 9000,
            "link_port": 9100,
            "world_url": "http://localhost:9000/x",
            "publish_cron": True,
            "cron_poll_seconds": 1,
            "scan_days": 9999,
        },
    )
    cfg = S.PalaceSettings.from_yaml(hermes_home)
    assert cfg.engine_main() == engine.resolve() / S.ENGINE_MAIN and cfg.www_dir() == engine.resolve() / S.ENGINE_WWW
    assert (cfg.engine_port, cfg.link_port, cfg.page_url()) == (9000, 9100, "http://localhost:9000/x")
    assert cfg.switches["cron"] is True and cfg.switches["salud"] is True
    assert cfg.poll_seconds == S.POLL_SECONDS_MIN and cfg.scan_days == 365  # clamped, documented


def test_ctx_getter_and_yaml_reader_agree(hermes_home):
    write_config(hermes_home, {"engine_port": 9001, "publish_sessions": "on"})
    from_file = S.PalaceSettings.from_getter(S.yaml_getter(hermes_home), hermes_home)
    values = {"engine_port": 9001, "publish_sessions": "on"}
    from_ctx = S.PalaceSettings.from_getter(lambda k, d=None: values.get(k, d), hermes_home)
    assert from_file == from_ctx


def test_old_environment_variables_are_ignored(hermes_home, monkeypatch, tmp_path):
    for name in ("ENGINE", "WORLD", "ENGINE_PORT", "LINK_PORT", "WORLD_URL"):
        monkeypatch.setenv(f"HERMES_PALACE_{name}", "garbage:///")
    cfg = S.PalaceSettings.from_yaml(hermes_home)
    assert cfg.engine_path is None and cfg.engine_port == 8790 and cfg.world_url == ""


def test_only_the_link_key_is_read_from_the_environment():
    """Allow-list: the secret is the one HERMES_PALACE_* variable in the code base."""
    found = set()
    for path in [ROOT / "__init__.py", ROOT / "dashboard" / "plugin_api.py", *(ROOT / "hermes_palace_lib").glob("*.py")]:
        found |= set(re.findall(r"HERMES_PALACE_[A-Z_]+", path.read_text(encoding="utf-8")))
    assert found == {"HERMES_PALACE_LINK_KEY"}


@pytest.mark.parametrize(
    "key,value",
    [
        ("engine_port", 80),
        ("engine_port", "abc"),
        ("engine_port", True),
        ("link_port", 70000),
        ("world_url", "http://evil.example:80/"),
        ("world_url", f"http://{LOOP}:80@evil.com/"),
        ("world_url", f"https://{LOOP}:80/"),
        ("world_url", "http://127.0.0.1/"),
        ("world_path", "relative/dir"),
        ("world_path", "/tmp/a/../b"),
        ("engine_path", "/"),
    ],
)
def test_invalid_values_name_the_key(hermes_home, key, value):
    write_config(hermes_home, {key: value})
    with pytest.raises(S.SettingsError, match=key):
        S.PalaceSettings.from_yaml(hermes_home)


def test_world_path_refuses_symlink_home_and_parents(hermes_home, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    for bad in (link, hermes_home, hermes_home.parent, Path("/")):
        write_config(hermes_home, {"world_path": str(bad)})
        with pytest.raises(S.SettingsError, match="world_path"):
            S.PalaceSettings.from_yaml(hermes_home)
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(hermes_home, {"world_path": str(tmp_path)})
    with pytest.raises(S.SettingsError, match="world_path"):
        S.PalaceSettings.from_yaml(hermes_home)
    write_config(hermes_home, {"world_path": str(real / "w")})
    assert S.PalaceSettings.from_yaml(hermes_home).world_path == (real / "w").resolve()


def test_unreadable_config_yaml_is_logged_and_defaults_apply(hermes_home, caplog):
    (hermes_home / "config.yaml").write_text("plugins: [unclosed")
    with caplog.at_level("WARNING", logger="hermes_palace.settings"):
        cfg = S.PalaceSettings.from_yaml(hermes_home)
    assert cfg.engine_port == 8790 and "cannot read plugin settings" in caplog.text

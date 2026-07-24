"""Tests for scripts/setup-browser.py.

The script points the `default` browser profile at a system Chrome/Chromium,
which is the supported fallback when the bundled-component downloader is not
published yet (crates/server/src/browser/component.rs COMPONENT_MANIFEST). These
tests exercise detection and the DB write against a temporary sqlite DB with a
minimal browser_profiles table (only the columns the script touches).
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "setup-browser.py"


def load_script():
    # The filename has a hyphen, so it can't be imported normally; load it by
    # path under a normal module name.
    spec = importlib.util.spec_from_file_location("setup_browser_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["setup_browser_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """A temp DB seeded with a `default` profile like the real migration does."""
    db = tmp_path / "agentgpt.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE browser_profiles ("
        "id TEXT PRIMARY KEY, name TEXT, engine TEXT, executable_path TEXT, "
        "profile_path TEXT, created_at TEXT, updated_at TEXT, last_used_at TEXT)"
    )
    conn.execute(
        "INSERT INTO browser_profiles (id, name, engine, executable_path, profile_path) "
        "VALUES ('default', 'Default', 'bundled_chromium', NULL, '/tmp/profile')"
    )
    conn.commit()
    conn.close()
    return db


def read_profile(db: Path) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT engine, executable_path FROM browser_profiles WHERE id = 'default'"
    ).fetchone()
    conn.close()
    return (row[0], row[1])


def test_set_executable_updates_engine_and_path(tmp_db: Path) -> None:
    mod = load_script()
    mod.set_executable(tmp_db, "/usr/bin/google-chrome")
    engine, exe = read_profile(tmp_db)
    assert engine == "system"
    assert exe == "/usr/bin/google-chrome"


def test_set_executable_is_idempotent_and_refreshes(tmp_db: Path) -> None:
    mod = load_script()
    mod.set_executable(tmp_db, "/path/a/chrome")
    mod.set_executable(tmp_db, "/path/b/chrome")  # second write overwrites
    assert read_profile(tmp_db) == ("system", "/path/b/chrome")


def test_detect_browser_returns_none_when_nothing_exists(monkeypatch) -> None:
    mod = load_script()
    # Point every candidate at a nonexistent path so detection finds nothing.
    monkeypatch.setattr(mod, "CANDIDATES", {"linux": ["/nope/a", "/nope/b"], "darwin": [], "win32": []})
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod.detect_browser() is None


def test_detect_browser_returns_first_existing(monkeypatch, tmp_path: Path) -> None:
    mod = load_script()
    real = tmp_path / "chrome"
    real.write_text("x")
    monkeypatch.setattr(
        mod, "CANDIDATES", {"linux": ["/missing", str(real), str(tmp_path / "other")], "darwin": [], "win32": []}
    )
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod.detect_browser() == str(real)


def test_database_path_defaults_to_repo_appdata(monkeypatch) -> None:
    mod = load_script()
    monkeypatch.delenv("AGENTGPT_DATA_DIR", raising=False)
    p = mod.database_path()
    assert p.name == "agentgpt.sqlite3"
    assert "app-data" in p.parts


def test_database_path_honors_data_dir(monkeypatch, tmp_path: Path) -> None:
    mod = load_script()
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    assert mod.database_path() == tmp_path / "agentgpt.sqlite3"

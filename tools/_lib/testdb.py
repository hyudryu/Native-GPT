"""Test helper: build a scratch app database from the real migrations.

Applies every SQL file in `crates/server/migrations/` in filename order to
a temp SQLite file, so tool tests run against the production schema (schema
drift breaks tests, not users). `schema_migrations` bookkeeping is a Rust
concern; the raw SQL files are self-contained.

Usage:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
    from testdb import create_test_db

    @pytest.fixture()
    def mod(monkeypatch, tmp_path):
        create_test_db(tmp_path)
        monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
        ...
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def migrations_dir() -> Path:
    """Repo-root crates/server/migrations, resolved from this file's path."""
    return Path(__file__).resolve().parents[2] / "crates" / "server" / "migrations"


def create_test_db(directory: Path) -> Path:
    """Create `<directory>/agentgpt.sqlite3` with all migrations applied."""
    db_file = Path(directory) / "agentgpt.sqlite3"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for sql_file in sorted(migrations_dir().glob("*.sql")):
            conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    return db_file

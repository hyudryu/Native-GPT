"""Shared SQLite access for database-backed tools.

The Rust host owns the app database (rusqlite, WAL mode, foreign keys on).
Python tools open the same file directly with the stdlib `sqlite3` module;
WAL permits concurrent readers/writers across processes. Connections use a
busy timeout so a writer never fails immediately on host-side locks.

Path resolution mirrors `crates/server/src/db.rs::default_path`:
`AGENTGPT_DATA_DIR` when set, else `<repo_root>/app-data/database/`.
Loaded by file path from `tools/<id>/tool.py` (no package context).
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path

_LIB_PATH = Path(__file__).resolve().parent / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)

DB_FILENAME = "agentgpt.sqlite3"
BUSY_TIMEOUT_MS = 5000


def db_path() -> Path:
    """Resolve the SQLite database path (host convention).

    `AGENTGPT_DATA_DIR` points at the directory holding the database file;
    otherwise the file lives under `<repo_root>/app-data/database/`.
    """
    data_dir = os.environ.get("AGENTGPT_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).resolve() / DB_FILENAME
    return _paths.repo_root() / "app-data" / "database" / DB_FILENAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the app database with host-compatible pragmas.

    WAL + busy_timeout + foreign keys, Row factory for dict-like access.
    Raises FileNotFoundError when the database has not been created yet
    (the Rust host creates it on first launch).
    """
    target = (path or db_path()).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"database not found: {target}")
    conn = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def project_id_for_conversation(
    conn: sqlite3.Connection, conversation_id: str | None
) -> str | None:
    """Resolve the project a conversation belongs to (None when unscoped).

    The `conversations` table (migration 0002) carries a nullable
    `project_id`; conversations outside any project return None.
    """
    if not conversation_id:
        return None
    row = conn.execute(
        "SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        return None
    return row["project_id"]

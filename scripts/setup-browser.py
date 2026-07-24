#!/usr/bin/env python3
"""Point the Native GPT Browser at a system Chrome/Chromium so it can run.

The in-app "Install Browser" flow downloads a bundled Chromium component, but
that packaging pipeline is not published yet (the manifest URL is a placeholder,
see crates/server/src/browser/component.rs COMPONENT_MANIFEST). Until it ships,
the browser can instead launch a system-installed Chrome/Chromium if the
`default` browser profile's `executable_path` is set — `start_inner` in
crates/server/src/browser/manager.rs uses it as the fallback when no bundled
component is present.

This script detects a system Chrome/Chromium/Edge (Chromium-based) and writes
its path into the `default` profile, which clears `BROWSER_NOT_INSTALLED`.
It is idempotent: re-running it refreshes the path only when it finds a
browser, and does nothing if the profile already points at a valid executable.

Usage:
    python scripts/setup-browser.py            # auto-detect + set
    python scripts/setup-browser.py --status   # just report current state
    python scripts/setup-browser.py --path "C:/path/to/chrome.exe"

Run it while the host is stopped (or stopped/started after — SQLite WAL tolerates
concurrent access, but restart the host so it re-reads the profile).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# ---- repo + DB location ----

# Match crates/server/src/db.rs default_path(): AGENTGPT_DATA_DIR if set,
# otherwise <repo_root>/app-data/database/agentgpt.sqlite3. The repo root is the
# parent of this script's `scripts/` directory.
REPO_ROOT = Path(__file__).resolve().parent.parent


def database_path() -> Path:
    data_dir = os.environ.get("AGENTGPT_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "agentgpt.sqlite3"
    return REPO_ROOT / "app-data" / "database" / "agentgpt.sqlite3"


# ---- browser detection ----

# Candidate executables per platform, in preference order. Edge and Brave are
# Chromium-based and work for CDP/screencast. Only the first existing file is
# used. (On Windows these are the standard install locations; `chrome.exe` lives
# under `Application/` and the major version directory varies, so we search the
# fixed `Application/` path which is stable across versions.)
CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
        r"C:\Program Files\Chromium\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/usr/bin/brave-browser",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ],
}


def detect_browser() -> str | None:
    """Return the first existing candidate executable path, or None."""
    # sys.platform values: win32, darwin, linux (and others). Fall back to the
    # linux list for anything unrecognized on a unix-like system.
    key = "linux"
    if sys.platform.startswith("win"):
        key = "win32"
    elif sys.platform == "darwin":
        key = "darwin"
    for candidate in CANDIDATES.get(key, CANDIDATES["linux"]):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


# ---- DB read/write ----

DEFAULT_PROFILE_ID = "default"


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"database not found: {db_path}\nrun the host once to initialize it")
    # mode=ro for the status check path; writes re-open rwc below. WAL mode
    # allows this even while the host holds the DB open.
    return sqlite3.connect(str(db_path))


def current_profile(conn: sqlite3.Connection) -> tuple[str | None, str | None] | None:
    row = conn.execute(
        "SELECT engine, executable_path FROM browser_profiles WHERE id = ?",
        (DEFAULT_PROFILE_ID,),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def set_executable(db_path: Path, exe: str) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "UPDATE browser_profiles SET executable_path = ?, engine = 'system', "
            "updated_at = ? WHERE id = ?",
            (exe, now, DEFAULT_PROFILE_ID),
        ).rowcount
        conn.commit()
        if n == 0:
            sys.exit(f"profile '{DEFAULT_PROFILE_ID}' not found in the database")
    finally:
        conn.close()


# ---- CLI ----

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Point Native GPT Browser at a system Chrome/Chromium.",
    )
    parser.add_argument(
        "--path",
        help="explicit executable path (skips auto-detection)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print the current profile config and exit (no changes)",
    )
    args = parser.parse_args()

    db = database_path()

    if args.status:
        conn = connect(db)
        prof = current_profile(conn)
        conn.close()
        if prof is None:
            print(f"profile '{DEFAULT_PROFILE_ID}' not found in {db}")
            return 1
        engine, exe = prof
        print(f"database:      {db}")
        print(f"profile:       {DEFAULT_PROFILE_ID}")
        print(f"engine:        {engine}")
        print(f"executable:    {exe or '(none — bundled component required)'}")
        if exe and not os.path.isfile(exe):
            print(f"WARNING: configured executable does not exist: {exe}")
        elif exe:
            print("status:        system browser configured")
        else:
            print("status:        no browser configured — run without --status to set one")
        return 0

    # Resolve the target executable: explicit path or auto-detect.
    if args.path:
        exe = args.path
        if not os.path.isfile(exe):
            sys.exit(f"executable not found: {exe}")
    else:
        exe = detect_browser()
        if exe is None:
            print("No system Chrome/Chromium/Edge found in standard locations.")
            print("Install Google Chrome, then re-run, or pass an explicit path:")
            print("  python scripts/setup-browser.py --path \"/path/to/chrome\"")
            return 1

    # Idempotency: skip the write if already pointing at this exact path.
    conn = connect(db)
    prof = current_profile(conn)
    conn.close()
    if prof is None:
        sys.exit(f"profile '{DEFAULT_PROFILE_ID}' not found in {db}")
    if prof[1] == exe:
        print(f"Already configured: {exe}")
        print("Restart the host to pick up the profile if it's running.")
        return 0

    set_executable(db, exe)
    print(f"Set browser executable: {exe}")
    print(f"Profile '{DEFAULT_PROFILE_ID}' engine -> 'system'.")
    print("Restart the host (it caches the profile) for the change to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

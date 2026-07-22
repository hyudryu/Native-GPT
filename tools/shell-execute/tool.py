"""Shell execute Strands tool — approval-gated, soft-sandboxed subprocess.

Runs a single shell command in the repo root under a subprocess with:
  - `env` cleared and re-populated from a minimal allowlist (no API keys,
    no user env vars leak).
  - `cwd` set to the repo root.
  - `timeout` enforced (default 60s).

`shell=True` is used, which means cmd.exe on Windows and /bin/sh on POSIX.
The exact command is surfaced to the user BEFORE it runs via the
`requires_approval` manifest flag → the runtime's HumanInTheLoop intervention
prompts the UI and the user sees the command before approving.

Honest scope: same as python_execute — NOT a true sandbox. The approval gate
is the real safety control.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from strands import tool

# Keep in sync with python_execute's allowlist — the soft-sandbox policy is
# shared across execution tools. Duplicated (not imported) so this tool stays
# standalone.
ENV_ALLOWLIST = {
    "PATH",
    "SYSTEMROOT",
    "TEMP", "TMP",
    "TZ", "LC_ALL", "LANG",
    "AGENTGPT_REPO_ROOT",
    "USERPROFILE", "APPDATA",  # cmd.exe on Windows needs these to start
    "HOME",  # POSIX shells need this for ~/.profile lookups
}

DEFAULT_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 8 * 1024  # 8 KB cap on the command itself


class ShellExecuteError(ValueError):
    """Raised for any shell_execute failure (size cap, spawn failure)."""


def _build_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}


def _repo_root() -> Path:
    configured = os.environ.get("AGENTGPT_REPO_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path.cwd().resolve()


def run_command(
    command: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | Path | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Run `command` via the shell. Standard-schema result dict.

    `runner` is injectable for tests (defaults to subprocess.run).
    """
    if not isinstance(command, str) or not command.strip():
        raise ShellExecuteError("command must be a non-empty string")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ShellExecuteError(
            f"command exceeds {MAX_COMMAND_BYTES} byte cap"
        )
    try:
        timeout_int = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ShellExecuteError(
            f"timeout_seconds must be an integer: {timeout_seconds!r}"
        ) from exc
    if timeout_int < 1 or timeout_int > 600:
        raise ShellExecuteError("timeout_seconds must be between 1 and 600")

    workdir = str(cwd) if cwd is not None else str(_repo_root())
    env = _build_env()
    run = runner or subprocess.run
    started = time.monotonic()
    try:
        completed = run(
            command,
            shell=True,
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=timeout_int,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "summary": f"timed out after {timeout_int}s",
            "data": {
                "stdout": (exc.stdout or b"").decode("utf-8", errors="replace")
                    if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                "stderr": (exc.stderr or b"").decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                "exit_code": None,
                "duration_ms": duration,
                "timed_out": True,
                "command": command,
            },
            "error": {"code": "timeout", "message": f"exceeded {timeout_int}s"},
        }
    duration = int((time.monotonic() - started) * 1000)

    stdout = (completed.stdout or "")[:MAX_OUTPUT_BYTES]
    stderr = (completed.stderr or "")[:MAX_OUTPUT_BYTES]
    truncated = (
        len(completed.stdout or "") > MAX_OUTPUT_BYTES
        or len(completed.stderr or "") > MAX_OUTPUT_BYTES
    )
    ok = completed.returncode == 0

    return {
        "ok": ok,
        "summary": (
            f"exited {completed.returncode} in {duration}ms"
            if ok
            else f"failed (exit {completed.returncode}) in {duration}ms"
        ),
        "data": {
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": completed.returncode,
            "duration_ms": duration,
            "truncated": truncated,
        },
        "error": None if ok else {
            "code": "nonzero_exit",
            "message": f"process exited with code {completed.returncode}",
        },
    }


@tool
def shell_execute(
    command: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a single shell command in the repository root.

    Uses `cmd.exe` on Windows and `/bin/sh` on POSIX. REQUIRES user approval
    before each call — the user sees the exact command before it runs. The
    environment is cleared (no API keys leak). A timeout is enforced.

    Use this for git, package managers, compilers, tests, and other
    command-line workflows that aren't covered by the more specific tools.

    Args:
        command: A single shell command. Caps at 8 KB. Multi-line commands
            work but each call should be one logical operation.
        timeout_seconds: Max execution time (1-600, default 60).

    Returns:
        `{ok, summary, data: {command, stdout, stderr, exit_code, duration_ms,
        truncated}, error}`. `ok=false` if the process exited non-zero or
        timed out.
    """

    try:
        return run_command(command, timeout_seconds=timeout_seconds)
    except ShellExecuteError as exc:
        return {
            "ok": False,
            "summary": "Command rejected before execution",
            "data": {"command": command},
            "error": {"code": "exec_error", "message": str(exc)},
        }


# Manifest flag consumed by the runtime's approval wiring.
REQUIRES_APPROVAL = True

TOOL = shell_execute

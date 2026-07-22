"""Python execute Strands tool — soft-sandboxed subprocess execution.

Runs user-provided Python code in a freshly-created temp workspace under a
subprocess with:
  - `env` cleared and re-populated from a minimal allowlist (no API keys,
    no user env vars leak).
  - `cwd` set to the temp workspace (cleaned up after).
  - `timeout` enforced (default 30s).
  - When `network == "none"`: a bootstrap prepended to the user code that
    raises on any `socket.socket(...)` call. This is REAL but TRIVIALLY
    BYPASSABLE (the subprocess can `del socket.socket`); the actual safety
    control is the `requires_approval` manifest flag, which prompts the user
    to review the code before it runs.

Honest scope: this is NOT a true sandbox. A malicious script that has
compromised the model can still read /etc/passwd, fork, re-import socket,
etc. The two genuine protections are (1) cleared env so no secrets leak
into the subprocess, and (2) the human approval gate so the user sees the
code before approving it. Real OS sandboxing (Job Objects on Windows,
namespaces on Linux) is Phase 2c.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from strands import tool

# Env vars we keep in the subprocess. Deliberately minimal — no user secrets,
# no API keys. Anything not on this list is dropped.
ENV_ALLOWLIST = {
    "PATH",  # so the subprocess can find its own executables
    "SYSTEMROOT",  # Windows — Python won't start without it
    "TEMP", "TMP",  # tempdir
    "TZ", "LC_ALL", "LANG",  # locale + timezone for datetime ops
    "AGENTGPT_REPO_ROOT",  # so the inner Python can resolve paths if needed
}

DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB cap on stdout/stderr
MAX_CODE_BYTES = 64 * 1024  # 64 KB cap on the submitted code itself

# Bootstrap prepended to user code when network="none". Imports the socket
# module and replaces socket.socket with a class that always raises. The user
# code can still bypass this (del socket.socket; import socket as s; ...) —
# that's why this is a soft sandbox and the real control is the approval gate.
_NETWORK_BLOCK_BOOTSTRAP = """import socket as _agentgpt_blocked_socket

class _AgentGPTBlockedSocket:
    def __init__(self, *args, **kwargs):
        raise PermissionError("network access is disabled in this sandbox")

_agentgpt_blocked_socket.socket = _AgentGPTBlockedSocket
del _agentgpt_blocked_socket
"""


class PythonExecuteError(ValueError):
    """Raised for any python_execute failure (size cap, spawn failure)."""


def _build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    # Always provide a writable temp dir.
    env.setdefault("TEMP", tempfile.gettempdir())
    env.setdefault("TMP", tempfile.gettempdir())
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            env[key] = value
    return env


def run_code(
    code: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    network: str = "none",
    input_files: dict[str, str] | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Execute `code` in a subprocess. Standard-schema result dict.

    `runner` is injectable for tests (defaults to subprocess.run). It must
    match the `subprocess.run` signature.
    """
    if not isinstance(code, str) or not code.strip():
        raise PythonExecuteError("code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise PythonExecuteError(
            f"code exceeds {MAX_CODE_BYTES} byte cap (got {len(code.encode('utf-8'))})"
        )
    try:
        timeout_int = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise PythonExecuteError(
            f"timeout_seconds must be an integer: {timeout_seconds!r}"
        ) from exc
    if timeout_int < 1 or timeout_int > 600:
        raise PythonExecuteError("timeout_seconds must be between 1 and 600")

    workspace = Path(tempfile.mkdtemp(prefix="agentgpt-py-"))
    try:
        # Stage any input files the caller wants available.
        if input_files:
            for name, content in input_files.items():
                if not isinstance(name, str) or not isinstance(content, str):
                    continue
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        full_code = (_NETWORK_BLOCK_BOOTSTRAP if network == "none" else "") + code
        script = workspace / "_user_code.py"
        script.write_text(full_code, encoding="utf-8")

        env = _build_env()
        run = runner or subprocess.run
        started = time.monotonic()
        try:
            completed = run(
                [sys.executable, "-I", "-c", full_code],
                cwd=str(workspace),
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
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": completed.returncode,
                "duration_ms": duration,
                "truncated": truncated,
                "workspace_files": [
                    p.name for p in workspace.iterdir() if p.name != "_user_code.py"
                ],
            },
            "error": None if ok else {
                "code": "nonzero_exit",
                "message": f"process exited with code {completed.returncode}",
            },
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@tool
def python_execute(
    code: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run Python code in an isolated subprocess.

    The code runs in a fresh temp workspace with a minimal environment (no API
    keys, no user env vars). Network access is blocked by default. A timeout
    is enforced (default 30s, max 600s). REQUIRES user approval before each
    call — the user sees the code you want to run before it executes.

    Use this for math, data analysis, file transformation, or any computation
    that benefits from a real Python runtime. Avoid using it for tasks that
    the deterministic tools (calculate, search_files, read_file) already cover.

    Args:
        code: Python source code to execute. Caps at 64 KB.
        timeout_seconds: Max execution time (1-600, default 30).

    Returns:
        `{ok, summary, data: {stdout, stderr, exit_code, duration_ms,
        workspace_files}, error}`. `ok=false` if the process exited non-zero
        or timed out.
    """

    try:
        return run_code(code, timeout_seconds=timeout_seconds, network="none")
    except PythonExecuteError as exc:
        return {
            "ok": False,
            "summary": "Code rejected before execution",
            "data": {},
            "error": {"code": "exec_error", "message": str(exc)},
        }


# Manifest flag consumed by the runtime's approval wiring (see
# apps/agent-runtime/src/agentgpt_runtime/chat.py). Mirrors the
# `requires_approval: true` in manifest.json.
REQUIRES_APPROVAL = True

TOOL = python_execute

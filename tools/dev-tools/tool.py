"""Dev Tools Strands tools — run/inspect project tests, linters, formatters.

Multi-tool folder: `TOOL` is a list of Strands tools. Execution mirrors
`tools/shell-execute/tool.py`: shell subprocess with a sanitized environment
(minimal allowlist, no API keys), cwd confined to the allowed roots via
`tools/_lib/paths.py`, enforced timeouts, and capped output. The manifest
sets requires_approval=true and risk="execute" — these tools run arbitrary
project commands, so the runtime prompts the user before each call.

Auto-detection (used when `command` is omitted):
  tests      pytest (pyproject.toml/pytest.ini/setup.cfg) -> cargo test
             (Cargo.toml) -> pnpm test / npm test (package.json)
  linter     ruff check (ruff config) -> eslint / npm run lint -> cargo clippy
  formatter  ruff format -> prettier -> cargo fmt

`inspect_build_errors` parses combined output best-effort for rustc
(`error[E…]:` + `--> file:line:col`), pytest (`FAILED path::test - msg`),
tsc (`file(line,col): error TSxxxx: msg`), and ruff/eslint/gcc-style
(`file:line:col: CODE msg`) diagnostics.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/paths.py` as a module (no package context when the
# runtime imports this file standalone).
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

# Keep in sync with tools/shell-execute's allowlist — the soft-sandbox policy
# is shared across execution tools.
ENV_ALLOWLIST = {
    "PATH",
    "SYSTEMROOT",
    "TEMP", "TMP",
    "TZ", "LC_ALL", "LANG",
    "AGENTGPT_REPO_ROOT",
    "USERPROFILE", "APPDATA",
    "HOME",
}

MAX_OUTPUT_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 8 * 1024
OUTPUT_TAIL_BYTES = 8 * 1024
MAX_ERRORS = 200


class DevToolError(ValueError):
    """Any dev-tools failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _failure(
    code: str, summary: str, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "summary": summary,
        "data": data or {},
        "error": {"code": code, "message": message},
    }


def _build_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}


def _repo(repository_path: str | None) -> Path:
    if repository_path is None:
        return _paths.repo_root()
    if not isinstance(repository_path, str) or not repository_path.strip():
        raise DevToolError("invalid_repository", "repository_path must be a non-empty string")
    resolved = resolve_under_root(repository_path)
    if not resolved.is_dir():
        raise DevToolError("invalid_repository", f"not a directory: {repository_path}")
    return resolved


def _validate_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise DevToolError("invalid_command", "command must be a non-empty string")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise DevToolError("invalid_command", f"command exceeds {MAX_COMMAND_BYTES} byte cap")
    return command


def _validate_timeout(value: Any, maximum: int = 1800) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise DevToolError("invalid_timeout", f"timeout must be an integer: {value!r}") from None
    if timeout < 1 or timeout > maximum:
        raise DevToolError("invalid_timeout", f"timeout must be 1..{maximum} seconds")
    return timeout


def _run(command: str, cwd: Path, timeout: int) -> dict[str, Any]:
    """Run `command` via the shell; returns a run record dict."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            env=_build_env(),
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        return {
            "exit_code": None,
            "stdout": stdout[-MAX_OUTPUT_BYTES:],
            "stderr": stderr[-MAX_OUTPUT_BYTES:],
            "timed_out": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[-MAX_OUTPUT_BYTES:],
        "stderr": (completed.stderr or "")[-MAX_OUTPUT_BYTES:],
        "timed_out": False,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# ── command detection ───────────────────────────────────────────────────────


def _has_ruff_config(repo: Path) -> bool:
    if (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        return True
    pyproject = repo / "pyproject.toml"
    return pyproject.is_file() and "[tool.ruff]" in pyproject.read_text(
        encoding="utf-8", errors="replace"
    )


def _has_prettier_config(repo: Path) -> bool:
    for name in (".prettierrc", ".prettierrc.json", ".prettierrc.yml", ".prettierrc.js",
                 "prettier.config.js", "prettier.config.mjs"):
        if (repo / name).is_file():
            return True
    package = repo / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return "prettier" in data or "prettier" in data.get("devDependencies", {})
    return False


def _npm_runner(repo: Path) -> str:
    return "pnpm" if (repo / "pnpm-lock.yaml").is_file() else "npm"


def _detect_test_command(repo: Path) -> str:
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file() or (repo / "setup.cfg").is_file():
        return "python -m pytest -q"
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "package.json").is_file():
        return f"{_npm_runner(repo)} test"
    raise DevToolError(
        "detection_failed",
        "could not detect a test runner (no pyproject.toml/pytest.ini, "
        "Cargo.toml, or package.json); pass an explicit command",
    )


def _detect_lint_command(repo: Path) -> str:
    if _has_ruff_config(repo):
        return "ruff check ."
    for name in (".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.cjs",
                 "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"):
        if (repo / name).is_file():
            return "npx eslint ."
    package = repo / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "lint" in scripts:
            return f"{_npm_runner(repo)} run lint"
    if (repo / "Cargo.toml").is_file():
        return "cargo clippy --all-targets"
    raise DevToolError(
        "detection_failed",
        "could not detect a linter (no ruff/eslint config, lint script, or "
        "Cargo.toml); pass an explicit command",
    )


def _detect_format_command(repo: Path) -> str:
    if _has_ruff_config(repo):
        return "ruff format ."
    if _has_prettier_config(repo):
        return "npx prettier --write ."
    package = repo / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "format" in scripts:
            return f"{_npm_runner(repo)} run format"
    if (repo / "Cargo.toml").is_file():
        return "cargo fmt"
    raise DevToolError(
        "detection_failed",
        "could not detect a formatter (no ruff/prettier config, format "
        "script, or Cargo.toml); pass an explicit command",
    )


def _command_result(kind: str, command: str, run: dict[str, Any], cwd: Path) -> dict[str, Any]:
    data = {
        "kind": kind,
        "command": command,
        "repository_path": str(cwd),
        "exit_code": run["exit_code"],
        "duration_ms": run["duration_ms"],
        "stdout": run["stdout"],
        "stderr": run["stderr"],
        "timed_out": run["timed_out"],
    }
    if run["timed_out"]:
        return _failure("timeout", f"{kind} timed out", "process exceeded its timeout", data)
    if run["exit_code"] != 0:
        return _failure(
            "nonzero_exit",
            f"{kind} failed (exit {run['exit_code']})",
            f"`{command}` exited with code {run['exit_code']}",
            data,
        )
    return _result(f"{kind} passed in {run['duration_ms']}ms", data)


# ── error parsing (inspect_build_errors) ────────────────────────────────────

_RE_RUSTC_HEADER = re.compile(r"^(error|warning)(\[E\d+\])?:\s*(.+)$")
_RE_RUSTC_LOCATION = re.compile(r"^\s*-->\s+(.+?):(\d+):(\d+)\s*$")
_RE_TSC = re.compile(r"^(.+?)\((\d+),(\d+)\):\s*(error|warning)\s+([A-Z]+\d+):\s*(.+)$")
_RE_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+?)(::\S+)?\s+-\s+(.+)$")
_RE_GENERIC = re.compile(
    r"^([A-Za-z0-9_./\\\-]+\.[A-Za-z0-9]+):(\d+):(\d+):\s*"
    r"(?:(error|warning):?\s*)?([A-Z]+\d+)?:?\s*(.+)$"
)


def _parse_build_errors(output: str) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines) and len(errors) < MAX_ERRORS:
        line = lines[i]
        rustc = _RE_RUSTC_HEADER.match(line)
        if rustc:
            severity, code, message = rustc.group(1), rustc.group(2), rustc.group(3)
            location = None
            # rustc prints the `--> file:line:col` within the next few lines.
            for lookahead in lines[i + 1 : i + 4]:
                matched = _RE_RUSTC_LOCATION.match(lookahead)
                if matched:
                    location = matched
                    break
            errors.append(
                {
                    "file": location.group(1) if location else None,
                    "line": int(location.group(2)) if location else None,
                    "column": int(location.group(3)) if location else None,
                    "severity": severity,
                    "code": code.strip("[]") if code else None,
                    "message": message.strip(),
                    "source": "rustc",
                    "raw": line.strip(),
                }
            )
            i += 1
            continue
        tsc = _RE_TSC.match(line)
        if tsc:
            errors.append(
                {
                    "file": tsc.group(1),
                    "line": int(tsc.group(2)),
                    "column": int(tsc.group(3)),
                    "severity": tsc.group(4),
                    "code": tsc.group(5),
                    "message": tsc.group(6).strip(),
                    "source": "tsc",
                    "raw": line.strip(),
                }
            )
            i += 1
            continue
        failed = _RE_PYTEST_FAILED.match(line)
        if failed:
            node = failed.group(1)
            file_part, _, test_part = node.partition(".py")
            errors.append(
                {
                    "file": (file_part + ".py") if file_part else node,
                    "line": None,
                    "column": None,
                    "severity": "error",
                    "code": None,
                    "message": failed.group(3).strip(),
                    "source": "pytest",
                    "raw": line.strip(),
                }
            )
            i += 1
            continue
        generic = _RE_GENERIC.match(line)
        if generic and (generic.group(4) or generic.group(5)):
            errors.append(
                {
                    "file": generic.group(1),
                    "line": int(generic.group(2)),
                    "column": int(generic.group(3)),
                    "severity": generic.group(4) or "error",
                    "code": generic.group(5),
                    "message": generic.group(6).strip(),
                    "source": "generic",
                    "raw": line.strip(),
                }
            )
        i += 1
    return errors


# ── tools ───────────────────────────────────────────────────────────────────


@tool
def run_tests(
    command: str | None = None,
    repository_path: str | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run the project's test suite. REQUIRES user approval (arbitrary command).

    Args:
        command: Explicit test command. When omitted, auto-detects:
            python -m pytest -q (pyproject.toml/pytest.ini/setup.cfg),
            cargo test (Cargo.toml), or pnpm/npm test (package.json).
        repository_path: Project directory (default: repo root; must be under
            the allowed roots).
        timeout_seconds: 1-1800, default 300.

    Returns:
        `{ok, summary, data: {kind, command, exit_code, duration_ms, stdout,
        stderr, timed_out}, error}`. ok=False on test failures (nonzero exit).
    """

    try:
        repo = _repo(repository_path)
        cmd = _validate_command(command) if command is not None else _detect_test_command(repo)
        timeout = _validate_timeout(timeout_seconds)
        return _command_result("tests", cmd, _run(cmd, repo, timeout), repo)
    except (DevToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, DevToolError) else "path_escape"
        return _failure(code, "could not run tests", str(exc))


@tool
def run_linter(
    command: str | None = None,
    repository_path: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run the project's linter. REQUIRES user approval (arbitrary command).

    Args:
        command: Explicit lint command. When omitted, auto-detects:
            ruff check . (ruff config), npx eslint . (eslint config),
            npm/pnpm run lint (lint script), or cargo clippy (Cargo.toml).
        repository_path: Project directory (default: repo root).
        timeout_seconds: 1-1800, default 120.

    Returns:
        `{ok, summary, data: {kind, command, exit_code, duration_ms, stdout,
        stderr, timed_out}, error}`. ok=False when lint findings exit nonzero.
    """

    try:
        repo = _repo(repository_path)
        cmd = _validate_command(command) if command is not None else _detect_lint_command(repo)
        timeout = _validate_timeout(timeout_seconds)
        return _command_result("linter", cmd, _run(cmd, repo, timeout), repo)
    except (DevToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, DevToolError) else "path_escape"
        return _failure(code, "could not run linter", str(exc))


@tool
def run_formatter(
    command: str | None = None,
    repository_path: str | None = None,
    paths: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run the project's formatter. REQUIRES user approval. MUTATES files in
    place — review the diff afterwards (git_diff) if anything looks off.

    Args:
        command: Explicit format command. When omitted, auto-detects:
            ruff format . (ruff config), npx prettier --write . (prettier
            config), npm/pnpm run format (format script), or cargo fmt
            (Cargo.toml).
        repository_path: Project directory (default: repo root).
        paths: Optional files/dirs to format (appended to the command).
        timeout_seconds: 1-1800, default 120.

    Returns:
        `{ok, summary, data: {kind, command, exit_code, duration_ms, stdout,
        stderr, timed_out}, error}`.
    """

    try:
        repo = _repo(repository_path)
        cmd = _validate_command(command) if command is not None else _detect_format_command(repo)
        if paths:
            if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
                raise DevToolError("invalid_paths", "paths must be a list of non-empty strings")
            if any(p.strip().startswith("-") for p in paths):
                raise DevToolError("invalid_paths", "paths must not start with '-'")
            cmd = f"{cmd} {' '.join(p.strip() for p in paths)}"
        timeout = _validate_timeout(timeout_seconds)
        return _command_result("formatter", cmd, _run(cmd, repo, timeout), repo)
    except (DevToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, DevToolError) else "path_escape"
        return _failure(code, "could not run formatter", str(exc))


@tool
def inspect_build_errors(
    command: str,
    repository_path: str | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run a build/check command and parse its output into structured errors.

    REQUIRES user approval (arbitrary command). Parses rustc (`error[E…]`
    blocks), pytest (`FAILED … - msg`), tsc (`file(line,col): error TS…`),
    and ruff/eslint/gcc-style (`file:line:col: CODE msg`) diagnostics
    best-effort; unrecognized output remains available in `output_tail`.

    Args:
        command: The build/check command to run (required).
        repository_path: Project directory (default: repo root).
        timeout_seconds: 1-1800, default 300.

    Returns:
        `{ok, summary, data: {command, exit_code, duration_ms, errors:
        [{file, line, column, severity, code, message, source, raw}],
        error_count, output_tail, timed_out}, error}`. `ok` mirrors whether
        the command itself could run; check `exit_code` for build success.
    """

    try:
        repo = _repo(repository_path)
        cmd = _validate_command(command)
        timeout = _validate_timeout(timeout_seconds)
        run = _run(cmd, repo, timeout)
        combined = f"{run['stdout']}\n{run['stderr']}"
        errors = _parse_build_errors(combined)
        data = {
            "command": cmd,
            "repository_path": str(repo),
            "exit_code": run["exit_code"],
            "duration_ms": run["duration_ms"],
            "errors": errors,
            "error_count": len(errors),
            "output_tail": combined[-OUTPUT_TAIL_BYTES:],
            "timed_out": run["timed_out"],
        }
        if run["timed_out"]:
            return _failure("timeout", "build command timed out", "process exceeded its timeout", data)
        if run["exit_code"] != 0:
            return _result(
                f"exit {run['exit_code']}: {len(errors)} parsed error(s)",
                data,
            )
        return _result(f"build clean ({len(errors)} diagnostics)", data)
    except (DevToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, DevToolError) else "path_escape"
        return _failure(code, "could not run build command", str(exc))


TOOL = [run_tests, run_linter, run_formatter, inspect_build_errors]

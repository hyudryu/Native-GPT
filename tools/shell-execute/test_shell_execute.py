"""Tests for tools/shell-execute/tool.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "shell_execute_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _fake_runner(stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    def runner(command, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return runner


def test_runs_command_returns_stdout(mod) -> None:
    result = mod.run_command("echo hello", runner=_fake_runner(stdout="hello\n"))
    assert result["ok"] is True
    assert result["data"]["stdout"] == "hello\n"
    assert result["data"]["exit_code"] == 0
    assert result["data"]["command"] == "echo hello"


def test_nonzero_exit_is_failure(mod) -> None:
    result = mod.run_command(
        "exit 3",
        runner=_fake_runner(stderr="oops", returncode=3),
    )
    assert result["ok"] is False
    assert result["data"]["exit_code"] == 3
    assert result["error"]["code"] == "nonzero_exit"


def test_timeout_returns_timeout_error(mod) -> None:
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])
    result = mod.run_command("sleep 999", timeout_seconds=2, runner=runner)
    assert result["ok"] is False
    assert result["data"]["timed_out"] is True
    assert result["error"]["code"] == "timeout"
    assert "2s" in result["error"]["message"]


def test_env_is_stripped_to_allowlist(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me-too")
    env_received: dict[str, str] = {}

    def runner(command, *, env, **kwargs):
        env_received.update(env)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    mod.run_command("echo ok", runner=runner)
    assert "OPENAI_API_KEY" not in env_received
    assert "AWS_SECRET_ACCESS_KEY" not in env_received


def test_shell_true_is_used(mod) -> None:
    """The runner must be invoked with shell=True so the command is interpreted."""
    received: dict[str, Any] = {}
    def runner(command, **kwargs):
        received.update(kwargs)
        received["command"] = command
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_command("echo hi", runner=runner)
    assert received.get("shell") is True


def test_cwd_defaults_to_repo_root(mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    received: dict[str, Any] = {}
    def runner(command, *, cwd, **kwargs):
        received["cwd"] = cwd
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_command("pwd", runner=runner)
    assert Path(received["cwd"]) == tmp_path


def test_rejects_empty_command(mod) -> None:
    with pytest.raises(mod.ShellExecuteError):
        mod.run_command("")
    with pytest.raises(mod.ShellExecuteError):
        mod.run_command("   ")


def test_rejects_oversized_command(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_COMMAND_BYTES", 16)
    with pytest.raises(mod.ShellExecuteError, match="exceeds"):
        mod.run_command("x" * 100)


def test_rejects_invalid_timeout(mod) -> None:
    with pytest.raises(mod.ShellExecuteError):
        mod.run_command("echo x", timeout_seconds=0)
    with pytest.raises(mod.ShellExecuteError):
        mod.run_command("echo x", timeout_seconds=601)
    with pytest.raises(mod.ShellExecuteError):
        mod.run_command("echo x", timeout_seconds="big")  # type: ignore[arg-type]


def test_output_truncation(mod) -> None:
    huge = "x" * (mod.MAX_OUTPUT_BYTES + 100)
    result = mod.run_command("echo", runner=_fake_runner(stdout=huge))
    assert len(result["data"]["stdout"]) == mod.MAX_OUTPUT_BYTES
    assert result["data"]["truncated"] is True


def test_requires_approval_flag_exported(mod) -> None:
    assert mod.REQUIRES_APPROVAL is True


def test_summary_includes_exit_code_and_duration(mod) -> None:
    result = mod.run_command("echo", runner=_fake_runner(returncode=0))
    assert "exited 0" in result["summary"]
    assert "ms" in result["summary"]

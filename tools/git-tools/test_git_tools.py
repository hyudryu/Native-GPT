"""Tests for tools/git-tools/tool.py.

Each test gets a throwaway git repo under tmp_path; AGENTGPT_ALLOWED_ROOTS
points at tmp_path so `repository_path` resolution accepts it. Tests skip
when the git CLI is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "git_tools_under_test"

GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not GIT_AVAILABLE, reason="git CLI not available")


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_ALLOWED_ROOTS", str(tmp_path))
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "HOME", "USERPROFILE"}}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args], cwd=str(repo), env=env, capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


# ── read-only ───────────────────────────────────────────────────────────────


def test_status_clean_and_dirty(mod, repo: Path) -> None:
    clean = mod.git_status(repository_path=str(repo))
    assert clean["ok"] is True
    assert clean["data"]["branch"]["head"] == "main"
    assert clean["data"]["entries"] == []

    (repo / "new.txt").write_text("x", encoding="utf-8")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = mod.git_status(repository_path=str(repo))
    statuses = {e["path"]: e["status"] for e in dirty["data"]["entries"]}
    assert statuses["new.txt"] == "untracked"
    assert statuses["README.md"] == "ordinary"


def test_log_and_show(mod, repo: Path) -> None:
    log = mod.git_log(repository_path=str(repo))
    assert log["ok"] is True
    assert log["data"]["count"] == 1
    assert log["data"]["commits"][0]["subject"] == "initial"

    sha = log["data"]["commits"][0]["sha"]
    shown = mod.git_show(sha, repository_path=str(repo))
    assert shown["ok"] is True
    assert "initial" in shown["data"]["raw"]

    file_view = mod.git_show("HEAD", path="README.md", repository_path=str(repo))
    assert file_view["ok"] is True
    assert file_view["data"]["content"] == "hello\n"


def test_diff_working_tree_and_staged(mod, repo: Path) -> None:
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    diff = mod.git_diff(repository_path=str(repo))
    assert diff["ok"] is True
    assert diff["data"]["files"][0]["path"] == "README.md"
    assert "+changed" in diff["data"]["raw"]
    staged = mod.git_diff(repository_path=str(repo), staged=True)
    assert staged["data"]["files"] == []


def test_list_branches(mod, repo: Path) -> None:
    result = mod.git_list_branches(repository_path=str(repo))
    assert result["ok"] is True
    branches = result["data"]["branches"]
    assert branches == [{"name": "main", "current": True, "upstream": None, "remote": False}]


def test_remote_status_without_upstream(mod, repo: Path) -> None:
    result = mod.git_get_remote_status(repository_path=str(repo))
    assert result["ok"] is True
    assert result["data"]["has_upstream"] is False


def test_remote_status_ahead(mod, repo: Path, tmp_path: Path) -> None:
    # Bare remote + clone so upstream tracking exists.
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(repo), str(bare))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    _git(clone, "config", "commit.gpgsign", "false")
    (clone / "new.txt").write_text("x", encoding="utf-8")
    _git(clone, "add", "new.txt")
    _git(clone, "commit", "-m", "second")
    result = mod.git_get_remote_status(repository_path=str(clone))
    assert result["ok"] is True
    assert result["data"]["has_upstream"] is True
    assert result["data"]["ahead"] == 1
    assert result["data"]["behind"] == 0


# ── mutations ───────────────────────────────────────────────────────────────


def test_branch_create_and_checkout(mod, repo: Path) -> None:
    created = mod.git_create_branch("feature", repository_path=str(repo))
    assert created["ok"] is True
    switched = mod.git_checkout("feature", repository_path=str(repo))
    assert switched["ok"] is True
    status = mod.git_status(repository_path=str(repo))
    assert status["data"]["branch"]["head"] == "feature"
    # checkout -b for a missing branch
    made = mod.git_checkout("another", create_if_missing=True, repository_path=str(repo))
    assert made["ok"] is True


def test_stage_unstage_commit(mod, repo: Path) -> None:
    (repo / "a.txt").write_text("a", encoding="utf-8")
    staged = mod.git_stage(["a.txt"], repository_path=str(repo))
    assert staged["ok"] is True
    unstaged = mod.git_unstage(["a.txt"], repository_path=str(repo))
    assert unstaged["ok"] is True
    assert mod.git_status(repository_path=str(repo))["data"]["entries"][0]["status"] == "untracked"

    committed = mod.git_commit("add a", paths=["a.txt"], repository_path=str(repo))
    assert committed["ok"] is True
    assert committed["data"]["subject"] == "add a"
    log = mod.git_log(repository_path=str(repo))
    assert log["data"]["count"] == 2

    # amend
    amended = mod.git_commit("add a (amended)", amend=True, repository_path=str(repo))
    assert amended["ok"] is True
    assert mod.git_log(repository_path=str(repo))["data"]["commits"][0]["subject"] == "add a (amended)"


def test_restore_discards_changes(mod, repo: Path) -> None:
    (repo / "README.md").write_text("broken\n", encoding="utf-8")
    restored = mod.git_restore(["README.md"], repository_path=str(repo))
    assert restored["ok"] is True
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_ref_rejects_flag_injection(mod, repo: Path) -> None:
    result = mod.git_checkout("--orphan", repository_path=str(repo))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_ref"


# ── merge / conflicts ───────────────────────────────────────────────────────


def _make_conflict(repo: Path) -> None:
    _git(repo, "checkout", "-b", "side")
    (repo / "README.md").write_text("side version\n", encoding="utf-8")
    _git(repo, "commit", "-am", "side change")
    _git(repo, "checkout", "main")
    (repo / "README.md").write_text("main version\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main change")


def test_merge_conflict_list_resolve_abort(mod, repo: Path) -> None:
    _make_conflict(repo)
    merged = mod.git_merge("side", repository_path=str(repo))
    assert merged["ok"] is False
    assert merged["error"]["code"] == "merge_conflict"
    assert merged["data"]["conflicts"] == ["README.md"]

    conflicts = mod.git_list_conflicts(repository_path=str(repo))
    assert conflicts["data"]["conflicts"] == ["README.md"]
    assert conflicts["data"]["operation"] == "merge"

    resolved = mod.git_resolve_conflict("README.md", "ours", repository_path=str(repo))
    assert resolved["ok"] is True
    assert (repo / "README.md").read_text(encoding="utf-8") == "main version\n"
    assert mod.git_list_conflicts(repository_path=str(repo))["data"]["count"] == 0


def test_abort_operation(mod, repo: Path) -> None:
    _make_conflict(repo)
    mod.git_merge("side", repository_path=str(repo))
    aborted = mod.git_abort_operation(repository_path=str(repo))
    assert aborted["ok"] is True
    assert aborted["data"]["operation"] == "merge"
    assert (repo / "README.md").read_text(encoding="utf-8") == "main version\n"

    nothing = mod.git_abort_operation(repository_path=str(repo))
    assert nothing["ok"] is False
    assert nothing["error"]["code"] == "no_operation"


def test_rebase(mod, repo: Path) -> None:
    _git(repo, "checkout", "-b", "feature")
    (repo / "feat.txt").write_text("f", encoding="utf-8")
    _git(repo, "add", "feat.txt")
    _git(repo, "commit", "-m", "feature work")
    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("m", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-m", "main work")
    _git(repo, "checkout", "feature")
    result = mod.git_rebase("main", repository_path=str(repo))
    assert result["ok"] is True
    subjects = [c["subject"] for c in mod.git_log(repository_path=str(repo))["data"]["commits"]]
    assert subjects == ["feature work", "main work", "initial"]


def test_redact_credentials(mod) -> None:
    text = "fatal: https://user:ghp_abcdef0123456789abcd@example.com/repo.git failed"
    redacted = mod._redact(text)
    assert "ghp_abcdef0123456789abcd" not in redacted
    assert "user:" not in redacted

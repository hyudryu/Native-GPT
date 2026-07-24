"""Git Strands tools — structured git operations over the local repository.

Multi-tool folder: `TOOL` is a list of Strands tools. Everything shells out
to the `git` CLI with list-form argv (no shell), a sanitized environment
(mirroring `tools/shell-execute`), `GIT_TERMINAL_PROMPT=0` so git can never
prompt for credentials, and a 30s timeout. `repository_path` is resolved
under the allowed roots via `tools/_lib/paths.py`; it defaults to the repo
root.

Safety posture
--------------
- Read ops (status/diff/log/show/branches/remote-status/conflicts) never
  mutate.
- Mutations are local and reversible via the reflog. Push never uses
  `--force`; only `--force-with-lease` when explicitly requested.
- Output is redacted for credential-shaped URLs/tokens before returning.
- fetch/pull/push hit the user's own configured git remotes (outbound
  network); everything else is offline.
- Approval: this folder's manifest sets requires_approval=false because the
  flag gates every tool in a folder and gating status/diff would be
  unusable. Mutating tool docstrings carry explicit warnings; per-op host
  approval is a later stage (see manifest description).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
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

GIT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 64 * 1024

# Minimal sanitized environment (mirrors tools/shell-execute's allowlist).
ENV_ALLOWLIST = {
    "PATH",
    "SYSTEMROOT",
    "TEMP", "TMP",
    "TZ", "LC_ALL", "LANG",
    "AGENTGPT_REPO_ROOT",
    "USERPROFILE", "APPDATA",
    "HOME",
}

# Credential redaction: userinfo in URLs plus common token shapes.
_CREDENTIAL_PATTERNS = [
    (re.compile(r"(https?://)([^/\s:]+):([^@/\s]+)@"), r"\1***:***@"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), r"\1_***"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}\b"), "glpat-***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "xox*-***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_***"),
]


class GitToolError(ValueError):
    """Any git-tool failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _redact(text: str) -> str:
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _failure(
    code: str, summary: str, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "summary": summary,
        "data": data or {},
        "error": {"code": code, "message": _redact(message)},
    }


def _build_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    # Never allow git to prompt on a terminal for credentials — the sidecar
    # has no TTY and a prompt would hang until the timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _repo(repository_path: str | None) -> Path:
    """Resolve the working repo: explicit path under allowed roots, else root."""
    if repository_path is None:
        return _paths.repo_root()
    if not isinstance(repository_path, str) or not repository_path.strip():
        raise GitToolError("invalid_repository", "repository_path must be a non-empty string")
    resolved = resolve_under_root(repository_path)
    if not resolved.is_dir():
        raise GitToolError("invalid_repository", f"not a directory: {repository_path}")
    return resolved


def _validate_ref(value: str, label: str = "ref") -> str:
    """Reject refs that look like flags (argv injection guard)."""
    if not isinstance(value, str) or not value.strip():
        raise GitToolError("invalid_ref", f"{label} must be a non-empty string")
    if value.strip().startswith("-"):
        raise GitToolError("invalid_ref", f"{label} must not start with '-': {value!r}")
    return value


def _validate_paths(paths: list[str], label: str = "paths") -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise GitToolError("invalid_paths", f"{label} must be a non-empty list")
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise GitToolError("invalid_paths", f"{label} entries must be non-empty strings")
        if p.strip().startswith("-"):
            raise GitToolError("invalid_paths", f"{label} entries must not start with '-': {p!r}")
    return [p.strip() for p in paths]


def _run_git(
    args: list[str],
    cwd: Path,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run `git <args>`; returns {exit_code, stdout, stderr, timed_out}."""
    if shutil.which("git") is None:
        raise GitToolError("git_unavailable", "git CLI not found on PATH")
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=_build_env(),
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout": "", "stderr": "", "timed_out": True}
    return {
        "exit_code": completed.returncode,
        "stdout": _redact(completed.stdout or ""),
        "stderr": _redact(completed.stderr or ""),
        "timed_out": False,
    }


def _git_or_fail(args: list[str], cwd: Path, action: str) -> dict[str, Any]:
    """Run git; raise GitToolError on failure. Returns the run dict."""
    run = _run_git(args, cwd)
    if run["timed_out"]:
        raise GitToolError("timeout", f"git {action} exceeded {GIT_TIMEOUT_SECONDS}s")
    if run["exit_code"] != 0:
        message = run["stderr"].strip() or run["stdout"].strip() or f"exit {run['exit_code']}"
        raise GitToolError("git_error", f"git {action} failed: {message[:500]}")
    return run


def _finish(run: dict[str, Any], ok_summary: str, data: dict[str, Any], action: str) -> dict[str, Any]:
    """Standard result from a raw git run for pass-through style tools."""
    if run["timed_out"]:
        return _failure("timeout", f"git {action} timed out", f"exceeded {GIT_TIMEOUT_SECONDS}s")
    if run["exit_code"] != 0:
        message = run["stderr"].strip() or run["stdout"].strip() or "unknown error"
        return _failure("git_error", f"git {action} failed", message[:500], data)
    return _result(ok_summary, data)


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Uniform error mapping for the @tool wrappers."""
    try:
        return fn(*args, **kwargs)
    except GitToolError as exc:
        return _failure(exc.code, "git operation failed", str(exc))
    except PathEscapeError as exc:
        return _failure("path_escape", "repository path rejected", str(exc))


# ── porcelain v2 parsing ────────────────────────────────────────────────────


def _parse_status_porcelain(text: str) -> dict[str, Any]:
    branch: dict[str, Any] = {"head": None, "upstream": None, "ahead": 0, "behind": 0}
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            branch["head"] = line[len("# branch.head "):]
        elif line.startswith("# branch.upstream "):
            branch["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            match = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                branch["ahead"] = int(match.group(1))
                branch["behind"] = int(match.group(2))
        elif line.startswith("? "):
            entries.append({"path": line[2:], "status": "untracked", "staged": None, "unstaged": None})
        elif line.startswith("1 ") or line.startswith("2 ") or line.startswith("u "):
            parts = line.split(" ")
            kind = {"1": "ordinary", "2": "renamed", "u": "unmerged"}[parts[0]]
            xy = parts[1]
            path = parts[-1]
            entry: dict[str, Any] = {
                "path": path,
                "status": kind,
                "staged": xy[0] if xy[0] != "." else None,
                "unstaged": xy[1] if xy[1] != "." else None,
            }
            if kind == "renamed" and len(parts) >= 2:
                entry["original_path"] = parts[-2]
            entries.append(entry)
    return {"branch": branch, "entries": entries}


_LOG_SEP = "\x1f"
_LOG_END = "\x1e"


def _parse_log(text: str) -> list[dict[str, Any]]:
    commits = []
    for record in text.split(_LOG_END):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_LOG_SEP)
        if len(parts) < 6:
            continue
        commits.append(
            {
                "sha": parts[0],
                "short_sha": parts[1],
                "author": parts[2],
                "email": parts[3],
                "date": parts[4],
                "subject": parts[5],
            }
        )
    return commits


# ── read-only tools ─────────────────────────────────────────────────────────


@tool
def git_status(repository_path: str | None = None) -> dict[str, Any]:
    """Structured working-tree status (porcelain v2).

    Args:
        repository_path: Repo directory (default: the repo root). Must be
            under the allowed roots.

    Returns:
        `{ok, summary, data: {branch: {head, upstream, ahead, behind},
        entries: [{path, status, staged, unstaged}]}, error}`. Read-only.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        run = _git_or_fail(["status", "--porcelain=v2", "--branch"], repo, "status")
        parsed = _parse_status_porcelain(run["stdout"])
        count = len(parsed["entries"])
        head = parsed["branch"]["head"] or "(detached)"
        return _result(
            f"{head}: {count} changed file(s)" if count else f"{head}: clean",
            parsed,
        )

    return _wrap(impl)


@tool
def git_diff(
    repository_path: str | None = None,
    path: str | None = None,
    staged: bool = False,
    commit: str | None = None,
) -> dict[str, Any]:
    """Diff the working tree, the index, or against a commit.

    Args:
        repository_path: Repo directory (default: repo root).
        path: Limit the diff to one path.
        staged: Diff the index vs HEAD instead of the working tree.
        commit: Diff the working tree against this revision (e.g. "HEAD~3").

    Returns:
        `{ok, summary, data: {files: [{path, added, deleted}], raw,
        truncated}, error}`. Read-only; `raw` is capped at 64 KB.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        base = ["diff"]
        if staged:
            base.append("--staged")
        if commit is not None:
            base.append(_validate_ref(commit, "commit"))
        if path is not None:
            base += ["--", _validate_ref(path, "path")]
        numstat = _git_or_fail([*base, "--numstat"], repo, "diff")
        files = []
        for line in numstat["stdout"].splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                files.append(
                    {
                        "path": parts[2],
                        "added": None if parts[0] == "-" else int(parts[0]),
                        "deleted": None if parts[1] == "-" else int(parts[1]),
                    }
                )
        patch = _git_or_fail(base, repo, "diff")
        raw = patch["stdout"]
        truncated = len(raw) > MAX_OUTPUT_CHARS
        return _result(
            f"{len(files)} file(s) changed",
            {"files": files, "raw": raw[:MAX_OUTPUT_CHARS], "truncated": truncated},
        )

    return _wrap(impl)


@tool
def git_log(
    repository_path: str | None = None,
    limit: int = 20,
    path: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Recent commit history, parsed into structured records.

    Args:
        repository_path: Repo directory (default: repo root).
        limit: Max commits (1-100, default 20).
        path: Only commits touching this path.
        branch: Revision/branch to start from (default HEAD).

    Returns:
        `{ok, summary, data: {commits: [{sha, short_sha, author, email,
        date, subject}], count}, error}`. Read-only.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        try:
            limit_int = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            raise GitToolError("invalid_limit", f"limit must be an integer: {limit!r}") from None
        fmt = f"%H{_LOG_SEP}%h{_LOG_SEP}%an{_LOG_SEP}%ae{_LOG_SEP}%aI{_LOG_SEP}%s{_LOG_END}"
        args = ["log", f"--max-count={limit_int}", f"--pretty=format:{fmt}"]
        if branch is not None:
            args.append(_validate_ref(branch, "branch"))
        if path is not None:
            args += ["--", _validate_ref(path, "path")]
        run = _git_or_fail(args, repo, "log")
        commits = _parse_log(run["stdout"])
        return _result(f"{len(commits)} commit(s)", {"commits": commits, "count": len(commits)})

    return _wrap(impl)


@tool
def git_show(revision: str, path: str | None = None, repository_path: str | None = None) -> dict[str, Any]:
    """Show a commit (metadata + patch) or a file's content at a revision.

    Args:
        revision: Commit-ish (sha, branch, tag, HEAD~1...).
        path: When given, return the file's content at `revision` instead of
            the commit view.
        repository_path: Repo directory (default: repo root).

    Returns:
        Commit view: `{ok, summary, data: {revision, raw, truncated}, error}`.
        File view: `data: {revision, path, content, truncated}`. Read-only.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        ref = _validate_ref(revision, "revision")
        if path is not None:
            rel = _validate_ref(path, "path")
            run = _git_or_fail(["show", f"{ref}:{rel}"], repo, "show")
            content = run["stdout"]
            truncated = len(content) > MAX_OUTPUT_CHARS
            return _result(
                f"{rel} at {ref} ({len(content)} chars)",
                {"revision": ref, "path": rel, "content": content[:MAX_OUTPUT_CHARS], "truncated": truncated},
            )
        run = _git_or_fail(["show", "--stat", "--patch", ref], repo, "show")
        raw = run["stdout"]
        truncated = len(raw) > MAX_OUTPUT_CHARS
        return _result(
            f"commit {ref}",
            {"revision": ref, "raw": raw[:MAX_OUTPUT_CHARS], "truncated": truncated},
        )

    return _wrap(impl)


@tool
def git_list_branches(repository_path: str | None = None, include_remote: bool = False) -> dict[str, Any]:
    """List local (and optionally remote) branches with upstream info.

    Args:
        repository_path: Repo directory (default: repo root).
        include_remote: Also list remote-tracking branches.

    Returns:
        `{ok, summary, data: {branches: [{name, current, upstream, remote}],
        count}, error}`. Read-only.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        args = ["branch", "--format=%(refname:short)%1f%(upstream:short)%1f%(HEAD)"]
        if include_remote:
            args.append("--all")
        run = _git_or_fail(args, repo, "branch")
        branches = []
        for line in run["stdout"].splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            name, upstream, head = parts[0], parts[1] or None, parts[2].strip() == "*"
            branches.append(
                {
                    "name": name,
                    "current": head,
                    "upstream": upstream,
                    # With --all, remote-tracking refs appear as remotes/<r>/<b>.
                    "remote": name.startswith("remotes/"),
                }
            )
        return _result(f"{len(branches)} branch(es)", {"branches": branches, "count": len(branches)})

    return _wrap(impl)


@tool
def git_get_remote_status(repository_path: str | None = None) -> dict[str, Any]:
    """Ahead/behind counts vs the upstream branch — WITHOUT fetching.

    Compares against the last-fetched remote-tracking ref only; use git_fetch
    first if you need fresh numbers.

    Args:
        repository_path: Repo directory (default: repo root).

    Returns:
        `{ok, summary, data: {branch, upstream, ahead, behind}, error}`.
        Read-only and offline.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        run = _git_or_fail(["status", "--porcelain=v2", "--branch"], repo, "status")
        parsed = _parse_status_porcelain(run["stdout"])
        branch = parsed["branch"]
        if not branch["upstream"]:
            return _result(
                f"{branch['head'] or '(detached)'}: no upstream configured",
                {**branch, "has_upstream": False},
            )
        return _result(
            f"{branch['head']} vs {branch['upstream']}: "
            f"{branch['ahead']} ahead, {branch['behind']} behind",
            {**branch, "has_upstream": True},
        )

    return _wrap(impl)


@tool
def git_list_conflicts(repository_path: str | None = None) -> dict[str, Any]:
    """List files with unresolved merge conflicts.

    Args:
        repository_path: Repo directory (default: repo root).

    Returns:
        `{ok, summary, data: {conflicts: [path...], count, operation},
        error}`. Read-only. `operation` is "merge" | "rebase" | "cherry-pick"
        | "revert" | None.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        run = _git_or_fail(["diff", "--name-only", "--diff-filter=U"], repo, "diff")
        conflicts = [line for line in run["stdout"].splitlines() if line.strip()]
        operation = _detect_operation(repo)
        return _result(
            f"{len(conflicts)} conflicted file(s)" if conflicts else "no conflicts",
            {"conflicts": conflicts, "count": len(conflicts), "operation": operation},
        )

    return _wrap(impl)


# ── branch mutation ─────────────────────────────────────────────────────────


@tool
def git_create_branch(
    name: str,
    start_point: str | None = None,
    checkout: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Create a branch (optionally switching to it). MUTATES the repository.

    Args:
        name: New branch name.
        start_point: Commit-ish to branch from (default HEAD).
        checkout: Also switch to the new branch.

    Returns:
        `{ok, summary, data: {branch, start_point, checked_out}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        branch = _validate_ref(name, "branch name")
        args = ["checkout", "-b", branch] if checkout else ["branch", branch]
        if start_point is not None:
            args.append(_validate_ref(start_point, "start_point"))
        _git_or_fail(args, repo, "create-branch")
        return _result(
            f"created branch {branch}" + (" (checked out)" if checkout else ""),
            {"branch": branch, "start_point": start_point, "checked_out": checkout},
        )

    return _wrap(impl)


@tool
def git_checkout(
    branch_or_commit: str,
    create_if_missing: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Switch branches or detach at a commit. MUTATES the working tree.

    Uncommitted changes can block the switch (git refuses) — commit, stash,
    or restore them first.

    Args:
        branch_or_commit: Branch name or commit-ish.
        create_if_missing: Create the branch if it doesn't exist
            (git checkout -b).

    Returns:
        `{ok, summary, data: {ref, created}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        ref = _validate_ref(branch_or_commit, "branch_or_commit")
        args = ["checkout", "-b", ref] if create_if_missing else ["checkout", ref]
        run = _run_git(args, repo)
        return _finish(
            run,
            f"checked out {ref}" + (" (new branch)" if create_if_missing else ""),
            {"ref": ref, "created": create_if_missing, "raw": (run["stderr"] or run["stdout"]).strip()[:2000]},
            "checkout",
        )

    return _wrap(impl)


# ── staging / commit ────────────────────────────────────────────────────────


@tool
def git_stage(paths: list[str], repository_path: str | None = None) -> dict[str, Any]:
    """Stage paths (git add). MUTATES the index.

    Args:
        paths: Files/dirs to stage (relative to the repo).

    Returns:
        `{ok, summary, data: {staged: paths}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        safe = _validate_paths(paths)
        _git_or_fail(["add", "--", *safe], repo, "add")
        return _result(f"staged {len(safe)} path(s)", {"staged": safe})

    return _wrap(impl)


@tool
def git_unstage(paths: list[str], repository_path: str | None = None) -> dict[str, Any]:
    """Unstage paths (git restore --staged). MUTATES the index; the working
    tree is left untouched.

    Args:
        paths: Files to unstage.

    Returns:
        `{ok, summary, data: {unstaged: paths}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        safe = _validate_paths(paths)
        _git_or_fail(["restore", "--staged", "--", *safe], repo, "unstage")
        return _result(f"unstaged {len(safe)} path(s)", {"unstaged": safe})

    return _wrap(impl)


@tool
def git_commit(
    message: str,
    paths: list[str] | None = None,
    amend: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Commit staged changes (optionally staging `paths` first). MUTATES the
    repository — the message is recorded permanently in history (though
    --amend and reset keep it reversible).

    Args:
        message: Commit message (required, non-empty).
        paths: When given, stage these paths before committing.
        amend: Amend the previous commit instead of creating a new one.

    Returns:
        `{ok, summary, data: {sha, short_sha, subject, amended}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        if not isinstance(message, str) or not message.strip():
            raise GitToolError("invalid_message", "commit message must be non-empty")
        if paths:
            safe = _validate_paths(paths)
            _git_or_fail(["add", "--", *safe], repo, "add")
        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")
        _git_or_fail(args, repo, "commit")
        show = _git_or_fail(
            ["log", "-1", f"--pretty=format:%H{_LOG_SEP}%h{_LOG_SEP}%s"], repo, "log"
        )
        sha, short, subject = (show["stdout"].split(_LOG_SEP) + ["", "", ""])[:3]
        return _result(
            f"committed {short}: {subject}",
            {"sha": sha, "short_sha": short, "subject": subject, "amended": amend},
        )

    return _wrap(impl)


@tool
def git_restore(
    paths: list[str],
    staged: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Restore paths from the index/HEAD, DISCARDING local changes. MUTATES
    the working tree — discarded edits are NOT recoverable (unlike commits,
    which the reflog preserves).

    Args:
        paths: Files to restore.
        staged: Also unstage (restore from HEAD instead of the index).

    Returns:
        `{ok, summary, data: {restored: paths, from_head}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        safe = _validate_paths(paths)
        args = ["restore"]
        if staged:
            args += ["--staged", "--worktree", "--source=HEAD"]
        _git_or_fail([*args, "--", *safe], repo, "restore")
        return _result(
            f"restored {len(safe)} path(s) — local changes discarded",
            {"restored": safe, "from_head": staged},
        )

    return _wrap(impl)


# ── network operations (user's own configured remotes) ─────────────────────


@tool
def git_fetch(remote: str | None = None, repository_path: str | None = None) -> dict[str, Any]:
    """Fetch from a remote (OUTBOUND NETWORK to the user's configured git
    remote). Updates remote-tracking refs; never touches the working tree.

    Args:
        remote: Remote name (default: all remotes).

    Returns:
        `{ok, summary, data: {remote, raw}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        args = ["fetch"]
        if remote is not None:
            args.append(_validate_ref(remote, "remote"))
        else:
            args.append("--all")
        run = _run_git(args, repo)
        return _finish(
            run,
            f"fetched {remote or 'all remotes'}",
            {"remote": remote, "raw": (run["stderr"] or run["stdout"]).strip()[:4000]},
            "fetch",
        )

    return _wrap(impl)


@tool
def git_pull(
    remote: str | None = None,
    branch: str | None = None,
    rebase: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Pull from a remote (OUTBOUND NETWORK). MUTATES the working tree and
    may create merge commits or start a rebase — on conflicts use
    git_list_conflicts / git_resolve_conflict / git_abort_operation.

    Args:
        remote: Remote name (default: the branch's configured upstream).
        branch: Remote branch (default: upstream's branch).
        rebase: Rebase instead of merge.

    Returns:
        `{ok, summary, data: {remote, branch, rebase, raw}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        args = ["pull"]
        if rebase:
            args.append("--rebase")
        if remote is not None:
            args.append(_validate_ref(remote, "remote"))
        if branch is not None:
            args.append(_validate_ref(branch, "branch"))
        run = _run_git(args, repo)
        raw = (run["stdout"] + run["stderr"]).strip()
        return _finish(
            run,
            "pull complete" + (" (rebase)" if rebase else ""),
            {"remote": remote, "branch": branch, "rebase": rebase, "raw": raw[:4000]},
            "pull",
        )

    return _wrap(impl)


@tool
def git_push(
    remote: str | None = None,
    branch: str | None = None,
    set_upstream: bool = False,
    force_with_lease: bool = False,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Push to a remote (OUTBOUND NETWORK). MUTATES REMOTE STATE — visible to
    collaborators. Force is limited to --force-with-lease (refuses to clobber
    unseen upstream work); plain --force is deliberately not offered.

    Args:
        remote: Remote name (default: origin or the configured upstream).
        branch: Local branch to push (default: current branch).
        set_upstream: Set the upstream tracking ref (-u).
        force_with_lease: Safer force push (aborts if the remote moved since
            the last fetch).

    Returns:
        `{ok, summary, data: {remote, branch, set_upstream,
        force_with_lease, raw}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        if force_with_lease:
            args.append("--force-with-lease")
        if remote is not None:
            args.append(_validate_ref(remote, "remote"))
        if branch is not None:
            args.append(_validate_ref(branch, "branch"))
        run = _run_git(args, repo)
        raw = (run["stdout"] + run["stderr"]).strip()
        return _finish(
            run,
            f"pushed {branch or 'current branch'} to {remote or 'default remote'}",
            {
                "remote": remote,
                "branch": branch,
                "set_upstream": set_upstream,
                "force_with_lease": force_with_lease,
                "raw": raw[:4000],
            },
            "push",
        )

    return _wrap(impl)


# ── merge / rebase / conflicts ──────────────────────────────────────────────


@tool
def git_merge(
    branch: str,
    strategy: str | None = None,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Merge a branch into the current one. MUTATES the repository; may stop
    with conflicts (use git_list_conflicts, git_resolve_conflict,
    git_abort_operation).

    Args:
        branch: Branch to merge in.
        strategy: Optional merge strategy name ("ours", "theirs", "ort", ...).

    Returns:
        `{ok, summary, data: {branch, merged, conflicts, raw}, error}`.
        A conflict stop returns ok=False with code "merge_conflict".
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        ref = _validate_ref(branch, "branch")
        args = ["merge"]
        if strategy is not None:
            args += ["-s", _validate_ref(strategy, "strategy")]
        args.append(ref)
        run = _run_git(args, repo)
        raw = (run["stdout"] + run["stderr"]).strip()
        if run["exit_code"] not in (0, None) and "CONFLICT" in raw:
            conflicts_run = _run_git(["diff", "--name-only", "--diff-filter=U"], repo)
            conflicts = [ln for ln in conflicts_run["stdout"].splitlines() if ln.strip()]
            return _failure(
                "merge_conflict",
                f"merge of {ref} stopped with {len(conflicts)} conflict(s)",
                "resolve with git_resolve_conflict or abort with git_abort_operation",
                {"branch": ref, "conflicts": conflicts, "raw": raw[:4000]},
            )
        return _finish(
            run,
            f"merged {ref}",
            {"branch": ref, "merged": True, "raw": raw[:4000]},
            "merge",
        )

    return _wrap(impl)


@tool
def git_rebase(branch: str, repository_path: str | None = None) -> dict[str, Any]:
    """Rebase the current branch onto another. MUTATES history (rewrites
    local commits); may stop on conflicts (use git_resolve_conflict then
    `git rebase --continue` via shell_execute, or git_abort_operation).

    Args:
        branch: Upstream branch/commit to rebase onto.

    Returns:
        `{ok, summary, data: {branch, raw}, error}`. A conflict stop returns
        ok=False with code "rebase_conflict".
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        ref = _validate_ref(branch, "branch")
        run = _run_git(["rebase", ref], repo)
        raw = (run["stdout"] + run["stderr"]).strip()
        if run["exit_code"] not in (0, None):
            conflicts_run = _run_git(["diff", "--name-only", "--diff-filter=U"], repo)
            conflicts = [ln for ln in conflicts_run["stdout"].splitlines() if ln.strip()]
            if conflicts or _detect_operation(repo) == "rebase":
                return _failure(
                    "rebase_conflict",
                    f"rebase onto {ref} stopped ({len(conflicts)} conflict(s))",
                    "resolve with git_resolve_conflict or abort with git_abort_operation",
                    {"branch": ref, "conflicts": conflicts, "raw": raw[:4000]},
                )
        return _finish(run, f"rebased onto {ref}", {"branch": ref, "raw": raw[:4000]}, "rebase")

    return _wrap(impl)


def _git_dir(repo: Path) -> Path:
    run = _run_git(["rev-parse", "--git-dir"], repo)
    raw = run["stdout"].strip()
    path = Path(raw)
    return path if path.is_absolute() else (repo / path).resolve()


def _detect_operation(repo: Path) -> str | None:
    git_dir = _git_dir(repo)
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    if (git_dir / "REVERT_HEAD").exists():
        return "revert"
    return None


@tool
def git_abort_operation(repository_path: str | None = None) -> dict[str, Any]:
    """Abort an in-progress merge, rebase, cherry-pick, or revert. MUTATES
    the repository but returns it to the pre-operation state.

    Args:
        repository_path: Repo directory (default: repo root).

    Returns:
        `{ok, summary, data: {aborted, operation}, error}`.
        ok=False with code "no_operation" when nothing is in progress.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        operation = _detect_operation(repo)
        if operation is None:
            raise GitToolError("no_operation", "no merge/rebase/cherry-pick/revert in progress")
        subcommand = {"merge": "merge", "rebase": "rebase", "cherry-pick": "cherry-pick", "revert": "revert"}[operation]
        _git_or_fail([subcommand, "--abort"], repo, f"{subcommand} --abort")
        return _result(f"aborted {operation}", {"aborted": True, "operation": operation})

    return _wrap(impl)


@tool
def git_resolve_conflict(
    path: str,
    resolution: str,
    repository_path: str | None = None,
) -> dict[str, Any]:
    """Resolve one conflicted file by taking "ours" or "theirs", then stage
    it. MUTATES the working tree — the other side's version of the file is
    discarded for that path.

    Args:
        path: Conflicted file (from git_list_conflicts).
        resolution: "ours" (current branch) or "theirs" (incoming).

    Returns:
        `{ok, summary, data: {path, resolution, staged}, error}`.
    """

    def impl() -> dict[str, Any]:
        repo = _repo(repository_path)
        rel = _validate_ref(path, "path")
        side = str(resolution).strip().lower()
        if side not in {"ours", "theirs"}:
            raise GitToolError("invalid_resolution", f"resolution must be 'ours' or 'theirs' (got {resolution!r})")
        _git_or_fail(["checkout", f"--{side}", "--", rel], repo, f"checkout --{side}")
        _git_or_fail(["add", "--", rel], repo, "add")
        return _result(
            f"resolved {rel} with {side}",
            {"path": rel, "resolution": side, "staged": True},
        )

    return _wrap(impl)


TOOL = [
    git_status,
    git_diff,
    git_log,
    git_show,
    git_list_branches,
    git_create_branch,
    git_checkout,
    git_stage,
    git_unstage,
    git_commit,
    git_restore,
    git_get_remote_status,
    git_fetch,
    git_pull,
    git_push,
    git_merge,
    git_rebase,
    git_abort_operation,
    git_list_conflicts,
    git_resolve_conflict,
]

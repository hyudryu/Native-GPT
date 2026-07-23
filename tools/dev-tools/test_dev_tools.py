"""Tests for tools/dev-tools/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "dev_tools_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_ALLOWED_ROOTS", str(tmp_path))
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


# ── detection ───────────────────────────────────────────────────────────────


def test_detect_test_command(mod, tmp_path: Path) -> None:
    proj = tmp_path / "py"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert mod._detect_test_command(proj) == "python -m pytest -q"

    rust = tmp_path / "rs"
    rust.mkdir()
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    assert mod._detect_test_command(rust) == "cargo test"

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text("{}", encoding="utf-8")
    assert mod._detect_test_command(node) == "npm test"
    (node / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert mod._detect_test_command(node) == "pnpm test"

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(mod.DevToolError):
        mod._detect_test_command(empty)


def test_detect_lint_and_format_commands(mod, tmp_path: Path) -> None:
    ruffproj = tmp_path / "ruffproj"
    ruffproj.mkdir()
    (ruffproj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    assert mod._detect_lint_command(ruffproj) == "ruff check ."
    assert mod._detect_format_command(ruffproj) == "ruff format ."

    esproj = tmp_path / "esproj"
    esproj.mkdir()
    (esproj / "package.json").write_text('{"scripts": {"lint": "eslint ."}}', encoding="utf-8")
    (esproj / "eslint.config.js").write_text("", encoding="utf-8")
    assert mod._detect_lint_command(esproj) == "npx eslint ."

    cargo = tmp_path / "cargo"
    cargo.mkdir()
    (cargo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    assert mod._detect_lint_command(cargo) == "cargo clippy --all-targets"
    assert mod._detect_format_command(cargo) == "cargo fmt"


# ── execution ───────────────────────────────────────────────────────────────


def test_run_tests_with_real_pytest(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (proj / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = mod.run_tests(repository_path=str(proj), timeout_seconds=120)
    assert result["ok"] is True
    assert result["data"]["command"] == "python -m pytest -q"
    assert result["data"]["exit_code"] == 0


def test_run_tests_failure_exit(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = mod.run_tests(
        command='python -c "import sys; sys.exit(3)"',
        repository_path=str(proj),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "nonzero_exit"
    assert result["data"]["exit_code"] == 3


def test_run_tests_detection_failure(mod, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = mod.run_tests(repository_path=str(empty))
    assert result["ok"] is False
    assert result["error"]["code"] == "detection_failed"


def test_run_linter_explicit_command(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = mod.run_linter(command='python -c "print(1)"', repository_path=str(proj))
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0


def test_run_formatter_appends_paths(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = mod.run_formatter(
        command='python -c "import sys; print(sys.argv)"',
        paths=["src/a.py", "src/b.py"],
        repository_path=str(proj),
    )
    assert result["ok"] is True
    assert "src/a.py" in result["data"]["command"]
    bad = mod.run_formatter(
        command="fmt", paths=["--evil"], repository_path=str(proj)
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "invalid_paths"


def test_repository_path_escape_rejected(mod, tmp_path: Path) -> None:
    result = mod.run_tests(command="echo hi", repository_path="../../..")
    assert result["ok"] is False
    assert result["error"]["code"] == "path_escape"


# ── inspect_build_errors parsing ────────────────────────────────────────────

_FAKE_BUILD_OUTPUT = r"""
error[E0308]: mismatched types
 --> src/main.rs:10:5
src/app.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.
FAILED tests/test_a.py::test_x - AssertionError: nope
src/lib.py:12:4: E501 line too long
"""


def test_inspect_build_errors_parses_formats(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    # Use the parser directly on representative output.
    errors = mod._parse_build_errors(_FAKE_BUILD_OUTPUT)
    by_source = {e["source"]: e for e in errors}
    assert by_source["rustc"]["file"] == "src/main.rs"
    assert by_source["rustc"]["line"] == 10
    assert by_source["rustc"]["code"] == "E0308"
    assert by_source["tsc"]["file"] == "src/app.ts"
    assert by_source["tsc"]["code"] == "TS2322"
    assert by_source["pytest"]["file"] == "tests/test_a.py"
    assert "AssertionError" in by_source["pytest"]["message"]
    assert by_source["generic"]["code"] == "E501"
    assert by_source["generic"]["line"] == 12


def test_inspect_build_errors_end_to_end(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    command = (
        'python -c "import sys; '
        "print('error[E0308]: mismatched types'); "
        "print(' --> src/main.rs:10:5'); "
        "print('FAILED tests/test_a.py::test_x - AssertionError: nope'); "
        'sys.exit(1)"'
    )
    result = mod.inspect_build_errors(command, repository_path=str(proj))
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 1
    assert result["data"]["error_count"] == 2
    sources = {e["source"] for e in result["data"]["errors"]}
    assert sources == {"rustc", "pytest"}
    assert result["data"]["output_tail"]


def test_inspect_build_errors_clean_build(mod, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    result = mod.inspect_build_errors('python -c "print(1)"', repository_path=str(proj))
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["error_count"] == 0

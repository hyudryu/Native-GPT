"""Tests for tools/skills/tool.py (plus the built-in skills shipped in /skills)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "skills_tool_under_test"
REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST = {
    "schema_version": 1,
    "id": "test-skill",
    "name": "Test Skill",
    "description": "A fake skill about widget testing for unit tests.",
    "version": "1.0.0",
    "publisher": "Test Publisher",
    "type": "instructional",
    "trusted": False,
    "load_policy": "on_demand",
    "prompt_file": "SKILL.md",
    "tool_dependencies": ["fake-tool"],
    "service_dependencies": ["planner"],
    "default_enabled": True,
}


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Module with a fake skills root AND fake tools root under tmp_path."""
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.setenv("AGENTGPT_TOOLS_ROOT", str(tmp_path / "tools"))
    (tmp_path / "tools").mkdir()
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


@pytest.fixture()
def real_mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Module pointed at the REAL repo skills/ and tools/ roots (DB still temp)."""
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.delenv("AGENTGPT_SKILLS_ROOT", raising=False)
    monkeypatch.delenv("AGENTGPT_TOOLS_ROOT", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _make_skill(
    tmp_path: Path,
    skill_id: str = "test-skill",
    manifest: dict | None = None,
    body: str = "# Test Skill\n\nFollow the widget testing discipline.\n",
) -> Path:
    data = dict(MANIFEST if manifest is None else manifest)
    data["id"] = skill_id
    folder = tmp_path / "skills" / skill_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "skill.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "SKILL.md").write_text(body, encoding="utf-8")
    return folder


def _make_tool(tmp_path: Path, tool_id: str = "fake-tool") -> None:
    folder = tmp_path / "tools" / tool_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"id": tool_id}), encoding="utf-8")
    (folder / "tool.py").write_text("TOOL = []\n", encoding="utf-8")


# ── list / get ───────────────────────────────────────────────────────────────


def test_list_discovers_skills_with_enablement(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    _make_skill(tmp_path, "off-skill", manifest={**MANIFEST, "default_enabled": False})
    result = mod.list_skills()
    assert result["ok"] is True
    by_id = {s["skill_id"]: s for s in result["data"]["skills"]}
    assert by_id["test-skill"]["enabled"] is True
    assert by_id["off-skill"]["enabled"] is False

    only_enabled = mod.list_skills(enabled=True)
    assert {s["skill_id"] for s in only_enabled["data"]["skills"]} == {"test-skill"}


def test_list_skips_broken_manifests(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    broken = tmp_path / "skills" / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "skill.json").write_text("{not json", encoding="utf-8")
    result = mod.list_skills()
    assert [s["skill_id"] for s in result["data"]["skills"]] == ["test-skill"]


def test_get_returns_manifest_and_prompt(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path, body="# Custom Body\n\nDo the thing.\n")
    result = mod.get_skill("test-skill")
    assert result["ok"] is True
    assert result["data"]["manifest"]["name"] == "Test Skill"
    assert "Do the thing." in result["data"]["prompt"]
    assert result["data"]["enabled"] is True


def test_get_unknown_skill_raises(mod) -> None:
    with pytest.raises(mod.SkillToolError) as excinfo:
        mod.get_skill("nope")
    assert excinfo.value.code == "not_found"


# ── search ───────────────────────────────────────────────────────────────────


def test_search_ranks_by_token_match(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    _make_skill(
        tmp_path,
        "other-skill",
        manifest={**MANIFEST, "name": "Other", "description": "unrelated topic"},
        body="# Other\n\nNothing about the query here.\n",
    )
    result = mod.search_skills("widget testing")
    assert result["ok"] is True
    assert result["data"]["count"] >= 1
    assert result["data"]["hits"][0]["skill_id"] == "test-skill"


def test_search_filters_by_required_capabilities(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    hit = mod.search_skills("widget", required_capabilities=["fake-tool"])
    assert hit["data"]["count"] == 1
    miss = mod.search_skills("widget", required_capabilities=["no-such-capability"])
    assert miss["data"]["count"] == 0


# ── enable / disable ─────────────────────────────────────────────────────────


def test_enable_disable_roundtrip_and_precedence(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)  # default_enabled: True
    result = mod.set_enabled("test-skill", False, scope="user")
    assert result["data"]["effective_enabled"] is False
    assert mod.get_skill("test-skill")["data"]["enabled"] is False

    # A more specific scope row wins over the user-scope disable.
    mod.set_enabled("test-skill", True, scope="conversation", scope_id="conv-1")
    listed = mod.list_skills()["data"]["skills"][0]
    assert listed["enabled"] is True
    # But evaluating only the user scope still shows disabled.
    scoped = mod.list_skills(scope="user")["data"]["skills"][0]
    assert scoped["enabled"] is False

    mod.set_enabled("test-skill", True, scope="user")
    assert mod.get_skill("test-skill")["data"]["enabled"] is True


def test_enable_rejects_unknown_skill(mod) -> None:
    with pytest.raises(mod.SkillToolError) as excinfo:
        mod.set_enabled("ghost", True)
    assert excinfo.value.code == "not_found"


# ── validate ─────────────────────────────────────────────────────────────────


def test_validate_happy_path(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    _make_tool(tmp_path, "fake-tool")
    _make_tool(tmp_path, "todo-list")  # planner service maps here
    result = mod.validate_skill("test-skill")
    assert result["data"]["valid"] is True
    assert all(c["passed"] for c in result["data"]["checks"])


def test_validate_reports_missing_fields_and_dependencies(mod, tmp_path: Path) -> None:
    manifest = {k: v for k, v in MANIFEST.items() if k != "version"}
    _make_skill(tmp_path, manifest=manifest)
    result = mod.validate_skill("test-skill")
    assert result["data"]["valid"] is False
    failed = {c["check"] for c in result["data"]["checks"] if not c["passed"]}
    assert "required_fields" in failed
    assert "tool_dependency:fake-tool" in failed
    assert "service_dependency:planner" in failed


def test_validate_missing_folder(mod) -> None:
    result = mod.validate_skill("ghost")
    assert result["data"]["valid"] is False
    assert result["data"]["checks"][0]["check"] == "folder_exists"


# ── install / uninstall ──────────────────────────────────────────────────────


def test_install_copies_into_skills_root(mod, tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "cool-skill"
    source.mkdir(parents=True)
    manifest = {**MANIFEST, "id": "cool-skill", "publisher": "Someone Else"}
    (source / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "SKILL.md").write_text("# Cool\n", encoding="utf-8")

    result = mod.install("incoming/cool-skill")
    assert result["ok"] is True, result
    assert (tmp_path / "skills" / "cool-skill" / "skill.json").is_file()
    assert mod.get_skill("cool-skill")["ok"] is True

    with pytest.raises(mod.SkillToolError) as excinfo:
        mod.install("incoming/cool-skill")
    assert excinfo.value.code == "already_exists"


def test_install_rejects_invalid_source(mod, tmp_path: Path) -> None:
    bad = tmp_path / "incoming" / "no-prompt"
    bad.mkdir(parents=True)
    (bad / "skill.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    with pytest.raises(mod.SkillToolError) as excinfo:
        mod.install("incoming/no-prompt")
    assert excinfo.value.code == "invalid_manifest"


def test_uninstall_removes_folder_and_settings(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    mod.set_enabled("test-skill", False)
    result = mod.uninstall("test-skill")
    assert result["ok"] is True
    assert not (tmp_path / "skills" / "test-skill").exists()
    assert mod.list_skills()["data"]["count"] == 0


def test_uninstall_refuses_builtin(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path, manifest={**MANIFEST, "publisher": "Native GPT"})
    with pytest.raises(mod.SkillToolError) as excinfo:
        mod.uninstall("test-skill")
    assert excinfo.value.code == "builtin_protected"
    assert (tmp_path / "skills" / "test-skill").exists()


# ── dependencies ─────────────────────────────────────────────────────────────


def test_get_dependencies_reports_availability(mod, tmp_path: Path) -> None:
    _make_skill(tmp_path)
    _make_tool(tmp_path, "fake-tool")
    _make_tool(tmp_path, "todo-list")
    result = mod.get_dependencies("test-skill")
    assert result["data"]["all_available"] is True
    assert result["data"]["services"] == [
        {"service": "planner", "maps_to_tool": "todo-list", "available": True}
    ]

    (tmp_path / "tools" / "fake-tool" / "tool.py").unlink()
    result = mod.get_dependencies("test-skill")
    assert result["data"]["all_available"] is False
    assert result["data"]["tools"][0]["available"] is False


# ── real built-in skills ─────────────────────────────────────────────────────


def test_builtin_skills_discovered_and_valid(real_mod) -> None:
    listed = real_mod.list_skills()
    ids = {s["skill_id"] for s in listed["data"]["skills"]}
    assert {"critical-thinking", "plan-execute-verify"} <= ids

    for skill_id in ("critical-thinking", "plan-execute-verify"):
        result = real_mod.validate_skill(skill_id)
        failed = [c for c in result["data"]["checks"] if not c["passed"]]
        assert result["data"]["valid"] is True, (skill_id, failed)
        got = real_mod.get_skill(skill_id)
        assert got["data"]["manifest"]["publisher"] == "Native GPT"
        assert len(got["data"]["prompt"]) > 1000
        deps = real_mod.get_dependencies(skill_id)
        assert deps["data"]["all_available"] is True


def test_builtin_skills_uninstall_protected(real_mod) -> None:
    with pytest.raises(real_mod.SkillToolError) as excinfo:
        real_mod.uninstall("critical-thinking")
    assert excinfo.value.code == "builtin_protected"


# ── wrapper contract ─────────────────────────────────────────────────────────


def test_tool_wrapper_returns_error_dict(mod) -> None:
    result = mod.skills_get("ghost")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_tool_export_lists_all_tools(mod) -> None:
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "skills_list",
        "skills_get",
        "skills_search",
        "skills_enable",
        "skills_disable",
        "skills_validate",
        "skills_install",
        "skills_uninstall",
        "skills_get_dependencies",
    }

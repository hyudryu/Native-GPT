"""Skills Registry Strands tools — discover, load, and manage instructional skills.

Multi-tool folder: `TOOL` is a list of Strands tools. A skill is a folder
under the skills root (`$AGENTGPT_SKILLS_ROOT` when set — the test hook —
else `<repo>/skills/`) containing:

    skills/<skill-id>/
      skill.json    # manifest (see REQUIRED_MANIFEST_FIELDS)
      SKILL.md      # the instructional prompt, loaded on demand via skills_get

The FILESYSTEM is the source of truth for what exists; the database holds
state about skills:

  - `skill_settings` (migration 0011) — enable/disable rows keyed
    (skill_id, scope, scope_id). Effective enablement is resolved along the
    precedence chain conversation > project > profile > user > global;
    with no row, the manifest's `default_enabled` wins.
  - `skills` (migration 0011) — registry mirror of discovered/installed
    manifests, refreshed (upsert only) when skills_list runs and maintained
    by install/uninstall.

Dependency resolution: tool_dependencies refer to `tools/<id>` folders
(available when manifest.json + tool.py exist there; `$AGENTGPT_TOOLS_ROOT`
overrides the tools root for tests). service_dependencies use the known
service names in SERVICE_DEPENDENCY_MAP (planner/goal-supervisor/memory/
knowledge), each mapping to the tool folder that provides the service.

Risk note: install/uninstall write to the local skills directory, but the
folder risk is "read" because skills are inert instructional text — nothing
executes a skill; the model reads it. Uninstall refuses built-in skills
(publisher "Native GPT").
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands import tool

# Load shared `_lib` helpers by file path (no package context when the
# runtime imports this file standalone).
_LIB_DIR = Path(__file__).resolve().parent.parent / "_lib"


def _load_lib(filename: str, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _LIB_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_db = _load_lib("db.py", "agentgpt_tools_db")
_paths = _load_lib("paths.py", "agentgpt_tools_paths")

SKILL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "id",
    "name",
    "description",
    "version",
    "publisher",
    "type",
    "trusted",
    "load_policy",
    "prompt_file",
    "tool_dependencies",
    "service_dependencies",
    "default_enabled",
)
SCOPES = ("user", "profile", "project", "conversation", "global")
# Most specific first; the first settings row found on this chain wins.
SCOPE_PRECEDENCE = ("conversation", "project", "profile", "user", "global")
BUILTIN_PUBLISHER = "Native GPT"
SERVICE_DEPENDENCY_MAP = {
    "planner": "todo-list",
    "goal-supervisor": "goal-supervisor",
    "memory": "memory",
    "knowledge": "knowledge",
}
SEARCH_LIMIT_MAX = 50
PROMPT_MAX_BYTES = 512 * 1024  # refuse to load absurdly large prompt files


class SkillToolError(ValueError):
    """Any skills-tool failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ok(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise SkillToolError("db_unavailable", str(exc)) from exc


def _skills_root() -> Path:
    override = os.environ.get("AGENTGPT_SKILLS_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return _paths.repo_root() / "skills"


def _tools_root() -> Path:
    override = os.environ.get("AGENTGPT_TOOLS_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return _paths.repo_root() / "tools"


def _validate_skill_id(skill_id: Any) -> str:
    skill_id = _require_text(skill_id, "skill_id")
    if not SKILL_ID_RE.fullmatch(skill_id):
        raise SkillToolError("validation_error", f"invalid skill id: {skill_id!r}")
    return skill_id


def _skill_dir(skill_id: str) -> Path:
    skill_id = _validate_skill_id(skill_id)
    directory = (_skills_root() / skill_id).resolve()
    if _skills_root() not in directory.parents:
        raise SkillToolError("validation_error", f"invalid skill id: {skill_id!r}")
    return directory


def _read_manifest(directory: Path) -> dict[str, Any]:
    """Parse a skill folder's manifest; raises validation_error when broken."""
    manifest_path = directory / "skill.json"
    if not manifest_path.is_file():
        raise SkillToolError("not_found", f"no skill.json in {directory.name}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillToolError("invalid_manifest", f"skill.json unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise SkillToolError("invalid_manifest", "skill.json must be an object with an id")
    return data


def _discover() -> list[dict[str, Any]]:
    """All skill folders under the skills root, sorted by id.

    Folders with a broken manifest are skipped (skills_validate reports on a
    specific skill's problems instead of breaking discovery).
    """
    root = _skills_root()
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            manifest = _read_manifest(child)
        except SkillToolError:
            continue
        manifest = dict(manifest)
        manifest.setdefault("id", child.name)
        found.append({"manifest": manifest, "directory": child})
    found.sort(key=lambda entry: entry["manifest"]["id"])
    return found


def _find_skill(skill_id: str) -> dict[str, Any]:
    directory = _skill_dir(skill_id)
    if not directory.is_dir():
        raise SkillToolError("not_found", f"skill not found: {skill_id}")
    manifest = _read_manifest(directory)
    if manifest.get("id") != skill_id:
        raise SkillToolError(
            "invalid_manifest",
            f"manifest id {manifest.get('id')!r} does not match folder {skill_id!r}",
        )
    return {"manifest": manifest, "directory": directory}


def _normalize_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise SkillToolError("validation_error", f"{field} must be a list of non-empty strings")
    return [v.strip() for v in value]


def _normalize_scope(scope: Any) -> str:
    if scope not in SCOPES:
        raise SkillToolError("validation_error", f"scope must be one of {SCOPES}")
    return scope


def _effective_enabled(
    conn: sqlite3.Connection, skill_id: str, manifest: dict[str, Any], scope: str | None = None
) -> bool:
    """Resolve enablement along the precedence chain, then manifest default."""
    chain = [scope, "global"] if scope else list(SCOPE_PRECEDENCE)
    placeholders = ", ".join("?" for _ in chain)
    rows = conn.execute(
        f"SELECT scope, enabled FROM skill_settings WHERE skill_id = ? AND scope IN ({placeholders})",
        (skill_id, *chain),
    ).fetchall()
    by_scope = {row["scope"]: bool(row["enabled"]) for row in rows}
    for level in chain:
        if level in by_scope:
            return by_scope[level]
    return bool(manifest.get("default_enabled", False))


def _sync_registry(conn: sqlite3.Connection, discovered: list[dict[str, Any]]) -> None:
    """Upsert discovered skills into the `skills` table (never deletes)."""
    now = _now()
    for entry in discovered:
        manifest = entry["manifest"]
        conn.execute(
            "INSERT INTO skills (id, name, description, version, publisher, type, trusted,"
            " load_policy, prompt_file, tool_dependencies_json, service_dependencies_json,"
            " default_enabled, install_path, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
            " description = excluded.description, version = excluded.version,"
            " publisher = excluded.publisher, type = excluded.type, trusted = excluded.trusted,"
            " load_policy = excluded.load_policy, prompt_file = excluded.prompt_file,"
            " tool_dependencies_json = excluded.tool_dependencies_json,"
            " service_dependencies_json = excluded.service_dependencies_json,"
            " default_enabled = excluded.default_enabled,"
            " install_path = excluded.install_path, updated_at = excluded.updated_at",
            (
                manifest["id"],
                manifest.get("name", manifest["id"]),
                manifest.get("description"),
                manifest.get("version"),
                manifest.get("publisher"),
                manifest.get("type"),
                1 if manifest.get("trusted") else 0,
                manifest.get("load_policy"),
                manifest.get("prompt_file"),
                json.dumps(manifest.get("tool_dependencies") or [], ensure_ascii=False),
                json.dumps(manifest.get("service_dependencies") or [], ensure_ascii=False),
                1 if manifest.get("default_enabled") else 0,
                str(entry["directory"]),
                now,
                now,
            ),
        )


def _summary_dict(
    conn: sqlite3.Connection, entry: dict[str, Any], scope: str | None
) -> dict[str, Any]:
    manifest = entry["manifest"]
    return {
        "skill_id": manifest["id"],
        "name": manifest.get("name", manifest["id"]),
        "description": manifest.get("description"),
        "version": manifest.get("version"),
        "publisher": manifest.get("publisher"),
        "type": manifest.get("type"),
        "trusted": bool(manifest.get("trusted", False)),
        "load_policy": manifest.get("load_policy"),
        "tool_dependencies": manifest.get("tool_dependencies") or [],
        "service_dependencies": manifest.get("service_dependencies") or [],
        "enabled": _effective_enabled(conn, manifest["id"], manifest, scope),
        "default_enabled": bool(manifest.get("default_enabled", False)),
        "path": str(entry["directory"]),
    }


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def _tool_available(tool_id: str) -> bool:
    folder = _tools_root() / tool_id
    return (folder / "manifest.json").is_file() and (folder / "tool.py").is_file()


# ── plain implementations ───────────────────────────────────────────────────


def list_skills(
    scope: str | None = None,
    enabled: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Discover skills and join enablement state; see skills_list."""
    if scope is not None:
        scope = _normalize_scope(scope)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise SkillToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > 100:
        raise SkillToolError("validation_error", "limit must be between 1 and 100")

    discovered = _discover()
    conn = _connect()
    try:
        _sync_registry(conn, discovered)
        conn.commit()
        rows = [_summary_dict(conn, entry, scope) for entry in discovered]
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is bool(enabled)]
        if cursor:
            cursor = _validate_skill_id(cursor)
            rows = [row for row in rows if row["skill_id"] > cursor]
        page = rows[:limit]
        next_cursor = page[-1]["skill_id"] if len(rows) > limit and page else None
        return _ok(
            f"{len(page)} skill(s)",
            {"skills": page, "count": len(page), "next_cursor": next_cursor},
        )
    finally:
        conn.close()


def get_skill(skill_id: str) -> dict[str, Any]:
    """Manifest + SKILL.md body for one skill; see skills_get."""
    entry = _find_skill(skill_id)
    manifest = entry["manifest"]
    prompt_file = manifest.get("prompt_file") or "SKILL.md"
    prompt_path = (entry["directory"] / prompt_file).resolve()
    if entry["directory"].resolve() not in prompt_path.parents:
        raise SkillToolError("invalid_manifest", "prompt_file escapes the skill folder")
    if not prompt_path.is_file():
        raise SkillToolError("not_found", f"prompt file missing: {prompt_file}")
    if prompt_path.stat().st_size > PROMPT_MAX_BYTES:
        raise SkillToolError("invalid_manifest", "prompt file exceeds the 512 KB cap")
    prompt = prompt_path.read_text(encoding="utf-8")
    conn = _connect()
    try:
        enabled = _effective_enabled(conn, manifest["id"], manifest)
    finally:
        conn.close()
    return _ok(
        f"skill {manifest['id']}: {manifest.get('name', manifest['id'])}",
        {
            "skill_id": manifest["id"],
            "manifest": manifest,
            "prompt_file": prompt_file,
            "prompt": prompt,
            "enabled": enabled,
            "path": str(entry["directory"]),
        },
    )


def search_skills(
    query: str,
    required_capabilities: Any = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Token-match search over skill name/description/SKILL.md; see skills_search."""
    query = _require_text(query, "query")
    capabilities = _normalize_str_list(required_capabilities, "required_capabilities")
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise SkillToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > SEARCH_LIMIT_MAX:
        raise SkillToolError(
            "validation_error", f"limit must be between 1 and {SEARCH_LIMIT_MAX}"
        )

    query_terms = _tokens(query)
    if not query_terms:
        raise SkillToolError("validation_error", "query has no searchable terms")

    hits: list[dict[str, Any]] = []
    for entry in _discover():
        manifest = entry["manifest"]
        haystacks = {
            "name": _tokens(str(manifest.get("name", ""))),
            "description": _tokens(str(manifest.get("description", ""))),
            "body": set(),
        }
        prompt_file = manifest.get("prompt_file") or "SKILL.md"
        prompt_path = entry["directory"] / prompt_file
        if prompt_path.is_file() and prompt_path.stat().st_size <= PROMPT_MAX_BYTES:
            haystacks["body"] = _tokens(prompt_path.read_text(encoding="utf-8"))

        if capabilities:
            capability_text = " ".join(
                [
                    str(manifest.get("name", "")),
                    str(manifest.get("description", "")),
                    " ".join(manifest.get("tool_dependencies") or []),
                    " ".join(manifest.get("service_dependencies") or []),
                ]
            ).lower()
            if any(capability.lower() not in capability_text for capability in capabilities):
                continue

        name_hits = len(query_terms & haystacks["name"])
        description_hits = len(query_terms & haystacks["description"])
        body_hits = len(query_terms & haystacks["body"])
        total = len(query_terms)
        score = 0.5 * (name_hits / total) + 0.3 * (description_hits / total) + 0.2 * (
            body_hits / total
        )
        if score <= 0:
            continue
        hits.append(
            {
                "skill_id": manifest["id"],
                "name": manifest.get("name", manifest["id"]),
                "description": manifest.get("description"),
                "score": round(score, 4),
                "matched": {
                    "name": name_hits,
                    "description": description_hits,
                    "body": body_hits,
                },
            }
        )
    hits.sort(key=lambda hit: (-hit["score"], hit["skill_id"]))
    hits = hits[:limit]
    return _ok(
        f"{len(hits)} skill(s) matching {query!r}",
        {"hits": hits, "count": len(hits), "query": query},
    )


def set_enabled(
    skill_id: str, enabled: bool, scope: str = "user", scope_id: str | None = None
) -> dict[str, Any]:
    """Upsert a skill_settings enablement row; see skills_enable/skills_disable."""
    skill_id = _validate_skill_id(skill_id)
    scope = _normalize_scope(scope)
    scope_id = (scope_id or "").strip()
    entry = _find_skill(skill_id)  # must exist to toggle
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO skill_settings (skill_id, scope, scope_id, enabled, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(skill_id, scope, scope_id) DO UPDATE SET"
            " enabled = excluded.enabled, updated_at = excluded.updated_at",
            (skill_id, scope, scope_id, 1 if enabled else 0, _now()),
        )
        conn.commit()
        effective = _effective_enabled(conn, skill_id, entry["manifest"])
        return _ok(
            f"skill {skill_id} {'enabled' if enabled else 'disabled'} at {scope} scope",
            {
                "skill_id": skill_id,
                "scope": scope,
                "scope_id": scope_id,
                "enabled": enabled,
                "effective_enabled": effective,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_skill(skill_id: str) -> dict[str, Any]:
    """Structural validation of a skill folder; see skills_validate."""
    skill_id = _validate_skill_id(skill_id)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str = "") -> bool:
        checks.append({"check": name, "passed": passed, "detail": detail})
        return passed

    directory = _skill_dir(skill_id)
    if not check("folder_exists", directory.is_dir(), str(directory)):
        return _ok(f"skill {skill_id}: invalid", {"skill_id": skill_id, "valid": False,
                                                  "checks": checks})

    manifest: dict[str, Any] | None = None
    manifest_path = directory / "skill.json"
    if check("manifest_exists", manifest_path.is_file()):
        try:
            manifest = _read_manifest(directory)
            check("manifest_parses", True)
        except SkillToolError as exc:
            check("manifest_parses", False, str(exc))

    if manifest is not None:
        missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
        check("required_fields", not missing, f"missing: {', '.join(missing)}" if missing else "")
        check(
            "id_matches_folder",
            manifest.get("id") == skill_id,
            f"manifest id is {manifest.get('id')!r}",
        )
        prompt_file = manifest.get("prompt_file") or "SKILL.md"
        prompt_path = (directory / prompt_file).resolve()
        prompt_ok = (
            directory.resolve() in prompt_path.parents or prompt_path.parent == directory.resolve()
        ) and prompt_path.is_file()
        check("prompt_file_present", prompt_ok, prompt_file)

        for dependency in manifest.get("tool_dependencies") or []:
            check(
                f"tool_dependency:{dependency}",
                _tool_available(dependency),
                "tools/%s %s" % (dependency, "found" if _tool_available(dependency) else "missing"),
            )
        for service in manifest.get("service_dependencies") or []:
            target = SERVICE_DEPENDENCY_MAP.get(service)
            check(
                f"service_dependency:{service}",
                target is not None and _tool_available(target),
                f"maps to tools/{target}" if target else "unknown service name",
            )

    valid = all(item["passed"] for item in checks)
    return _ok(
        f"skill {skill_id}: {'valid' if valid else 'invalid'}",
        {"skill_id": skill_id, "valid": valid, "checks": checks},
    )


def install(source_path: str) -> dict[str, Any]:
    """Copy a local skill folder into the skills root; see skills_install."""
    source_path = _require_text(source_path, "source_path")
    source = _paths.resolve_under_root(source_path)
    if not source.is_dir():
        raise SkillToolError("not_found", f"source is not a directory: {source_path}")
    manifest = _read_manifest(source)
    prompt_file = manifest.get("prompt_file") or "SKILL.md"
    if not (source / prompt_file).is_file():
        raise SkillToolError(
            "invalid_manifest", f"source is missing its prompt file ({prompt_file})"
        )
    skill_id = _validate_skill_id(manifest.get("id"))
    missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
    if missing:
        raise SkillToolError(
            "invalid_manifest", f"manifest is missing fields: {', '.join(missing)}"
        )

    target = _skills_root() / skill_id
    if target.exists():
        raise SkillToolError(
            "already_exists", f"skill {skill_id} already installed at {target}"
        )
    _skills_root().mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    conn = _connect()
    try:
        _sync_registry(conn, [{"manifest": manifest, "directory": target}])
        conn.commit()
    finally:
        conn.close()
    return _ok(
        f"skill installed: {skill_id}",
        {"skill_id": skill_id, "installed_to": str(target), "source": str(source)},
    )


def uninstall(skill_id: str) -> dict[str, Any]:
    """Remove an installed skill folder and its DB rows; refuses built-ins."""
    entry = _find_skill(skill_id)
    manifest = entry["manifest"]
    if manifest.get("publisher") == BUILTIN_PUBLISHER:
        raise SkillToolError(
            "builtin_protected",
            f"skill {skill_id} is a built-in (publisher {BUILTIN_PUBLISHER!r}); "
            "disable it with skills_disable instead of uninstalling",
        )
    shutil.rmtree(entry["directory"])
    conn = _connect()
    try:
        conn.execute("DELETE FROM skill_settings WHERE skill_id = ?", (skill_id,))
        conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        conn.commit()
    finally:
        conn.close()
    return _ok(
        f"skill uninstalled: {skill_id}",
        {"skill_id": skill_id, "removed": True, "settings_removed": True},
    )


def get_dependencies(skill_id: str) -> dict[str, Any]:
    """Resolved tool/service dependency list with availability flags."""
    entry = _find_skill(skill_id)
    manifest = entry["manifest"]
    tools = [
        {
            "tool_id": dependency,
            "available": _tool_available(dependency),
            "path": str(_tools_root() / dependency),
        }
        for dependency in manifest.get("tool_dependencies") or []
    ]
    services = []
    for service in manifest.get("service_dependencies") or []:
        target = SERVICE_DEPENDENCY_MAP.get(service)
        services.append(
            {
                "service": service,
                "maps_to_tool": target,
                "available": target is not None and _tool_available(target),
            }
        )
    all_available = all(t["available"] for t in tools) and all(
        s["available"] for s in services
    )
    return _ok(
        f"skill {skill_id}: {len(tools)} tool + {len(services)} service dependencies",
        {
            "skill_id": skill_id,
            "tools": tools,
            "services": services,
            "all_available": all_available,
        },
    )


# ── Strands tool wrappers ─────────────────────────────────────────────────


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except SkillToolError as exc:
        return {
            "ok": False,
            "summary": str(exc),
            "data": {},
            "error": {"code": exc.code, "message": str(exc)},
        }
    except _paths.PathEscapeError as exc:
        return {
            "ok": False,
            "summary": str(exc),
            "data": {},
            "error": {"code": "invalid_path", "message": str(exc)},
        }
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "summary": f"database error: {exc}",
            "data": {},
            "error": {"code": "db_error", "message": str(exc)},
        }


@tool
def skills_list(
    scope: str | None = None,
    enabled: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List installed skills with their effective enablement state.

    Discovers skill folders under the skills root, joins skill_settings rows
    (precedence: conversation > project > profile > user > global, then the
    manifest's default_enabled), and mirrors manifests into the skills table.
    Pagination is keyset on skill_id (pass `next_cursor` back as `cursor`).

    Args:
        scope: Evaluate enablement for this scope (falls back to global rows
            and then the manifest default).
        enabled: True = only enabled skills, False = only disabled ones.
        limit: Page size (1-100, default 20).
        cursor: Keyset cursor from a previous response.

    Returns:
        `{ok, summary, data: {skills: [...], count, next_cursor}, error}`.
    """
    return _wrap(list_skills, scope, enabled, limit, cursor)


@tool
def skills_get(skill_id: str) -> dict[str, Any]:
    """Load a skill's manifest AND its SKILL.md instructions (on-demand load).

    This is how you adopt a skill: read the `prompt` field and follow it for
    the rest of the task. Use skills_search / skills_list to discover ids.

    Args:
        skill_id: Skill id (folder name under the skills root).

    Returns:
        `{ok, summary, data: {skill_id, manifest, prompt_file, prompt,
        enabled, path}, error}`.
    """
    return _wrap(get_skill, skill_id)


@tool
def skills_search(
    query: str,
    required_capabilities: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search skills by token match over name, description, and SKILL.md body.

    Args:
        query: Free-text search (tokenized; ranked name > description > body).
        required_capabilities: Tokens that must each appear in the skill's
            name, description, or declared dependencies.
        limit: Maximum hits (1-50, default 10).

    Returns:
        `{ok, summary, data: {hits: [{skill_id, name, description, score,
        matched}], count}, error}`.
    """
    return _wrap(search_skills, query, required_capabilities, limit)


@tool
def skills_enable(
    skill_id: str, scope: str = "user", scope_id: str | None = None
) -> dict[str, Any]:
    """Enable a skill at a scope (upserts a skill_settings row).

    Args:
        skill_id: Skill to enable.
        scope: user | profile | project | conversation | global.
        scope_id: Id within the scope (empty for user/global-wide rows).

    Returns:
        `{ok, summary, data: {skill_id, scope, scope_id, enabled,
        effective_enabled}, error}`.
    """
    return _wrap(set_enabled, skill_id, True, scope, scope_id)


@tool
def skills_disable(
    skill_id: str, scope: str = "user", scope_id: str | None = None
) -> dict[str, Any]:
    """Disable a skill at a scope (upserts a skill_settings row).

    Args:
        skill_id: Skill to disable.
        scope: user | profile | project | conversation | global.
        scope_id: Id within the scope (empty for user/global-wide rows).

    Returns:
        `{ok, summary, data: {skill_id, scope, scope_id, enabled,
        effective_enabled}, error}`.
    """
    return _wrap(set_enabled, skill_id, False, scope, scope_id)


@tool
def skills_validate(skill_id: str) -> dict[str, Any]:
    """Validate a skill folder: manifest schema, prompt file, dependencies.

    Checks every required manifest field, that the manifest id matches the
    folder, that the prompt file exists, and that every declared tool/service
    dependency resolves against the local tools directory.

    Args:
        skill_id: Skill to validate.

    Returns:
        `{ok, summary, data: {skill_id, valid, checks: [{check, passed,
        detail}]}, error}`.
    """
    return _wrap(validate_skill, skill_id)


@tool
def skills_install(source_path: str) -> dict[str, Any]:
    """Install a skill by copying a local folder into the skills root.

    The source must be under the allowed roots and contain a valid skill.json
    plus its prompt file. Refuses to overwrite an existing skill id.

    Args:
        source_path: Path to the skill folder (contains skill.json + SKILL.md).

    Returns:
        `{ok, summary, data: {skill_id, installed_to, source}, error}`.
    """
    return _wrap(install, source_path)


@tool
def skills_uninstall(skill_id: str) -> dict[str, Any]:
    """Uninstall a skill: remove its folder, settings rows, and registry row.

    Built-in skills (publisher "Native GPT") are protected — disable them
    with skills_disable instead.

    Args:
        skill_id: Skill to uninstall.

    Returns:
        `{ok, summary, data: {skill_id, removed, settings_removed}, error}`.
    """
    return _wrap(uninstall, skill_id)


@tool
def skills_get_dependencies(skill_id: str) -> dict[str, Any]:
    """List a skill's tool/service dependencies with availability flags.

    Tool dependencies resolve against the local tools directory (folder with
    manifest.json + tool.py); service dependencies map known service names
    (planner, goal-supervisor, memory, knowledge) to their providing tool.

    Args:
        skill_id: Skill to inspect.

    Returns:
        `{ok, summary, data: {skill_id, tools: [{tool_id, available}],
        services: [{service, maps_to_tool, available}], all_available},
        error}`.
    """
    return _wrap(get_dependencies, skill_id)


TOOL = [
    skills_list,
    skills_get,
    skills_search,
    skills_enable,
    skills_disable,
    skills_validate,
    skills_install,
    skills_uninstall,
    skills_get_dependencies,
]

"""Tests for tools/notifications/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "notifications_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _send(mod, title: str, **kwargs) -> str:
    result = mod.send(title, **kwargs)
    assert result["ok"] is True, result
    return result["data"]["notification_id"]


# ── send ─────────────────────────────────────────────────────────────────────


def test_send_persists_row(mod, tmp_path: Path) -> None:
    notification_id = _send(
        mod, "Report ready", message="The weekly report is done", urgency="high"
    )
    listed = mod.list_rows()
    assert listed["data"]["count"] == 1
    row = listed["data"]["notifications"][0]
    assert row["notification_id"] == notification_id
    assert row["urgency"] == "high"
    assert row["read"] is False
    assert row["dismissed"] is False


def test_send_validates_urgency(mod) -> None:
    with pytest.raises(mod.NotificationToolError) as excinfo:
        mod.send("x", urgency="critical")
    assert excinfo.value.code == "validation_error"


def test_send_requires_title(mod) -> None:
    with pytest.raises(mod.NotificationToolError):
        mod.send("")


# ── list / read / dismiss ────────────────────────────────────────────────────


def test_list_unread_only_and_pagination(mod) -> None:
    ids = [_send(mod, f"n{i}") for i in range(3)]
    mod.mark_read(ids[0])

    unread = mod.list_rows(unread_only=True)
    assert unread["data"]["count"] == 2
    assert all(not n["read"] for n in unread["data"]["notifications"])

    page1 = mod.list_rows(limit=2)
    assert page1["data"]["count"] == 2
    assert page1["data"]["next_cursor"] is not None
    page2 = mod.list_rows(limit=2, cursor=page1["data"]["next_cursor"])
    assert page2["data"]["count"] == 1
    seen = {n["notification_id"] for n in page1["data"]["notifications"]}
    seen |= {n["notification_id"] for n in page2["data"]["notifications"]}
    assert seen == set(ids)


def test_mark_read_is_idempotent(mod) -> None:
    notification_id = _send(mod, "read me")
    first = mod.mark_read(notification_id)
    assert first["data"]["already_read"] is False
    second = mod.mark_read(notification_id)
    assert second["data"]["already_read"] is True


def test_dismiss_excludes_from_listing(mod) -> None:
    keep = _send(mod, "keep")
    drop = _send(mod, "drop")
    result = mod.dismiss(drop)
    assert result["data"]["dismissed"] is True

    listed = mod.list_rows()
    ids = {n["notification_id"] for n in listed["data"]["notifications"]}
    assert ids == {keep}
    again = mod.dismiss(drop)
    assert again["data"]["already_dismissed"] is True


def test_unknown_notification_raises(mod) -> None:
    with pytest.raises(mod.NotificationToolError) as excinfo:
        mod.mark_read("ntf_missing")
    assert excinfo.value.code == "not_found"


# ── wrapper contract ─────────────────────────────────────────────────────────


def test_tool_wrapper_returns_error_dict(mod) -> None:
    result = mod.dismiss_notification("ntf_missing")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_tool_export_lists_all_tools(mod) -> None:
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "send_notification",
        "list_notifications",
        "mark_notification_read",
        "dismiss_notification",
    }

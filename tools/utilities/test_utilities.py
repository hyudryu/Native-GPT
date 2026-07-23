"""Tests for tools/utilities/tool.py."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "utilities_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


# ── convert_units ───────────────────────────────────────────────────────────


def test_convert_length(mod) -> None:
    result = mod.convert_units(1, "mi", "km")
    assert result["ok"] is True
    assert result["data"]["result"] == pytest.approx(1.609344)
    assert result["data"]["category"] == "length"


def test_convert_temperature(mod) -> None:
    result = mod.convert_units(32, "f", "c")
    assert result["ok"] is True
    assert result["data"]["result"] == pytest.approx(0.0)
    result = mod.convert_units(100, "celsius", "kelvin")
    assert result["data"]["result"] == pytest.approx(373.15)


def test_convert_data_binary_vs_decimal(mod) -> None:
    assert mod.convert_units(1, "gib", "mb")["data"]["result"] == pytest.approx(1073.741824)
    assert mod.convert_units(8, "bits", "bytes")["data"]["result"] == pytest.approx(1.0)


def test_convert_unknown_and_incompatible_units(mod) -> None:
    result = mod.convert_units(1, "furlong", "m")
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_unit"
    result = mod.convert_units(1, "kg", "m")
    assert result["ok"] is False
    assert result["error"]["code"] == "incompatible_units"
    result = mod.convert_units(1, "c", "m")
    assert result["ok"] is False
    assert result["error"]["code"] == "incompatible_units"


# ── parse_datetime / convert_timezone / date_difference ─────────────────────


def test_parse_datetime_iso(mod) -> None:
    result = mod.parse_datetime("2026-07-23T14:00:00+02:00")
    assert result["ok"] is True
    assert result["data"]["matched_format"] == "iso8601"
    assert result["data"]["utc_offset"] == 2.0


def test_parse_datetime_common_forms(mod) -> None:
    for text in ("2026-07-23 14:00", "July 23 2026", "July 23, 2026", "23 July 2026", "07/23/2026"):
        result = mod.parse_datetime(text)
        assert result["ok"] is True, text
        assert result["data"]["date"] == "2026-07-23"


def test_parse_datetime_with_timezone(mod) -> None:
    result = mod.parse_datetime("2026-07-23 14:00", timezone="America/New_York")
    assert result["ok"] is True
    assert result["data"]["unix_timestamp"] is not None
    assert "New_York" in result["data"]["timezone"]


def test_parse_datetime_rejects_natural_language(mod) -> None:
    result = mod.parse_datetime("tomorrow")
    assert result["ok"] is False
    assert result["error"]["code"] == "unparseable_datetime"


def test_convert_timezone(mod) -> None:
    result = mod.convert_timezone("2026-07-23 14:00", "Europe/Berlin", "America/New_York")
    assert result["ok"] is True
    # Berlin is UTC+2 in July, New York UTC-4: 14:00 Berlin = 08:00 New York.
    assert "T08:00:00" in result["data"]["result_iso"]


def test_convert_timezone_unknown_zone(mod) -> None:
    result = mod.convert_timezone("2026-07-23 14:00", "Mars/Olympus", "UTC")
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_timezone"


def test_date_difference_units(mod) -> None:
    result = mod.date_difference("2026-01-01", "2026-01-31", unit="days")
    assert result["ok"] is True
    assert result["data"]["value"] == 30.0
    result = mod.date_difference("2026-01-15", "2026-03-15", unit="months")
    assert result["data"]["value"] == 2
    result = mod.date_difference("2026-01-01", "2025-01-01", unit="days")
    assert result["data"]["value"] == -365.0


def test_date_difference_all_units(mod) -> None:
    result = mod.date_difference("2025-07-23", "2026-07-23")
    assert result["ok"] is True
    assert result["data"]["months"] == 12
    assert result["data"]["years"] == pytest.approx(1.0)
    assert result["data"]["days"] == 365.0


def test_date_difference_partial_month_not_counted(mod) -> None:
    # Jan 31 -> Feb 28 is not a whole calendar month.
    result = mod.date_difference("2026-01-31", "2026-02-28", unit="months")
    assert result["data"]["value"] == 0


# ── UUIDs and hashing ───────────────────────────────────────────────────────


def test_generate_uuid_v4(mod) -> None:
    result = mod.generate_uuid()
    assert result["ok"] is True
    assert result["data"]["version"] == 4
    assert len(result["data"]["uuid"]) == 36


def test_generate_uuid_invalid_version(mod) -> None:
    result = mod.generate_uuid(version=7)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_version"


def test_hash_text(mod) -> None:
    result = mod.hash_text("hello")
    assert result["ok"] is True
    assert result["data"]["hexdigest"] == hashlib.sha256(b"hello").hexdigest()
    assert mod.hash_text("hello", "md5")["data"]["hexdigest"] == hashlib.md5(b"hello").hexdigest()  # noqa: S324
    bad = mod.hash_text("hello", "rot13")
    assert bad["ok"] is False
    assert bad["error"]["code"] == "unknown_algorithm"


def test_hash_file(mod, tmp_path: Path) -> None:
    payload = b"chunked hashing works" * 10000  # > 64 KB to exercise chunking
    (tmp_path / "blob.bin").write_bytes(payload)
    result = mod.hash_file("blob.bin")
    assert result["ok"] is True
    assert result["data"]["hexdigest"] == hashlib.sha256(payload).hexdigest()
    assert result["data"]["bytes"] == len(payload)


def test_hash_file_rejects_escape(mod, tmp_path: Path) -> None:
    result = mod.hash_file("../outside.bin")
    assert result["ok"] is False
    assert result["error"]["code"] == "path_escape"


def test_hash_file_missing(mod) -> None:
    result = mod.hash_file("nope.bin")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"

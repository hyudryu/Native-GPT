"""Tests for tools/fs-extensions/tool.py."""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "fs_extensions_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── stat_file ───────────────────────────────────────────────────────────────


def test_stat_file(mod, tmp_path: Path) -> None:
    payload = b"stat me"
    (tmp_path / "a.txt").write_bytes(payload)
    result = mod.stat_file("a.txt")
    assert result["ok"] is True
    data = result["data"]
    assert data["is_file"] is True and data["is_dir"] is False
    assert data["size_bytes"] == len(payload)
    assert data["sha256"] == _sha256(payload)
    assert data["permissions"].startswith("0o")


def test_stat_directory_and_missing(mod, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    result = mod.stat_file("d")
    assert result["ok"] is True
    assert result["data"]["is_dir"] is True
    assert result["data"]["sha256"] is None
    missing = mod.stat_file("nope")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"


# ── copy_file ───────────────────────────────────────────────────────────────


def test_copy_file(mod, tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_bytes(b"copy me")
    result = mod.copy_file("src.txt", "out/dst.txt")
    assert result["ok"] is True
    assert (tmp_path / "out" / "dst.txt").read_bytes() == b"copy me"
    assert result["data"]["sha256"] == _sha256(b"copy me")


def test_copy_refuses_silent_overwrite(mod, tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"1")
    (tmp_path / "b").write_bytes(b"2")
    result = mod.copy_file("a", "b")
    assert result["ok"] is False
    assert result["error"]["code"] == "destination_exists"
    ok = mod.copy_file("a", "b", overwrite=True)
    assert ok["ok"] is True
    assert (tmp_path / "b").read_bytes() == b"1"


def test_copy_hash_mismatch_aborts(mod, tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"1")
    result = mod.copy_file("a", "b", expected_source_sha256="0" * 64)
    assert result["ok"] is False
    assert result["error"]["code"] == "hash_mismatch"
    assert not (tmp_path / "b").exists()


# ── trash / restore / list / permanent delete ───────────────────────────────


def test_trash_and_restore_roundtrip(mod, tmp_path: Path) -> None:
    (tmp_path / "doc.txt").write_bytes(b"important")
    trashed = mod.trash_file("doc.txt")
    assert trashed["ok"] is True
    trash_id = trashed["data"]["trash_id"]
    assert not (tmp_path / "doc.txt").exists()

    listing = mod.list_trashed_files()
    assert listing["ok"] is True
    assert [i["trash_id"] for i in listing["data"]["items"]] == [trash_id]
    assert listing["data"]["items"][0]["sha256"] == _sha256(b"important")

    restored = mod.restore_trashed_file(trash_id)
    assert restored["ok"] is True
    assert (tmp_path / "doc.txt").read_bytes() == b"important"
    assert mod.list_trashed_files()["data"]["items"] == []


def test_restore_to_destination_and_collision(mod, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    trash_id = mod.trash_file("a.txt")["data"]["trash_id"]
    (tmp_path / "a.txt").write_bytes(b"new occupant")
    # Original path taken -> collision error.
    conflict = mod.restore_trashed_file(trash_id)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "destination_exists"
    # Explicit destination works.
    ok = mod.restore_trashed_file(trash_id, destination="recovered.txt")
    assert ok["ok"] is True
    assert (tmp_path / "recovered.txt").read_bytes() == b"x"


def test_trash_refuses_directories_and_checks_hash(mod, tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    result = mod.trash_file("dir")
    assert result["ok"] is False
    assert result["error"]["code"] == "is_directory"
    (tmp_path / "f").write_bytes(b"f")
    bad = mod.trash_file("f", expected_sha256="1" * 64)
    assert bad["ok"] is False
    assert bad["error"]["code"] == "hash_mismatch"
    assert (tmp_path / "f").exists()


def test_permanently_delete_live_and_trashed(mod, tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_bytes(b"bye")
    result = mod.permanently_delete_file(path="gone.txt")
    assert result["ok"] is True
    assert not (tmp_path / "gone.txt").exists()

    (tmp_path / "t.txt").write_bytes(b"t")
    trash_id = mod.trash_file("t.txt")["data"]["trash_id"]
    purged = mod.permanently_delete_file(trash_id=trash_id)
    assert purged["ok"] is True
    assert mod.list_trashed_files()["data"]["items"] == []

    both = mod.permanently_delete_file(path="x", trash_id="y")
    assert both["ok"] is False
    assert both["error"]["code"] == "invalid_arguments"


def test_list_trashed_files_paging(mod, tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"f{i}").write_bytes(b"x")
        assert mod.trash_file(f"f{i}")["ok"] is True
    page1 = mod.list_trashed_files(limit=2)
    assert page1["data"]["count"] == 2
    assert page1["data"]["next_cursor"] == "2"
    page2 = mod.list_trashed_files(limit=2, cursor=page1["data"]["next_cursor"])
    assert page2["data"]["count"] == 1
    assert page2["data"]["next_cursor"] is None


# ── read_binary_range ───────────────────────────────────────────────────────


def test_read_binary_range(mod, tmp_path: Path) -> None:
    payload = bytes(range(256)) * 2
    (tmp_path / "bin.dat").write_bytes(payload)
    result = mod.read_binary_range("bin.dat", offset=10, length=20)
    assert result["ok"] is True
    assert base64.b64decode(result["data"]["base64"]) == payload[10:30]
    assert result["data"]["truncated"] is True
    assert result["data"]["size_bytes"] == len(payload)


def test_read_binary_range_caps_and_validates(mod, tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"abc")
    too_big = mod.read_binary_range("bin.dat", offset=0, length=2 * 1024 * 1024)
    assert too_big["ok"] is False
    assert too_big["error"]["code"] == "invalid_length"
    negative = mod.read_binary_range("bin.dat", offset=-1, length=1)
    assert negative["ok"] is False


# ── archives ────────────────────────────────────────────────────────────────


def test_create_and_list_zip(mod, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "one.txt").write_bytes(b"1")
    (tmp_path / "two.txt").write_bytes(b"2")
    created = mod.create_archive(["sub", "two.txt"], "zip", output_name="out.zip")
    assert created["ok"] is True
    assert created["data"]["members"] == 2

    listing = mod.list_archive_contents("out.zip")
    assert listing["ok"] is True
    names = {m["name"] for m in listing["data"]["members"]}
    assert names == {"sub/one.txt", "two.txt"}
    assert all(m["safe"] for m in listing["data"]["members"])


def test_create_and_extract_tar_gz(mod, tmp_path: Path) -> None:
    (tmp_path / "in.txt").write_bytes(b"tarred")
    created = mod.create_archive(["in.txt"], "tar.gz", output_name="out.tar.gz")
    assert created["ok"] is True
    extracted = mod.extract_archive("out.tar.gz", "unpacked")
    assert extracted["ok"] is True
    assert (tmp_path / "unpacked" / "in.txt").read_bytes() == b"tarred"


def test_extract_refuses_overwrite_unless_allowed(mod, tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_bytes(b"v1")
    mod.create_archive(["x.txt"], "zip", output_name="x.zip")
    (tmp_path / "dest").mkdir()
    (tmp_path / "dest" / "x.txt").write_bytes(b"existing")
    refused = mod.extract_archive("x.zip", "dest")
    assert refused["ok"] is False
    assert refused["error"]["code"] == "destination_exists"
    ok = mod.extract_archive("x.zip", "dest", overwrite=True)
    assert ok["ok"] is True
    assert (tmp_path / "dest" / "x.txt").read_bytes() == b"v1"


def test_extract_rejects_zip_slip(mod, tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
        zf.writestr("ok.txt", "fine")
    result = mod.extract_archive("evil.zip", "dest")
    assert result["ok"] is False
    assert result["error"]["code"] == "unsafe_member"
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "dest" / "ok.txt").exists()  # all-or-nothing guard


def test_list_flags_unsafe_members(mod, tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("/abs/path.txt", "x")
        zf.writestr("good.txt", "y")
    listing = mod.list_archive_contents("evil.zip")
    assert listing["ok"] is True
    by_name = {m["name"]: m for m in listing["data"]["members"]}
    assert by_name["/abs/path.txt"]["safe"] is False
    assert by_name["good.txt"]["safe"] is True


def test_extract_rejects_tar_symlink(mod, tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
        data = b"content"
        tf.addfile(tarfile.TarInfo("real.txt"), io.BytesIO(data))
    result = mod.extract_archive("evil.tar.gz", "dest")
    assert result["ok"] is False
    assert result["error"]["code"] == "unsafe_member"


def test_archive_rejects_escaping_input(mod, tmp_path: Path) -> None:
    result = mod.create_archive(["../../outside"], "zip")
    assert result["ok"] is False
    assert result["error"]["code"] in {"path_escape", "not_found"}

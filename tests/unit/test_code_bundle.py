"""Content-addressed code bundle coverage for dirty gate runs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.code_bundle import (
    freeze_code_bundle,
    materialize_code_bundle_checkout,
    validate_code_bundle,
)
from leanfaith.schemas.manifest import collect_code_state

_MANIFEST = "CODE_BUNDLE_MANIFEST.json"
_TREE_HASH = "a" * 64


def _file(path: str, payload: bytes, *, mode: int = 0o644) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": mode,
    }


def _manifest(files: list[dict[str, object]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "code_state": {
            "git_revision": "1" * 40,
            "git_dirty": False,
            "base_git_commit": "1" * 40,
            "code_tree_hash": _TREE_HASH,
            "tracked_diff_hash": "2" * 64,
            "untracked_files": [],
        },
        "files": files,
    }


def _write_bundle(
    path: Path,
    manifest: dict[str, Any],
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> Path:
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        info = tarfile.TarInfo(_MANIFEST)
        info.size = len(manifest_bytes)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for member, payload in members:
            archive.addfile(member, None if payload is None else io.BytesIO(payload))
    return path


def _regular(name: str, payload: bytes, *, mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    return info, payload


def test_code_bundle_is_deterministic_and_validates_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    (root / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    (root / "untracked.yaml").write_text("value: 3\n", encoding="utf-8")

    first, first_hash, first_state = freeze_code_bundle(root, tmp_path / "first")
    second, second_hash, second_state = freeze_code_bundle(root, tmp_path / "second")
    assert first_hash == second_hash
    assert first.read_bytes() == second.read_bytes()
    assert first_state.code_tree_hash == second_state.code_tree_hash
    assert validate_code_bundle(first, first_state.code_tree_hash or "") == first_hash
    with pytest.raises(ValueError, match="does not match"):
        validate_code_bundle(first, "0" * 64)


def test_code_bundle_materializes_as_exact_clean_checkout(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    tracked = root / "tracked.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    tracked.chmod(0o755)
    subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    tracked.write_text("x = 2\n", encoding="utf-8")
    (root / "untracked.yaml").write_text("value: 3\n", encoding="utf-8")
    (root / "ignored.txt").write_text("not code\n", encoding="utf-8")

    bundle, digest, frozen_state = freeze_code_bundle(root, tmp_path / "bundles")
    expected_tree_hash = frozen_state.code_tree_hash
    assert expected_tree_hash is not None

    first = materialize_code_bundle_checkout(
        bundle,
        tmp_path / "checkout-a",
        expected_tree_hash,
    )
    second = materialize_code_bundle_checkout(
        bundle,
        tmp_path / "checkout-b",
        expected_tree_hash,
    )

    assert first.bundle_sha256 == second.bundle_sha256 == digest
    assert first.code_tree_hash == second.code_tree_hash == expected_tree_hash
    assert first.git_revision == second.git_revision
    assert first.file_count == second.file_count == 3
    assert (first.root / "tracked.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (first.root / "tracked.py").stat().st_mode & 0o777 == 0o755
    assert (first.root / "untracked.yaml").read_text(encoding="utf-8") == "value: 3\n"
    assert not (first.root / "ignored.txt").exists()
    assert not (first.root / _MANIFEST).exists()
    materialized_state = collect_code_state(first.root)
    assert materialized_state.git_dirty is False
    assert materialized_state.untracked_files == ()
    assert materialized_state.code_tree_hash == expected_tree_hash
    with pytest.raises(FileExistsError, match="destination already exists"):
        materialize_code_bundle_checkout(bundle, first.root, expected_tree_hash)


def test_code_bundle_materialization_failure_leaves_no_checkout(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    bundle = _write_bundle(
        tmp_path / "corrupt-materialization.tar.gz",
        _manifest([_file("source.py", payload)]),
        [_regular("source.py", b"x = 2\n")],
    )
    destination = tmp_path / "checkout"

    with pytest.raises(ValueError, match="does not match declared sha256"):
        materialize_code_bundle_checkout(bundle, destination, _TREE_HASH)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".checkout.partial-*"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"unexpected": True}, "unexpected"),
        ({"code_state": {"git_revision": "not-a-revision"}}, "git_revision"),
    ],
)
def test_code_bundle_rejects_invalid_manifest_schema(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = b"x = 1\n"
    manifest = _manifest([_file("source.py", payload)])
    nested = mutation.get("code_state")
    if isinstance(nested, dict):
        manifest["code_state"].update(nested)
    else:
        manifest.update(mutation)
    bundle = _write_bundle(
        tmp_path / "invalid-schema.tar.gz",
        manifest,
        [_regular("source.py", payload)],
    )

    with pytest.raises(ValueError, match=message):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_corrupt_member_bytes(tmp_path: Path) -> None:
    declared = b"x = 1\n"
    bundle = _write_bundle(
        tmp_path / "corrupt.tar.gz",
        _manifest([_file("source.py", declared)]),
        [_regular("source.py", b"x = 2\n")],
    )

    with pytest.raises(ValueError, match="does not match declared sha256"):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_missing_and_extra_members(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    missing = _write_bundle(
        tmp_path / "missing.tar.gz",
        _manifest([_file("source.py", payload)]),
        [],
    )
    with pytest.raises(ValueError, match="missing declared members"):
        validate_code_bundle(missing, _TREE_HASH)

    extra = _write_bundle(
        tmp_path / "extra.tar.gz",
        _manifest([_file("source.py", payload)]),
        [_regular("source.py", payload), _regular("extra.py", b"extra\n")],
    )
    with pytest.raises(ValueError, match="undeclared members"):
        validate_code_bundle(extra, _TREE_HASH)


def test_code_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    bundle = _write_bundle(
        tmp_path / "traversal.tar.gz",
        _manifest([_file("../source.py", payload)]),
        [_regular("../source.py", payload)],
    )

    with pytest.raises(ValueError, match="safe canonical relative path"):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_duplicate_archive_member(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    bundle = _write_bundle(
        tmp_path / "duplicate.tar.gz",
        _manifest([_file("source.py", payload)]),
        [_regular("source.py", payload), _regular("source.py", payload)],
    )

    with pytest.raises(ValueError, match="duplicate code bundle archive member"):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_duplicate_declared_path(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    entry = _file("source.py", payload)
    bundle = _write_bundle(
        tmp_path / "duplicate-declaration.tar.gz",
        _manifest([entry, entry]),
        [_regular("source.py", payload)],
    )

    with pytest.raises(ValueError, match="duplicate code bundle manifest path"):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_empty_manifest_file_set(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "empty.tar.gz", _manifest([]), [])

    with pytest.raises(ValueError, match="must declare at least one file"):
        validate_code_bundle(bundle, _TREE_HASH)


def test_code_bundle_rejects_symlink_and_mode_mismatch(tmp_path: Path) -> None:
    payload = b"x = 1\n"
    symlink = tarfile.TarInfo("source.py")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "../outside.py"
    bundle = _write_bundle(
        tmp_path / "symlink.tar.gz",
        _manifest([_file("source.py", payload)]),
        [(symlink, None)],
    )
    with pytest.raises(ValueError, match="not a regular file"):
        validate_code_bundle(bundle, _TREE_HASH)

    wrong_mode = _write_bundle(
        tmp_path / "wrong-mode.tar.gz",
        _manifest([_file("source.py", payload, mode=0o755)]),
        [_regular("source.py", payload, mode=0o644)],
    )
    with pytest.raises(ValueError, match="does not match declared mode"):
        validate_code_bundle(wrong_mode, _TREE_HASH)

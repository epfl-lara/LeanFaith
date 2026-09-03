"""Lean-free fail-closed publication tests for additive SFT1 releases."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.sft1.sprint import publish as publish_module
from leanfaith.sft1.sprint.publish import (
    PublishError,
    _load_publication_runtime,
    _upload_verified,
    local_files,
    publish_run,
    validate_publication_evidence,
)

ROOT = Path(__file__).resolve().parents[3]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _release_artifact(root: Path) -> Path:
    """Create the smallest Wave 4-shaped, checksum-declared release artifact."""

    compacted = root / "compacted" / "test-run"
    shard = compacted / "shard-0001"
    shard.mkdir(parents=True)
    rows = b'{"candidate":"candidate","label":false,"reference":"reference"}\n'
    sidecars = b'{"pair_id":"pair-1"}\n'
    groups = b'{"group_id":"group-1"}\n'
    (shard / "rows.jsonl").write_bytes(rows)
    (shard / "sidecars.jsonl").write_bytes(sidecars)
    (shard / "closure_groups.jsonl").write_bytes(groups)
    shard_manifest = {
        "schema_version": 1,
        "shard": 1,
        "row_count": 1,
        "rows_sha256": sha256_hex(rows),
        "sidecars_sha256": sha256_hex(sidecars),
        "closure_groups_sha256": sha256_hex(groups),
        "complete": True,
    }
    _write_json(shard / "manifest.json", shard_manifest)

    cache = b'{"cache":"record"}\n'
    cache_path = compacted / "cache_records" / "shard-0001.jsonl"
    cache_path.parent.mkdir()
    cache_path.write_bytes(cache)
    screen = b'[{"pair_id":"screened","reason":"candidate_only"}]\n'
    capacity = b'["capacity-group"]\n'
    negative_share = b'["n25-group"]\n'
    (compacted / "screen_rejections.json").write_bytes(screen)
    (compacted / "capacity_dropped_groups.json").write_bytes(capacity)
    (compacted / "negative_share_dropped_groups.json").write_bytes(negative_share)
    manifest = {
        "schema_version": 1,
        "row_fields": ["reference", "candidate", "label"],
        "retained_rows": 1,
        "shards": [shard_manifest],
        "cache_snapshots": [
            {
                "file": "cache_records/shard-0001.jsonl",
                "sha256": sha256_hex(cache),
            }
        ],
        "screen_rejections": {
            "file": "screen_rejections.json",
            "sha256": sha256_hex(screen),
        },
        "capacity_dropped_groups": {
            "file": "capacity_dropped_groups.json",
            "sha256": sha256_hex(capacity),
        },
        "negative_share_cap": {
            "dropped_group_ids_file": "negative_share_dropped_groups.json",
            "dropped_group_ids_sha256": sha256_hex(negative_share),
        },
    }
    _write_json(compacted / "manifest.json", manifest)
    _write_json(
        compacted / "release_report.json",
        {"passed": True, "checks": {"exact_three_fields": True}},
    )
    _write_json(
        compacted / "integrity_report.json",
        {"passed": True, "issues": [], "issue_counts": {"all": 0}},
    )
    (compacted / "provider-token.secret").write_text("must-not-upload", encoding="utf-8")
    return compacted


@pytest.mark.parametrize(
    ("report", "payload", "message"),
    [
        ("release_report.json", None, "release report"),
        ("release_report.json", {"passed": False}, "did not pass"),
        (
            "release_report.json",
            {"passed": True, "checks": {"exact_three_fields": False}},
            "failed check",
        ),
        ("integrity_report.json", None, "integrity report"),
        ("integrity_report.json", {"passed": False, "issues": []}, "did not pass"),
        (
            "integrity_report.json",
            {"passed": True, "issues": ["bad label"]},
            "empty issues",
        ),
        (
            "integrity_report.json",
            {"passed": True, "issues": [], "issue_counts": {"bad": 1}},
            "issue counts",
        ),
    ],
)
def test_publication_requires_independent_passed_reports(
    tmp_path: Path, report: str, payload: dict[str, Any] | None, message: str
) -> None:
    compacted = _release_artifact(tmp_path)
    path = compacted / report
    if payload is None:
        path.unlink()
    else:
        _write_json(path, payload)

    with pytest.raises(PublishError, match=message):
        validate_publication_evidence(compacted)


def test_upload_set_is_manifest_driven_and_includes_wave4_ledgers(tmp_path: Path) -> None:
    compacted = _release_artifact(tmp_path)

    discovered = {path.relative_to(compacted).as_posix() for path in local_files(compacted)}

    assert discovered == {
        "cache_records/shard-0001.jsonl",
        "capacity_dropped_groups.json",
        "integrity_report.json",
        "manifest.json",
        "negative_share_dropped_groups.json",
        "release_report.json",
        "screen_rejections.json",
        "shard-0001/closure_groups.jsonl",
        "shard-0001/manifest.json",
        "shard-0001/rows.jsonl",
        "shard-0001/sidecars.jsonl",
    }
    assert "provider-token.secret" not in discovered


@pytest.mark.parametrize(
    "relative",
    [
        "shard-0001/rows.jsonl",
        "shard-0001/closure_groups.jsonl",
        "screen_rejections.json",
    ],
)
def test_missing_manifest_declared_artifact_is_rejected(tmp_path: Path, relative: str) -> None:
    compacted = _release_artifact(tmp_path)
    (compacted / relative).unlink()

    with pytest.raises(PublishError, match=r"missing|regular file"):
        local_files(compacted)


def test_manifest_declared_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    compacted = _release_artifact(tmp_path)
    (compacted / "capacity_dropped_groups.json").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(PublishError, match="checksum mismatch"):
        local_files(compacted)


@pytest.mark.parametrize(
    ("field", "filename"),
    [
        ("shortcut_screens", "shortcut_screens.json"),
        ("pairwise_diagnostics", "pairwise_diagnostics.json"),
        ("wave3_gate", "wave3_gate_report.json"),
        ("composition_gate", "composition_gate_report.json"),
    ],
)
def test_wave4_and_wave5_gate_evidence_is_manifest_driven(
    tmp_path: Path, field: str, filename: str
) -> None:
    compacted = _release_artifact(tmp_path)
    evidence = b'{"passed":true}\n'
    (compacted / filename).write_bytes(evidence)
    manifest_path = compacted / "manifest.json"
    manifest = publish_module.read_json_object(manifest_path)
    manifest[field] = {"file": filename, "sha256": sha256_hex(evidence)}
    _write_json(manifest_path, manifest)

    assert compacted / filename in local_files(compacted)
    (compacted / filename).write_bytes(b'{"passed":false}\n')
    with pytest.raises(PublishError, match="checksum mismatch"):
        local_files(compacted)


@pytest.mark.parametrize(
    ("field", "filename"),
    [
        ("release_report_sha256", "release_report.json"),
        ("integrity_report_sha256", "integrity_report.json"),
    ],
)
def test_manifest_report_hash_binding_is_enforced(
    tmp_path: Path, field: str, filename: str
) -> None:
    compacted = _release_artifact(tmp_path)
    manifest_path = compacted / "manifest.json"
    manifest = publish_module.read_json_object(manifest_path)
    manifest[field] = "0" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(PublishError, match=field):
        validate_publication_evidence(compacted)

    manifest[field] = hash_file(compacted / filename)
    _write_json(manifest_path, manifest)
    assert validate_publication_evidence(compacted).hashes[filename] == manifest[field]


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, staging: Path) -> None:
    monkeypatch.setattr(
        publish_module,
        "_load_publication_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            config=SimpleNamespace(output=SimpleNamespace(staging_root=str(staging)))
        ),
    )


def test_publish_missing_report_fails_before_hub_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    compacted = _release_artifact(staging)
    (compacted / "integrity_report.json").unlink()
    _stub_runtime(monkeypatch, staging)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=lambda: pytest.fail("failed preflight must not access Hub")),
    )

    with pytest.raises(PublishError, match="integrity report"):
        publish_run(
            ROOT,
            run_id="test-run",
            repo_id="Lemmy00/test-private",
            remote_prefix="wave3/natural_core_v1",
        )


def test_historical_prefix_is_immutable_before_hub_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    _release_artifact(staging)
    _stub_runtime(monkeypatch, staging)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=lambda: pytest.fail("immutable prefix must not access Hub")),
    )

    with pytest.raises(PublishError, match="immutable"):
        publish_run(
            ROOT,
            run_id="test-run",
            repo_id="Lemmy00/test-private",
            remote_prefix="wave2/core_v1",
        )


def test_explicit_release_directory_bypasses_single_run_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compacted = _release_artifact(tmp_path / "arbitrary-release-root")
    monkeypatch.setattr(
        publish_module,
        "_load_publication_runtime",
        lambda *_args, **_kwargs: pytest.fail("explicit release must not load a run runtime"),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=lambda: pytest.fail("immutable prefix must not access Hub")),
    )

    with pytest.raises(PublishError, match="immutable"):
        publish_run(
            ROOT,
            run_id="wave3-natural-core-v1",
            repo_id="Lemmy00/test-private",
            remote_prefix="wave2/core_v1",
            compacted_dir=compacted,
        )


def test_explicit_release_directory_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compacted = _release_artifact(tmp_path / "artifact")
    link = tmp_path / "release-link"
    link.symlink_to(compacted, target_is_directory=True)
    monkeypatch.setattr(
        publish_module,
        "_load_publication_runtime",
        lambda *_args, **_kwargs: pytest.fail("explicit release must not load a run runtime"),
    )

    with pytest.raises(PublishError, match="must not be a symlink"):
        publish_run(
            ROOT,
            run_id="wave3-natural-core-v1",
            repo_id="Lemmy00/test-private",
            remote_prefix="wave3/natural_core_v1",
            compacted_dir=link,
        )


def test_remote_prefix_no_clobber_happens_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    artifact = local / "manifest.json"
    artifact.write_text("{}\n", encoding="utf-8")
    commit_called = False

    class FakeOperation:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakeApi:
        def repo_info(self, **_kwargs: object) -> object:
            return SimpleNamespace(private=True, sha="a" * 40)

        def list_repo_files(self, **_kwargs: object) -> list[str]:
            return ["wave3/natural_core_v1/existing.json"]

        def create_commit(self, **_kwargs: object) -> object:
            nonlocal commit_called
            commit_called = True
            return SimpleNamespace(oid="b" * 40)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            CommitOperationAdd=FakeOperation,
            hf_hub_download=lambda **_kwargs: pytest.fail("occupied prefix must not download"),
        ),
    )
    with pytest.raises(PublishError, match="already occupied"):
        _upload_verified(
            FakeApi(),
            repo_id="Lemmy00/test-private",
            local_root=local,
            files=[artifact],
            remote_prefix="wave3/natural_core_v1",
            commit_message="test",
        )
    assert commit_called is False


def test_expected_parent_mismatch_happens_before_remote_path_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    artifact = local / "manifest.json"
    artifact.write_text("{}\n", encoding="utf-8")
    path_check_called = False
    commit_called = False

    class FakeOperation:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakeApi:
        def repo_info(self, **_kwargs: object) -> object:
            return SimpleNamespace(private=True, sha="b" * 40)

        def list_repo_files(self, **_kwargs: object) -> list[str]:
            nonlocal path_check_called
            path_check_called = True
            return []

        def create_commit(self, **_kwargs: object) -> object:
            nonlocal commit_called
            commit_called = True
            return SimpleNamespace(oid="c" * 40)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            CommitOperationAdd=FakeOperation,
            hf_hub_download=lambda **_kwargs: pytest.fail("parent mismatch must not download"),
        ),
    )
    with pytest.raises(PublishError, match="parent revision changed"):
        _upload_verified(
            FakeApi(),
            repo_id="Lemmy00/test-private",
            local_root=local,
            files=[artifact],
            remote_prefix="wave5/compiler_core_v1/part-00000",
            commit_message="test",
            expected_parent="a" * 40,
        )
    assert path_check_called is False
    assert commit_called is False


def test_publish_receipt_binds_both_reports_and_declared_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    compacted = _release_artifact(staging)
    remote_files: dict[str, Path] = {}

    class FakeOperation:
        def __init__(self, *, path_in_repo: str, path_or_fileobj: Path) -> None:
            self.path_in_repo = path_in_repo
            self.path_or_fileobj = Path(path_or_fileobj)

    class FakeApi:
        def create_repo(self, **_kwargs: object) -> None:
            return None

        def repo_info(self, **_kwargs: object) -> object:
            return SimpleNamespace(private=True, sha="a" * 40)

        def list_repo_files(self, **_kwargs: object) -> list[str]:
            return []

        def create_commit(self, *, operations: list[FakeOperation], **_kwargs: object) -> object:
            remote_files.update(
                {operation.path_in_repo: operation.path_or_fileobj for operation in operations}
            )
            return SimpleNamespace(oid="b" * 40)

    api = FakeApi()
    fake_hub = SimpleNamespace(
        CommitOperationAdd=FakeOperation,
        HfApi=lambda: api,
        hf_hub_download=lambda *, filename, **_kwargs: str(remote_files[filename]),
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    _stub_runtime(monkeypatch, staging)

    receipt = publish_run(
        ROOT,
        run_id="test-run",
        repo_id="Lemmy00/test-private",
        remote_prefix="wave3/natural_core_v1",
    )

    expected_reports = {name: hash_file(compacted / name) for name in publish_module.REPORT_FILES}
    assert receipt["report_sha256"] == expected_reports
    assert receipt["release_report_sha256"] == expected_reports["release_report.json"]
    assert receipt["integrity_report_sha256"] == expected_reports["integrity_report.json"]
    assert (
        receipt["file_sha256"]["wave3/natural_core_v1/release_report.json"]
        == (expected_reports["release_report.json"])
    )
    assert (
        receipt["file_sha256"]["wave3/natural_core_v1/integrity_report.json"]
        == (expected_reports["integrity_report.json"])
    )
    assert "wave3/natural_core_v1/shard-0001/closure_groups.jsonl" in remote_files
    assert "wave3/natural_core_v1/screen_rejections.json" in remote_files
    assert not any("secret" in path for path in remote_files)


def test_publication_loader_supports_strict_wave4_wrapper() -> None:
    loaded = _load_publication_runtime(
        ROOT, ROOT / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
    )

    assert loaded.config.sprint_id == "sft1_wave4_composed_core_v1"
    assert loaded.config.output.shard_size == 1000

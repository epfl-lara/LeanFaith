"""LF-024 diagnostic batch publication and invocation-provenance regressions."""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.cli import resolve_labels as module
from leanfaith.cli.resolve_labels import (
    LabelResolutionBatchArtifacts,
    LabelResolutionBatchInputError,
    LabelResolutionCommitControl,
    resolve_label_batch,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.schemas.enums import ArtifactClass
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.manifest import CodeState, RunManifest, read_manifest
from leanfaith.schemas.pair import PairRecord

NOW = datetime.datetime(2026, 8, 11, 14, 0, tzinfo=datetime.UTC)
NONCE = "1a2b3c4d"
RUN_ID = "run_20260811T140000Z_1a2b3c4d"
CODE_STATE = CodeState(
    git_revision="1" * 40,
    git_dirty=False,
    base_git_commit="1" * 40,
    code_tree_hash="2" * 64,
    tracked_diff_hash="3" * 64,
)


@dataclass(frozen=True, slots=True)
class TransactionFixture:
    paths: RepoPaths
    root: Path
    targets: Path
    evidence: Path
    admissions: Path
    candidates: Path
    output: Path
    manifest: Path


def _write_jsonl(path: Path, records: tuple[PairRecord, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records)
    )


def _fixture(tmp_path: Path) -> TransactionFixture:
    source_root = find_repo_root(Path(__file__))
    root = tmp_path / "repo"
    policy = root / "policies" / "label_resolution_v1.yaml"
    gate = root / "reports" / "gates" / "gate_0.json"
    policy.parent.mkdir(parents=True)
    gate.parent.mkdir(parents=True)
    shutil.copyfile(source_root / "policies" / "label_resolution_v1.yaml", policy)
    shutil.copyfile(source_root / "reports" / "gates" / "gate_0.json", gate)

    inputs = root / "inputs"
    pair = PairRecord(
        pair_id=make_id("pair", {"fixture": "lf024-transaction"}),
        theorem_a_id=make_id("thm", {"fixture": "lf024-transaction-a"}),
        theorem_b_id=make_id("thm", {"fixture": "lf024-transaction-b"}),
        pair_source="lf024_transaction_fixture",
        split_group_ids=("ancestry:lf024-transaction",),
    )
    targets = inputs / "targets.jsonl"
    evidence = inputs / "evidence.jsonl"
    admissions = inputs / "admissions.jsonl"
    candidates = inputs / "candidates.jsonl"
    _write_jsonl(targets, (pair,))
    for path in (evidence, admissions, candidates):
        path.write_bytes(b"")
    return TransactionFixture(
        paths=RepoPaths(root=root),
        root=root,
        targets=targets,
        evidence=evidence,
        admissions=admissions,
        candidates=candidates,
        output=root / "data" / "labeled" / "transaction-test",
        manifest=root / "runs" / RUN_ID / "manifest.json",
    )


def _run(
    fixture: TransactionFixture,
    *,
    artifact_class: ArtifactClass = ArtifactClass.DIAGNOSTIC,
) -> LabelResolutionBatchArtifacts:
    return resolve_label_batch(
        paths=fixture.paths,
        target_path=fixture.targets,
        evidence_path=fixture.evidence,
        admission_path=fixture.admissions,
        candidate_path=fixture.candidates,
        output_dir=fixture.output,
        artifact_class=artifact_class,
        resolved_at=NOW,
        run_nonce=NONCE,
        code_state=CODE_STATE,
    )


def _transaction_partials(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.partial")))


def test_success_publishes_complete_batch_and_canonical_invocation_argv(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    root = fixture.root.resolve()

    assert result.output_dir == fixture.output.resolve()
    assert result.run_manifest_path == fixture.manifest
    assert result.run_manifest_sha256 == hash_file(result.run_manifest_path)
    assert all(
        path.is_file()
        for path in (
            result.linked_targets_path,
            result.labels_path,
            result.audits_path,
            result.derivations_path,
            result.conflicts_path,
            result.overrides_path,
        )
    )
    assert _transaction_partials(root) == ()

    control_path = result.output_dir / ".leanfaith_lf024_commit_control.json"
    control = LabelResolutionCommitControl.model_validate(
        json.loads(control_path.read_text(encoding="utf-8"))
    )
    assert control.run_id == result.run_id
    assert control.expected_manifest_sha256 == result.run_manifest_sha256
    assert control.expected_manifest_relative_path == f"runs/{result.run_id}/manifest.json"

    manifest = read_manifest(result.run_manifest_path, RunManifest)
    argv = manifest.argv
    assert "--artifact-class" not in argv
    expected_paths = {
        "--targets": fixture.targets.resolve(),
        "--evidence": fixture.evidence.resolve(),
        "--admissions": fixture.admissions.resolve(),
        "--candidates": fixture.candidates.resolve(),
        "--output-dir": fixture.output.resolve(),
        "--root": root,
    }
    for option, expected in expected_paths.items():
        assert Path(argv[argv.index(option) + 1]) == expected


def test_mid_partition_failure_leaves_no_visible_output_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original = module._write_jsonl
    calls = 0

    def fail_second_write(records: Sequence[StrictModel], path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected partition write failure")
        return original(records, path)

    monkeypatch.setattr(module, "_write_jsonl", fail_second_write)
    with pytest.raises(OSError, match="injected partition write failure"):
        _run(fixture)

    root = fixture.root
    assert not fixture.output.exists()
    assert not fixture.manifest.exists()
    assert _transaction_partials(root) == ()


def test_manifest_publish_failure_rolls_back_output_and_restores_empty_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    output = fixture.output
    output.mkdir(parents=True)
    manifest = fixture.manifest
    original_replace = os.replace

    def fail_manifest_publish(source: Path, destination: Path) -> None:
        if Path(destination) == manifest:
            raise OSError("injected manifest publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest_publish)
    with pytest.raises(OSError, match="injected manifest publication failure"):
        _run(fixture)

    root = fixture.root
    assert output.is_dir()
    assert tuple(output.iterdir()) == ()
    assert not manifest.exists()
    assert _transaction_partials(root) == ()


def test_post_publication_manifest_hash_mismatch_rolls_back_both_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.output.mkdir(parents=True)
    original_hash_file = hash_file

    def mismatch_published_manifest(path: Path) -> str:
        candidate = Path(path)
        if candidate == fixture.manifest and candidate.exists():
            return "f" * 64
        return original_hash_file(candidate)

    monkeypatch.setattr(module, "hash_file", mismatch_published_manifest)
    with pytest.raises(
        LabelResolutionBatchInputError,
        match="published run manifest hash differs",
    ):
        _run(fixture)

    assert fixture.output.is_dir()
    assert tuple(fixture.output.iterdir()) == ()
    assert not fixture.manifest.exists()
    assert _transaction_partials(fixture.root) == ()


def test_input_drift_during_staging_rolls_back_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original_hash_inputs = module._hash_inputs
    calls = 0

    def drift_on_publication(
        inputs: Sequence[tuple[str, Path]],
        *,
        root: Path,
    ) -> dict[str, str]:
        nonlocal calls
        calls += 1
        hashes = original_hash_inputs(inputs, root=root)
        if calls == 3:
            first = next(iter(hashes))
            hashes[first] = "f" * 64
        return hashes

    monkeypatch.setattr(module, "_hash_inputs", drift_on_publication)
    with pytest.raises(LabelResolutionBatchInputError, match="during output staging"):
        _run(fixture)

    root = fixture.root
    assert not fixture.output.exists()
    assert not fixture.manifest.exists()
    assert _transaction_partials(root) == ()


@pytest.mark.parametrize("artifact_class", [ArtifactClass.SMOKE, ArtifactClass.PRODUCTION])
def test_non_diagnostic_artifact_classes_fail_before_writing(
    tmp_path: Path,
    artifact_class: ArtifactClass,
) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(LabelResolutionBatchInputError, match="only diagnostic"):
        _run(fixture, artifact_class=artifact_class)

    assert not fixture.output.exists()
    assert not fixture.manifest.exists()


def test_committed_output_is_rejected_without_modification(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    output_bytes = {
        path.name: path.read_bytes() for path in result.output_dir.iterdir() if path.is_file()
    }

    with pytest.raises(LabelResolutionBatchInputError, match="already externally committed"):
        _run(fixture)

    assert {
        path.name: path.read_bytes() for path in result.output_dir.iterdir() if path.is_file()
    } == output_bytes
    assert hash_file(fixture.manifest) == result.run_manifest_sha256


def test_missing_commit_marker_quarantines_owned_orphan_then_reuses_output(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first = _run(fixture)
    fixture.manifest.unlink()

    second = _run(fixture)

    quarantines = tuple(fixture.output.parent.glob(f"{fixture.output.name}.orphan-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / ".leanfaith_lf024_commit_control.json").is_file()
    assert second.run_manifest_sha256 == hash_file(second.run_manifest_path)
    assert second.output_dir == first.output_dir


def test_mismatched_commit_marker_is_preserved_with_quarantined_orphan(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _run(fixture)
    fixture.manifest.write_bytes(b"tampered manifest\n")

    result = _run(fixture)

    (quarantine,) = tuple(fixture.output.parent.glob(f"{fixture.output.name}.orphan-*"))
    assert (quarantine / "mismatched_external_manifest.json").read_bytes() == (
        b"tampered manifest\n"
    )
    assert result.run_manifest_sha256 == hash_file(result.run_manifest_path)


def test_unrecognized_nonempty_output_is_rejected_without_quarantine(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.output.mkdir(parents=True)
    (fixture.output / "foreign.txt").write_text("not resolver-owned", encoding="utf-8")

    with pytest.raises(LabelResolutionBatchInputError, match="no valid resolver commit control"):
        _run(fixture)

    assert (fixture.output / "foreign.txt").read_text(encoding="utf-8") == "not resolver-owned"
    assert tuple(fixture.output.parent.glob(f"{fixture.output.name}.orphan-*")) == ()


def test_active_output_lock_prevents_false_orphan_recovery(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lock_path = fixture.output.parent / f".{fixture.output.name}.lf024.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LabelResolutionBatchInputError, match="lock is already held"):
            _run(fixture)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert not fixture.output.exists()
    assert not fixture.manifest.exists()

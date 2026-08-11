"""Focused tests for deterministic, audit-only LF-022 inventory snapshots."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

import pytest

import leanfaith.generation.lf022_inventory_snapshot as snapshot_module
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.lf022_inventory_snapshot import (
    LF022InventoryCollectionSnapshot,
    LF022InventoryCollectionSpec,
    LF022InventoryCountBucket,
    LF022InventorySnapshotError,
    LF022InventorySnapshotSpec,
    _CollectionInventory,
    _VariantObservation,
    build_lf022_inventory_snapshot,
    write_lf022_inventory_snapshot,
)


def _empty_bucket(*, variants: int, missing: int) -> LF022InventoryCountBucket:
    return LF022InventoryCountBucket(
        planned_task_count=variants + missing,
        observed_terminal_count=variants,
        missing_terminal_count=missing,
        terminal_status_counts={"provisional_variants_created": variants},
        gross_variant_count=variants,
        unique_variant_id_count=variants,
        unique_content_count=variants,
        unique_pair_count=variants,
        lean_checked_count=0,
        lean_outcome_counts={},
        lean_valid_count=0,
        lean_valid_unique_content_count=0,
        lean_valid_unique_pair_count=0,
        codex_audit_eligible_count=0,
        codex_audit_completed_count=0,
        codex_same_claim_counts={},
        codex_relation_counts={},
        codex_completed_unique_content_count=0,
        codex_completed_unique_pair_count=0,
    )


def _inventory(
    *, collection_id: str, model: str, variant_id: str, content: str, pair: str
) -> _CollectionInventory:
    bucket = _empty_bucket(variants=1, missing=1)
    return _CollectionInventory(
        snapshot=LF022InventoryCollectionSnapshot(
            collection_id=collection_id,
            proposer_family_id=collection_id,
            proposer_model=model,
            batch_id="lf022_public_batch:" + "a" * 64,
            batch_manifest_sha256="b" * 64,
            terminal_artifact_set_sha256="c" * 64,
            generation_complete=False,
            lean_check_complete=False,
            codex_audit_complete=False,
            counts=bucket,
        ),
        variants=(
            _VariantObservation(
                collection_id=collection_id,
                proposer_model=model,
                variant_id=variant_id,
                candidate_content_hash=content,
                pair_hash=pair,
            ),
        ),
        lean_valid_variant_ids=frozenset(),
        codex_completed_variant_ids=frozenset(),
    )


def _write_spec(path: Path, *, mode: str = "partial_live") -> str:
    collections = []
    for collection_id, family, model in (
        ("a", "family_a", "model/a"),
        ("b", "family_b", "model/b"),
    ):
        collections.append(
            {
                "collection_id": collection_id,
                "artifact_root": str(path.parent),
                "proposer_family_id": family,
                "proposer_model": model,
                "batch_manifest": {"path": f"{collection_id}.json", "sha256": "1" * 64},
                "lean_check_manifest": (
                    {"path": f"{collection_id}-checks.json", "sha256": "2" * 64}
                    if mode == "final"
                    else None
                ),
                "codex_audit_manifest": (
                    {"path": f"{collection_id}-audit.json", "sha256": "3" * 64}
                    if mode == "final"
                    else None
                ),
            }
        )
    payload = {
        "schema_version": 1,
        "method_version": "lf022_inventory_snapshot_v1",
        "mode": mode,
        "collections": collections,
        "audit_only": True,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
    }
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    return hash_file(path)


def test_partial_snapshot_is_nonfinal_and_reports_cross_model_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_hash = _write_spec(spec_path)
    inventories = {
        "a": _inventory(
            collection_id="a",
            model="model/a",
            variant_id="var:" + "1" * 64,
            content="d" * 64,
            pair="e" * 64,
        ),
        "b": _inventory(
            collection_id="b",
            model="model/b",
            variant_id="var:" + "2" * 64,
            content="d" * 64,
            pair="e" * 64,
        ),
    }

    def fake_inventory(
        spec: LF022InventoryCollectionSpec,
        *,
        repo_root: Path,
        mode: Literal["final", "partial_live"],
        terminal_cut: tuple[tuple[str, str], ...] | None,
    ) -> _CollectionInventory:
        assert repo_root == tmp_path.resolve()
        assert mode == "partial_live"
        assert terminal_cut == ()
        return inventories[spec.collection_id]

    monkeypatch.setattr(snapshot_module, "_inventory_collection", fake_inventory)
    monkeypatch.setattr(
        snapshot_module,
        "_scan_collection_terminal_artifact_set",
        lambda *_args, **_kwargs: (),
    )
    report = build_lf022_inventory_snapshot(
        repo_root=tmp_path,
        spec_path=spec_path,
        expected_spec_sha256=spec_hash,
    )

    assert report.snapshot_status == "non_final_point_in_time"
    assert report.non_final
    assert report.overall.gross_variant_count == 2
    assert report.overall.unique_content_count == 1
    assert report.overall.unique_pair_count == 1
    assert report.candidate_content_overlap.cross_model_key_count == 1
    assert report.source_candidate_pair_overlap.cross_model_key_count == 1
    assert report.candidate_content_overlap.pairwise_model_intersections == {"model/a | model/b": 1}
    assert not report.semantic_labels_created
    assert not report.training_eligible


def test_final_spec_requires_check_and_audit_bindings() -> None:
    with pytest.raises(ValueError, match="final snapshot requires check and audit"):
        LF022InventorySnapshotSpec.model_validate(
            {
                "mode": "final",
                "collections": [
                    {
                        "collection_id": "a",
                        "artifact_root": ".",
                        "proposer_family_id": "family",
                        "proposer_model": "model",
                        "batch_manifest": {"path": "batch.json", "sha256": "1" * 64},
                    }
                ],
            }
        )


def test_spec_hash_mismatch_fails_before_artifact_inspection(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    with pytest.raises(LF022InventorySnapshotError, match="snapshot spec hash mismatch"):
        build_lf022_inventory_snapshot(
            repo_root=tmp_path,
            spec_path=spec_path,
            expected_spec_sha256="0" * 64,
        )


def test_count_bucket_rejects_unreconciled_terminal_counts() -> None:
    with pytest.raises(ValueError, match="terminal status counts do not reconcile"):
        LF022InventoryCountBucket.model_validate(
            {
                **_empty_bucket(variants=1, missing=0).model_dump(mode="json"),
                "terminal_status_counts": Counter({"provisional_variants_created": 2}),
            }
        )


def test_snapshot_write_is_exact_replay_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_hash = _write_spec(spec_path)
    inventories = {
        "a": _inventory(
            collection_id="a",
            model="model/a",
            variant_id="var:" + "1" * 64,
            content="d" * 64,
            pair="e" * 64,
        ),
        "b": _inventory(
            collection_id="b",
            model="model/b",
            variant_id="var:" + "2" * 64,
            content="f" * 64,
            pair="9" * 64,
        ),
    }
    monkeypatch.setattr(
        snapshot_module,
        "_inventory_collection",
        lambda spec, **_: inventories[spec.collection_id],
    )
    monkeypatch.setattr(
        snapshot_module,
        "_scan_collection_terminal_artifact_set",
        lambda *_args, **_kwargs: (),
    )
    report = build_lf022_inventory_snapshot(
        repo_root=tmp_path,
        spec_path=spec_path,
        expected_spec_sha256=spec_hash,
    )
    output = tmp_path / "snapshot.json"
    first = write_lf022_inventory_snapshot(report, output_path=output)
    second = write_lf022_inventory_snapshot(report, output_path=output)
    assert first == second
    output.write_text("changed\n", encoding="utf-8")
    with pytest.raises(LF022InventorySnapshotError, match="immutable snapshot conflict"):
        write_lf022_inventory_snapshot(report, output_path=output)


def test_snapshot_write_rejects_concurrent_different_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_hash = _write_spec(spec_path)
    inventories = {
        "a": _inventory(
            collection_id="a",
            model="model/a",
            variant_id="var:" + "1" * 64,
            content="d" * 64,
            pair="e" * 64,
        ),
        "b": _inventory(
            collection_id="b",
            model="model/b",
            variant_id="var:" + "2" * 64,
            content="f" * 64,
            pair="9" * 64,
        ),
    }
    monkeypatch.setattr(
        snapshot_module,
        "_inventory_collection",
        lambda spec, **_: inventories[spec.collection_id],
    )
    monkeypatch.setattr(
        snapshot_module,
        "_scan_collection_terminal_artifact_set",
        lambda *_args, **_kwargs: (),
    )
    report = build_lf022_inventory_snapshot(
        repo_root=tmp_path,
        spec_path=spec_path,
        expected_spec_sha256=spec_hash,
    )
    output = tmp_path / "snapshot.json"

    def competing_link(_source: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"different-writer\n")
        raise FileExistsError

    monkeypatch.setattr(snapshot_module.os, "link", competing_link)
    with pytest.raises(LF022InventorySnapshotError, match="concurrent immutable"):
        write_lf022_inventory_snapshot(report, output_path=output)
    assert output.read_bytes() == b"different-writer\n"
    assert not tuple(tmp_path.glob(".snapshot.json.*.partial"))


def test_partial_snapshot_rejects_frozen_terminal_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_hash = _write_spec(spec_path)
    inventories = {
        "a": _inventory(
            collection_id="a",
            model="model/a",
            variant_id="var:" + "1" * 64,
            content="d" * 64,
            pair="e" * 64,
        ),
        "b": _inventory(
            collection_id="b",
            model="model/b",
            variant_id="var:" + "2" * 64,
            content="f" * 64,
            pair="9" * 64,
        ),
    }
    monkeypatch.setattr(
        snapshot_module,
        "_inventory_collection",
        lambda spec, **_: inventories[spec.collection_id],
    )
    calls = 0

    def drifting_cut(
        spec: LF022InventoryCollectionSpec, **_kwargs: object
    ) -> tuple[tuple[str, str], ...]:
        nonlocal calls
        calls += 1
        digest = "a" * 64 if calls <= 2 else ("b" * 64 if spec.collection_id == "a" else "a" * 64)
        return ((f"task-{spec.collection_id}", digest),)

    monkeypatch.setattr(snapshot_module, "_scan_collection_terminal_artifact_set", drifting_cut)
    with pytest.raises(LF022InventorySnapshotError, match="frozen live terminal tree drifted"):
        build_lf022_inventory_snapshot(
            repo_root=tmp_path,
            spec_path=spec_path,
            expected_spec_sha256=spec_hash,
        )

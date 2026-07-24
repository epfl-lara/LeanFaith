"""LF-020 clean-cache semantic replay and accounting audit."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.evidence.replay import EvidenceReplayInputError, compare_lf020_replays
from leanfaith.lean.cache import EvidenceCacheKey, make_evidence_cache_entry
from leanfaith.schemas import (
    ArtifactClass,
    AuditValue,
    CodeState,
    DataStage,
    DefeqValue,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceTargetKind,
    OutputManifest,
    PairRecord,
    make_id,
)
from tests.unit.record_factories import (
    PAIR_ID,
    REPR_A,
    THM_A,
    THM_B,
    UTC_NOW,
    pair_record,
)


@dataclass(frozen=True)
class _Tree:
    output: Path
    cache: Path
    upstream_id: str
    source_pair: PairRecord
    terminal: EvidenceRecord
    audit: EvidenceRecord


def _write_jsonl(path: Path, records: tuple[object, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n"  # type: ignore[attr-defined]
            for record in records
        )
    )


def _cache_key() -> EvidenceCacheKey:
    return EvidenceCacheKey(
        pair_id=PAIR_ID,
        theorem_a_id=THM_A,
        theorem_b_id=THM_B,
        theorem_a_statement_hash="1" * 64,
        theorem_b_statement_hash="2" * 64,
        representation_a_id=REPR_A,
        representation_b_id=make_id("repr", {"fixture": "replay-b"}),
        representation_a_content_hash="3" * 64,
        representation_b_content_hash="4" * 64,
        representation_version="repr_v2",
        context_id=f"ctx:{'0' * 64}",
        context_fingerprint="0" * 64,
        environment_schema_version=1,
        environment_hash="5" * 64,
        evidence_kind=EvidenceKind.DEFEQ,
        evidence_direction="none",
        method_version="defeq_v1",
        timeout_seconds=2.0,
        config_hash="6" * 64,
        semantic_policy_version="semantic_policy_v1",
        semantic_policy_hash="7" * 64,
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="pinned-repl",
        project_revision="fixture-project",
    )


def _manifest(output: Path, *, evidence_count: int, pair_count: int) -> OutputManifest:
    checksums = {
        str(output / filename): hash_file(output / filename)
        for filename in (
            "evidence.jsonl",
            "pairs.jsonl",
            "failures.jsonl",
            "artifact_catalog.json",
            "cache_catalog.json",
        )
    }
    return OutputManifest(
        stage=DataStage.EVIDENCE_COLLECTED,
        artifact_class=ArtifactClass.SMOKE,
        run_id="run_20260723T120000Z_1234abcd",
        source="lf020_symbolic_evidence",
        source_revision="fixture",
        config_hash="8" * 64,
        record_schema_version=2,
        row_count=evidence_count,
        attempted_row_count=pair_count,
        terminal_outcome_counts={
            "input_pairs": pair_count,
            "enriched_pairs": pair_count,
            "evidence_records": evidence_count,
            "pair_failures": 0,
            "cache_hits": 0,
            "cache_misses": pair_count,
            "upstream_evidence_records": pair_count,
            "resolved_labels_created": 0,
        },
        file_checksums=checksums,
        output_partition_checksums={
            str(output / "evidence.jsonl"): checksums[str(output / "evidence.jsonl")],
            str(output / "pairs.jsonl"): checksums[str(output / "pairs.jsonl")],
            str(output / "artifact_catalog.json"): checksums[str(output / "artifact_catalog.json")],
            str(output / "cache_catalog.json"): checksums[str(output / "cache_catalog.json")],
        },
        failure_partition_checksums={
            str(output / "failures.jsonl"): checksums[str(output / "failures.jsonl")]
        },
        code=CodeState(
            git_revision="9" * 40,
            git_dirty=False,
            base_git_commit="9" * 40,
            code_tree_hash="a" * 64,
        ),
        created_at=UTC_NOW,
    )


def _build_tree(
    root: Path,
    *,
    side: str,
    defeq_outcome: str = "equal",
    audit_check: bool = True,
    request_hash: str = "b" * 64,
    source_resolved_label_id: str | None = None,
    resolved_label_id: str | None = None,
) -> _Tree:
    output = root / f"output-{side}"
    cache = root / f"cache-{side}"
    output.mkdir(parents=True)
    upstream_id = make_id("ev", {"fixture": "upstream-transform-audit"})
    terminal_id = make_id("ev", {"fixture": "lf020-defeq"})
    audit_id = make_id("ev", {"fixture": "lf020-defeq-audit"})
    created_at = UTC_NOW + (datetime.timedelta(days=1) if side == "right" else datetime.timedelta())
    raw_artifact_path = root / side / "raw-defeq.json"
    audit_artifact_path = root / side / "audit-detail.json"
    raw_artifact_path.parent.mkdir(parents=True)
    raw_artifact_path.write_text('{"fixture":"raw-defeq"}\n', encoding="utf-8")
    audit_artifact_path.write_text('{"fixture":"audit-detail"}\n', encoding="utf-8")
    raw_artifact_hash = hash_file(raw_artifact_path)
    audit_artifact_hash = hash_file(audit_artifact_path)
    terminal = EvidenceRecord(
        evidence_id=terminal_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        kind=EvidenceKind.DEFEQ,
        status=EvidenceExecutionStatus.SUCCESS,
        value=DefeqValue(outcome=defeq_outcome),  # type: ignore[arg-type]
        method_version="defeq_v1",
        config_hash="6" * 64,
        raw_artifact=f"{side}/raw-defeq.json",
        created_at=created_at,
        metadata={
            "run_id": f"run-{side}",
            "raw_artifact_sha256": raw_artifact_hash,
        },
    )
    audit = EvidenceRecord(
        evidence_id=audit_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        kind=EvidenceKind.AXIOM_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=AuditValue(
            checks={"certificate_accepted": audit_check},
            violation_codes=() if audit_check else ("fixture_violation",),
            detail_artifact=f"{side}/audit-detail.json",
        ),
        method_version="defeq_v1/axiom_dependency_audit_v1",
        config_hash="6" * 64,
        raw_artifact=f"{side}/audit-detail.json",
        created_at=created_at,
    )
    source_pair: PairRecord = pair_record(
        evidence_ids=(upstream_id,),
        resolved_label_id=source_resolved_label_id,
    )
    pair: PairRecord = pair_record(
        evidence_ids=(upstream_id, terminal_id, audit_id),
        resolved_label_id=resolved_label_id,
    )
    _write_jsonl(output / "evidence.jsonl", (terminal, audit))
    _write_jsonl(output / "pairs.jsonl", (pair,))
    (output / "failures.jsonl").write_text("", encoding="utf-8")

    key = _cache_key()
    entry = make_evidence_cache_entry(
        key,
        terminal,
        auxiliary_evidence=(audit,),
        generated_code_hash="c" * 64,
        lean_request_hashes=(request_hash,),
        certificate_dependency_hash="d" * 64,
        artifact_hashes={
            f"{side}/raw-defeq.json": raw_artifact_hash,
            f"{side}/audit-detail.json": audit_artifact_hash,
        },
    )
    cache_path = cache / "v1" / entry.cache_key_hash[:2] / f"{entry.cache_key_hash}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(canonical_json_bytes(entry.model_dump(mode="json")) + b"\n")
    artifact_entries = sorted(
        (
            {
                "path": f"{side}/raw-defeq.json",
                "sha256": raw_artifact_hash,
                "kind": "evidence_artifact",
            },
            {
                "path": f"{side}/audit-detail.json",
                "sha256": audit_artifact_hash,
                "kind": "evidence_artifact",
            },
        ),
        key=lambda item: (item["kind"], item["path"], item["sha256"]),
    )
    (output / "artifact_catalog.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "run_id": "run_20260723T120000Z_1234abcd",
                "entries": artifact_entries,
            }
        )
        + b"\n"
    )
    (output / "cache_catalog.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "run_id": "run_20260723T120000Z_1234abcd",
                "entries": [
                    {
                        "cache_key_hash": entry.cache_key_hash,
                        "path": cache_path.relative_to(cache).as_posix(),
                        "sha256": hash_file(cache_path),
                    }
                ],
            }
        )
        + b"\n"
    )
    manifest = _manifest(output, evidence_count=2, pair_count=1)
    (output / "manifest.json").write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )
    return _Tree(
        output=output,
        cache=cache,
        upstream_id=upstream_id,
        source_pair=source_pair,
        terminal=terminal,
        audit=audit,
    )


def test_clean_cache_replay_ignores_times_and_artifact_paths(tmp_path: Path) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert report.passed
    assert all(report.checks.model_dump().values())
    assert report.left.semantic_fingerprint == report.right.semantic_fingerprint
    assert report.left.cache_fingerprint == report.right.cache_fingerprint
    assert report.left.run_id == "run_20260723T120000Z_1234abcd"
    assert report.left.artifact_class == ArtifactClass.SMOKE
    assert report.left.output_manifest_sha256 == hash_file(left.output / "manifest.json")
    assert report.right.output_manifest_sha256 == hash_file(right.output / "manifest.json")
    assert report.left.artifact_file_count == 2
    assert report.left.artifact_catalog_sha256 == hash_file(left.output / "artifact_catalog.json")
    assert report.left.cache_catalog_sha256 == hash_file(left.output / "cache_catalog.json")
    assert len(report.left.artifact_catalog_hash) == 64
    assert len(report.left.cache_catalog_hash) == 64
    assert len(report.left.cache_snapshot_catalog_hash) == 64
    assert report.source_pair_fingerprint == hash_canonical(
        [left.source_pair.model_dump(mode="json")]
    )
    assert report.upstream_evidence_id_fingerprint == hash_canonical((left.upstream_id,))
    assert report.request_hash_policy == "ordered_exact_by_cache_key_v1"
    assert report.model_dump(mode="json")["report_hash"] == report.report_hash


def test_terminal_value_and_cache_payload_mismatch_fail_replay(tmp_path: Path) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right", defeq_outcome="not_equal")

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert not report.checks.terminal_job_semantics_match
    assert not report.checks.cache_payload_semantics_match
    assert any("terminal job semantics differs" in error for error in report.errors)


def test_audit_paths_are_ignored_but_checks_and_violations_are_not(
    tmp_path: Path,
) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right", audit_check=False)

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert not report.checks.audit_semantics_match
    assert not report.checks.cache_payload_semantics_match


def test_request_hash_mismatch_is_compared_by_stable_cache_key(tmp_path: Path) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right", request_hash="0" * 64)

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert report.checks.cache_keys_match
    assert report.checks.cache_payload_semantics_match
    assert not report.checks.cache_execution_hashes_match


def test_preserved_preexisting_label_is_allowed(
    tmp_path: Path,
) -> None:
    label_id = make_id("lbl", {"fixture": "preexisting"})
    left = _build_tree(
        tmp_path,
        side="left",
        source_resolved_label_id=label_id,
        resolved_label_id=label_id,
    )
    right = _build_tree(
        tmp_path,
        side="right",
        source_resolved_label_id=label_id,
        resolved_label_id=label_id,
    )

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert report.passed
    assert report.checks.no_labels_or_promotions


@pytest.mark.parametrize(
    ("source_label_case", "output_label_case"),
    [
        (None, "new"),
        ("source", "changed"),
    ],
)
def test_changed_or_new_label_is_rejected(
    tmp_path: Path,
    source_label_case: str | None,
    output_label_case: str,
) -> None:
    source_label_id = (
        None if source_label_case is None else make_id("lbl", {"fixture": source_label_case})
    )
    output_label_id = make_id("lbl", {"fixture": output_label_case})
    left = _build_tree(
        tmp_path,
        side="left",
        source_resolved_label_id=source_label_id,
        resolved_label_id=output_label_id,
    )
    right = _build_tree(
        tmp_path,
        side="right",
        source_resolved_label_id=source_label_id,
        resolved_label_id=output_label_id,
    )

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert report.checks.left_accounting_closed
    assert report.checks.right_accounting_closed
    assert not report.checks.no_labels_or_promotions
    assert report.left.label_or_promotion_violation_count == 1


def test_accounting_requires_complete_upstream_union(tmp_path: Path) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(),
    )

    assert not report.passed
    assert not report.checks.left_accounting_closed
    assert not report.checks.right_accounting_closed
    assert report.checks.no_labels_or_promotions
    assert report.left.unresolved_pair_evidence_id_count == 1


@pytest.mark.parametrize("operation", ["tamper", "remove"])
def test_missing_or_tampered_evidence_artifact_fails_replay(
    tmp_path: Path,
    operation: str,
) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")
    target = tmp_path / "right/raw-defeq.json"
    if operation == "tamper":
        target.write_text('{"fixture":"tampered"}\n', encoding="utf-8")
    else:
        target.unlink()

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert not report.checks.right_accounting_closed
    assert any(
        "artifact" in error and ("mismatch" in error or "missing" in error)
        for error in report.errors
    )


@pytest.mark.parametrize("operation", ["tamper", "remove"])
def test_missing_or_tampered_cache_snapshot_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")
    snapshot = next(right.cache.rglob("*.json"))
    if operation == "tamper":
        snapshot.write_bytes(b" " + snapshot.read_bytes())
    else:
        snapshot.unlink()

    with pytest.raises(EvidenceReplayInputError):
        compare_lf020_replays(
            left_output_dir=left.output,
            left_cache_root=left.cache,
            left_artifact_root=tmp_path,
            right_output_dir=right.output,
            right_cache_root=right.cache,
            right_artifact_root=tmp_path,
            source_pairs=(left.source_pair,),
            upstream_evidence_ids=(left.upstream_id,),
        )


@pytest.mark.parametrize("catalog_name", ["artifact_catalog.json", "cache_catalog.json"])
def test_missing_catalog_fails_closed(tmp_path: Path, catalog_name: str) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")
    (right.output / catalog_name).unlink()

    with pytest.raises(EvidenceReplayInputError):
        compare_lf020_replays(
            left_output_dir=left.output,
            left_cache_root=left.cache,
            left_artifact_root=tmp_path,
            right_output_dir=right.output,
            right_cache_root=right.cache,
            right_artifact_root=tmp_path,
            source_pairs=(left.source_pair,),
            upstream_evidence_ids=(left.upstream_id,),
        )


@pytest.mark.parametrize(
    "mode",
    ["artifact_missing_entry", "artifact_extra_entry", "cache_missing_entry"],
)
def test_catalog_entry_set_must_be_exact(tmp_path: Path, mode: str) -> None:
    left = _build_tree(tmp_path, side="left")
    right = _build_tree(tmp_path, side="right")
    if mode.startswith("artifact_"):
        path = right.output / "artifact_catalog.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        if mode == "artifact_missing_entry":
            document["entries"].pop()
        else:
            extra = tmp_path / "right/extra.json"
            extra.write_text('{"fixture":"extra"}\n', encoding="utf-8")
            document["entries"].append(
                {
                    "path": "right/extra.json",
                    "sha256": hash_file(extra),
                    "kind": "evidence_artifact",
                }
            )
            document["entries"].sort(key=lambda item: (item["kind"], item["path"], item["sha256"]))
    else:
        path = right.output / "cache_catalog.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entries"] = []
    path.write_bytes(canonical_json_bytes(document) + b"\n")

    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(left.source_pair,),
        upstream_evidence_ids=(left.upstream_id,),
    )

    assert not report.passed
    assert not report.checks.right_accounting_closed
    assert any("catalog" in error for error in report.errors)

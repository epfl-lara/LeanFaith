"""Typed artifact verification at the LF-024 evidence-admission boundary."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

import leanfaith.labeling.admission as admission_module
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.evidence.replay import compare_lf020_replays
from leanfaith.labeling.admission import (
    AdmissionArtifactLocator,
    EvidenceAdmissionVerificationError,
    LF020EvidenceAdmissionDiagnosticResult,
    LF020ExpectedAdmissionBinding,
    LF020ExpectedEvidenceBinding,
    LF020ReplayInputs,
    LF020TargetRuntimeBinding,
    build_lf020_expected_admission_binding,
    verify_lf020_evidence_admission,
)
from leanfaith.labeling.aggregation import (
    EvidenceAdmissionRecord,
    build_evidence_admission_record,
)
from leanfaith.lean.cache import (
    EvidenceCacheEntry,
    EvidenceCacheKey,
    evidence_semantic_hash,
    make_evidence_cache_entry,
)
from leanfaith.schemas import (
    ArtifactClass,
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
    pair_record,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)
MANIFEST_CONFIG_HASH = "8" * 64
MANIFEST_CONTEXT_HASH = "9" * 64


@dataclass(frozen=True, slots=True)
class _Side:
    output: Path
    cache: Path
    evidence: EvidenceRecord
    cache_entry: EvidenceCacheEntry


@dataclass(frozen=True, slots=True)
class _Fixture:
    root: Path
    left: _Side
    right: _Side
    source_pair: PairRecord
    report_path: Path
    admission: EvidenceAdmissionRecord
    expected: LF020ExpectedAdmissionBinding
    replay_inputs: LF020ReplayInputs
    manifest_locator: AdmissionArtifactLocator
    replay_locator: AdmissionArtifactLocator


def _write_model(path: Path, model: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        canonical_json_bytes(model.model_dump(mode="json")) + b"\n"  # type: ignore[attr-defined]
    )


def _write_jsonl(path: Path, records: tuple[object, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n"  # type: ignore[attr-defined]
            for record in records
        )
    )


def _cache_key(*, semantic_policy_hash: str) -> EvidenceCacheKey:
    return EvidenceCacheKey(
        pair_id=PAIR_ID,
        theorem_a_id=THM_A,
        theorem_b_id=THM_B,
        theorem_a_statement_hash="1" * 64,
        theorem_b_statement_hash="2" * 64,
        representation_a_id=REPR_A,
        representation_b_id=make_id("repr", {"fixture": "lf024-admission-b"}),
        representation_a_content_hash="3" * 64,
        representation_b_content_hash="4" * 64,
        representation_version="repr_v3",
        context_id=f"ctx:{'5' * 64}",
        context_fingerprint="6" * 64,
        environment_schema_version=1,
        environment_hash="7" * 64,
        evidence_kind=EvidenceKind.DEFEQ,
        evidence_direction="none",
        method_version="defeq_rfl_v1",
        timeout_seconds=2.0,
        config_hash="a" * 64,
        semantic_policy_version="semantic_policy_v1",
        semantic_policy_hash=semantic_policy_hash,
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="pinned-repl",
        project_revision="fixture-project",
    )


def _manifest(output: Path, *, run_id: str, created_at: datetime.datetime) -> OutputManifest:
    output_checksums = {
        str(output / filename): hash_file(output / filename)
        for filename in (
            "artifact_catalog.json",
            "cache_catalog.json",
            "evidence.jsonl",
            "pairs.jsonl",
        )
    }
    failure_checksums = {str(output / "failures.jsonl"): hash_file(output / "failures.jsonl")}
    return OutputManifest(
        stage=DataStage.EVIDENCE_COLLECTED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id=run_id,
        source="lf020_symbolic_evidence",
        source_revision="fixture-source",
        config_hash=MANIFEST_CONFIG_HASH,
        record_schema_version=2,
        row_count=1,
        attempted_row_count=1,
        terminal_outcome_counts={
            "input_pairs": 1,
            "enriched_pairs": 1,
            "evidence_records": 1,
            "pair_failures": 0,
            "cache_hits": 0,
            "cache_misses": 1,
            "upstream_evidence_records": 0,
            "resolved_labels_created": 0,
        },
        file_checksums={**output_checksums, **failure_checksums},
        output_partition_checksums=output_checksums,
        failure_partition_checksums=failure_checksums,
        environment_hash="7" * 64,
        context_hash=MANIFEST_CONTEXT_HASH,
        code_tree_hash="b" * 64,
        code=CodeState(
            git_revision="c" * 40,
            git_dirty=False,
            base_git_commit="c" * 40,
            code_tree_hash="b" * 64,
            tracked_diff_hash="d" * 64,
        ),
        created_at=created_at,
    )


def _build_side(
    root: Path,
    *,
    side: str,
    source_pair: PairRecord,
    key: EvidenceCacheKey,
    created_at: datetime.datetime,
) -> _Side:
    output = root / f"output-{side}"
    cache = root / f"cache-{side}"
    output.mkdir(parents=True)
    evidence = EvidenceRecord(
        evidence_id=make_id(
            "ev",
            {
                "fixture": "lf024-admission",
                "cache_key": key.model_dump(mode="json"),
                "outcome": "equal",
            },
        ),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        kind=EvidenceKind.DEFEQ,
        status=EvidenceExecutionStatus.SUCCESS,
        value=DefeqValue(outcome="equal"),
        method_version=key.method_version,
        config_hash=key.config_hash,
        created_at=created_at,
        metadata={"cache_key": make_id("cache", key.model_dump(mode="json")).split(":", 1)[1]},
    )
    cache_entry = make_evidence_cache_entry(
        key,
        evidence,
        generated_code_hash="e" * 64,
        lean_request_hashes=("f" * 64,),
    )
    evidence = evidence.model_copy(update={"metadata": {"cache_key": cache_entry.cache_key_hash}})
    cache_entry = make_evidence_cache_entry(
        key,
        evidence,
        generated_code_hash="e" * 64,
        lean_request_hashes=("f" * 64,),
    )
    enriched = PairRecord.model_validate(
        {
            **source_pair.model_dump(mode="python"),
            "evidence_ids": (evidence.evidence_id,),
        }
    )
    _write_jsonl(output / "evidence.jsonl", (evidence,))
    _write_jsonl(output / "pairs.jsonl", (enriched,))
    (output / "failures.jsonl").write_bytes(b"")
    _write_model(
        output / "artifact_catalog.json",
        _Catalog(run_id=f"run_20260811T12000{0 if side == 'left' else 1}Z_deadbeef"),
    )
    cache_path = (
        cache / "v1" / cache_entry.cache_key_hash[:2] / (f"{cache_entry.cache_key_hash}.json")
    )
    _write_model(cache_path, cache_entry)
    _write_model(
        output / "cache_catalog.json",
        _CacheCatalogFixture(
            run_id=f"run_20260811T12000{0 if side == 'left' else 1}Z_deadbeef",
            entries=(
                _CacheEntryFixture(
                    cache_key_hash=cache_entry.cache_key_hash,
                    path=cache_path.relative_to(cache).as_posix(),
                    sha256=hash_file(cache_path),
                ),
            ),
        ),
    )
    manifest = _manifest(
        output,
        run_id=f"run_20260811T12000{0 if side == 'left' else 1}Z_deadbeef",
        created_at=created_at,
    )
    _write_model(output / "manifest.json", manifest)
    return _Side(output=output, cache=cache, evidence=evidence, cache_entry=cache_entry)


class _Catalog(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    entries: tuple[object, ...] = ()


class _CacheEntryFixture(StrictModel):
    cache_key_hash: str
    path: str
    sha256: str


class _CacheCatalogFixture(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    entries: tuple[_CacheEntryFixture, ...]


def _fixture(tmp_path: Path) -> _Fixture:
    label_policy = tmp_path / "policies/label_resolution_v1.yaml"
    semantic_policy = tmp_path / "policies/semantic_policy_v1.md"
    gate_report = tmp_path / "reports/gates/gate_0.json"
    label_policy.parent.mkdir(parents=True)
    gate_report.parent.mkdir(parents=True)
    label_policy.write_bytes((ROOT / "policies/label_resolution_v1.yaml").read_bytes())
    semantic_policy.write_bytes((ROOT / "policies/semantic_policy_v1.md").read_bytes())
    gate_report.write_bytes((ROOT / "reports/gates/gate_0.json").read_bytes())
    source_pair = pair_record(evidence_ids=())
    key = _cache_key(semantic_policy_hash=hash_file(semantic_policy))
    left = _build_side(
        tmp_path,
        side="left",
        source_pair=source_pair,
        key=key,
        created_at=NOW,
    )
    right = _build_side(
        tmp_path,
        side="right",
        source_pair=source_pair,
        key=key,
        created_at=NOW + datetime.timedelta(seconds=1),
    )
    replay_inputs = LF020ReplayInputs(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(source_pair,),
    )
    report = compare_lf020_replays(
        left_output_dir=left.output,
        left_cache_root=left.cache,
        left_artifact_root=tmp_path,
        right_output_dir=right.output,
        right_cache_root=right.cache,
        right_artifact_root=tmp_path,
        source_pairs=(source_pair,),
    )
    assert report.passed
    report_path = tmp_path / "reports/evidence/lf020_replay.json"
    _write_model(report_path, report)
    manifest_path = left.output / "manifest.json"
    admission = build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        evidence_ids=(left.evidence.evidence_id,),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id="lf020-output:left",
        manifest_artifact_sha256=hash_file(manifest_path),
        replay_artifact_id="lf020-replay:fixture",
        replay_artifact_sha256=hash_file(report_path),
        replay_passed=True,
        policy_sha256=hash_file(label_policy),
    )
    runtime = LF020TargetRuntimeBinding(
        pair_id=key.pair_id,
        theorem_a_id=key.theorem_a_id,
        theorem_b_id=key.theorem_b_id,
        theorem_a_statement_hash=key.theorem_a_statement_hash,
        theorem_b_statement_hash=key.theorem_b_statement_hash,
        representation_a_id=key.representation_a_id,
        representation_b_id=key.representation_b_id,
        representation_a_content_hash=key.representation_a_content_hash,
        representation_b_content_hash=key.representation_b_content_hash,
        representation_version=key.representation_version,
        context_id=key.context_id,
        context_fingerprint=key.context_fingerprint,
        environment_schema_version=key.environment_schema_version,
        environment_hash=key.environment_hash,
        semantic_policy_version=key.semantic_policy_version,
        semantic_policy_sha256=key.semantic_policy_hash,
        lean_version=key.lean_version,
        lean_interact_version=key.lean_interact_version,
        repl_revision=key.repl_revision,
        project_revision=key.project_revision,
    )
    expected_record = LF020ExpectedEvidenceBinding(
        evidence_id=left.evidence.evidence_id,
        kind=left.evidence.kind,
        status=left.evidence.status,
        method_version=left.evidence.method_version,
        config_hash=left.evidence.config_hash or "",
        semantic_evidence_sha256=evidence_semantic_hash(left.evidence),
        cache_key_hash=left.cache_entry.cache_key_hash,
        evidence_direction=left.cache_entry.cache_key.evidence_direction,
        timeout_seconds=left.cache_entry.cache_key.timeout_seconds,
    )
    expected = build_lf020_expected_admission_binding(
        target_id=PAIR_ID,
        label_resolution_policy_sha256=hash_file(label_policy),
        output_manifest_config_hash=MANIFEST_CONFIG_HASH,
        output_manifest_context_hash=MANIFEST_CONTEXT_HASH,
        source_pair_fingerprint=hash_canonical([source_pair.model_dump(mode="json")]),
        upstream_evidence_id_fingerprint=hash_canonical(()),
        runtime=runtime,
        evidence=(expected_record,),
    )
    return _Fixture(
        root=tmp_path,
        left=left,
        right=right,
        source_pair=source_pair,
        report_path=report_path,
        admission=admission,
        expected=expected,
        replay_inputs=replay_inputs,
        manifest_locator=AdmissionArtifactLocator(
            artifact_id=admission.manifest_artifact_id,
            relative_path=manifest_path.relative_to(tmp_path).as_posix(),
        ),
        replay_locator=AdmissionArtifactLocator(
            artifact_id=admission.replay_artifact_id,
            relative_path=report_path.relative_to(tmp_path).as_posix(),
        ),
    )


def _verify(
    fixture: _Fixture,
    *,
    admission: EvidenceAdmissionRecord | None = None,
    manifest_locator: AdmissionArtifactLocator | None = None,
    replay_locator: AdmissionArtifactLocator | None = None,
    replay_inputs: LF020ReplayInputs | None = None,
    expected: LF020ExpectedAdmissionBinding | None = None,
) -> LF020EvidenceAdmissionDiagnosticResult:
    return verify_lf020_evidence_admission(
        verification_root=fixture.root,
        admission=admission or fixture.admission,
        manifest_locator=manifest_locator or fixture.manifest_locator,
        replay_locator=replay_locator or fixture.replay_locator,
        replay_inputs=replay_inputs or fixture.replay_inputs,
        expected=expected or fixture.expected,
    )


def test_verifies_exact_artifact_runtime_and_evidence_graph(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _verify(fixture)

    assert result.evidence_records == (fixture.left.evidence,)
    assert result.receipt.target_id == PAIR_ID
    assert result.receipt.evidence_ids == (fixture.left.evidence.evidence_id,)
    assert result.receipt.matched_replay_side == "left"
    assert result.receipt.context_id == fixture.left.cache_entry.cache_key.context_id
    assert result.receipt.production_guard_removed is False
    assert result.receipt.production_authority_established is False
    assert result.receipt.admissions_created == 0
    assert result.receipt.labels_created == 0


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture.left.output / "manifest.json").unlink()

    with pytest.raises(EvidenceAdmissionVerificationError, match="missing"):
        _verify(fixture)


@pytest.mark.parametrize("role", ["manifest", "replay"])
def test_admission_hash_tampering_fails_closed(tmp_path: Path, role: str) -> None:
    fixture = _fixture(tmp_path)
    path = fixture.left.output / "manifest.json" if role == "manifest" else fixture.report_path
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(EvidenceAdmissionVerificationError, match=f"{role} hash mismatch"):
        _verify(fixture)


def test_cross_target_admission_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    other_pair = make_id("pair", {"fixture": "lf024-admission-other"})
    admission = build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=other_pair,
        evidence_ids=(fixture.left.evidence.evidence_id,),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id=fixture.admission.manifest_artifact_id,
        manifest_artifact_sha256=fixture.admission.manifest_artifact_sha256,
        replay_artifact_id=fixture.admission.replay_artifact_id,
        replay_artifact_sha256=fixture.admission.replay_artifact_sha256,
        replay_passed=True,
        policy_sha256=fixture.admission.policy_sha256,
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="no evidence"):
        _verify(fixture, admission=admission)


def test_stale_label_policy_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    policy_path = fixture.root / "policies/label_resolution_v1.yaml"
    policy_path.write_bytes(policy_path.read_bytes() + b"changed: true\n")

    with pytest.raises(EvidenceAdmissionVerificationError, match="Gate-0 label policy"):
        _verify(fixture)


def test_stale_semantic_policy_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    policy_path = fixture.root / "policies/semantic_policy_v1.md"
    policy_path.write_bytes(policy_path.read_bytes() + b"changed\n")

    with pytest.raises(EvidenceAdmissionVerificationError, match="stale semantic policy"):
        _verify(fixture)


@pytest.mark.parametrize("field", ["context_fingerprint", "project_revision"])
def test_stale_context_or_runtime_fails_closed(tmp_path: Path, field: str) -> None:
    fixture = _fixture(tmp_path)
    replacement = "0" * 64 if field == "context_fingerprint" else "stale-project"
    runtime = fixture.expected.runtime.model_copy(update={field: replacement})
    expected = build_lf020_expected_admission_binding(
        target_id=fixture.expected.target_id,
        label_resolution_policy_sha256=fixture.expected.label_resolution_policy_sha256,
        output_manifest_config_hash=fixture.expected.output_manifest_config_hash,
        output_manifest_context_hash=fixture.expected.output_manifest_context_hash,
        source_pair_fingerprint=fixture.expected.source_pair_fingerprint,
        upstream_evidence_id_fingerprint=fixture.expected.upstream_evidence_id_fingerprint,
        runtime=runtime,
        evidence=fixture.expected.evidence,
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="target/context binding"):
        _verify(fixture, expected=expected)


def test_stale_method_or_config_binding_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    bound = fixture.expected.evidence[0]
    stale = bound.model_copy(update={"config_hash": "0" * 64})
    expected = build_lf020_expected_admission_binding(
        target_id=fixture.expected.target_id,
        label_resolution_policy_sha256=fixture.expected.label_resolution_policy_sha256,
        output_manifest_config_hash=fixture.expected.output_manifest_config_hash,
        output_manifest_context_hash=fixture.expected.output_manifest_context_hash,
        source_pair_fingerprint=fixture.expected.source_pair_fingerprint,
        upstream_evidence_id_fingerprint=fixture.expected.upstream_evidence_id_fingerprint,
        runtime=fixture.expected.runtime,
        evidence=(stale,),
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="method/config binding"):
        _verify(fixture, expected=expected)


def test_recomputed_replay_detects_tampered_cache_even_if_report_is_unchanged(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    cache_path = next(fixture.right.cache.rglob("*.json"))
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["lean_request_hashes"] = ["0" * 64]
    cache_path.write_bytes(canonical_json_bytes(document) + b"\n")

    with pytest.raises(EvidenceAdmissionVerificationError, match="recomputation"):
        _verify(fixture)


def test_locator_id_cannot_be_substituted(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    locator = fixture.manifest_locator.model_copy(update={"artifact_id": "other-manifest"})

    with pytest.raises(EvidenceAdmissionVerificationError, match="locator ID"):
        _verify(fixture, manifest_locator=locator)


@pytest.mark.parametrize(
    "field",
    ["source_pair_fingerprint", "upstream_evidence_id_fingerprint"],
)
def test_frozen_replay_lineage_cannot_be_substituted(tmp_path: Path, field: str) -> None:
    fixture = _fixture(tmp_path)
    values = fixture.expected.model_dump(mode="python", exclude={"binding_id"})
    values[field] = "0" * 64
    expected = build_lf020_expected_admission_binding(
        target_id=values["target_id"],
        label_resolution_policy_sha256=values["label_resolution_policy_sha256"],
        output_manifest_config_hash=values["output_manifest_config_hash"],
        output_manifest_context_hash=values["output_manifest_context_hash"],
        source_pair_fingerprint=values["source_pair_fingerprint"],
        upstream_evidence_id_fingerprint=values["upstream_evidence_id_fingerprint"],
        runtime=fixture.expected.runtime,
        evidence=fixture.expected.evidence,
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="lineage"):
        _verify(fixture, expected=expected)


def test_locator_parent_symlink_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    linked_output = fixture.root / "linked-output"
    linked_output.symlink_to(fixture.left.output, target_is_directory=True)
    locator = fixture.manifest_locator.model_copy(
        update={"relative_path": "linked-output/manifest.json"}
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="symlink component"):
        _verify(fixture, manifest_locator=locator)


def test_symlink_verification_root_fails_before_resolution(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    fixture = _fixture(real_root)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(
        EvidenceAdmissionVerificationError,
        match="verification_root traverses symlink component",
    ):
        verify_lf020_evidence_admission(
            verification_root=linked_root,
            admission=fixture.admission,
            manifest_locator=fixture.manifest_locator,
            replay_locator=fixture.replay_locator,
            replay_inputs=fixture.replay_inputs,
            expected=fixture.expected,
        )


def test_manifest_partition_path_outside_matched_output_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    manifest_path = fixture.left.output / "manifest.json"
    manifest = OutputManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    outside_evidence = fixture.root / "outside-output" / "evidence.jsonl"
    outside_evidence.parent.mkdir()
    outside_evidence.write_bytes((fixture.left.output / "evidence.jsonl").read_bytes())
    outside_path = str(outside_evidence)

    def move_evidence_checksum(checksums: dict[str, str]) -> dict[str, str]:
        moved = {
            path: digest
            for path, digest in checksums.items()
            if Path(path).name != "evidence.jsonl"
        }
        moved[outside_path] = hash_file(outside_evidence)
        return moved

    manifest = manifest.model_copy(
        update={
            "file_checksums": move_evidence_checksum(manifest.file_checksums),
            "output_partition_checksums": move_evidence_checksum(
                manifest.output_partition_checksums
            ),
        }
    )
    _write_model(manifest_path, manifest)
    report = compare_lf020_replays(
        left_output_dir=fixture.replay_inputs.left_output_dir,
        left_cache_root=fixture.replay_inputs.left_cache_root,
        left_artifact_root=fixture.replay_inputs.left_artifact_root,
        right_output_dir=fixture.replay_inputs.right_output_dir,
        right_cache_root=fixture.replay_inputs.right_cache_root,
        right_artifact_root=fixture.replay_inputs.right_artifact_root,
        source_pairs=fixture.replay_inputs.source_pairs,
        upstream_evidence_ids=fixture.replay_inputs.upstream_evidence_ids,
    )
    assert report.passed
    _write_model(fixture.report_path, report)
    admission = build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        evidence_ids=(fixture.left.evidence.evidence_id,),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id=fixture.admission.manifest_artifact_id,
        manifest_artifact_sha256=hash_file(manifest_path),
        replay_artifact_id=fixture.admission.replay_artifact_id,
        replay_artifact_sha256=hash_file(fixture.report_path),
        replay_passed=True,
        policy_sha256=fixture.admission.policy_sha256,
    )

    with pytest.raises(EvidenceAdmissionVerificationError, match="not contained"):
        _verify(fixture, admission=admission)


def test_artifact_graph_mutation_during_verification_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    real_compare = compare_lf020_replays

    def compare_then_mutate(**kwargs: object) -> object:
        report = real_compare(**kwargs)  # type: ignore[arg-type]
        (fixture.right.output / "failures.jsonl").write_bytes(b"mutated-after-replay\n")
        return report

    monkeypatch.setattr(admission_module, "compare_lf020_replays", compare_then_mutate)

    with pytest.raises(EvidenceAdmissionVerificationError, match="changed during verification"):
        _verify(fixture)

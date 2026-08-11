"""Closed-graph operation tests for the LF-024 diagnostic batch resolver.

These tests deliberately exercise :func:`resolve_label_batch` rather than the
Typer registration.  Keeping the operation contract separate prevents CLI
tests from accidentally replacing the fail-closed graph, replay, and manifest
coverage that protects the labeled dataset boundary.
"""

from __future__ import annotations

import datetime
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from leanfaith.cli.resolve_labels import (
    LabelResolutionBatchInputError,
    resolve_label_batch,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.labeling.aggregation import (
    EvidenceAdmissionRecord,
    build_evidence_admission_record,
)
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    AuthorityArtifactBinding,
    AuthorityArtifactKind,
    CandidateCommitment,
    ResolutionCandidate,
    ResolutionSource,
    load_active_label_resolution_policy,
    make_authority_artifact_binding,
    make_resolution_candidate,
)
from leanfaith.labeling.resolution import (
    ResolutionArtifacts,
    ResolutionAuditRecord,
)
from leanfaith.labeling.resolution import (
    resolve_target as resolve_single_target,
)
from leanfaith.schemas.enums import (
    ArtifactClass,
    Decision,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
    NLTrust,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.evidence import EvidenceRecord, TypecheckValue
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.label import ResolvedLabel
from leanfaith.schemas.manifest import CodeState, RunManifest, read_manifest
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 8, 11, 15, 30, tzinfo=datetime.UTC)
PAIR_ID = make_id("pair", {"fixture": "batch-resolution"})
OTHER_PAIR_ID = make_id("pair", {"fixture": "batch-resolution-other"})
NL_LEAN_ID = make_id("nllean", {"fixture": "batch-resolution-nl"})
THEOREM_A_ID = make_id("thm", {"fixture": "batch-resolution-a"})
THEOREM_B_ID = make_id("thm", {"fixture": "batch-resolution-b"})
EVIDENCE_ID = make_id("ev", {"fixture": "batch-typecheck"})


@pytest.fixture
def repo(tmp_path: Path) -> RepoPaths:
    """Build the smallest repository carrying the active policy binding."""

    for relative_path in (
        Path("policies/label_resolution_v1.yaml"),
        Path("reports/gates/gate_0.json"),
    ):
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative_path, destination)
    return RepoPaths(root=tmp_path)


def _code_state() -> CodeState:
    return CodeState(
        git_revision="1" * 40,
        git_dirty=False,
        base_git_commit="1" * 40,
        code_tree_hash="2" * 64,
        tracked_diff_hash="3" * 64,
    )


def _pair(
    evidence_ids: tuple[str, ...] = (),
    *,
    pair_id: str = PAIR_ID,
    resolved_label_id: str | None = None,
) -> PairRecord:
    return PairRecord(
        pair_id=pair_id,
        theorem_a_id=THEOREM_A_ID,
        theorem_b_id=THEOREM_B_ID,
        pair_source="batch_resolution_fixture",
        split_group_ids=("group:batch-resolution",),
        resolved_label_id=resolved_label_id,
        evidence_ids=evidence_ids,
    )


def _nl_target() -> NLPLeanRecord:
    return NLPLeanRecord(
        nl_lean_id=NL_LEAN_ID,
        problem_id="batch-resolution-problem",
        problem_group="problem:batch-resolution",
        source="fixture",
        source_revision="v1",
        nl_statement="Show that the candidate expresses the intended claim.",
        nl_trust=NLTrust.TRUSTED,
        candidate_theorem_id=THEOREM_B_ID,
        split_group_ids=("problem:batch-resolution",),
    )


def _evidence(
    *,
    target_id: str = PAIR_ID,
    evidence_id: str = EVIDENCE_ID,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=target_id,
        kind=EvidenceKind.TYPECHECK,
        status=EvidenceExecutionStatus.SUCCESS,
        value=TypecheckValue(outcome="valid"),
        method_version="batch_resolution_fixture_v1",
        config_hash="d" * 64,
        created_at=NOW,
    )


def _admission(
    policy: ActiveLabelResolutionPolicy,
    evidence: EvidenceRecord,
) -> EvidenceAdmissionRecord:
    return build_evidence_admission_record(
        target_kind=evidence.target_kind,
        target_id=evidence.target_id,
        evidence_ids=(evidence.evidence_id,),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id="manifest:batch-resolution",
        manifest_artifact_sha256="a" * 64,
        replay_artifact_id="replay:batch-resolution",
        replay_artifact_sha256="b" * 64,
        replay_passed=True,
        policy_sha256=policy.policy_file_sha256,
    )


def _artifact(
    kind: AuthorityArtifactKind,
    suffix: str,
) -> AuthorityArtifactBinding:
    return make_authority_artifact_binding(
        artifact_kind=kind,
        artifact_id=f"authority:{suffix}",
        artifact_sha256=make_id("blob", {"suffix": suffix}).split(":", 1)[1],
    )


def _human_candidate(
    policy: ActiveLabelResolutionPolicy,
    evidence_ids: tuple[str, ...],
    *,
    target_id: str = PAIR_ID,
    suffix: str = "human",
) -> ResolutionCandidate:
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=target_id,
        source=ResolutionSource.HUMAN_ADJUDICATION,
        quality_tier=QualityTier.GOLD_HUMAN,
        resolution_method="expert_adjudication",
        authority_artifacts=(_artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION, suffix),),
        accepted_evidence_ids=evidence_ids,
        commitment=CandidateCommitment.TERMINAL,
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
        provenance=(f"fixture:{suffix}",),
    )


def _benchmark_nl_candidate(
    policy: ActiveLabelResolutionPolicy,
) -> ResolutionCandidate:
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.NL_LEAN,
        target_id=NL_LEAN_ID,
        source=ResolutionSource.FROZEN_BENCHMARK_POLICY,
        quality_tier=QualityTier.BENCHMARK,
        resolution_method="benchmark_import",
        authority_artifacts=(
            _artifact(AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL, "benchmark-nl"),
        ),
        accepted_evidence_ids=(),
        commitment=CandidateCommitment.TERMINAL,
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
        provenance=("fixture:benchmark-nl",),
    )


def _write_jsonl(path: Path, records: Sequence[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n"  # type: ignore[attr-defined]
            for record in records
        )
    )


def _read_jsonl[ModelT](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    return tuple(
        model.model_validate(json.loads(line))  # type: ignore[attr-defined]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _input_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    return (
        tmp_path / "inputs" / "targets.jsonl",
        tmp_path / "inputs" / "evidence.jsonl",
        tmp_path / "inputs" / "admissions.jsonl",
        tmp_path / "inputs" / "candidates.jsonl",
    )


def test_resolve_label_batch_writes_closed_graph_outputs_and_manifest(
    repo: RepoPaths,
) -> None:
    policy = load_active_label_resolution_policy(repo.root)
    evidence = _evidence()
    admission = _admission(policy, evidence)
    candidate = _human_candidate(policy, (evidence.evidence_id,))
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair((evidence.evidence_id,)),))
    _write_jsonl(evidence_path, (evidence,))
    _write_jsonl(admission_path, (admission,))
    _write_jsonl(candidate_path, (candidate,))

    result = resolve_label_batch(
        paths=repo,
        target_path=target_path,
        evidence_path=evidence_path,
        admission_path=admission_path,
        candidate_path=candidate_path,
        resolved_at=NOW,
        run_nonce="a1b2c3d4",
        code_state=_code_state(),
    )

    labels = _read_jsonl(result.labels_path, ResolvedLabel)
    linked = _read_jsonl(result.linked_targets_path, PairRecord)
    audits = _read_jsonl(result.audits_path, ResolutionAuditRecord)
    manifest = read_manifest(result.run_manifest_path, RunManifest)
    assert result.target_kind is SemanticLabelTargetKind.LEAN_PAIR
    assert result.target_count == result.resolved_count == result.derivation_count == 1
    assert result.unresolved_count == result.conflict_count == result.override_count == 0
    assert labels[0].same_claim is True
    assert labels[0].relation is RelationLabel.EQUIVALENT
    assert labels[0].train_eligibility is False
    assert labels[0].eval_eligibility is False
    assert labels[0].label_id == make_id(
        "lbl",
        {
            "schema": "resolved_label_lf024_v1",
            **labels[0].model_dump(mode="json", exclude={"label_id"}),
        },
    )
    assert linked[0].resolved_label_id == labels[0].label_id
    assert audits[0].output_label_id == labels[0].label_id
    assert audits[0].linked_target_sha256 == hash_canonical(linked[0].model_dump(mode="json"))
    assert result.conflicts_path.read_bytes() == b""
    assert result.overrides_path.read_bytes() == b""
    assert manifest.artifact_class is ArtifactClass.DIAGNOSTIC
    assert manifest.execution["linked_evidence_graph_closed"] is True
    assert manifest.execution["candidate_partition_explicit"] is True
    assert manifest.execution["candidate_set_closed"] is False
    assert "closed_input_graph" not in manifest.execution
    assert manifest.execution["candidate_inference"] is False
    assert manifest.execution["candidate_promotion"] is False
    assert manifest.execution["production_admission"] is False
    assert manifest.status_counts["input_resolution_candidates"] == 1
    assert manifest.status_counts["candidates_invented"] == 0
    assert manifest.status_counts["candidates_promoted"] == 0
    assert result.run_manifest_sha256 == hash_file(result.run_manifest_path)


def test_empty_candidate_partition_produces_unresolved_review(
    repo: RepoPaths,
) -> None:
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair(),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())

    result = resolve_label_batch(
        paths=repo,
        target_path=target_path,
        evidence_path=evidence_path,
        admission_path=admission_path,
        candidate_path=candidate_path,
        resolved_at=NOW,
        run_nonce="b1b2b3b4",
        code_state=_code_state(),
    )

    (label,) = _read_jsonl(result.labels_path, ResolvedLabel)
    assert result.resolved_count == 0
    assert result.unresolved_count == 1
    assert label.same_claim is None
    assert label.relation is None
    assert label.resolution_outcome is ResolutionOutcome.UNRESOLVED
    assert label.quality_tier is QualityTier.UNKNOWN
    assert label.requires_adjudication is True
    assert label.decision is Decision.REVIEW
    assert label.train_eligibility is False
    assert label.eval_eligibility is False


def test_resolve_label_batch_supports_nl_lean_targets(repo: RepoPaths) -> None:
    policy = load_active_label_resolution_policy(repo.root)
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_nl_target(),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, (_benchmark_nl_candidate(policy),))

    result = resolve_label_batch(
        paths=repo,
        target_path=target_path,
        evidence_path=evidence_path,
        admission_path=admission_path,
        candidate_path=candidate_path,
        resolved_at=NOW,
        run_nonce="c1c2c3c4",
        code_state=_code_state(),
    )

    (label,) = _read_jsonl(result.labels_path, ResolvedLabel)
    (linked,) = _read_jsonl(result.linked_targets_path, NLPLeanRecord)
    assert result.target_kind is SemanticLabelTargetKind.NL_LEAN
    assert result.linked_targets_path.name == "nl_lean.jsonl"
    assert label.quality_tier is QualityTier.BENCHMARK
    assert label.train_eligibility is False
    assert label.eval_eligibility is False
    assert linked.resolved_label_id == label.label_id


def test_terminal_diagnostic_prior_label_replays_idempotently(repo: RepoPaths) -> None:
    policy = load_active_label_resolution_policy(repo.root)
    evidence = _evidence()
    admission = _admission(policy, evidence)
    candidate = _human_candidate(policy, (evidence.evidence_id,), suffix="prior-replay")
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair((evidence.evidence_id,)),))
    _write_jsonl(evidence_path, (evidence,))
    _write_jsonl(admission_path, (admission,))
    _write_jsonl(candidate_path, (candidate,))
    first = resolve_label_batch(
        paths=repo,
        target_path=target_path,
        evidence_path=evidence_path,
        admission_path=admission_path,
        candidate_path=candidate_path,
        output_dir=repo.root / "first-prior-replay",
        resolved_at=NOW,
        run_nonce="f1f2f3f4",
        code_state=_code_state(),
    )
    (first_label,) = _read_jsonl(first.labels_path, ResolvedLabel)
    (first_target,) = _read_jsonl(first.linked_targets_path, PairRecord)
    prior_path = repo.root / "inputs" / "prior_labels.jsonl"
    _write_jsonl(target_path, (first_target,))
    _write_jsonl(prior_path, (first_label,))

    replay = resolve_label_batch(
        paths=repo,
        target_path=target_path,
        evidence_path=evidence_path,
        admission_path=admission_path,
        candidate_path=candidate_path,
        prior_label_path=prior_path,
        output_dir=repo.root / "second-prior-replay",
        resolved_at=NOW,
        run_nonce="f5f6f7f8",
        code_state=_code_state(),
    )

    (replayed_label,) = _read_jsonl(replay.labels_path, ResolvedLabel)
    assert replayed_label == first_label
    assert replayed_label.train_eligibility is False
    assert replayed_label.eval_eligibility is False


def test_mixed_pair_and_nl_targets_fail_before_output(repo: RepoPaths) -> None:
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair(), _nl_target()))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())
    output_dir = repo.root / "should-not-exist"

    with pytest.raises(LabelResolutionBatchInputError, match="cannot be mixed"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            output_dir=output_dir,
            resolved_at=NOW,
            code_state=_code_state(),
        )
    assert not output_dir.exists()


def test_missing_linked_evidence_fails_closed(repo: RepoPaths) -> None:
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair((EVIDENCE_ID,)),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())

    with pytest.raises(LabelResolutionBatchInputError, match="evidence set is not closed"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            resolved_at=NOW,
            code_state=_code_state(),
        )


def test_evidence_without_admission_fails_closed(repo: RepoPaths) -> None:
    policy = load_active_label_resolution_policy(repo.root)
    evidence = _evidence()
    candidate = _human_candidate(policy, (evidence.evidence_id,))
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair((evidence.evidence_id,)),))
    _write_jsonl(evidence_path, (evidence,))
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, (candidate,))

    with pytest.raises(LabelResolutionBatchInputError, match=r"admission|admitted"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            resolved_at=NOW,
            code_state=_code_state(),
        )


def test_orphan_candidate_fails_closed(repo: RepoPaths) -> None:
    policy = load_active_label_resolution_policy(repo.root)
    other_evidence_id = make_id("ev", {"fixture": "batch-orphan-evidence"})
    candidate = _human_candidate(
        policy,
        (other_evidence_id,),
        target_id=OTHER_PAIR_ID,
        suffix="orphan",
    )
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair(),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, (candidate,))

    with pytest.raises(LabelResolutionBatchInputError, match="targets absent item"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            resolved_at=NOW,
            code_state=_code_state(),
        )


def test_duplicate_json_keys_are_rejected(repo: RepoPaths) -> None:
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_pair().model_dump(mode="json"))
    target_path.write_text(
        payload[:-1] + f', "pair_id": "{PAIR_ID}"}}\n',
        encoding="utf-8",
    )
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())

    with pytest.raises(LabelResolutionBatchInputError, match="duplicate JSON key"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            resolved_at=NOW,
            code_state=_code_state(),
        )


def test_linked_target_requires_exact_prior_label(repo: RepoPaths) -> None:
    missing_prior_id = make_id("lbl", {"fixture": "missing-prior"})
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair(resolved_label_id=missing_prior_id),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())

    with pytest.raises(LabelResolutionBatchInputError, match="exact prior_label"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            resolved_at=NOW,
            code_state=_code_state(),
        )


def test_input_drift_during_resolution_is_rejected(
    repo: RepoPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import resolve_labels as module

    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    _write_jsonl(target_path, (_pair(),))
    _write_jsonl(evidence_path, ())
    _write_jsonl(admission_path, ())
    _write_jsonl(candidate_path, ())

    def drift_after_resolution(**kwargs: object) -> ResolutionArtifacts:
        artifacts = resolve_single_target(**kwargs)  # type: ignore[arg-type]
        candidate_path.write_bytes(candidate_path.read_bytes() + b"\n")
        return artifacts

    monkeypatch.setattr(module, "resolve_target", drift_after_resolution)
    output_dir = repo.root / "drift-output"
    with pytest.raises(LabelResolutionBatchInputError, match="changed during resolution"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            output_dir=output_dir,
            resolved_at=NOW,
            code_state=_code_state(),
        )
    assert not output_dir.exists()


def test_batch_jsonl_permutations_preserve_semantic_partition_bytes(
    repo: RepoPaths,
) -> None:
    """Batch grouping must not make semantic artifacts depend on line order."""

    policy = load_active_label_resolution_policy(repo.root)
    first_evidence = _evidence(
        evidence_id=make_id("ev", {"fixture": "batch-order-first"}),
    )
    second_evidence = _evidence(
        target_id=OTHER_PAIR_ID,
        evidence_id=make_id("ev", {"fixture": "batch-order-second"}),
    )
    first_target = _pair((first_evidence.evidence_id,))
    second_target = _pair(
        (second_evidence.evidence_id,),
        pair_id=OTHER_PAIR_ID,
    )
    first_admission = _admission(policy, first_evidence)
    second_admission = _admission(policy, second_evidence)
    first_candidate = _human_candidate(
        policy,
        (first_evidence.evidence_id,),
        suffix="batch-order-first",
    )
    second_candidate = _human_candidate(
        policy,
        (second_evidence.evidence_id,),
        target_id=OTHER_PAIR_ID,
        suffix="batch-order-second",
    )

    def run_ordered(
        *,
        directory: Path,
        reversed_order: bool,
        nonce: str,
    ) -> tuple[bytes, ...]:
        target_path = directory / "targets.jsonl"
        evidence_path = directory / "evidence.jsonl"
        admission_path = directory / "admissions.jsonl"
        candidate_path = directory / "candidates.jsonl"
        ordered: tuple[Sequence[object], ...] = (
            (first_target, second_target),
            (first_evidence, second_evidence),
            (first_admission, second_admission),
            (first_candidate, second_candidate),
        )
        if reversed_order:
            ordered = tuple(tuple(reversed(records)) for records in ordered)
        for path, records in zip(
            (target_path, evidence_path, admission_path, candidate_path),
            ordered,
            strict=True,
        ):
            _write_jsonl(path, records)
        artifacts = resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            output_dir=repo.root / "outputs" / directory.name,
            resolved_at=NOW,
            run_nonce=nonce,
            code_state=_code_state(),
        )
        return tuple(
            path.read_bytes()
            for path in (
                artifacts.linked_targets_path,
                artifacts.labels_path,
                artifacts.audits_path,
                artifacts.derivations_path,
                artifacts.conflicts_path,
                artifacts.overrides_path,
            )
        )

    forward = run_ordered(
        directory=repo.root / "inputs-forward",
        reversed_order=False,
        nonce="d1d2d3d4",
    )
    backward = run_ordered(
        directory=repo.root / "inputs-backward",
        reversed_order=True,
        nonce="e1e2e3e4",
    )
    assert backward == forward


def test_production_mode_is_rejected_before_reading_inputs(repo: RepoPaths) -> None:
    target_path, evidence_path, admission_path, candidate_path = _input_paths(repo.root)
    with pytest.raises(LabelResolutionBatchInputError, match="only diagnostic"):
        resolve_label_batch(
            paths=repo,
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            artifact_class=ArtifactClass.PRODUCTION,
            resolved_at=NOW,
            code_state=_code_state(),
        )

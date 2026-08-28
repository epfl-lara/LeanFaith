from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    AuthorityArtifactBinding,
    AuthorityArtifactKind,
    CandidateCommitment,
    LabelResolutionPolicyError,
    ResolutionCandidate,
    ResolutionSource,
    load_active_label_resolution_policy,
    make_authority_artifact_binding,
    make_authority_artifact_binding_id,
    make_resolution_candidate,
    make_resolution_candidate_id,
)
from leanfaith.schemas.enums import (
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import make_id

REPO_ROOT = Path(__file__).resolve().parents[2]
PAIR_ID = make_id("pair", {"fixture": "quality-target"})
POLICY_PATH = REPO_ROOT / "policies/label_resolution_v1.yaml"
GATE_PATH = REPO_ROOT / "reports/gates/gate_0.json"

EVIDENCE_IDS = tuple(make_id("ev", {"fixture": index}) for index in range(6))
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture(scope="module")
def policy() -> ActiveLabelResolutionPolicy:
    return load_active_label_resolution_policy(REPO_ROOT)


def _artifact(
    kind: AuthorityArtifactKind,
    *,
    name: str = "authority",
    digest: str = HASH_A,
) -> AuthorityArtifactBinding:
    return make_authority_artifact_binding(
        artifact_kind=kind,
        artifact_id=f"fixture:{name}",
        artifact_sha256=digest,
    )


def _candidate(
    policy: ActiveLabelResolutionPolicy,
    *,
    source: ResolutionSource = ResolutionSource.HUMAN_ADJUDICATION,
    quality_tier: QualityTier = QualityTier.GOLD_HUMAN,
    resolution_method: str = "expert_adjudication",
    artifact_kind: AuthorityArtifactKind = AuthorityArtifactKind.HUMAN_ADJUDICATION,
    authority_artifacts: tuple[AuthorityArtifactBinding, ...] | None = None,
    accepted_evidence_ids: tuple[str, ...] = (EVIDENCE_IDS[0],),
    commitment: CandidateCommitment = CandidateCommitment.TERMINAL,
    same_claim: bool | None = True,
    resolution_outcome: ResolutionOutcome = ResolutionOutcome.SAME_CLAIM,
    relation: RelationLabel | None = RelationLabel.EQUIVALENT,
    provenance: tuple[str, ...] = ("fixture",),
    error_types: tuple[str, ...] = (),
    truth_A_implies_B: bool | None = None,
    truth_B_implies_A: bool | None = None,
    F2_truth_equivalent: bool | None = None,
    F0_representation_equivalent: bool | None = None,
) -> ResolutionCandidate:
    artifacts = authority_artifacts or (_artifact(artifact_kind),)
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        source=source,
        quality_tier=quality_tier,
        resolution_method=resolution_method,
        authority_artifacts=artifacts,
        accepted_evidence_ids=accepted_evidence_ids,
        commitment=commitment,
        same_claim=same_claim,
        resolution_outcome=resolution_outcome,
        relation=relation,
        provenance=provenance,
        F0_representation_equivalent=F0_representation_equivalent,
        truth_A_implies_B=truth_A_implies_B,
        truth_B_implies_A=truth_B_implies_A,
        F2_truth_equivalent=F2_truth_equivalent,
        error_types=error_types,
    )


def test_nontrusted_candidate_cannot_self_assert_f0(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    with pytest.raises(ValidationError, match="cannot self-assert F0"):
        _candidate(
            policy,
            source=ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
            quality_tier=QualityTier.SILVER_CONSENSUS,
            resolution_method="independent_consensus_audited",
            artifact_kind=AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION,
            accepted_evidence_ids=EVIDENCE_IDS[:4],
            F0_representation_equivalent=True,
        )


def _copy_policy_inputs(destination: Path) -> None:
    (destination / "policies").mkdir(parents=True)
    (destination / "reports/gates").mkdir(parents=True)
    shutil.copy2(POLICY_PATH, destination / "policies/label_resolution_v1.yaml")
    shutil.copy2(GATE_PATH, destination / "reports/gates/gate_0.json")


def test_active_policy_is_bound_to_exact_gate0_policy_bytes(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))

    assert policy.policy_version == "label_resolution_v1"
    assert policy.policy_file_sha256 == hash_file(POLICY_PATH)
    assert policy.policy_file_sha256 == gate["inputs"]["label_resolution_policy"]
    assert policy.gate_file_sha256 == hash_file(GATE_PATH)
    assert policy.gate_decision == "pass_internal_research_only"
    assert tuple(entry.rank for entry in policy.precedence) == (1, 2, 3, 4, 5)


def test_policy_loader_fails_closed_when_policy_bytes_drift(tmp_path: Path) -> None:
    _copy_policy_inputs(tmp_path)
    policy_path = tmp_path / "policies/label_resolution_v1.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8") + "\n# unbound drift\n",
        encoding="utf-8",
    )

    with pytest.raises(LabelResolutionPolicyError, match="does not bind the exact"):
        load_active_label_resolution_policy(tmp_path)


def test_policy_loader_fails_closed_when_gate_check_fails(tmp_path: Path) -> None:
    _copy_policy_inputs(tmp_path)
    gate_path = tmp_path / "reports/gates/gate_0.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["checks"]["canonical_policies_complete"] = False
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(LabelResolutionPolicyError, match="missing or failing check"):
        load_active_label_resolution_policy(tmp_path)


@pytest.mark.parametrize(
    ("section", "mutation", "key"),
    [
        ("checks", "delete", "canonical_policies_complete"),
        ("checks", "add", "unexpected_check"),
        ("inputs", "delete", "phase_report"),
        ("inputs", "add", "unexpected_input"),
    ],
)
def test_policy_loader_requires_exact_gate0_registry_keys(
    tmp_path: Path,
    section: str,
    mutation: str,
    key: str,
) -> None:
    _copy_policy_inputs(tmp_path)
    gate_path = tmp_path / "reports/gates/gate_0.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    registry = gate[section]
    if mutation == "delete":
        del registry[key]
    else:
        registry[key] = True if section == "checks" else "f" * 64
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(
        LabelResolutionPolicyError,
        match=rf"gate_0\.{section} keys differ from canonical Gate-0 v2 schema",
    ):
        load_active_label_resolution_policy(tmp_path)


@pytest.mark.parametrize("schema_version", [1, 3, "2", None])
def test_policy_loader_requires_gate0_schema_v2(
    tmp_path: Path,
    schema_version: object,
) -> None:
    _copy_policy_inputs(tmp_path)
    gate_path = tmp_path / "reports/gates/gate_0.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["schema_version"] = schema_version
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(LabelResolutionPolicyError, match="schema_version=2"):
        load_active_label_resolution_policy(tmp_path)


def test_policy_loader_rejects_extra_gate0_top_level_key(tmp_path: Path) -> None:
    _copy_policy_inputs(tmp_path)
    gate_path = tmp_path / "reports/gates/gate_0.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["unexpected"] = True
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(
        LabelResolutionPolicyError,
        match="gate_0 keys differ from canonical Gate-0 v2 schema",
    ):
        load_active_label_resolution_policy(tmp_path)


def test_policy_loader_requires_sha256_for_every_gate0_input(tmp_path: Path) -> None:
    _copy_policy_inputs(tmp_path)
    gate_path = tmp_path / "reports/gates/gate_0.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["inputs"]["phase_report"] = "not-a-sha256"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(LabelResolutionPolicyError, match="lowercase SHA-256"):
        load_active_label_resolution_policy(tmp_path)


def test_authority_binding_is_content_addressed_and_round_trips() -> None:
    binding = _artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION)

    assert binding.authority_binding_id == make_authority_artifact_binding_id(
        artifact_kind=binding.artifact_kind,
        artifact_id=binding.artifact_id,
        artifact_sha256=binding.artifact_sha256,
    )
    assert AuthorityArtifactBinding.model_validate_json(binding.model_dump_json()) == binding

    payload = binding.model_dump(mode="python")
    with pytest.raises(ValidationError, match="does not match canonical artifact content"):
        AuthorityArtifactBinding.model_validate({**payload, "artifact_sha256": HASH_B})


@pytest.mark.parametrize(
    ("same_claim", "outcome", "relation"),
    [
        (True, ResolutionOutcome.SAME_CLAIM, RelationLabel.EQUIVALENT),
        (False, ResolutionOutcome.NOT_SAME_CLAIM, RelationLabel.INCOMPARABLE),
        (False, ResolutionOutcome.NOT_SAME_CLAIM, RelationLabel.UNRELATED),
        (None, ResolutionOutcome.AMBIGUOUS, RelationLabel.AMBIGUOUS),
    ],
)
def test_human_authority_admits_terminal_semantic_candidates(
    policy: ActiveLabelResolutionPolicy,
    same_claim: bool | None,
    outcome: ResolutionOutcome,
    relation: RelationLabel,
) -> None:
    candidate = _candidate(
        policy,
        same_claim=same_claim,
        resolution_outcome=outcome,
        relation=relation,
    )

    assert candidate.commitment == CandidateCommitment.TERMINAL
    assert candidate.same_claim is same_claim
    assert candidate.relation == relation
    assert candidate.candidate_id == make_resolution_candidate_id(candidate)


def test_benchmark_authority_can_admit_terminal_ambiguity(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    candidate = _candidate(
        policy,
        source=ResolutionSource.FROZEN_BENCHMARK_POLICY,
        quality_tier=QualityTier.BENCHMARK,
        resolution_method="benchmark_import",
        artifact_kind=AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL,
        accepted_evidence_ids=(),
        same_claim=None,
        resolution_outcome=ResolutionOutcome.AMBIGUOUS,
        relation=RelationLabel.AMBIGUOUS,
    )

    assert candidate.source_rank == 2
    assert candidate.same_claim is None


def test_promoted_conservative_candidate_must_be_terminal_positive(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    positive = _candidate(
        policy,
        source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        quality_tier=QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
        resolution_method="p01_alpha_certificate",
        artifact_kind=AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION,
    )
    assert positive.same_claim is True

    with pytest.raises(ValidationError, match="conservative transformation candidates"):
        _candidate(
            policy,
            source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
            quality_tier=QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
            resolution_method="p01_alpha_certificate",
            artifact_kind=AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION,
            same_claim=False,
            resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
            relation=RelationLabel.UNRELATED,
        )


def test_silver_consensus_requires_four_evidence_ids_for_positive_candidate(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    common = {
        "source": ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
        "quality_tier": QualityTier.SILVER_CONSENSUS,
        "resolution_method": "independent_consensus_audited",
        "artifact_kind": AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION,
    }

    with pytest.raises(ValidationError, match="insufficient accepted evidence"):
        _candidate(policy, accepted_evidence_ids=EVIDENCE_IDS[:3], **common)  # type: ignore[arg-type]

    candidate = _candidate(policy, accepted_evidence_ids=EVIDENCE_IDS[:4], **common)  # type: ignore[arg-type]
    assert candidate.same_claim is True
    assert candidate.quality_tier == QualityTier.SILVER_CONSENSUS
    assert len(candidate.accepted_evidence_ids) == 4


def test_separator_certificate_is_partial_negative_only(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    separator = _candidate(
        policy,
        source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="separator_certificate",
        artifact_kind=AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
        commitment=CandidateCommitment.PARTIAL_NEGATIVE,
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=None,
    )
    assert separator.commitment == CandidateCommitment.PARTIAL_NEGATIVE
    assert separator.relation is None

    with pytest.raises(ValidationError, match="separator certificate alone is partial"):
        _candidate(
            policy,
            source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
            quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
            resolution_method="separator_certificate",
            artifact_kind=AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
            same_claim=False,
            resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
            relation=RelationLabel.INCOMPARABLE,
        )


@pytest.mark.parametrize("relation", [RelationLabel.A_STRONGER, RelationLabel.B_STRONGER])
def test_directional_proof_plus_separator_requires_terminal_direction(
    policy: ActiveLabelResolutionPolicy,
    relation: RelationLabel,
) -> None:
    candidate = _candidate(
        policy,
        source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="directional_proof_plus_separator",
        artifact_kind=AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=relation,
    )
    assert candidate.commitment == CandidateCommitment.TERMINAL
    assert candidate.relation == relation


@pytest.mark.parametrize(
    ("commitment", "relation", "message"),
    [
        (
            CandidateCommitment.TERMINAL,
            RelationLabel.INCOMPARABLE,
            "requires a terminal directional relation",
        ),
        (
            CandidateCommitment.PARTIAL_NEGATIVE,
            None,
            "partial_negative is reserved",
        ),
    ],
)
def test_directional_proof_plus_separator_rejects_non_directional_or_partial_forms(
    policy: ActiveLabelResolutionPolicy,
    commitment: CandidateCommitment,
    relation: RelationLabel | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _candidate(
            policy,
            source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
            quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
            resolution_method="directional_proof_plus_separator",
            artifact_kind=AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
            commitment=commitment,
            same_claim=False,
            resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
            relation=relation,
        )


@pytest.mark.parametrize(
    ("tier", "method"),
    [
        (QualityTier.PROVISIONAL, "smoke_alpha_certificate"),
        (QualityTier.UNKNOWN, "unregistered_unknown"),
    ],
)
def test_provisional_and_unknown_candidates_cannot_cross_authority_boundary(
    policy: ActiveLabelResolutionPolicy,
    tier: QualityTier,
    method: str,
) -> None:
    with pytest.raises(ValueError, match="not registered by the active policy"):
        _candidate(policy, quality_tier=tier, resolution_method=method)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "authority_artifacts",
            (
                _artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION),
                _artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION),
            ),
            "authority_artifacts must not contain duplicates",
        ),
        (
            "accepted_evidence_ids",
            (EVIDENCE_IDS[0], EVIDENCE_IDS[0]),
            "accepted_evidence_ids must not contain duplicates",
        ),
        ("error_types", ("E01", "E01"), "error_types must not contain duplicates"),
        ("provenance", ("fixture", "fixture"), "provenance must not contain duplicates"),
    ],
)
def test_factory_rejects_duplicate_authority_inputs(
    policy: ActiveLabelResolutionPolicy,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _candidate(policy, **{field: value})  # type: ignore[arg-type]


def test_factory_canonicalizes_order_and_produces_deterministic_id(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    required = _artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION, name="z", digest=HASH_B)
    supporting = _artifact(AuthorityArtifactKind.SUPPORTING_AUDIT, name="a", digest=HASH_A)

    first = _candidate(
        policy,
        authority_artifacts=(required, supporting),
        accepted_evidence_ids=(EVIDENCE_IDS[2], EVIDENCE_IDS[0], EVIDENCE_IDS[1]),
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=RelationLabel.UNRELATED,
        provenance=("z-source", "a-source"),
        error_types=("E25", "E01"),
    )
    second = _candidate(
        policy,
        authority_artifacts=(supporting, required),
        accepted_evidence_ids=(EVIDENCE_IDS[1], EVIDENCE_IDS[2], EVIDENCE_IDS[0]),
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=RelationLabel.UNRELATED,
        provenance=("a-source", "z-source"),
        error_types=("E01", "E25"),
    )

    assert first == second
    assert first.candidate_id == second.candidate_id
    assert first.authority_artifacts == tuple(
        sorted(
            (required, supporting),
            key=lambda item: (item.artifact_kind.value, item.artifact_id, item.artifact_sha256),
        )
    )
    assert first.accepted_evidence_ids == tuple(sorted(first.accepted_evidence_ids))
    assert first.error_types == ("E01", "E25")
    assert first.provenance == ("a-source", "z-source")


def test_candidate_round_trip_and_content_tampering_are_fail_closed(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    candidate = _candidate(policy)
    assert ResolutionCandidate.model_validate_json(candidate.model_dump_json()) == candidate

    payload = candidate.model_dump(mode="python")
    with pytest.raises(ValidationError, match="candidate_id does not match canonical"):
        ResolutionCandidate.model_validate({**payload, "provenance": ("changed-but-still-valid",)})


def test_direct_candidate_reader_rejects_noncanonical_sequence_order(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    candidate = _candidate(
        policy,
        accepted_evidence_ids=EVIDENCE_IDS[:2],
        provenance=("a", "b"),
    )
    payload = candidate.model_dump(mode="python")

    with pytest.raises(ValidationError, match="accepted_evidence_ids must be sorted and unique"):
        ResolutionCandidate.model_validate(
            {**payload, "accepted_evidence_ids": tuple(reversed(candidate.accepted_evidence_ids))}
        )
    with pytest.raises(ValidationError, match="provenance must be nonempty, sorted, and unique"):
        ResolutionCandidate.model_validate(
            {**payload, "provenance": tuple(reversed(candidate.provenance))}
        )

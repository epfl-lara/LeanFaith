"""Authority-bound resolution candidates for LF-024.

This module is deliberately narrower than the resolver.  It admits only
content-addressed candidates produced by the four policy-authorized sources;
generation intention, provisional data, and unresolved semantic guesses never
cross this boundary.  Loading the policy is also fail-closed: the exact policy
file bytes must be named by a passing Gate-0 report.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import ConfigError, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.variant import _check_ecodes

_POLICY_RELATIVE_PATH = "policies/label_resolution_v1.yaml"
_GATE_RELATIVE_PATH = "reports/gates/gate_0.json"
_POLICY_VERSION = "label_resolution_v1"
_AUTHORITY_PREFIX = "authority_binding"
_CANDIDATE_PREFIX = "resolution_candidate"

_EXPECTED_GATE0_TOP_LEVEL_KEYS = frozenset(
    {
        "checks",
        "date",
        "decision",
        "evidence",
        "gate",
        "inputs",
        "notes",
        "open_items_outside_gate_scope",
        "schema_version",
    }
)
_EXPECTED_GATE0_CHECK_KEYS = frozenset(
    {
        "backbone_pilot_preregistered",
        "benchmark_registry_frozen",
        "canonical_policies_complete",
        "external_provider_slots_disabled",
        "formalrx_unavailability_nonblocking",
        "private_access_basis_recorded",
        "private_external_transmission_prohibited",
        "private_license_status_undeclared",
        "private_redistribution_prohibited",
        "public_replication_profile_recorded",
        "toolchains_in_supported_range",
        "verified_private_revision_canonical",
    }
)
_EXPECTED_GATE0_INPUT_KEYS = frozenset(
    {
        "backbone_pilot",
        "backbone_registry",
        "benchmark_denylist",
        "benchmark_freeze",
        "benchmark_registry",
        "environment_lock",
        "formalrx_comparison_policy",
        "formalrx_config",
        "label_resolution_policy",
        "phase_report",
        "preregistration_policy",
        "private_source_policy",
        "provider_registry",
        "public_replication_profile",
        "sci_crosswalk",
        "semantic_policy",
        "sft_classic_source_config",
        "split_config",
        "training_arms",
    }
)


class LabelResolutionPolicyError(ValueError):
    """The policy or its Gate-0 activation binding is invalid."""


class ResolutionSource(StrEnum):
    HUMAN_ADJUDICATION = "human_adjudication"
    FROZEN_BENCHMARK_POLICY = "frozen_benchmark_policy"
    PROMOTED_CERTIFICATE_OR_SEPARATOR = "promoted_certificate_or_separator"
    PROMOTED_INDEPENDENT_CONSENSUS = "promoted_independent_consensus"


class CandidateCommitment(StrEnum):
    TERMINAL = "terminal"
    PARTIAL_NEGATIVE = "partial_negative"


class AuthorityArtifactKind(StrEnum):
    HUMAN_ADJUDICATION = "human_adjudication"
    FROZEN_BENCHMARK_LABEL = "frozen_benchmark_label"
    CONSERVATIVE_FAMILY_PROMOTION = "conservative_family_promotion"
    CERTIFICATE_OR_SEPARATOR = "certificate_or_separator"
    INDEPENDENT_CONSENSUS_PROMOTION = "independent_consensus_promotion"
    SUPPORTING_AUDIT = "supporting_audit"


class PolicyPrecedenceEntry(StrictModel):
    rank: int = Field(ge=1)
    source: str
    quality_tiers: tuple[QualityTier, ...]
    strength: Literal["strong", "weak"]


class RegisteredResolutionMethod(StrictModel):
    method: str = Field(min_length=1)
    route: str = Field(min_length=1)
    tier: QualityTier


_EXPECTED_PRECEDENCE = (
    (1, "human_adjudication", (QualityTier.GOLD_HUMAN,), "strong"),
    (2, "frozen_benchmark_policy", (QualityTier.BENCHMARK,), "strong"),
    (
        3,
        "promoted_certificate_or_separator",
        (QualityTier.GOLD_CONSERVATIVE_TRANSFORM, QualityTier.GOLD_COUNTEREXAMPLE),
        "strong",
    ),
    (4, "promoted_independent_consensus", (QualityTier.SILVER_CONSENSUS,), "weak"),
    (5, "generation_intention", (QualityTier.PROVISIONAL,), "weak"),
)

_EXPECTED_METHODS = (
    ("separator_certificate", "N1", QualityTier.GOLD_COUNTEREXAMPLE),
    ("directional_proof_plus_separator", "N2", QualityTier.GOLD_COUNTEREXAMPLE),
    ("expert_adjudication", "N3", QualityTier.GOLD_HUMAN),
    ("expert_binder_aligned_claim_comparison", "N3", QualityTier.GOLD_HUMAN),
    ("independent_consensus_audited", "N4", QualityTier.SILVER_CONSENSUS),
    (
        "p01_alpha_certificate",
        "positive_conservative_family_promotion",
        QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
    ),
    ("benchmark_import", "frozen_benchmark_policy", QualityTier.BENCHMARK),
    ("smoke_alpha_certificate", "smoke_plumbing_only", QualityTier.PROVISIONAL),
)


class ActiveLabelResolutionPolicy(StrictModel):
    """Validated essential projection of the exact Gate-0-bound policy."""

    schema_version: Literal[1] = 1
    policy_version: Literal["label_resolution_v1"] = "label_resolution_v1"
    policy_relative_path: Literal["policies/label_resolution_v1.yaml"] = (
        "policies/label_resolution_v1.yaml"
    )
    policy_file_sha256: str = Field(pattern=HEX64_PATTERN)
    authored_status: str = Field(min_length=1)
    gate_relative_path: Literal["reports/gates/gate_0.json"] = "reports/gates/gate_0.json"
    gate_file_sha256: str = Field(pattern=HEX64_PATTERN)
    gate_decision: Literal["pass_internal_research_only"]
    precedence: tuple[PolicyPrecedenceEntry, ...]
    registered_methods: tuple[RegisteredResolutionMethod, ...]

    @model_validator(mode="after")
    def _essential_registry_is_exact(self) -> ActiveLabelResolutionPolicy:
        precedence = tuple(
            (entry.rank, entry.source, entry.quality_tiers, entry.strength)
            for entry in self.precedence
        )
        if precedence != _EXPECTED_PRECEDENCE:
            raise ValueError("label-resolution precedence differs from canonical v1 registry")
        methods = tuple((item.method, item.route, item.tier) for item in self.registered_methods)
        if methods != _EXPECTED_METHODS:
            raise ValueError("resolution-method registry differs from canonical v1 registry")
        return self


_SOURCE_RANK: dict[ResolutionSource, int] = {
    ResolutionSource.HUMAN_ADJUDICATION: 1,
    ResolutionSource.FROZEN_BENCHMARK_POLICY: 2,
    ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR: 3,
    ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS: 4,
}

_METHOD_BINDINGS: dict[str, tuple[ResolutionSource, QualityTier]] = {
    "expert_adjudication": (
        ResolutionSource.HUMAN_ADJUDICATION,
        QualityTier.GOLD_HUMAN,
    ),
    "expert_binder_aligned_claim_comparison": (
        ResolutionSource.HUMAN_ADJUDICATION,
        QualityTier.GOLD_HUMAN,
    ),
    "benchmark_import": (
        ResolutionSource.FROZEN_BENCHMARK_POLICY,
        QualityTier.BENCHMARK,
    ),
    "p01_alpha_certificate": (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
    ),
    "separator_certificate": (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_COUNTEREXAMPLE,
    ),
    "directional_proof_plus_separator": (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_COUNTEREXAMPLE,
    ),
    "independent_consensus_audited": (
        ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
        QualityTier.SILVER_CONSENSUS,
    ),
}

_REQUIRED_ARTIFACT_KIND: dict[tuple[ResolutionSource, QualityTier], AuthorityArtifactKind] = {
    (
        ResolutionSource.HUMAN_ADJUDICATION,
        QualityTier.GOLD_HUMAN,
    ): AuthorityArtifactKind.HUMAN_ADJUDICATION,
    (
        ResolutionSource.FROZEN_BENCHMARK_POLICY,
        QualityTier.BENCHMARK,
    ): AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL,
    (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
    ): AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION,
    (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_COUNTEREXAMPLE,
    ): AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
    (
        ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
        QualityTier.SILVER_CONSENSUS,
    ): AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION,
}

_TARGET_PATTERNS = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}
_NEGATIVE_RELATIONS = frozenset(
    {
        RelationLabel.A_STRONGER,
        RelationLabel.B_STRONGER,
        RelationLabel.INCOMPARABLE,
        RelationLabel.UNRELATED,
    }
)


class AuthorityArtifactBinding(StrictModel):
    """Content-addressed binding to one artifact authorized to create a candidate."""

    schema_version: Literal[1] = 1
    authority_binding_id: str = Field(pattern=id_pattern(_AUTHORITY_PREFIX))
    artifact_kind: AuthorityArtifactKind
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _content_address(self) -> AuthorityArtifactBinding:
        expected = make_authority_artifact_binding_id(
            artifact_kind=self.artifact_kind,
            artifact_id=self.artifact_id,
            artifact_sha256=self.artifact_sha256,
        )
        if self.authority_binding_id != expected:
            raise ValueError("authority_binding_id does not match canonical artifact content")
        return self


class ResolutionCandidate(StrictModel):
    """One authoritative, policy-bound semantic commitment before conflict resolution."""

    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=id_pattern(_CANDIDATE_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    policy_version: Literal["label_resolution_v1"] = "label_resolution_v1"
    policy_file_sha256: str = Field(pattern=HEX64_PATTERN)
    source: ResolutionSource
    source_rank: int = Field(ge=1, le=4)
    quality_tier: QualityTier
    resolution_method: str = Field(min_length=1)
    authority_artifacts: tuple[AuthorityArtifactBinding, ...] = Field(min_length=1)
    accepted_evidence_ids: tuple[str, ...] = ()
    commitment: CandidateCommitment
    same_claim: bool | None
    resolution_outcome: ResolutionOutcome
    relation: RelationLabel | None
    F0_representation_equivalent: bool | None = None
    truth_A_implies_B: bool | None = None
    truth_B_implies_A: bool | None = None
    F2_truth_equivalent: bool | None = None
    error_types: tuple[str, ...] = ()
    provenance: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _authority_and_semantics(self) -> ResolutionCandidate:
        if re.fullmatch(_TARGET_PATTERNS[self.target_kind], self.target_id) is None:
            raise ValueError("target_id does not match target_kind")
        if self.source_rank != _SOURCE_RANK[self.source]:
            raise ValueError("source_rank is not canonical for resolution source")
        binding = _METHOD_BINDINGS.get(self.resolution_method)
        if binding != (self.source, self.quality_tier):
            raise ValueError("resolution method is unregistered for source/tier")
        if self.quality_tier in {QualityTier.PROVISIONAL, QualityTier.UNKNOWN}:
            raise ValueError("provisional/unknown candidates cannot cross authority boundary")

        artifact_keys = tuple(
            (item.artifact_kind.value, item.artifact_id, item.artifact_sha256)
            for item in self.authority_artifacts
        )
        if artifact_keys != tuple(sorted(artifact_keys)) or len(set(artifact_keys)) != len(
            artifact_keys
        ):
            raise ValueError("authority_artifacts must be sorted and unique")
        required_kind = _REQUIRED_ARTIFACT_KIND[(self.source, self.quality_tier)]
        if required_kind not in {item.artifact_kind for item in self.authority_artifacts}:
            raise ValueError(f"authority candidate requires {required_kind.value} artifact")

        if self.accepted_evidence_ids != tuple(sorted(set(self.accepted_evidence_ids))):
            raise ValueError("accepted_evidence_ids must be sorted and unique")
        evidence_pattern = id_pattern(EVIDENCE_PREFIX)
        if any(re.fullmatch(evidence_pattern, item) is None for item in self.accepted_evidence_ids):
            raise ValueError("accepted_evidence_ids must contain only canonical ev: IDs")
        minimum_evidence = {
            ResolutionSource.HUMAN_ADJUDICATION: 1,
            ResolutionSource.FROZEN_BENCHMARK_POLICY: 0,
            ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR: 1,
            # The registered LF-022 aggregate is two independent judge
            # families evaluated in both AB and BA orientations.  A pair of
            # raw agreeing votes is not the promoted aggregate.
            ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS: 4,
        }[self.source]
        if len(self.accepted_evidence_ids) < minimum_evidence:
            raise ValueError("insufficient accepted evidence for resolution source")

        if self.error_types != tuple(sorted(set(self.error_types))):
            raise ValueError("error_types must be sorted and unique")
        _check_ecodes(self.error_types)
        if self.provenance != tuple(sorted(set(self.provenance))):
            raise ValueError("provenance must be nonempty, sorted, and unique")
        if any(not item.strip() for item in self.provenance):
            raise ValueError("provenance entries must be nonempty")

        if self.commitment == CandidateCommitment.PARTIAL_NEGATIVE:
            if not (
                self.same_claim is False
                and self.resolution_outcome == ResolutionOutcome.NOT_SAME_CLAIM
                and self.relation is None
            ):
                raise ValueError("partial_negative requires false/not_same_claim/relation=null")
            if not (
                self.quality_tier == QualityTier.GOLD_COUNTEREXAMPLE
                and self.resolution_method == "separator_certificate"
            ):
                raise ValueError(
                    "partial_negative is reserved for an accepted separator without "
                    "relation-bearing authority"
                )
        elif self.same_claim is True:
            if not (
                self.resolution_outcome == ResolutionOutcome.SAME_CLAIM
                and self.relation == RelationLabel.EQUIVALENT
            ):
                raise ValueError("positive terminal candidate requires same_claim/equivalent")
        elif self.same_claim is False:
            if not (
                self.resolution_outcome == ResolutionOutcome.NOT_SAME_CLAIM
                and self.relation in _NEGATIVE_RELATIONS
            ):
                raise ValueError(
                    "negative terminal candidate requires a terminal negative relation"
                )
        elif not (
            self.resolution_outcome == ResolutionOutcome.AMBIGUOUS
            and self.relation == RelationLabel.AMBIGUOUS
            and self.quality_tier in {QualityTier.GOLD_HUMAN, QualityTier.BENCHMARK}
        ):
            raise ValueError("null same_claim is allowed only for terminal trusted ambiguity")

        if (
            self.quality_tier == QualityTier.GOLD_CONSERVATIVE_TRANSFORM
            and self.same_claim is not True
        ):
            raise ValueError("conservative transformation candidates must be positive")
        if self.quality_tier == QualityTier.GOLD_COUNTEREXAMPLE and self.same_claim is not False:
            raise ValueError("counterexample candidates must be negative")
        if (
            self.resolution_method == "separator_certificate"
            and self.commitment != CandidateCommitment.PARTIAL_NEGATIVE
        ):
            raise ValueError(
                "a separator certificate alone is partial and cannot invent a terminal relation"
            )
        if self.resolution_method == "directional_proof_plus_separator" and (
            self.commitment != CandidateCommitment.TERMINAL
            or self.relation not in {RelationLabel.A_STRONGER, RelationLabel.B_STRONGER}
        ):
            raise ValueError(
                "directional proof plus separator requires a terminal directional relation"
            )
        if self.same_claim is True and any(code != "E29" for code in self.error_types):
            raise ValueError("same-claim candidates admit only cosmetic E29")
        if self.F0_representation_equivalent is not None and self.source not in {
            ResolutionSource.HUMAN_ADJUDICATION,
            ResolutionSource.FROZEN_BENCHMARK_POLICY,
        }:
            raise ValueError(
                "non-human/non-benchmark candidates cannot self-assert F0; "
                "mechanical F0 requires admitted representation evidence"
            )

        expected_f2: bool | None
        if self.truth_A_implies_B is True and self.truth_B_implies_A is True:
            expected_f2 = True
        elif self.truth_A_implies_B is False or self.truth_B_implies_A is False:
            expected_f2 = False
        else:
            expected_f2 = None
        if self.F2_truth_equivalent != expected_f2:
            raise ValueError("F2_truth_equivalent is inconsistent with directional truth fields")
        if self.same_claim is True and expected_f2 is False:
            raise ValueError("same-claim candidate conflicts with accepted F2 refutation")

        if self.candidate_id != make_resolution_candidate_id(self):
            raise ValueError("candidate_id does not match canonical candidate content")
        return self


def make_authority_artifact_binding_id(
    *,
    artifact_kind: AuthorityArtifactKind,
    artifact_id: str,
    artifact_sha256: str,
) -> str:
    return make_id(
        _AUTHORITY_PREFIX,
        {
            "schema_version": 1,
            "artifact_kind": artifact_kind.value,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256,
        },
    )


def make_authority_artifact_binding(
    *,
    artifact_kind: AuthorityArtifactKind,
    artifact_id: str,
    artifact_sha256: str,
) -> AuthorityArtifactBinding:
    return AuthorityArtifactBinding(
        authority_binding_id=make_authority_artifact_binding_id(
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
        ),
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
    )


def _candidate_payload(candidate: ResolutionCandidate) -> dict[str, Any]:
    return candidate.model_dump(mode="json", exclude={"candidate_id"})


def make_resolution_candidate_id(candidate: ResolutionCandidate) -> str:
    """Recompute a candidate ID from every bound semantic and authority field."""

    return make_id(_CANDIDATE_PREFIX, _candidate_payload(candidate))


def make_resolution_candidate(
    *,
    policy: ActiveLabelResolutionPolicy,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    source: ResolutionSource,
    quality_tier: QualityTier,
    resolution_method: str,
    authority_artifacts: tuple[AuthorityArtifactBinding, ...],
    accepted_evidence_ids: tuple[str, ...],
    commitment: CandidateCommitment,
    same_claim: bool | None,
    resolution_outcome: ResolutionOutcome,
    relation: RelationLabel | None,
    provenance: tuple[str, ...],
    F0_representation_equivalent: bool | None = None,
    truth_A_implies_B: bool | None = None,
    truth_B_implies_A: bool | None = None,
    F2_truth_equivalent: bool | None = None,
    error_types: tuple[str, ...] = (),
) -> ResolutionCandidate:
    """Build a canonical candidate after checking the method against the loaded policy."""

    policy_methods = {
        item.method: item.tier
        for item in policy.registered_methods
        if item.tier not in {QualityTier.PROVISIONAL, QualityTier.UNKNOWN}
    }
    if policy_methods.get(resolution_method) != quality_tier:
        raise ValueError("resolution method/tier is not registered by the active policy")
    artifact_keys = tuple(
        (item.artifact_kind.value, item.artifact_id, item.artifact_sha256)
        for item in authority_artifacts
    )
    if len(artifact_keys) != len(set(artifact_keys)):
        raise ValueError("authority_artifacts must not contain duplicates")
    if len(accepted_evidence_ids) != len(set(accepted_evidence_ids)):
        raise ValueError("accepted_evidence_ids must not contain duplicates")
    if len(error_types) != len(set(error_types)):
        raise ValueError("error_types must not contain duplicates")
    if len(provenance) != len(set(provenance)):
        raise ValueError("provenance must not contain duplicates")
    canonical_artifacts = tuple(
        sorted(
            authority_artifacts,
            key=lambda item: (item.artifact_kind.value, item.artifact_id, item.artifact_sha256),
        )
    )
    content: dict[str, Any] = {
        "schema_version": 1,
        "target_kind": target_kind,
        "target_id": target_id,
        "policy_version": policy.policy_version,
        "policy_file_sha256": policy.policy_file_sha256,
        "source": source,
        "source_rank": _SOURCE_RANK[source],
        "quality_tier": quality_tier,
        "resolution_method": resolution_method,
        "authority_artifacts": canonical_artifacts,
        "accepted_evidence_ids": tuple(sorted(accepted_evidence_ids)),
        "commitment": commitment,
        "same_claim": same_claim,
        "resolution_outcome": resolution_outcome,
        "relation": relation,
        "F0_representation_equivalent": F0_representation_equivalent,
        "truth_A_implies_B": truth_A_implies_B,
        "truth_B_implies_A": truth_B_implies_A,
        "F2_truth_equivalent": F2_truth_equivalent,
        "error_types": tuple(sorted(error_types)),
        "provenance": tuple(sorted(provenance)),
    }
    candidate_id = make_id(
        _CANDIDATE_PREFIX,
        ResolutionCandidate.model_construct(candidate_id="", **content).model_dump(
            mode="json", exclude={"candidate_id"}
        ),
    )
    return ResolutionCandidate(candidate_id=candidate_id, **content)


def _json_no_duplicates(path: Path) -> dict[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LabelResolutionPolicyError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)
    except (OSError, json.JSONDecodeError) as exc:
        raise LabelResolutionPolicyError(f"cannot load Gate-0 report {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LabelResolutionPolicyError("Gate-0 report root must be a JSON object")
    return value


def _mapping(value: object, *, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise LabelResolutionPolicyError(f"{location} must be a string-keyed mapping")
    return value


def _require_exact_keys(
    mapping: dict[str, Any],
    *,
    expected: frozenset[str],
    location: str,
) -> None:
    actual = frozenset(mapping)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise LabelResolutionPolicyError(
        f"{location} keys differ from canonical Gate-0 v2 schema; missing={missing}, extra={extra}"
    )


def _sequence(value: object, *, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise LabelResolutionPolicyError(f"{location} must be a list")
    return value


def load_active_label_resolution_policy(repo_root: Path) -> ActiveLabelResolutionPolicy:
    """Load policy v1 only when its exact bytes are activated by passing Gate 0."""

    root = repo_root.resolve()
    policy_path = root / _POLICY_RELATIVE_PATH
    gate_path = root / _GATE_RELATIVE_PATH
    try:
        raw = load_yaml_mapping(policy_path)
    except ConfigError as exc:
        raise LabelResolutionPolicyError(str(exc)) from exc
    policy_hash = hash_file(policy_path)
    gate_hash = hash_file(gate_path)
    gate = _json_no_duplicates(gate_path)

    _require_exact_keys(
        gate,
        expected=_EXPECTED_GATE0_TOP_LEVEL_KEYS,
        location="gate_0",
    )
    if gate.get("schema_version") != 2:
        raise LabelResolutionPolicyError("Gate 0 requires canonical schema_version=2")
    if gate.get("gate") != "gate_0" or gate.get("decision") != "pass_internal_research_only":
        raise LabelResolutionPolicyError("Gate 0 has not passed for internal research")
    checks = _mapping(gate.get("checks"), location="gate_0.checks")
    _require_exact_keys(
        checks,
        expected=_EXPECTED_GATE0_CHECK_KEYS,
        location="gate_0.checks",
    )
    if not all(value is True for value in checks.values()):
        raise LabelResolutionPolicyError("Gate 0 contains a missing or failing check")
    inputs = _mapping(gate.get("inputs"), location="gate_0.inputs")
    _require_exact_keys(
        inputs,
        expected=_EXPECTED_GATE0_INPUT_KEYS,
        location="gate_0.inputs",
    )
    invalid_input_hashes = sorted(
        key
        for key, value in inputs.items()
        if not isinstance(value, str) or re.fullmatch(HEX64_PATTERN, value) is None
    )
    if invalid_input_hashes:
        raise LabelResolutionPolicyError(
            "Gate 0 input bindings must be lowercase SHA-256 values; invalid="
            + repr(invalid_input_hashes)
        )
    if inputs.get("label_resolution_policy") != policy_hash:
        raise LabelResolutionPolicyError(
            "Gate 0 does not bind the exact label-resolution policy file SHA-256"
        )
    if raw.get("policy_version") != _POLICY_VERSION:
        raise LabelResolutionPolicyError("unexpected label-resolution policy version")

    precedence: list[PolicyPrecedenceEntry] = []
    for index, item in enumerate(_sequence(raw.get("precedence"), location="precedence")):
        item_map = _mapping(item, location=f"precedence[{index}]")
        try:
            precedence.append(
                PolicyPrecedenceEntry.model_validate(
                    {
                        "rank": item_map.get("rank"),
                        "source": item_map.get("source"),
                        "quality_tiers": item_map.get("quality_tiers"),
                        "strength": item_map.get("strength"),
                    }
                )
            )
        except ValidationError as exc:
            raise LabelResolutionPolicyError(f"invalid precedence[{index}]: {exc}") from exc

    resolution_methods = _mapping(raw.get("resolution_methods"), location="resolution_methods")
    registry = _sequence(resolution_methods.get("registry"), location="resolution_methods.registry")
    methods: list[RegisteredResolutionMethod] = []
    patterns: list[dict[str, Any]] = []
    for index, item in enumerate(registry):
        item_map = _mapping(item, location=f"resolution_methods.registry[{index}]")
        if "method" in item_map:
            try:
                methods.append(
                    RegisteredResolutionMethod.model_validate(
                        {
                            "method": item_map.get("method"),
                            "route": item_map.get("route"),
                            "tier": item_map.get("tier"),
                        }
                    )
                )
            except ValidationError as exc:
                raise LabelResolutionPolicyError(f"invalid method registry entry: {exc}") from exc
        elif "method_pattern" in item_map:
            patterns.append(item_map)
        else:
            raise LabelResolutionPolicyError("method registry entry lacks method/method_pattern")
    if len(patterns) != 1 or patterns[0].get("method_pattern") != "{family_id}_certificate":
        raise LabelResolutionPolicyError("canonical reserved certificate method pattern is missing")
    if resolution_methods.get("unresolved_route_method", object()) is not None:
        raise LabelResolutionPolicyError("unresolved route must have resolution_method=null")

    try:
        return ActiveLabelResolutionPolicy(
            policy_file_sha256=policy_hash,
            authored_status=str(raw.get("status", "")),
            gate_file_sha256=gate_hash,
            gate_decision="pass_internal_research_only",
            precedence=tuple(precedence),
            registered_methods=tuple(methods),
        )
    except ValidationError as exc:
        raise LabelResolutionPolicyError(f"invalid active label-resolution policy: {exc}") from exc


__all__ = [
    "ActiveLabelResolutionPolicy",
    "AuthorityArtifactBinding",
    "AuthorityArtifactKind",
    "CandidateCommitment",
    "LabelResolutionPolicyError",
    "RegisteredResolutionMethod",
    "ResolutionCandidate",
    "ResolutionSource",
    "load_active_label_resolution_policy",
    "make_authority_artifact_binding",
    "make_authority_artifact_binding_id",
    "make_resolution_candidate",
    "make_resolution_candidate_id",
]

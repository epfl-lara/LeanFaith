"""Fail-closed, non-training audit for scientific training-data readiness.

This module deliberately separates three statements that are easy to conflate:

* a frozen real-output frame is large/diverse enough to annotate;
* enough of that frame has genuine human terminal labels to estimate prevalence;
* a complete, ancestry-disjoint corpus exists for the preregistered model pilot.

Compilation, proof search, type correctness, and LLM agreement are never accepted
as F1 labels.  The audit reads metadata only and never trains or executes a model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.schemas.annotation import AnnotationRecord
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
    QualityTier,
    SemanticLabelTargetKind,
    TransformationFamilyStatus,
)
from leanfaith.schemas.evidence import (
    AuditValue,
    CounterexampleValue,
    EvidenceRecord,
    JudgmentValue,
)
from leanfaith.schemas.ids import id_pattern
from leanfaith.schemas.label import ResolvedLabel, check_label_target_link
from leanfaith.schemas.pair import PairRecord, check_pair_groups
from leanfaith.schemas.source import SourceManifest
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.schemas.variant import FamilyPromotionDecision

_NEGATIVE_SOURCES = ("G_rule", "G_sci", "G_open", "G_real")
ArmName = Literal["D0", "D1", "D2", "D3", "D4", "D5"]
_ARMS: tuple[ArmName, ...] = ("D0", "D1", "D2", "D3", "D4", "D5")
_HUMAN_PRODUCTS = (
    "training_gold",
    "selection_gold",
    "calibration_gold",
    "final_human_test",
)
TrainingRelation = Literal[
    "equivalent",
    "A_stronger",
    "B_stronger",
    "incomparable",
    "unrelated",
    "ambiguous",
]
LF022ArtifactName = Literal["variants", "pairs", "evidence", "resolved_labels", "promotions"]

_RELATIONS: tuple[TrainingRelation, ...] = (
    "equivalent",
    "A_stronger",
    "B_stronger",
    "incomparable",
    "unrelated",
    "ambiguous",
)
_CONFIRMATORY_RELATIONS: tuple[TrainingRelation, ...] = tuple(
    relation for relation in _RELATIONS if relation != "ambiguous"
)
_HEX64 = r"^[0-9a-f]{64}$"


def _relative_repo_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError("artifact paths must be nonempty repository-relative paths")
    return value


class TrainingLineageInputs(StrictModel):
    theorem_records: str
    pair_records: str
    resolved_labels: str
    evidence_records: str
    promotion_decisions: str
    annotation_records: str
    adjudication_records: str
    human_authentication_key: str
    allow_test_fixture_human_provenance: bool = False
    split_assignments: str
    source_manifests: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "theorem_records",
        "pair_records",
        "resolved_labels",
        "evidence_records",
        "promotion_decisions",
        "annotation_records",
        "adjudication_records",
        "human_authentication_key",
        "split_assignments",
    )
    @classmethod
    def _lineage_path_is_relative(cls, value: str) -> str:
        return _relative_repo_path(value)

    @field_validator("source_manifests")
    @classmethod
    def _source_paths_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(_relative_repo_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("source_manifests paths must be sorted and unique")
        return paths


class ReadinessInputs(StrictModel):
    prevalence_frame: str
    prevalence_human_labels: str
    training_inventory: str
    training_ambiguity_inventory: str
    generator_holdout_manifest: str
    human_products: dict[str, str]
    lf022_required_artifacts: dict[Literal["G_sci", "G_open"], str]
    lineage: TrainingLineageInputs

    @field_validator(
        "prevalence_frame",
        "prevalence_human_labels",
        "training_inventory",
        "training_ambiguity_inventory",
        "generator_holdout_manifest",
    )
    @classmethod
    def _paths_are_relative(cls, value: str) -> str:
        return _relative_repo_path(value)

    @field_validator("human_products")
    @classmethod
    def _human_products_are_complete(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(_HUMAN_PRODUCTS):
            raise ValueError(f"human_products must be exactly {list(_HUMAN_PRODUCTS)}")
        return {name: _relative_repo_path(path) for name, path in value.items()}

    @field_validator("lf022_required_artifacts")
    @classmethod
    def _lf022_paths_are_relative(
        cls,
        value: dict[Literal["G_sci", "G_open"], str],
    ) -> dict[Literal["G_sci", "G_open"], str]:
        if set(value) != {"G_sci", "G_open"}:
            raise ValueError("lf022_required_artifacts must contain exactly G_sci and G_open")
        return {source: _relative_repo_path(path) for source, path in value.items()}


class PrevalenceRequirements(StrictModel):
    minimum_frame_items: int = Field(ge=1)
    maximum_frame_items: int = Field(ge=1)
    minimum_generator_families: int = Field(ge=1)
    minimum_human_terminal_label_fraction: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> PrevalenceRequirements:
        if self.maximum_frame_items < self.minimum_frame_items:
            raise ValueError("maximum_frame_items must be >= minimum_frame_items")
        return self


class PilotRequirements(StrictModel):
    confirmatory_pair_count: int = Field(ge=2)
    reduced_data_minimum_pair_count: int = Field(ge=2)
    positive_fraction: float = Field(gt=0.0, lt=1.0)
    negative_fraction: float = Field(gt=0.0, lt=1.0)
    maximum_unique_variants_per_component_per_arm: int = Field(ge=1)

    @model_validator(mode="after")
    def _balanced_binary_contract(self) -> PilotRequirements:
        if self.confirmatory_pair_count % 2:
            raise ValueError("confirmatory_pair_count must be even")
        if self.reduced_data_minimum_pair_count % 2:
            raise ValueError("reduced_data_minimum_pair_count must be even")
        if not math.isclose(self.positive_fraction, 0.5):
            raise ValueError("pilot positive_fraction must be 0.5")
        if not math.isclose(self.negative_fraction, 0.5):
            raise ValueError("pilot negative_fraction must be 0.5")
        return self


class SelectionGoldRequirements(StrictModel):
    faithful: int = Field(ge=1)
    unfaithful: int = Field(ge=1)
    per_included_relation_class: int = Field(ge=1)


class D5Requirements(StrictModel):
    training_gold_required: Literal[True]
    human_gold_loss_weight: float = Field(strict=True)
    ancestry_oversampling: Literal[False]

    @model_validator(mode="after")
    def _weight_is_plan_value(self) -> D5Requirements:
        if not math.isclose(self.human_gold_loss_weight, 2.0):
            raise ValueError("D5 human_gold_loss_weight must equal the preregistered value 2")
        return self


class StatisticalAdequacyRequirements(StrictModel):
    calibration_gold_minimum_groups: int = Field(ge=1)
    final_human_test_minimum_groups: int = Field(ge=1)
    calibration_required_claim: Literal["H4_Gate10"]
    final_required_claim: Literal["main_task_aggregate_95_precision"]
    calibration_design_method: str = Field(min_length=1)
    final_design_method: str = Field(min_length=1)


class FamilyControls(StrictModel):
    apply_caps_to_arms: tuple[str, ...]
    deterministic_family_fraction_of_all_negative: float = Field(gt=0.0, le=1.0)
    minimum_llm_proposer_families: int = Field(ge=1)
    maximum_one_llm_family_fraction_of_llm_negative: float = Field(gt=0.0, le=1.0)
    minimum_real_generator_families: int = Field(ge=1)
    maximum_one_real_family_fraction_of_real_negative: float = Field(gt=0.0, le=1.0)

    @field_validator("apply_caps_to_arms")
    @classmethod
    def _arms_are_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not set(value).issubset(_ARMS):
            raise ValueError("family-cap arm names must be unique members of D0..D5")
        return value


class LabelPolicy(StrictModel):
    allowed_f1_label_bases: tuple[str, ...] = Field(min_length=1)
    forbidden_label_bases: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sets_are_disjoint(self) -> LabelPolicy:
        if len(set(self.allowed_f1_label_bases)) != len(self.allowed_f1_label_bases):
            raise ValueError("allowed_f1_label_bases must be unique")
        if len(set(self.forbidden_label_bases)) != len(self.forbidden_label_bases):
            raise ValueError("forbidden_label_bases must be unique")
        if set(self.allowed_f1_label_bases) & set(self.forbidden_label_bases):
            raise ValueError("allowed and forbidden label bases must be disjoint")
        return self


class ReportPaths(StrictModel):
    json_path: str
    markdown_path: str

    @field_validator("json_path", "markdown_path")
    @classmethod
    def _paths_are_relative(cls, value: str) -> str:
        return _relative_repo_path(value)


class TrainingDataReadinessPolicy(StrictModel):
    policy_id: Literal["training_data_readiness_v1"]
    schema_version: Literal[1]
    artifact_class: Literal["production", "test_fixture"] = "production"
    human_gold_admission_enabled: Literal[False] = False
    inputs: ReadinessInputs
    prevalence: PrevalenceRequirements
    pilot: PilotRequirements
    selection_gold_minimum_groups: SelectionGoldRequirements
    d5: D5Requirements
    statistical_adequacy: StatisticalAdequacyRequirements
    negative_arms: dict[str, dict[str, float]]
    full_arm_positive_mix: dict[str, float]
    family_controls: FamilyControls
    label_policy: LabelPolicy
    reports: ReportPaths

    @model_validator(mode="after")
    def _mixtures_are_complete(self) -> TrainingDataReadinessPolicy:
        if set(self.negative_arms) != set(_ARMS):
            raise ValueError("negative_arms must be exactly D0..D5")
        for arm, mixture in self.negative_arms.items():
            if not mixture or not set(mixture).issubset(_NEGATIVE_SOURCES):
                raise ValueError(f"{arm} contains an unknown or empty negative-source mixture")
            if any(value <= 0.0 for value in mixture.values()) or not math.isclose(
                sum(mixture.values()), 1.0
            ):
                raise ValueError(f"{arm} negative-source fractions must be positive and sum to 1")
        expected_positive = {
            "certified_positive",
            "human_or_promoted_faithful_real",
            "promoted_llm_equivalent",
        }
        if set(self.full_arm_positive_mix) != expected_positive:
            raise ValueError("full_arm_positive_mix has unexpected keys")
        if any(value <= 0.0 for value in self.full_arm_positive_mix.values()):
            raise ValueError("positive-source fractions must be positive")
        if not math.isclose(sum(self.full_arm_positive_mix.values()), 1.0):
            raise ValueError("positive-source fractions must sum to 1")
        return self


class HashedJSONLArtifact(StrictModel):
    path: str
    sha256: str = Field(pattern=_HEX64)
    record_count: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        return _relative_repo_path(value)


class LF022ReadinessManifest(StrictModel):
    """Production LF-022 promotion artifact admitted to training readiness."""

    schema_version: Literal[1]
    negative_source: Literal["G_sci", "G_open"]
    artifact_class: Literal["production"]
    promotion_status: Literal["silver", "gold_promoted"]
    variant_count: int = Field(ge=1)
    pair_count: int = Field(ge=1)
    evidence_count: int = Field(ge=1)
    resolved_label_count: int = Field(ge=1)
    promoted_record_count: int = Field(ge=1)
    promoted_pair_ids: tuple[str, ...] = Field(min_length=1)
    proposer_family_counts: dict[str, int]
    artifacts: dict[LF022ArtifactName, HashedJSONLArtifact]

    @model_validator(mode="after")
    def _manifest_is_self_consistent(self) -> LF022ReadinessManifest:
        required = {"variants", "pairs", "evidence", "resolved_labels", "promotions"}
        if set(self.artifacts) != required:
            raise ValueError(f"LF022 artifacts must be exactly {sorted(required)}")
        expected_counts: dict[LF022ArtifactName, int] = {
            "variants": self.variant_count,
            "pairs": self.pair_count,
            "evidence": self.evidence_count,
            "resolved_labels": self.resolved_label_count,
            "promotions": self.promoted_record_count,
        }
        for name, expected in expected_counts.items():
            if self.artifacts[name].record_count != expected:
                raise ValueError(f"{name} artifact count disagrees with manifest count")
        if len(set(self.promoted_pair_ids)) != len(self.promoted_pair_ids):
            raise ValueError("promoted_pair_ids must be unique")
        if len(self.promoted_pair_ids) != self.promoted_record_count:
            raise ValueError("promoted_pair_ids count must equal promoted_record_count")
        if self.promoted_record_count > min(
            self.variant_count,
            self.pair_count,
            self.resolved_label_count,
        ):
            raise ValueError("promoted_record_count exceeds a required upstream count")
        if not self.proposer_family_counts or any(
            not family or count <= 0 for family, count in self.proposer_family_counts.items()
        ):
            raise ValueError("proposer_family_counts must contain positive counts")
        if sum(self.proposer_family_counts.values()) != self.variant_count:
            raise ValueError("proposer family counts must sum to variant_count")
        return self


class GeneratorHoldoutManifest(StrictModel):
    """Frozen generator-family partition for confirmatory real-output training."""

    schema_version: Literal[1]
    manifest_id: str = Field(pattern=r"^generator-holdout:[0-9a-f]{64}$")
    artifact_class: Literal["production"]
    successful_generator_families: tuple[str, ...] = Field(min_length=4)
    supervision_generator_families: tuple[str, ...] = Field(min_length=3)
    heldout_generator_family: str = Field(min_length=1)
    source_artifact: str = Field(min_length=1)
    source_artifact_sha256: str = Field(pattern=_HEX64)

    @field_validator(
        "successful_generator_families",
        "supervision_generator_families",
    )
    @classmethod
    def _families_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(not family for family in value):
            raise ValueError("generator families must be nonempty, sorted, and unique")
        return value

    @field_validator("source_artifact")
    @classmethod
    def _source_artifact_is_relative(cls, value: str) -> str:
        return _relative_repo_path(value)

    @model_validator(mode="after")
    def _holdout_is_disjoint_and_content_addressed(self) -> GeneratorHoldoutManifest:
        successful = set(self.successful_generator_families)
        supervision = set(self.supervision_generator_families)
        if not supervision <= successful:
            raise ValueError("supervision families must be successful generator families")
        if self.heldout_generator_family not in successful:
            raise ValueError("held-out family must be a successful generator family")
        if self.heldout_generator_family in supervision:
            raise ValueError("held-out family cannot supply training supervision")
        payload = self.model_dump(mode="json", exclude={"manifest_id"})
        expected = "generator-holdout:" + hash_canonical(payload)
        if self.manifest_id != expected:
            raise ValueError("generator holdout manifest ID differs from content")
        return self


class _LF022Projection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _LF022VariantProjection(_LF022Projection):
    variant_id: str = Field(pattern=id_pattern("var"))
    validation_status: Literal["elaborates", "elaborates_with_placeholder"]


class _LF022PairProjection(_LF022Projection):
    pair_id: str = Field(pattern=id_pattern("pair"))
    negative_source: Literal["G_sci", "G_open"]
    proposer_family: str = Field(min_length=1)


class _LF022EvidenceProjection(_LF022Projection):
    evidence_id: str = Field(pattern=id_pattern("ev"))
    target_id: str = Field(pattern=id_pattern("pair"))
    status: Literal[
        "success",
        "timeout",
        "error",
        "unsupported",
        "abstain",
        "not_run",
    ]


class _LF022LabelProjection(_LF022Projection):
    label_id: str = Field(pattern=id_pattern("lbl"))
    target_id: str = Field(pattern=id_pattern("pair"))
    train_eligibility: Literal[True]
    quality_tier: Literal["silver_consensus", "gold_human", "gold_counterexample"]
    resolution_outcome: Literal["same_claim", "not_same_claim"]


class _LF022PromotionProjection(_LF022Projection):
    pair_id: str = Field(pattern=id_pattern("pair"))
    promoted: Literal[True]
    promotion_status: Literal["silver", "gold_promoted"]


_LF022_ARTIFACT_PROJECTIONS: dict[LF022ArtifactName, type[_LF022Projection]] = {
    "variants": _LF022VariantProjection,
    "pairs": _LF022PairProjection,
    "evidence": _LF022EvidenceProjection,
    "resolved_labels": _LF022LabelProjection,
    "promotions": _LF022PromotionProjection,
}


class StatisticalAdequacyAssessment(StrictModel):
    schema_version: Literal[1]
    assessment_id: str = Field(pattern=r"^statistical_design_v1:[0-9a-f]{64}$")
    assessment_kind: Literal["preregistered_design_adequacy"]
    partition: Literal["calibration_gold", "final_human_test"]
    status: Literal["design_adequate"]
    component_count: int = Field(ge=1)
    record_count: int = Field(ge=1)
    supported_claims: tuple[str, ...] = Field(min_length=1)
    method: str = Field(min_length=1)
    partition_design_hash: str = Field(pattern=_HEX64)
    interval_method: Literal["wilson", "clopper_pearson"]
    confidence_level: float = Field(strict=True)
    target_accepted_precision: float = Field(strict=True)
    minimum_required_component_count: int = Field(ge=1)

    @field_validator("supported_claims")
    @classmethod
    def _claims_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("supported_claims must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _content_addressed(self) -> StatisticalAdequacyAssessment:
        payload = self.model_dump(mode="json", exclude={"assessment_id"})
        expected = "statistical_design_v1:" + hash_canonical(payload)
        if self.assessment_id != expected:
            raise ValueError("statistical assessment ID differs from content")
        if not math.isclose(self.confidence_level, 0.95):
            raise ValueError("confidence_level must equal the preregistered value 0.95")
        if not math.isclose(self.target_accepted_precision, 0.95):
            raise ValueError("target_accepted_precision must equal the preregistered value 0.95")
        if self.component_count < self.minimum_required_component_count:
            raise ValueError("statistical design has fewer groups than its frozen minimum")
        return self


class SplitAssignmentRecord(StrictModel):
    """One frozen target-to-union-find-component assignment."""

    schema_version: Literal[1]
    target_kind: Literal["lean_pair"]
    target_id: str = Field(pattern=id_pattern("pair"))
    split_component_id: str = Field(pattern=r"^split-component:[0-9a-f]{64}$")


class HumanAdjudicationRecord(StrictModel):
    """Operator-attested raw adjudication; explicitly ineligible for human gold."""

    schema_version: Literal[1]
    adjudication_id: str = Field(pattern=r"^human-adjudication:[0-9a-f]{64}$")
    target_kind: Literal["lean_pair"]
    target_id: str = Field(pattern=id_pattern("pair"))
    annotation_ids: tuple[str, ...] = Field(min_length=2)
    adjudicator_id: str = Field(min_length=1)
    same_claim: bool | None
    relation: TrainingRelation
    resolution_outcome: Literal["same_claim", "not_same_claim", "ambiguous"]
    annotation_content_sha256: str = Field(pattern=_HEX64)
    human_assignment_ids: tuple[str, ...] = Field(min_length=2)
    human_submission_attestation_ids: tuple[str, ...] = Field(min_length=2)
    annotator_principal_hashes: tuple[str, ...] = Field(min_length=2)
    origin_assurance: Literal["operator_attested", "test_fixture"]
    operator_attestation_verified: Literal[True]
    backend_origin_verified: Literal[False]
    human_gold_eligible: Literal[False]
    fixture_only: bool
    backend_id: str = Field(min_length=1)
    adjudicator_principal_hash: str = Field(pattern=_HEX64)
    backend_adjudication_record_id: str = Field(min_length=1)
    authentication_key_id: str = Field(pattern=_HEX64)
    authentication_tag: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _terminal_and_content_addressed(self) -> HumanAdjudicationRecord:
        if self.annotation_ids != tuple(sorted(set(self.annotation_ids))):
            raise ValueError("adjudication annotation IDs must be sorted and unique")
        for field_name in (
            "human_assignment_ids",
            "human_submission_attestation_ids",
            "annotator_principal_hashes",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if not (
            len(self.annotation_ids)
            == len(self.human_assignment_ids)
            == len(self.human_submission_attestation_ids)
            == len(self.annotator_principal_hashes)
        ):
            raise ValueError("human provenance identities must align one-to-one")
        if self.fixture_only != (self.origin_assurance == "test_fixture"):
            raise ValueError("fixture flag differs from origin assurance")
        if self.origin_assurance == "test_fixture" and self.backend_id != (
            "pytest_fixture_backend"
        ):
            raise ValueError("test-fixture provenance requires the fixture backend")
        expected_outcome = (
            "same_claim"
            if self.same_claim is True
            else "not_same_claim"
            if self.same_claim is False
            else "ambiguous"
        )
        if self.resolution_outcome != expected_outcome:
            raise ValueError("adjudication outcome disagrees with same_claim")
        if self.same_claim is True and self.relation != "equivalent":
            raise ValueError("same-claim adjudication requires relation=equivalent")
        if self.same_claim is False and self.relation in {"equivalent", "ambiguous"}:
            raise ValueError("negative adjudication requires a non-equivalent relation")
        if self.same_claim is None and self.relation != "ambiguous":
            raise ValueError("null same_claim is permitted only for terminal ambiguity")
        payload = self.model_dump(
            mode="json",
            exclude={"adjudication_id", "authentication_tag"},
        )
        expected_id = "human-adjudication:" + hash_canonical(payload)
        if self.adjudication_id != expected_id:
            raise ValueError("human adjudication ID differs from content")
        return self


_HUMAN_ADJUDICATION_AUTH_DOMAIN = (
    b"leanfaith-training-readiness-human-adjudication-hmac-sha256-v1\x00"
)


def _human_adjudication_authentication_payload(
    adjudication: HumanAdjudicationRecord,
) -> dict[str, object]:
    return {
        "schema": "training_readiness_human_adjudication_authentication_v1",
        "adjudication_id": adjudication.adjudication_id,
        "content": adjudication.model_dump(
            mode="json",
            exclude={"authentication_tag"},
        ),
    }


def _human_adjudication_authentication_tag(
    adjudication: HumanAdjudicationRecord,
    *,
    key: bytes,
) -> str:
    return hmac.new(
        key,
        _HUMAN_ADJUDICATION_AUTH_DOMAIN
        + canonical_json_bytes(_human_adjudication_authentication_payload(adjudication)),
        hashlib.sha256,
    ).hexdigest()


def build_operator_attested_adjudication_record(
    content: Mapping[str, object],
    *,
    operator_key: bytes,
) -> HumanAdjudicationRecord:
    """Bind an adjudication to the current operator-only trust boundary.

    This authenticates integrity and an operator assertion only. It does not
    establish backend origin, human identity, independence, or gold
    eligibility; production readiness therefore refuses these records.
    """

    if len(operator_key) < 32:
        raise ValueError("operator authentication key must be at least 32 bytes")
    payload = {
        **content,
        "authentication_key_id": hashlib.sha256(operator_key).hexdigest(),
    }
    adjudication_id = "human-adjudication:" + hash_canonical(payload)
    provisional = HumanAdjudicationRecord.model_validate(
        {
            **payload,
            "adjudication_id": adjudication_id,
            "authentication_tag": "0" * 64,
        }
    )
    return HumanAdjudicationRecord.model_validate(
        {
            **provisional.model_dump(
                mode="json",
                exclude={"authentication_tag"},
            ),
            "authentication_tag": _human_adjudication_authentication_tag(
                provisional,
                key=operator_key,
            ),
        }
    )


class TrainingAuditRecord(StrictModel):
    """One pair in a readiness-only, non-release inventory."""

    schema_version: Literal[1]
    pair_id: str = Field(min_length=1)
    split_component_id: str = Field(min_length=1)
    same_claim: bool
    relation: Literal[
        "equivalent",
        "A_stronger",
        "B_stronger",
        "incomparable",
        "unrelated",
    ]
    arm_memberships: tuple[Literal["D0", "D1", "D2", "D3", "D4", "D5"], ...] = Field(min_length=1)
    label_bases: tuple[str, ...] = Field(min_length=1)
    resolved_label_id: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    promotion_decision_ids: tuple[str, ...] = ()
    training_gold_record_id: str | None = None
    arm_loss_weights: dict[Literal["D0", "D1", "D2", "D3", "D4", "D5"], float] = Field(
        default_factory=dict
    )
    normalized_arm_loss_weights: dict[Literal["D0", "D1", "D2", "D3", "D4", "D5"], float] = Field(
        default_factory=dict
    )
    ancestry_oversampled_arms: tuple[Literal["D0", "D1", "D2", "D3", "D4", "D5"], ...] = ()
    positive_source: (
        Literal[
            "certified_positive",
            "human_or_promoted_faithful_real",
            "promoted_llm_equivalent",
        ]
        | None
    ) = None
    negative_source: Literal["G_rule", "G_sci", "G_open", "G_real"] | None = None
    source_family: str | None = None
    transform_family: str | None = None
    duplicate_of: str | None = None

    @field_validator("arm_memberships")
    @classmethod
    def _arm_memberships_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("arm_memberships must be sorted and unique")
        return value

    @field_validator("label_bases")
    @classmethod
    def _label_bases_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("label_bases must be sorted and unique")
        return value

    @field_validator("evidence_ids", "promotion_decision_ids", "ancestry_oversampled_arms")
    @classmethod
    def _id_lists_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("lineage ID/arm tuples must be sorted and unique")
        return value

    @field_validator("arm_loss_weights", "normalized_arm_loss_weights")
    @classmethod
    def _weights_are_positive(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if any(weight <= 0.0 for weight in value.values()):
            raise ValueError("arm loss weights must be positive")
        return value

    @model_validator(mode="after")
    def _class_fields_are_coherent(self) -> TrainingAuditRecord:
        if self.same_claim:
            if self.relation != "equivalent":
                raise ValueError("same-claim records must use relation=equivalent")
            if self.positive_source is None or self.negative_source is not None:
                raise ValueError("same-claim records require only positive_source")
            if self.transform_family is not None:
                raise ValueError("positive records cannot declare transform_family")
        else:
            if self.relation == "equivalent":
                raise ValueError("negative records cannot use relation=equivalent")
            if self.negative_source is None or self.positive_source is not None:
                raise ValueError("negative records require only negative_source")
            if self.negative_source == "G_rule":
                if not self.transform_family or self.source_family is not None:
                    raise ValueError("G_rule requires transform_family and no source_family")
            elif not self.source_family or self.transform_family is not None:
                raise ValueError("non-rule negatives require source_family and no transform_family")
        if self.duplicate_of == self.pair_id:
            raise ValueError("duplicate_of cannot refer to the record itself")
        if not set(self.arm_loss_weights).issubset(self.arm_memberships):
            raise ValueError("arm_loss_weights may mention only arm memberships")
        if set(self.normalized_arm_loss_weights) != set(self.arm_memberships):
            raise ValueError("normalized_arm_loss_weights must cover exactly every arm membership")
        if not set(self.ancestry_oversampled_arms).issubset(self.arm_memberships):
            raise ValueError("ancestry_oversampled_arms may mention only arm memberships")
        return self


class AmbiguityTrainingRecord(StrictModel):
    """D5-only ambiguity-head item; never enters binary same-claim arms."""

    schema_version: Literal[1]
    record_id: str = Field(min_length=1)
    target_id: str = Field(pattern=id_pattern("pair"))
    split_component_id: str = Field(pattern=r"^split-component:[0-9a-f]{64}$")
    resolved_label_id: str = Field(pattern=id_pattern("lbl"))
    training_gold_record_id: str = Field(min_length=1)
    arm: Literal["D5"]
    raw_human_gold_loss_weight: float = Field(strict=True)
    normalized_component_weight: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _raw_weight_is_frozen(self) -> AmbiguityTrainingRecord:
        if not math.isclose(self.raw_human_gold_loss_weight, 2.0):
            raise ValueError("ambiguity-head human-gold raw loss weight must equal 2")
        return self


class PrevalenceHumanLabelRecord(StrictModel):
    """Adjudicated human label bound to one immutable prevalence-frame row."""

    schema_version: Literal[1]
    frame_record_id: str = Field(min_length=1)
    target_pair_id: str = Field(pattern=id_pattern("pair"))
    resolved_label_id: str = Field(pattern=id_pattern("lbl"))
    adjudication_id: str = Field(pattern=r"^human-adjudication:[0-9a-f]{64}$")
    adjudicated: Literal[True]
    label_basis: Literal["human_adjudication"]
    same_claim: bool | None
    relation: TrainingRelation
    resolution_outcome: Literal[
        "same_claim",
        "not_same_claim",
        "ambiguous",
    ]

    @model_validator(mode="after")
    def _outcome_is_coherent(self) -> PrevalenceHumanLabelRecord:
        expected = (
            "same_claim"
            if self.same_claim is True
            else "not_same_claim"
            if self.same_claim is False
            else "ambiguous"
        )
        if self.resolution_outcome != expected:
            raise ValueError("prevalence label outcome disagrees with same_claim")
        if self.same_claim is True and self.relation != "equivalent":
            raise ValueError("same-claim prevalence label requires equivalent")
        if self.same_claim is False and self.relation in {"equivalent", "ambiguous"}:
            raise ValueError("negative prevalence label requires terminal non-equivalent relation")
        if self.same_claim is None and self.relation != "ambiguous":
            raise ValueError("ambiguous prevalence label requires relation=ambiguous")
        return self


class GoldGroupRecord(StrictModel):
    record_id: str = Field(min_length=1)
    target_kind: Literal["lean_pair"]
    target_id: str = Field(pattern=id_pattern("pair"))
    resolved_label_id: str | None = Field(default=None, pattern=id_pattern("lbl"))
    adjudication_id: str | None = Field(
        default=None,
        pattern=r"^human-adjudication:[0-9a-f]{64}$",
    )
    sealed_label_vault_receipt_id: str | None = Field(
        default=None,
        pattern=r"^sealed-label-vault-receipt:[0-9a-f]{64}$",
    )
    split_component_id: str = Field(min_length=1)
    adjudicated: bool
    sampling_stratum: str = Field(min_length=1)
    inclusion_probability: float = Field(gt=0.0, le=1.0)
    design_weight: float = Field(gt=0.0)
    simple_random_real_output_subpanel: bool = False
    ambiguity_head_eligible: bool = False
    labels_hidden: bool = False
    same_claim: bool | None = None
    relation: (
        Literal[
            "equivalent",
            "A_stronger",
            "B_stronger",
            "incomparable",
            "unrelated",
            "ambiguous",
        ]
        | None
    ) = None
    label_bases: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _visible_label_is_coherent(self) -> GoldGroupRecord:
        if not math.isclose(
            self.design_weight,
            1.0 / self.inclusion_probability,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("design_weight must equal inverse inclusion_probability")
        if self.same_claim is True and self.relation != "equivalent":
            raise ValueError("faithful gold group must use relation=equivalent")
        if self.same_claim is False and self.relation in (None, "equivalent", "ambiguous"):
            raise ValueError("unfaithful gold group requires a non-equivalent terminal relation")
        if self.relation == "ambiguous" and self.same_claim is not None:
            raise ValueError("ambiguous relation requires same_claim=null")
        if self.same_claim is None and self.relation != "ambiguous" and not self.labels_hidden:
            raise ValueError("adjudicated gold records cannot remain unresolved")
        if not self.labels_hidden and self.ambiguity_head_eligible != (
            self.relation == "ambiguous"
        ):
            raise ValueError("ambiguity_head_eligible must identify exactly terminal ambiguity")
        if self.labels_hidden and (
            self.same_claim is not None
            or self.relation is not None
            or self.label_bases
            or self.ambiguity_head_eligible
        ):
            raise ValueError("hidden-label gold records cannot expose semantic fields")
        if self.labels_hidden:
            if self.resolved_label_id is not None or self.adjudication_id is not None:
                raise ValueError(
                    "sealed hidden-label records cannot expose label or adjudication links"
                )
            if self.sealed_label_vault_receipt_id is None:
                raise ValueError("sealed hidden-label records require an opaque vault receipt")
        elif (
            self.resolved_label_id is None
            or self.adjudication_id is None
            or self.sealed_label_vault_receipt_id is not None
        ):
            raise ValueError(
                "visible gold records require label/adjudication links and no vault receipt"
            )
        return self


class GoldPartitionManifest(StrictModel):
    schema_version: Literal[1]
    partition: Literal[
        "training_gold",
        "selection_gold",
        "calibration_gold",
        "final_human_test",
    ]
    sealed: bool
    labels_exposed_to_audit: bool
    distribution: str
    target_count: int = Field(ge=1)
    realized_eligible_count: int = Field(ge=1)
    sampling_design: str = Field(min_length=1)
    sampling_propensities_recorded: bool
    statistical_adequacy_status: Literal["adequate", "unsupported"]
    statistical_assessment_artifact: str | None = None
    statistical_assessment_sha256: str | None = Field(default=None, pattern=_HEX64)
    calibration_k_folds: int | None = Field(default=None, ge=2)
    simple_random_real_output_subpanel_count: int | None = Field(default=None, ge=1)
    records: tuple[GoldGroupRecord, ...] = Field(min_length=1)

    @field_validator("statistical_assessment_artifact")
    @classmethod
    def _assessment_path_is_relative(cls, value: str | None) -> str | None:
        return _relative_repo_path(value) if value is not None else None

    @model_validator(mode="after")
    def _partition_contract(self) -> GoldPartitionManifest:
        if len({record.record_id for record in self.records}) != len(self.records):
            raise ValueError("gold partition record IDs must be unique")
        if len({record.split_component_id for record in self.records}) != len(self.records):
            raise ValueError("gold readiness manifest requires one record per split component")
        if any(not record.adjudicated for record in self.records):
            raise ValueError("every gold readiness record must be adjudicated")
        if self.partition == "final_human_test":
            if not self.sealed or self.labels_exposed_to_audit:
                raise ValueError("final_human_test must be sealed with labels hidden")
            if any(not record.labels_hidden for record in self.records):
                raise ValueError("sealed final_human_test records must mark labels_hidden=true")
        elif not self.labels_exposed_to_audit or any(
            record.labels_hidden for record in self.records
        ):
            raise ValueError("non-final gold readiness manifests must expose audit labels")
        if self.target_count != len(self.records):
            raise ValueError("target_count must equal the frozen manifest record count")
        if self.realized_eligible_count != len(self.records):
            raise ValueError("realized_eligible_count must equal the eligible record count")
        if not self.sampling_propensities_recorded:
            raise ValueError("gold products must record sampling propensities")
        if self.partition in {"calibration_gold", "final_human_test"}:
            if self.statistical_adequacy_status != "adequate":
                raise ValueError(
                    f"{self.partition} must have an adequate preregistered statistical assessment"
                )
            if self.statistical_assessment_artifact is None:
                raise ValueError(f"{self.partition} requires statistical_assessment_artifact")
            if self.statistical_assessment_sha256 is None:
                raise ValueError(f"{self.partition} requires statistical_assessment_sha256")
        if self.partition == "calibration_gold":
            if self.distribution != "compiling_real_outputs":
                raise ValueError("calibration_gold must use compiling_real_outputs")
            if self.calibration_k_folds is None:
                raise ValueError("calibration_gold must declare its K-fold calibration plan")
        elif self.calibration_k_folds is not None:
            raise ValueError("calibration_k_folds is permitted only for calibration_gold")
        if self.partition == "final_human_test":
            if self.simple_random_real_output_subpanel_count is None:
                raise ValueError(
                    "final_human_test must include the preregistered simple-random "
                    "compiling-real-output subpanel"
                )
            if self.simple_random_real_output_subpanel_count > len(self.records):
                raise ValueError("simple-random subpanel cannot exceed final-test records")
            observed_srs = sum(record.simple_random_real_output_subpanel for record in self.records)
            if observed_srs != self.simple_random_real_output_subpanel_count:
                raise ValueError("simple-random subpanel count disagrees with record membership")
        elif self.simple_random_real_output_subpanel_count is not None:
            raise ValueError("simple_random_real_output_subpanel_count is final_human_test-only")
        elif any(record.simple_random_real_output_subpanel for record in self.records):
            raise ValueError("simple-random subpanel membership is final_human_test-only")
        return self


class ReadinessBlocker(StrictModel):
    code: str
    message: str
    observed: str
    required: str


class PrevalenceReadiness(StrictModel):
    frame_present: bool
    frame_sha256: str | None
    frame_item_count: int
    generator_family_counts: dict[str, int]
    unresolved_review_count: int
    human_terminal_label_count: int
    human_binary_label_count: int
    human_ambiguous_label_count: int
    frame_adequate_for_annotation: bool
    human_labels_adequate_for_prevalence: bool
    prevalence_estimate_ready: bool


class ArmReadiness(StrictModel):
    arm: str
    selected_pair_count: int
    positive_count: int
    negative_count: int
    negative_source_counts: dict[str, int]
    component_cap_violations: int
    source_mix_ok: bool
    class_balance_ok: bool
    family_controls_ok: bool
    positive_pool_ok: bool
    ready: bool


class HumanProductReadiness(StrictModel):
    product: str
    present: bool
    valid: bool
    artifact_sha256: str | None
    record_count: int
    component_count: int
    faithful_group_count: int | None
    unfaithful_group_count: int | None
    relation_group_counts: dict[str, int]


class TrainingReadiness(StrictModel):
    inventory_present: bool
    inventory_sha256: str | None
    inventory_record_count: int
    effective_nonduplicate_record_count: int
    safe_f1_label_count: int
    unsafe_f1_label_count: int
    forbidden_label_basis_counts: dict[str, int]
    unknown_label_basis_counts: dict[str, int]
    lf022_artifacts_present: bool
    missing_lf022_artifacts: tuple[str, ...]
    invalid_lf022_artifacts: tuple[str, ...]
    lf022_artifact_sha256s: dict[str, str]
    generator_holdout_manifest_present: bool
    generator_holdout_manifest_valid: bool
    generator_holdout_manifest_sha256: str | None
    heldout_generator_family: str | None
    lineage_artifacts_present: bool
    lineage_integrity_valid: bool
    lineage_error_count: int
    lineage_artifact_sha256s: dict[str, str]
    arm_results: tuple[ArmReadiness, ...]
    positive_pool_identical_across_arms: bool
    human_products: tuple[HumanProductReadiness, ...]
    human_partitions_component_disjoint: bool
    training_vs_nontraining_gold_disjoint: bool
    confirmatory_ready: bool
    reduced_data_ablation_ready: bool


class TrainingDataReadinessReport(StrictModel):
    schema_version: Literal[1]
    audit_id: str
    policy_id: Literal["training_data_readiness_v1"]
    policy_sha256: str
    artifact_class: Literal["production", "test_fixture"]
    mode: Literal["confirmatory", "reduced_data_ablation"]
    status: Literal[
        "READY",
        "READY_REDUCED_DATA_ABLATION",
        "READY_TEST_FIXTURE",
        "READY_REDUCED_DATA_TEST_FIXTURE",
        "NOT_READY",
    ]
    prevalence: PrevalenceReadiness
    training: TrainingReadiness
    blockers: tuple[ReadinessBlocker, ...]
    training_execution_authorized: bool
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_labels_inferred_from_compilation: Literal[False] = False
    semantic_labels_inferred_from_proof_search: Literal[False] = False
    semantic_labels_inferred_from_llm_agreement: Literal[False] = False

    @model_validator(mode="after")
    def _status_is_coherent(self) -> TrainingDataReadinessReport:
        globally_ready = self.prevalence.prevalence_estimate_ready and not self.blockers
        if self.mode == "confirmatory" and self.training.confirmatory_ready and globally_ready:
            expected = "READY" if self.artifact_class == "production" else "READY_TEST_FIXTURE"
        elif (
            self.mode == "reduced_data_ablation"
            and self.training.reduced_data_ablation_ready
            and globally_ready
        ):
            expected = (
                "READY_REDUCED_DATA_ABLATION"
                if self.artifact_class == "production"
                else "READY_REDUCED_DATA_TEST_FIXTURE"
            )
        else:
            expected = "NOT_READY"
        if self.status != expected:
            raise ValueError("readiness status does not match audited training state")
        authorized_statuses = {"READY", "READY_REDUCED_DATA_ABLATION"}
        if self.training_execution_authorized != (self.status in authorized_statuses):
            raise ValueError("training authorization must match readiness status")
        if (self.status == "NOT_READY") != bool(self.blockers):
            raise ValueError("NOT_READY must have blockers and ready reports must not")
        payload = self.model_dump(mode="json", exclude={"audit_id"})
        expected_id = "training_data_readiness_v1:" + hash_canonical(payload)
        if self.audit_id != expected_id:
            raise ValueError("audit_id does not match canonical report content")
        return self


def load_training_data_readiness_policy(
    path: Path,
) -> LoadedConfig[TrainingDataReadinessPolicy]:
    return load_config(path, TrainingDataReadinessPolicy)


def _read_jsonl(path: Path, model: type[StrictModel]) -> tuple[list[StrictModel], list[str]]:
    records: list[StrictModel] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"cannot read {path}: {exc}"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            errors.append(f"{path}:{line_number}: blank JSONL line")
            continue
        try:
            raw = json.loads(line)
            records.append(model.model_validate(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path}:{line_number}: {exc}")
    return records, errors


def _read_gold_manifest(path: Path) -> tuple[GoldPartitionManifest | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return GoldPartitionManifest.model_validate(raw), None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return None, str(exc)


def _read_json_object(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(raw, dict):
        return None, "artifact is not a JSON object"
    return raw, None


def _jsonl_objects(path: Path) -> tuple[list[dict[str, object]], list[str]]:
    records: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [str(exc)]
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            errors.append(f"line {index}: blank JSONL line")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {index}: {exc}")
            continue
        if not isinstance(record, dict):
            errors.append(f"line {index}: record is not an object")
            continue
        records.append(record)
    return records, errors


def _audit_lf022_artifacts(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    blockers: list[ReadinessBlocker],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    set[str],
    dict[str, set[str]],
    dict[str, str],
    dict[str, str],
]:
    missing: list[str] = []
    invalid: list[str] = []
    manifests: list[LF022ReadinessManifest] = []
    promoted_pair_ids: set[str] = set()
    promoted_pairs_by_source: dict[str, set[str]] = {"G_sci": set(), "G_open": set()}
    proposer_family_by_pair: dict[str, str] = {}
    manifest_hashes: dict[str, str] = {}
    for expected_source, relative_path in sorted(policy.inputs.lf022_required_artifacts.items()):
        path = repo_root / relative_path
        if not path.is_file():
            missing.append(relative_path)
            continue
        manifest_hashes[expected_source] = hash_file(path)
        raw, error = _read_json_object(path)
        try:
            manifest = LF022ReadinessManifest.model_validate(raw) if raw is not None else None
        except ValueError as exc:
            manifest = None
            error = str(exc)
        artifact_errors: list[str] = []
        if manifest is None:
            artifact_errors.append(error or "invalid LF022 readiness manifest")
        elif manifest.negative_source != expected_source:
            artifact_errors.append(
                f"negative_source={manifest.negative_source}, expected {expected_source}"
            )
        else:
            parsed_artifacts: dict[str, list[dict[str, object]]] = {}
            for artifact_name, reference in manifest.artifacts.items():
                artifact_path = repo_root / reference.path
                if not artifact_path.is_file():
                    artifact_errors.append(f"{artifact_name}: missing {reference.path}")
                    continue
                observed_hash = hash_file(artifact_path)
                if observed_hash != reference.sha256:
                    artifact_errors.append(
                        f"{artifact_name}: sha256 {observed_hash} != {reference.sha256}"
                    )
                    continue
                parsed, parse_errors = _jsonl_objects(artifact_path)
                if parse_errors:
                    artifact_errors.append(f"{artifact_name}: {'; '.join(parse_errors[:3])}")
                    continue
                if len(parsed) != reference.record_count:
                    artifact_errors.append(
                        f"{artifact_name}: {len(parsed)} records != {reference.record_count}"
                    )
                    continue
                projection = _LF022_ARTIFACT_PROJECTIONS[artifact_name]
                projection_errors: list[str] = []
                for record_number, record in enumerate(parsed, start=1):
                    try:
                        projection.model_validate(record)
                    except ValueError as exc:
                        projection_errors.append(f"record {record_number}: {exc}")
                if projection_errors:
                    artifact_errors.append(f"{artifact_name}: {'; '.join(projection_errors[:3])}")
                    continue
                parsed_artifacts[artifact_name] = parsed
            if not artifact_errors:
                pair_ids = {
                    str(record["pair_id"])
                    for record in parsed_artifacts["pairs"]
                    if isinstance(record.get("pair_id"), str)
                }
                for record in parsed_artifacts["pairs"]:
                    pair_id = str(record["pair_id"])
                    if record.get("negative_source") != expected_source:
                        artifact_errors.append(
                            f"pair {pair_id}: negative_source differs from manifest"
                        )
                    family = record.get("proposer_family")
                    if isinstance(family, str):
                        proposer_family_by_pair[pair_id] = family
                promotion_pair_ids = {
                    str(record["pair_id"])
                    for record in parsed_artifacts["promotions"]
                    if isinstance(record.get("pair_id"), str)
                }
                expected_promoted = set(manifest.promoted_pair_ids)
                if not expected_promoted <= pair_ids:
                    artifact_errors.append("promoted_pair_ids are absent from pair artifact")
                if promotion_pair_ids != expected_promoted:
                    artifact_errors.append(
                        "promotion artifact pair IDs do not equal promoted_pair_ids"
                    )
                promotion_statuses = {
                    str(record["promotion_status"])
                    for record in parsed_artifacts["promotions"]
                    if isinstance(record.get("promotion_status"), str)
                }
                if promotion_statuses != {manifest.promotion_status}:
                    artifact_errors.append(
                        "promotion artifact status disagrees with readiness manifest"
                    )
                label_target_ids = {
                    str(record["target_id"])
                    for record in parsed_artifacts["resolved_labels"]
                    if isinstance(record.get("target_id"), str)
                }
                if not expected_promoted <= label_target_ids:
                    artifact_errors.append(
                        "promoted_pair_ids are absent from resolved-label artifact"
                    )
                successful_evidence_targets = {
                    str(record["target_id"])
                    for record in parsed_artifacts["evidence"]
                    if record.get("status") == "success"
                    and isinstance(record.get("target_id"), str)
                }
                if not expected_promoted <= successful_evidence_targets:
                    artifact_errors.append(
                        "each promoted pair requires at least one successful evidence record"
                    )
        if artifact_errors:
            invalid.append(f"{relative_path}: {'; '.join(artifact_errors)}")
        elif manifest is not None:
            manifests.append(manifest)
            promoted_pair_ids.update(manifest.promoted_pair_ids)
            promoted_pairs_by_source[manifest.negative_source].update(manifest.promoted_pair_ids)

    if missing:
        blockers.append(
            ReadinessBlocker(
                code="LF022_ARTIFACTS_MISSING",
                message="SCI-conditioned/open-ended LF-022 data do not yet exist.",
                observed=", ".join(missing),
                required="both registered, production LF-022 readiness manifests",
            )
        )
    if invalid:
        blockers.append(
            ReadinessBlocker(
                code="LF022_ARTIFACTS_INVALID",
                message=(
                    "LF-022 readiness artifacts fail schema, hash, count, or promotion "
                    "integrity checks."
                ),
                observed="; ".join(invalid[:5]),
                required=(
                    "content-addressed production manifests with promoted pairs and "
                    "internally consistent record counts"
                ),
            )
        )
    if len(manifests) == 2:
        combined_families: Counter[str] = Counter()
        for manifest in manifests:
            combined_families.update(manifest.proposer_family_counts)
        combined_total = sum(combined_families.values())
        maximum_fraction = (
            max(combined_families.values()) / combined_total if combined_total else 1.0
        )
        controls = policy.family_controls
        if (
            len(combined_families) < controls.minimum_llm_proposer_families
            or maximum_fraction > controls.maximum_one_llm_family_fraction_of_llm_negative + 1e-12
        ):
            invalid.append("combined G_sci+G_open proposer-family diversity/cap check failed")
            blockers.append(
                ReadinessBlocker(
                    code="LF022_FAMILY_DIVERSITY_INVALID",
                    message="LF-022 proposer-family diversity is insufficient or overconcentrated.",
                    observed=(
                        f"{len(combined_families)} families; maximum fraction "
                        f"{maximum_fraction:.6f}"
                    ),
                    required=(
                        f"at least {controls.minimum_llm_proposer_families} families and "
                        "no family fraction above "
                        f"{controls.maximum_one_llm_family_fraction_of_llm_negative}"
                    ),
                )
            )
    if len(manifests) == 2:
        derived_families = Counter(
            proposer_family_by_pair[pair_id]
            for pair_id in promoted_pair_ids
            if pair_id in proposer_family_by_pair
        )
        declared_families: Counter[str] = Counter()
        for manifest in manifests:
            declared_families.update(manifest.proposer_family_counts)
        if derived_families != declared_families:
            invalid.append("proposer_family_counts do not derive from promoted pair records")
            blockers.append(
                ReadinessBlocker(
                    code="LF022_FAMILY_LINEAGE_INVALID",
                    message="LF-022 proposer-family counts are self-declared or inconsistent.",
                    observed=str(dict(sorted(derived_families.items()))),
                    required=str(dict(sorted(declared_families.items()))),
                )
            )
    return (
        tuple(missing),
        tuple(invalid),
        promoted_pair_ids,
        promoted_pairs_by_source,
        proposer_family_by_pair,
        dict(sorted(manifest_hashes.items())),
    )


def _audit_generator_holdout(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    records: Sequence[TrainingAuditRecord],
    blockers: list[ReadinessBlocker],
) -> tuple[bool, bool, str | None, str | None]:
    """Validate the frozen train/held-out generator-family partition."""

    path = repo_root / policy.inputs.generator_holdout_manifest
    if not path.is_file():
        blockers.append(
            ReadinessBlocker(
                code="GENERATOR_HOLDOUT_MANIFEST_MISSING",
                message="The frozen generator-family holdout manifest is absent.",
                observed="absent",
                required=policy.inputs.generator_holdout_manifest,
            )
        )
        return False, False, None, None

    artifact_sha256 = hash_file(path)
    raw, error = _read_json_object(path)
    try:
        manifest = GeneratorHoldoutManifest.model_validate(raw) if raw is not None else None
    except ValueError as exc:
        manifest = None
        error = str(exc)
    validation_errors: list[str] = []
    if manifest is None:
        validation_errors.append(error or "invalid generator holdout manifest")
    else:
        source_path = repo_root / manifest.source_artifact
        if not source_path.is_file():
            validation_errors.append(
                f"missing generator source artifact {manifest.source_artifact}"
            )
        elif hash_file(source_path) != manifest.source_artifact_sha256:
            validation_errors.append(
                f"generator source artifact hash mismatch for {manifest.source_artifact}"
            )
        supervision = set(manifest.supervision_generator_families)
        observed_real = {
            record.source_family
            for record in records
            if record.negative_source == "G_real" and record.source_family is not None
        }
        unexpected = sorted(observed_real - supervision)
        if unexpected:
            validation_errors.append(
                "G_real inventory uses non-supervision generator families: " + ", ".join(unexpected)
            )
        if manifest.heldout_generator_family in observed_real:
            validation_errors.append("held-out generator family appears in training inventory")

    valid = manifest is not None and not validation_errors
    if not valid:
        blockers.append(
            ReadinessBlocker(
                code="GENERATOR_HOLDOUT_MANIFEST_INVALID",
                message=(
                    "Generator training/holdout lineage is invalid or is not bound "
                    "to the current G_real inventory."
                ),
                observed="; ".join(validation_errors[:8]),
                required=(
                    "at least three successful supervision families plus one disjoint "
                    "held-out family, bound to a content-addressed source artifact"
                ),
            )
        )
    return (
        True,
        valid,
        artifact_sha256,
        manifest.heldout_generator_family if manifest is not None else None,
    )


def _audit_statistical_assessment(
    *,
    repo_root: Path,
    manifest: GoldPartitionManifest,
    required_claim: str,
    minimum_groups: int,
    required_method: str,
) -> str | None:
    if manifest.statistical_assessment_artifact is None:
        return "statistical assessment artifact is absent"
    path = repo_root / manifest.statistical_assessment_artifact
    if not path.is_file():
        return f"statistical assessment artifact is missing: {path}"
    if hash_file(path) != manifest.statistical_assessment_sha256:
        return "statistical assessment hash mismatch"
    raw, error = _read_json_object(path)
    if raw is None:
        return error or "invalid statistical assessment"
    try:
        assessment = StatisticalAdequacyAssessment.model_validate(raw)
    except ValueError as exc:
        return str(exc)
    if assessment.partition != manifest.partition:
        return "statistical assessment partition mismatch"
    design_payload = {
        "schema": "gold_partition_design_v1",
        "partition": manifest.partition,
        "distribution": manifest.distribution,
        "target_count": manifest.target_count,
        "sampling_design": manifest.sampling_design,
        "calibration_k_folds": manifest.calibration_k_folds,
        "simple_random_real_output_subpanel_count": (
            manifest.simple_random_real_output_subpanel_count
        ),
        "records": [
            {
                "record_id": record.record_id,
                "target_kind": record.target_kind,
                "target_id": record.target_id,
                "split_component_id": record.split_component_id,
                "sampling_stratum": record.sampling_stratum,
                "inclusion_probability": record.inclusion_probability,
                "design_weight": record.design_weight,
                "simple_random_real_output_subpanel": (record.simple_random_real_output_subpanel),
            }
            for record in manifest.records
        ],
    }
    if assessment.partition_design_hash != hash_canonical(design_payload):
        return "statistical assessment is not bound to the frozen partition design"
    if assessment.component_count != len(manifest.records):
        return "statistical assessment component count mismatch"
    if assessment.record_count != len(manifest.records):
        return "statistical assessment record count mismatch"
    if len(manifest.records) < minimum_groups:
        return f"{len(manifest.records)} groups < policy minimum {minimum_groups}"
    if assessment.minimum_required_component_count != minimum_groups:
        return "statistical assessment minimum differs from policy"
    if assessment.method != required_method:
        return "statistical assessment method differs from preregistered policy"
    if (
        manifest.partition == "calibration_gold"
        and manifest.calibration_k_folds is not None
        and assessment.record_count < manifest.calibration_k_folds
    ):
        return "calibration record count is smaller than its K-fold plan"
    if required_claim not in assessment.supported_claims:
        return f"statistical assessment does not support {required_claim}"
    return None


def _unique_index(
    records: Sequence[StrictModel],
    *,
    field: str,
    artifact_name: str,
    errors: list[str],
) -> dict[str, StrictModel]:
    index: dict[str, StrictModel] = {}
    for record in records:
        key = getattr(record, field)
        if key in index:
            errors.append(f"{artifact_name} has duplicate {field}={key}")
        else:
            index[key] = record
    return index


def _load_private_authentication_key(
    repo_root: Path,
    relative: str,
    *,
    purpose: str,
) -> tuple[bytes | None, str | None]:
    path = repo_root / relative
    if path.is_symlink() or not path.is_file():
        return None, f"{purpose} key is missing or is not a regular file: {relative}"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        key = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {purpose} key {relative}: {exc}"
    if mode & 0o077:
        return None, f"{purpose} key must be mode-0600: {relative}"
    if len(key) < 32:
        return None, f"{purpose} key must contain at least 32 bytes"
    return key, None


def annotation_content_sha256(annotations: Sequence[AnnotationRecord]) -> str:
    return hash_canonical(
        {
            "schema": "authenticated_human_annotation_set_v1",
            "annotations": [
                annotation.model_dump(mode="json")
                for annotation in sorted(annotations, key=lambda item: item.annotation_id)
            ],
        }
    )


def _audit_authenticated_adjudication(
    *,
    adjudication: HumanAdjudicationRecord,
    annotations: Mapping[str, AnnotationRecord],
    operator_key: bytes | None,
    allow_test_fixture: bool,
) -> list[str]:
    errors: list[str] = []
    raw_annotations: list[AnnotationRecord] = []
    for annotation_id in adjudication.annotation_ids:
        annotation = annotations.get(annotation_id)
        if annotation is None:
            errors.append(f"missing annotation {annotation_id}")
            continue
        raw_annotations.append(annotation)
        if (
            annotation.target_kind != SemanticLabelTargetKind.LEAN_PAIR
            or annotation.target_id != adjudication.target_id
        ):
            errors.append(f"annotation {annotation_id} targets another record")
    if len(raw_annotations) != len(adjudication.annotation_ids):
        return errors
    if len({annotation.annotator_id for annotation in raw_annotations}) < 2:
        errors.append("fewer than two independent annotator IDs")

    def metadata_values(name: str) -> tuple[str, ...]:
        values = tuple(
            sorted(
                value
                for annotation in raw_annotations
                if isinstance((value := annotation.metadata.get(name)), str) and value
            )
        )
        if len(values) != len(raw_annotations):
            errors.append(f"raw annotations lack bound {name}")
        return values

    if metadata_values("human_assignment_id") != adjudication.human_assignment_ids:
        errors.append("authenticated assignment IDs disagree with raw annotations")
    if (
        metadata_values("human_submission_attestation_id")
        != adjudication.human_submission_attestation_ids
    ):
        errors.append("submission attestation IDs disagree with raw annotations")
    if metadata_values("annotator_principal_hash") != adjudication.annotator_principal_hashes:
        errors.append("annotator principal hashes disagree with raw annotations")
    if any(
        annotation.metadata.get("fixture_only") != adjudication.fixture_only
        or annotation.metadata.get("raw_vote_only") is not True
        or annotation.metadata.get("resolved_label_created") is not False
        or annotation.metadata.get("gold_label_created") is not False
        or annotation.metadata.get("training_eligible") is not False
        for annotation in raw_annotations
    ):
        errors.append("raw annotation provenance flags disagree with adjudication")
    if any(
        not isinstance(annotation.metadata.get("import_role"), str)
        for annotation in raw_annotations
    ):
        errors.append("raw annotations lack an imported-raw provenance role")
    if adjudication.fixture_only and not allow_test_fixture:
        errors.append("test-fixture human provenance is forbidden by policy")
    if not adjudication.fixture_only and adjudication.origin_assurance != "operator_attested":
        errors.append("production raw adjudication lacks operator-attested provenance")
    if annotation_content_sha256(raw_annotations) != adjudication.annotation_content_sha256:
        errors.append("annotation content hash differs from authenticated adjudication")

    if operator_key is None:
        errors.append("operator authentication key is unavailable")
    else:
        operator_key_id = hashlib.sha256(operator_key).hexdigest()
        if adjudication.authentication_key_id != operator_key_id:
            errors.append("operator authentication key ID differs")
        elif not hmac.compare_digest(
            adjudication.authentication_tag,
            _human_adjudication_authentication_tag(adjudication, key=operator_key),
        ):
            errors.append("operator authentication tag is invalid")

    return errors


def _canonical_split_components(
    pairs: Mapping[str, PairRecord],
) -> dict[str, str]:
    """Recompute §19.5 connected components from the complete pair universe."""

    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        small, large = sorted((left_root, right_root))
        parent[large] = small

    for pair in pairs.values():
        groups = tuple(pair.split_group_ids)
        for group in groups:
            find(group)
        for group in groups[1:]:
            union(groups[0], group)

    component_groups: dict[str, set[str]] = {}
    for group in parent:
        component_groups.setdefault(find(group), set()).add(group)
    component_ids = {
        root: "split-component:"
        + hash_canonical(
            {
                "schema": "leanfaith_split_component_v1",
                "split_group_ids": sorted(groups),
            }
        )
        for root, groups in component_groups.items()
    }
    return {
        pair_id: component_ids[find(pair.split_group_ids[0])] for pair_id, pair in pairs.items()
    }


def _load_lineage_records(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
) -> tuple[
    dict[str, TheoremRecord],
    dict[str, PairRecord],
    dict[str, ResolvedLabel],
    dict[str, EvidenceRecord],
    dict[str, FamilyPromotionDecision],
    dict[str, AnnotationRecord],
    dict[str, HumanAdjudicationRecord],
    dict[str, SplitAssignmentRecord],
    dict[tuple[str, str], SourceManifest],
    list[str],
    bool,
    dict[str, str],
]:
    inputs = policy.inputs.lineage
    errors: list[str] = []
    present = True
    artifact_hashes: dict[str, str] = {}

    def load_jsonl(relative: str, model: type[StrictModel]) -> list[StrictModel]:
        nonlocal present
        path = repo_root / relative
        if not path.is_file():
            present = False
            errors.append(f"missing lineage artifact {relative}")
            return []
        artifact_hashes[relative] = hash_file(path)
        parsed, parse_errors = _read_jsonl(path, model)
        errors.extend(parse_errors)
        return parsed

    theorems = load_jsonl(inputs.theorem_records, TheoremRecord)
    pairs = load_jsonl(inputs.pair_records, PairRecord)
    labels = load_jsonl(inputs.resolved_labels, ResolvedLabel)
    evidence = load_jsonl(inputs.evidence_records, EvidenceRecord)
    promotions = load_jsonl(inputs.promotion_decisions, FamilyPromotionDecision)
    annotations = load_jsonl(inputs.annotation_records, AnnotationRecord)
    adjudications = load_jsonl(inputs.adjudication_records, HumanAdjudicationRecord)
    assignments = load_jsonl(inputs.split_assignments, SplitAssignmentRecord)
    sources: list[SourceManifest] = []
    for relative in inputs.source_manifests:
        path = repo_root / relative
        if not path.is_file():
            present = False
            errors.append(f"missing source manifest {relative}")
            continue
        artifact_hashes[relative] = hash_file(path)
        raw, error = _read_json_object(path)
        try:
            source = SourceManifest.model_validate(raw) if raw is not None else None
        except ValueError as exc:
            source = None
            error = str(exc)
        if source is None:
            errors.append(f"{relative}: {error}")
        else:
            sources.append(source)
    theorem_index = _unique_index(
        theorems, field="theorem_id", artifact_name="theorem records", errors=errors
    )
    pair_index = _unique_index(pairs, field="pair_id", artifact_name="pair records", errors=errors)
    label_index = _unique_index(
        labels, field="label_id", artifact_name="resolved labels", errors=errors
    )
    evidence_index = _unique_index(
        evidence, field="evidence_id", artifact_name="evidence records", errors=errors
    )
    promotion_index = _unique_index(
        promotions,
        field="decision_id",
        artifact_name="promotion decisions",
        errors=errors,
    )
    source_index: dict[tuple[str, str], SourceManifest] = {}
    for source in sources:
        key = (source.source, source.revision)
        if key in source_index:
            errors.append(
                f"source manifests have duplicate source/revision={source.source}@{source.revision}"
            )
        else:
            source_index[key] = source
    annotation_index = _unique_index(
        annotations,
        field="annotation_id",
        artifact_name="annotation records",
        errors=errors,
    )
    adjudication_index = _unique_index(
        adjudications,
        field="adjudication_id",
        artifact_name="adjudication records",
        errors=errors,
    )
    assignment_index = _unique_index(
        assignments,
        field="target_id",
        artifact_name="split assignments",
        errors=errors,
    )
    return (
        {key: value for key, value in theorem_index.items() if isinstance(value, TheoremRecord)},
        {key: value for key, value in pair_index.items() if isinstance(value, PairRecord)},
        {key: value for key, value in label_index.items() if isinstance(value, ResolvedLabel)},
        {key: value for key, value in evidence_index.items() if isinstance(value, EvidenceRecord)},
        {
            key: value
            for key, value in promotion_index.items()
            if isinstance(value, FamilyPromotionDecision)
        },
        {
            key: value
            for key, value in annotation_index.items()
            if isinstance(value, AnnotationRecord)
        },
        {
            key: value
            for key, value in adjudication_index.items()
            if isinstance(value, HumanAdjudicationRecord)
        },
        {
            key: value
            for key, value in assignment_index.items()
            if isinstance(value, SplitAssignmentRecord)
        },
        source_index,
        errors,
        present,
        dict(sorted(artifact_hashes.items())),
    )


def _audit_inventory_lineage(
    *,
    records: Sequence[TrainingAuditRecord],
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    human_manifests: Mapping[str, GoldPartitionManifest],
    ambiguity_records: Sequence[AmbiguityTrainingRecord],
    lf022_promoted_pair_ids: set[str],
    blockers: list[ReadinessBlocker],
) -> tuple[bool, bool, int, dict[str, str]]:
    (
        theorems,
        pairs,
        labels,
        evidence,
        promotions,
        annotations,
        adjudications,
        assignments,
        sources,
        errors,
        present,
        artifact_hashes,
    ) = _load_lineage_records(repo_root=repo_root, policy=policy)
    operator_key, operator_key_error = _load_private_authentication_key(
        repo_root,
        policy.inputs.lineage.human_authentication_key,
        purpose="human-annotation operator authentication",
    )
    if operator_key_error is not None:
        errors.append(operator_key_error)
    canonical_components = _canonical_split_components(pairs) if pairs else {}
    if set(assignments) != set(pairs):
        missing = sorted(set(pairs) - set(assignments))
        extra = sorted(set(assignments) - set(pairs))
        errors.append(
            "split assignment target set differs from PairRecords"
            f" (missing={missing[:3]}, extra={extra[:3]})"
        )
    for pair_id, pair in pairs.items():
        theorem_a = theorems.get(pair.theorem_a_id)
        theorem_b = theorems.get(pair.theorem_b_id)
        if theorem_a is not None and theorem_b is not None:
            errors.extend(
                f"{pair_id}: {error}" for error in check_pair_groups(pair, theorem_a, theorem_b)
            )
        assignment = assignments.get(pair_id)
        if assignment is not None and assignment.split_component_id != canonical_components.get(
            pair_id
        ):
            errors.append(f"{pair_id}: split assignment differs from canonical union-find result")

    adjudications_by_target: dict[str, HumanAdjudicationRecord] = {}
    for adjudication in adjudications.values():
        if adjudication.target_id in adjudications_by_target:
            errors.append(f"duplicate terminal adjudication for target {adjudication.target_id}")
        else:
            adjudications_by_target[adjudication.target_id] = adjudication
        errors.extend(
            f"{adjudication.adjudication_id}: {error}"
            for error in _audit_authenticated_adjudication(
                adjudication=adjudication,
                annotations=annotations,
                operator_key=operator_key,
                allow_test_fixture=(policy.inputs.lineage.allow_test_fixture_human_provenance),
            )
        )

    for product, manifest in human_manifests.items():
        for gold in manifest.records:
            human_pair = pairs.get(gold.target_id)
            assignment = assignments.get(gold.target_id)
            if human_pair is None or assignment is None:
                errors.append(f"{product}/{gold.record_id}: missing target pair/split assignment")
                continue
            if gold.split_component_id != assignment.split_component_id:
                errors.append(f"{product}/{gold.record_id}: split component is not canonical")
            if product == "final_human_test":
                if human_pair.resolved_label_id is not None:
                    errors.append(
                        f"{product}/{gold.record_id}: sealed PairRecord exposes a label link"
                    )
                if any(label.target_id == gold.target_id for label in labels.values()):
                    errors.append(
                        f"{product}/{gold.record_id}: sealed target appears in readable labels"
                    )
                if gold.target_id in adjudications_by_target:
                    errors.append(
                        f"{product}/{gold.record_id}: sealed target appears in "
                        "readable adjudications"
                    )
                continue
            if gold.resolved_label_id is None or gold.adjudication_id is None:
                errors.append(f"{product}/{gold.record_id}: visible human lineage links are absent")
                continue
            label = labels.get(gold.resolved_label_id)
            human_adjudication = adjudications.get(gold.adjudication_id)
            if label is None or label.target_id != gold.target_id:
                errors.append(f"{product}/{gold.record_id}: missing/mismatched ResolvedLabel")
            if human_adjudication is None or human_adjudication.target_id != gold.target_id:
                errors.append(f"{product}/{gold.record_id}: missing/mismatched adjudication")
            if label is not None and human_adjudication is not None:
                relation = label.relation.value if label.relation is not None else None
                if (
                    label.same_claim != human_adjudication.same_claim
                    or relation != human_adjudication.relation
                    or label.quality_tier != QualityTier.GOLD_HUMAN
                ):
                    errors.append(
                        f"{product}/{gold.record_id}: label/adjudication commitment disagrees"
                    )
            if label is not None and human_adjudication is not None:
                relation = label.relation.value if label.relation is not None else None
                if (
                    label.same_claim != gold.same_claim
                    or relation != gold.relation
                    or human_adjudication.same_claim != gold.same_claim
                    or human_adjudication.relation != gold.relation
                ):
                    errors.append(f"{product}/{gold.record_id}: semantic lineage disagrees")
    ambiguity_gold_manifest = human_manifests.get("training_gold")
    training_gold_for_ambiguity = (
        {
            record.record_id: record
            for record in ambiguity_gold_manifest.records
            if record.ambiguity_head_eligible
        }
        if ambiguity_gold_manifest is not None
        else {}
    )
    for ambiguity in ambiguity_records:
        ambiguity_gold = training_gold_for_ambiguity.get(ambiguity.training_gold_record_id)
        label = labels.get(ambiguity.resolved_label_id)
        assignment = assignments.get(ambiguity.target_id)
        if (
            ambiguity_gold is None
            or ambiguity_gold.target_id != ambiguity.target_id
            or ambiguity_gold.resolved_label_id != ambiguity.resolved_label_id
            or label is None
            or label.resolution_outcome.value != "ambiguous"
            or assignment is None
            or assignment.split_component_id != ambiguity.split_component_id
        ):
            errors.append(f"{ambiguity.record_id}: invalid ambiguity-only D5 lineage")
    training_gold_manifest = human_manifests.get("training_gold")
    training_gold = (
        {record.record_id: record for record in training_gold_manifest.records}
        if training_gold_manifest is not None
        else {}
    )
    for inventory in records:
        inventory_pair = pairs.get(inventory.pair_id)
        if inventory_pair is None:
            errors.append(f"{inventory.pair_id}: missing PairRecord")
            continue
        assignment = assignments.get(inventory.pair_id)
        if assignment is None or assignment.split_component_id != inventory.split_component_id:
            errors.append(f"{inventory.pair_id}: inventory split component is not canonical")
        if inventory_pair.resolved_label_id != inventory.resolved_label_id:
            errors.append(f"{inventory.pair_id}: resolved-label reverse link mismatch")
        if inventory.negative_source == "G_rule" and (
            inventory_pair.transformation_family != inventory.transform_family
        ):
            errors.append(f"{inventory.pair_id}: G_rule family is not derived from PairRecord")
        if (
            inventory.negative_source == "G_real"
            and inventory_pair.generator_id != inventory.source_family
        ):
            errors.append(f"{inventory.pair_id}: G_real family is not derived from PairRecord")
        label = labels.get(inventory.resolved_label_id)
        if label is None:
            errors.append(f"{inventory.pair_id}: missing ResolvedLabel")
            continue
        errors.extend(
            f"{inventory.pair_id}: {error}"
            for error in check_label_target_link(label, inventory_pair)
        )
        if label.same_claim != inventory.same_claim:
            errors.append(f"{inventory.pair_id}: same_claim disagrees with ResolvedLabel")
        if label.relation is None or label.relation.value != inventory.relation:
            errors.append(f"{inventory.pair_id}: relation disagrees with ResolvedLabel")
        if not label.train_eligibility:
            errors.append(f"{inventory.pair_id}: ResolvedLabel is not train-eligible")
        if set(inventory_pair.evidence_ids) != set(inventory.evidence_ids):
            errors.append(f"{inventory.pair_id}: evidence inventory disagrees with PairRecord")
        if not set(label.evidence_ids_used) <= set(inventory.evidence_ids):
            errors.append(f"{inventory.pair_id}: label uses evidence absent from inventory")
        linked_evidence: list[EvidenceRecord] = []
        for evidence_id in inventory.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                errors.append(f"{inventory.pair_id}: missing evidence {evidence_id}")
                continue
            linked_evidence.append(item)
            if (
                item.target_kind != EvidenceTargetKind.LEAN_PAIR
                or item.target_id != inventory.pair_id
            ):
                errors.append(f"{inventory.pair_id}: evidence {evidence_id} targets another record")
        linked_promotions: list[FamilyPromotionDecision] = []
        for decision_id in inventory.promotion_decision_ids:
            decision = promotions.get(decision_id)
            if decision is None:
                errors.append(f"{inventory.pair_id}: missing promotion {decision_id}")
            else:
                linked_promotions.append(decision)
        for theorem_id in (
            inventory_pair.theorem_a_id,
            inventory_pair.theorem_b_id,
        ):
            theorem = theorems.get(theorem_id)
            if theorem is None:
                errors.append(f"{inventory.pair_id}: missing theorem {theorem_id}")
                continue
            source = sources.get((theorem.source, theorem.source_revision))
            if source is None:
                errors.append(
                    f"{inventory.pair_id}: no SourceManifest for theorem source "
                    f"{theorem.source}@{theorem.source_revision}"
                )

        expected_human_answer = "same_claim" if inventory.same_claim else "not_same_claim"
        inventory_adjudication = adjudications_by_target.get(inventory.pair_id)
        human_evidence = any(
            item.kind == EvidenceKind.HUMAN_ANNOTATION
            and item.status == EvidenceExecutionStatus.SUCCESS
            and isinstance(item.value, JudgmentValue)
            and item.value.answer == expected_human_answer
            and item.value.relation == inventory.relation
            and item.evidence_id in label.evidence_ids_used
            and inventory_adjudication is not None
            and item.metadata.get("adjudication_id") == inventory_adjudication.adjudication_id
            for item in linked_evidence
        )
        gold_family = any(
            decision.decision == TransformationFamilyStatus.GOLD_PROMOTED
            and decision.family_id == inventory_pair.transformation_family
            for decision in linked_promotions
        )
        matching_family_promotion = any(
            decision.family_id == inventory_pair.transformation_family
            and decision.decision
            in {
                TransformationFamilyStatus.GOLD_PROMOTED,
                TransformationFamilyStatus.SILVER,
            }
            for decision in linked_promotions
        )
        item_transform_audit = any(
            item.kind == EvidenceKind.TRANSFORMATION_AUDIT
            and item.status == EvidenceExecutionStatus.SUCCESS
            and isinstance(item.value, AuditValue)
            and bool(item.value.checks)
            and all(check is True for check in item.value.checks.values())
            and not item.value.violation_codes
            and item.evidence_id in label.evidence_ids_used
            and item.metadata.get("family_id") == inventory_pair.transformation_family
            for item in linked_evidence
        )
        accepted_separator = any(
            item.kind == EvidenceKind.COUNTEREXAMPLE
            and item.status == EvidenceExecutionStatus.SUCCESS
            and isinstance(item.value, CounterexampleValue)
            and item.value.outcome == "found"
            and item.evidence_id in label.evidence_ids_used
            for item in linked_evidence
        )
        consensus_families = {
            str(item.metadata["model_family"])
            for item in linked_evidence
            if item.kind == EvidenceKind.LLM_JUDGMENT
            and item.status == EvidenceExecutionStatus.SUCCESS
            and isinstance(item.value, JudgmentValue)
            and item.value.answer == expected_human_answer
            and item.value.relation == inventory.relation
            and item.evidence_id in label.evidence_ids_used
            and isinstance(item.metadata.get("model_family"), str)
        }
        for basis in inventory.label_bases:
            if basis == "human_adjudication" and not (
                human_evidence and label.quality_tier == QualityTier.GOLD_HUMAN
            ):
                errors.append(
                    f"{inventory.pair_id}: human_adjudication lacks successful human evidence "
                    "and gold_human label"
                )
            elif basis == "certified_conservative_transformation" and not (
                gold_family
                and item_transform_audit
                and label.quality_tier == QualityTier.GOLD_CONSERVATIVE_TRANSFORM
            ):
                errors.append(
                    f"{inventory.pair_id}: certified basis lacks item audit/gold family promotion"
                )
            elif basis == "human_promoted_transformation" and not (
                human_evidence
                and matching_family_promotion
                and label.quality_tier == QualityTier.GOLD_HUMAN
            ):
                errors.append(
                    f"{inventory.pair_id}: human-promoted transform lacks human evidence/promotion"
                )
            elif basis == "human_promoted_llm_variant" and not (
                human_evidence
                and inventory.pair_id in lf022_promoted_pair_ids
                and label.quality_tier == QualityTier.GOLD_HUMAN
            ):
                errors.append(
                    f"{inventory.pair_id}: human-promoted LLM basis lacks LF022 promotion lineage"
                )
            elif basis == "promoted_independent_consensus" and not (
                inventory.pair_id in lf022_promoted_pair_ids
                and label.quality_tier == QualityTier.SILVER_CONSENSUS
                and len(consensus_families) >= 2
            ):
                errors.append(
                    f"{inventory.pair_id}: promoted consensus lacks two-family used evidence "
                    "and LF022 promotion lineage"
                )
            elif basis == "accepted_kernel_separator" and not (
                accepted_separator and label.quality_tier == QualityTier.GOLD_COUNTEREXAMPLE
            ):
                errors.append(
                    f"{inventory.pair_id}: accepted separator lacks used found-counterexample "
                    "evidence and gold_counterexample tier"
                )
            elif basis == "trusted_human_dataset_label":
                errors.append(
                    f"{inventory.pair_id}: trusted_human_dataset_label is disabled until "
                    "a typed pair-level dataset-label provenance record exists"
                )

        if inventory.training_gold_record_id is not None:
            bound_gold = training_gold.get(inventory.training_gold_record_id)
            if bound_gold is None:
                errors.append(f"{inventory.pair_id}: training_gold record does not exist")
            elif (
                bound_gold.target_id != inventory.pair_id
                or bound_gold.resolved_label_id != inventory.resolved_label_id
                or bound_gold.split_component_id != inventory.split_component_id
                or bound_gold.same_claim != inventory.same_claim
                or bound_gold.relation != inventory.relation
            ):
                errors.append(f"{inventory.pair_id}: training_gold record disagrees with inventory")

    valid = present and not errors
    if errors:
        blockers.append(
            ReadinessBlocker(
                code="TRAINING_LINEAGE_INVALID",
                message=(
                    "Training label bases do not replay from PairRecord, ResolvedLabel, "
                    "evidence, promotion, theorem, and source-manifest lineage."
                ),
                observed="; ".join(errors[:8]),
                required="complete cross-record lineage with a mechanically justified label basis",
            )
        )
    return present, valid, len(errors), artifact_hashes


def _allocation(total: int, fractions: Mapping[str, float]) -> dict[str, int]:
    """Largest-remainder integer allocation with deterministic key tie-breaking."""
    floors = {key: math.floor(total * fraction) for key, fraction in fractions.items()}
    missing = total - sum(floors.values())
    order = sorted(
        fractions,
        key=lambda key: (-(total * fractions[key] - floors[key]), key),
    )
    for key in order[:missing]:
        floors[key] += 1
    return floors


def _family_controls_ok(
    records: Sequence[TrainingAuditRecord],
    *,
    arm: str,
    negative_total: int,
    policy: TrainingDataReadinessPolicy,
) -> bool:
    if arm not in policy.family_controls.apply_caps_to_arms:
        return True
    negatives = [record for record in records if not record.same_claim]
    rule_counts = Counter(
        record.transform_family for record in negatives if record.negative_source == "G_rule"
    )
    rule_cap = math.floor(
        negative_total * policy.family_controls.deterministic_family_fraction_of_all_negative
        + 1e-12
    )
    if any(count > rule_cap for count in rule_counts.values()):
        return False

    llm_records = [record for record in negatives if record.negative_source in {"G_sci", "G_open"}]
    llm_counts = Counter(record.source_family for record in llm_records)
    if len(llm_counts) < policy.family_controls.minimum_llm_proposer_families:
        return False
    llm_cap = math.floor(
        len(llm_records) * policy.family_controls.maximum_one_llm_family_fraction_of_llm_negative
        + 1e-12
    )
    if any(count > llm_cap for count in llm_counts.values()):
        return False

    real_counts = Counter(
        record.source_family for record in negatives if record.negative_source == "G_real"
    )
    if len(real_counts) < policy.family_controls.minimum_real_generator_families:
        return False
    real_total = sum(real_counts.values())
    real_cap = math.floor(
        real_total * policy.family_controls.maximum_one_real_family_fraction_of_real_negative
        + 1e-12
    )
    return not any(count > real_cap for count in real_counts.values())


def _audit_prevalence(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    blockers: list[ReadinessBlocker],
) -> PrevalenceReadiness:
    frame_path = repo_root / policy.inputs.prevalence_frame
    if not frame_path.is_file():
        blockers.append(
            ReadinessBlocker(
                code="PREVALENCE_FRAME_MISSING",
                message="The frozen prevalence frame is missing.",
                observed="absent",
                required=policy.inputs.prevalence_frame,
            )
        )
        return PrevalenceReadiness(
            frame_present=False,
            frame_sha256=None,
            frame_item_count=0,
            generator_family_counts={},
            unresolved_review_count=0,
            human_terminal_label_count=0,
            human_binary_label_count=0,
            human_ambiguous_label_count=0,
            frame_adequate_for_annotation=False,
            human_labels_adequate_for_prevalence=False,
            prevalence_estimate_ready=False,
        )

    raw_records: list[dict[str, object]] = []
    errors: list[str] = []
    for line_number, line in enumerate(
        frame_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("record is not an object")
            raw_records.append(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"line {line_number}: {exc}")
    family_counts: Counter[str] = Counter()
    unresolved = 0
    frame_record_ids: set[str] = set()
    for raw in raw_records:
        frame_record_id = raw.get("frame_record_id")
        if isinstance(frame_record_id, str):
            frame_record_ids.add(frame_record_id)
        population = raw.get("population_item")
        if isinstance(population, dict):
            family = population.get("representative_family_id")
            if isinstance(family, str):
                family_counts[family] += 1
        if (
            raw.get("decision") == "REVIEW"
            and raw.get("same_claim") is None
            and raw.get("semantic_labels_created") is False
        ):
            unresolved += 1
    frame_count = len(raw_records)
    frame_adequate = (
        not errors
        and policy.prevalence.minimum_frame_items
        <= frame_count
        <= policy.prevalence.maximum_frame_items
        and len(family_counts) >= policy.prevalence.minimum_generator_families
    )
    label_path = repo_root / policy.inputs.prevalence_human_labels
    human_terminal = 0
    human_binary = 0
    human_ambiguous = 0
    label_errors: list[str] = []
    if label_path.is_file():
        parsed_labels, label_errors = _read_jsonl(
            label_path,
            PrevalenceHumanLabelRecord,
        )
        labels = [label for label in parsed_labels if isinstance(label, PrevalenceHumanLabelRecord)]
        label_ids = [label.frame_record_id for label in labels]
        if len(set(label_ids)) != len(label_ids):
            label_errors.append("duplicate frame_record_id in prevalence human labels")
        unknown_ids = sorted(set(label_ids) - frame_record_ids)
        if unknown_ids:
            label_errors.append(
                "prevalence labels reference unknown frame IDs: " + ", ".join(unknown_ids[:5])
            )
        lineage_errors: list[str] = []
        pair_records, pair_errors = _read_jsonl(
            repo_root / policy.inputs.lineage.pair_records,
            PairRecord,
        )
        resolved_labels, resolved_errors = _read_jsonl(
            repo_root / policy.inputs.lineage.resolved_labels,
            ResolvedLabel,
        )
        annotations, annotation_errors = _read_jsonl(
            repo_root / policy.inputs.lineage.annotation_records,
            AnnotationRecord,
        )
        adjudications, adjudication_errors = _read_jsonl(
            repo_root / policy.inputs.lineage.adjudication_records,
            HumanAdjudicationRecord,
        )
        lineage_errors.extend(
            (*pair_errors, *resolved_errors, *annotation_errors, *adjudication_errors)
        )
        pair_index = {item.pair_id: item for item in pair_records if isinstance(item, PairRecord)}
        resolved_index = {
            item.label_id: item for item in resolved_labels if isinstance(item, ResolvedLabel)
        }
        annotation_index = {
            item.annotation_id: item for item in annotations if isinstance(item, AnnotationRecord)
        }
        adjudication_index = {
            item.adjudication_id: item
            for item in adjudications
            if isinstance(item, HumanAdjudicationRecord)
        }
        operator_key, operator_key_error = _load_private_authentication_key(
            repo_root,
            policy.inputs.lineage.human_authentication_key,
            purpose="human-annotation operator authentication",
        )
        if operator_key_error is not None:
            lineage_errors.append(operator_key_error)
        for item in labels:
            pair = pair_index.get(item.target_pair_id)
            resolved = resolved_index.get(item.resolved_label_id)
            adjudication = adjudication_index.get(item.adjudication_id)
            if pair is None or resolved is None or adjudication is None:
                lineage_errors.append(
                    f"{item.frame_record_id}: missing pair/label/adjudication lineage"
                )
                continue
            raw_annotations = [
                annotation_index.get(annotation_id) for annotation_id in adjudication.annotation_ids
            ]
            authenticated_errors = _audit_authenticated_adjudication(
                adjudication=adjudication,
                annotations=annotation_index,
                operator_key=operator_key,
                allow_test_fixture=(policy.inputs.lineage.allow_test_fixture_human_provenance),
            )
            source_frame_ids = {
                annotation.metadata.get("source_frame_record_id")
                for annotation in raw_annotations
                if annotation is not None
            }
            if (
                resolved.target_id != item.target_pair_id
                or adjudication.target_id != item.target_pair_id
                or resolved.same_claim != item.same_claim
                or adjudication.same_claim != item.same_claim
                or resolved.relation is None
                or resolved.relation.value != item.relation
                or adjudication.relation != item.relation
                or any(annotation is None for annotation in raw_annotations)
                or len(
                    {
                        annotation.annotator_id
                        for annotation in raw_annotations
                        if annotation is not None
                    }
                )
                < 2
                or source_frame_ids != {item.frame_record_id}
                or authenticated_errors
            ):
                detail = f" ({'; '.join(authenticated_errors[:3])})" if authenticated_errors else ""
                lineage_errors.append(
                    f"{item.frame_record_id}: human lineage or frame-target binding disagrees"
                    + detail
                )
        label_errors.extend(lineage_errors)
        human_terminal = sum(
            label.frame_record_id in frame_record_ids
            and label.resolution_outcome in {"same_claim", "not_same_claim", "ambiguous"}
            for label in labels
        )
        human_binary = sum(
            label.same_claim is not None and label.frame_record_id in frame_record_ids
            for label in labels
        )
        human_ambiguous = sum(
            label.resolution_outcome == "ambiguous" and label.frame_record_id in frame_record_ids
            for label in labels
        )
    else:
        label_errors.append(
            f"human-label artifact is absent: {policy.inputs.prevalence_human_labels}"
        )
    required_human = math.ceil(
        frame_count * policy.prevalence.minimum_human_terminal_label_fraction
    )
    human_adequate = frame_adequate and not label_errors and human_terminal >= required_human
    if errors:
        blockers.append(
            ReadinessBlocker(
                code="PREVALENCE_FRAME_INVALID",
                message="The frozen prevalence frame contains invalid records.",
                observed="; ".join(errors[:5]),
                required="valid JSONL records only",
            )
        )
    if not frame_adequate:
        blockers.append(
            ReadinessBlocker(
                code="PREVALENCE_FRAME_INADEQUATE",
                message="The prevalence frame is not adequate for annotation.",
                observed=f"{frame_count} items across {len(family_counts)} families",
                required=(
                    f"{policy.prevalence.minimum_frame_items}-"
                    f"{policy.prevalence.maximum_frame_items} items across at least "
                    f"{policy.prevalence.minimum_generator_families} families"
                ),
            )
        )
    if not human_adequate:
        blockers.append(
            ReadinessBlocker(
                code="PREVALENCE_HUMAN_LABELS_MISSING",
                message=(
                    "Frame adequacy does not establish faithful prevalence; genuine "
                    "human terminal labels are still required."
                ),
                observed=(
                    f"{human_terminal} human terminal labels; " + "; ".join(label_errors[:3])
                ),
                required=(
                    f"{required_human} separately persisted, adjudicated human "
                    "terminal labels bound to immutable frame IDs"
                ),
            )
        )
    return PrevalenceReadiness(
        frame_present=True,
        frame_sha256=hash_file(frame_path),
        frame_item_count=frame_count,
        generator_family_counts=dict(sorted(family_counts.items())),
        unresolved_review_count=unresolved,
        human_terminal_label_count=human_terminal,
        human_binary_label_count=human_binary,
        human_ambiguous_label_count=human_ambiguous,
        frame_adequate_for_annotation=frame_adequate,
        human_labels_adequate_for_prevalence=human_adequate,
        prevalence_estimate_ready=frame_adequate and human_adequate,
    )


def _audit_human_products(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    blockers: list[ReadinessBlocker],
) -> tuple[
    tuple[HumanProductReadiness, ...],
    dict[str, GoldPartitionManifest],
]:
    results: list[HumanProductReadiness] = []
    manifests: dict[str, GoldPartitionManifest] = {}
    allowed = set(policy.label_policy.allowed_f1_label_bases)
    for product in _HUMAN_PRODUCTS:
        path = repo_root / policy.inputs.human_products[product]
        if not path.is_file():
            blockers.append(
                ReadinessBlocker(
                    code=f"{product.upper()}_MISSING",
                    message=f"The required {product} readiness manifest is absent.",
                    observed="absent",
                    required=policy.inputs.human_products[product],
                )
            )
            results.append(
                HumanProductReadiness(
                    product=product,
                    present=False,
                    valid=False,
                    artifact_sha256=None,
                    record_count=0,
                    component_count=0,
                    faithful_group_count=None,
                    unfaithful_group_count=None,
                    relation_group_counts={},
                )
            )
            continue
        manifest, error = _read_gold_manifest(path)
        valid = manifest is not None and manifest.partition == product
        if manifest is not None and product != "final_human_test":
            valid = valid and all(
                set(record.label_bases) <= allowed and "human_adjudication" in record.label_bases
                for record in manifest.records
            )
        statistical_error: str | None = None
        if manifest is not None and product == "calibration_gold":
            statistical_error = _audit_statistical_assessment(
                repo_root=repo_root,
                manifest=manifest,
                required_claim=policy.statistical_adequacy.calibration_required_claim,
                minimum_groups=(policy.statistical_adequacy.calibration_gold_minimum_groups),
                required_method=policy.statistical_adequacy.calibration_design_method,
            )
        elif manifest is not None and product == "final_human_test":
            if len(manifest.records) < policy.statistical_adequacy.final_human_test_minimum_groups:
                statistical_error = (
                    f"{len(manifest.records)} groups < "
                    f"{policy.statistical_adequacy.final_human_test_minimum_groups}"
                )
            else:
                statistical_error = _audit_statistical_assessment(
                    repo_root=repo_root,
                    manifest=manifest,
                    required_claim=policy.statistical_adequacy.final_required_claim,
                    minimum_groups=(policy.statistical_adequacy.final_human_test_minimum_groups),
                    required_method=policy.statistical_adequacy.final_design_method,
                )
        if statistical_error is not None:
            valid = False
        if not valid:
            blockers.append(
                ReadinessBlocker(
                    code=f"{product.upper()}_INVALID",
                    message=f"The {product} readiness manifest fails its contract.",
                    observed=(
                        error
                        or statistical_error
                        or "partition, human-label basis, or statistical-adequacy mismatch"
                    ),
                    required="valid, adjudicated, purpose-restricted gold manifest",
                )
            )
        if manifest is None:
            results.append(
                HumanProductReadiness(
                    product=product,
                    present=True,
                    valid=False,
                    artifact_sha256=hash_file(path),
                    record_count=0,
                    component_count=0,
                    faithful_group_count=None,
                    unfaithful_group_count=None,
                    relation_group_counts={},
                )
            )
            continue
        if valid:
            manifests[product] = manifest
        faithful = (
            sum(record.same_claim is True for record in manifest.records)
            if manifest.labels_exposed_to_audit
            else None
        )
        unfaithful = (
            sum(record.same_claim is False for record in manifest.records)
            if manifest.labels_exposed_to_audit
            else None
        )
        relation_counts: dict[str, int] = (
            {
                str(relation): count
                for relation, count in dict(
                    sorted(
                        Counter(
                            record.relation
                            for record in manifest.records
                            if record.relation is not None
                        ).items()
                    )
                ).items()
            }
            if manifest.labels_exposed_to_audit
            else {}
        )
        results.append(
            HumanProductReadiness(
                product=product,
                present=True,
                valid=valid,
                artifact_sha256=hash_file(path),
                record_count=len(manifest.records),
                component_count=len({record.split_component_id for record in manifest.records}),
                faithful_group_count=faithful,
                unfaithful_group_count=unfaithful,
                relation_group_counts=relation_counts,
            )
        )
    return tuple(results), manifests


def _audit_training(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
    reduced_data_ablation: bool,
    blockers: list[ReadinessBlocker],
) -> TrainingReadiness:
    inventory_path = repo_root / policy.inputs.training_inventory
    inventory_present = inventory_path.is_file()
    parse_errors: list[str] = []
    records: list[TrainingAuditRecord] = []
    if inventory_present:
        parsed, parse_errors = _read_jsonl(inventory_path, TrainingAuditRecord)
        records = [record for record in parsed if isinstance(record, TrainingAuditRecord)]
    else:
        blockers.append(
            ReadinessBlocker(
                code="TRAINING_INVENTORY_MISSING",
                message="No frozen per-pair training-readiness inventory exists.",
                observed="absent",
                required=policy.inputs.training_inventory,
            )
        )
    if parse_errors:
        blockers.append(
            ReadinessBlocker(
                code="TRAINING_INVENTORY_INVALID",
                message="The training-readiness inventory contains invalid records.",
                observed="; ".join(parse_errors[:5]),
                required="all records validate as training_readiness_record_v1",
            )
        )
    ambiguity_inventory_path = repo_root / policy.inputs.training_ambiguity_inventory
    ambiguity_records: list[AmbiguityTrainingRecord] = []
    ambiguity_parse_errors: list[str] = []
    if ambiguity_inventory_path.is_file():
        parsed_ambiguity, ambiguity_parse_errors = _read_jsonl(
            ambiguity_inventory_path,
            AmbiguityTrainingRecord,
        )
        ambiguity_records = [
            item for item in parsed_ambiguity if isinstance(item, AmbiguityTrainingRecord)
        ]
    if ambiguity_parse_errors:
        blockers.append(
            ReadinessBlocker(
                code="AMBIGUITY_TRAINING_INVENTORY_INVALID",
                message="The D5 ambiguity-head inventory contains invalid records.",
                observed="; ".join(ambiguity_parse_errors[:5]),
                required="valid D5-only terminal-ambiguity records",
            )
        )
    pair_counts = Counter(record.pair_id for record in records)
    duplicate_pair_ids = sorted(pair_id for pair_id, count in pair_counts.items() if count > 1)
    if duplicate_pair_ids:
        blockers.append(
            ReadinessBlocker(
                code="TRAINING_PAIR_IDS_DUPLICATED",
                message="Training inventory pair IDs are not unique.",
                observed=", ".join(duplicate_pair_ids[:10]),
                required="unique pair_id values",
            )
        )

    allowed = set(policy.label_policy.allowed_f1_label_bases)
    forbidden = set(policy.label_policy.forbidden_label_bases)
    forbidden_counts: Counter[str] = Counter()
    unknown_counts: Counter[str] = Counter()
    safe_records: list[TrainingAuditRecord] = []
    for record in records:
        bases = set(record.label_bases)
        for basis in bases & forbidden:
            forbidden_counts[basis] += 1
        for basis in bases - allowed - forbidden:
            unknown_counts[basis] += 1
        if bases and bases <= allowed:
            safe_records.append(record)
    unsafe_count = len(records) - len(safe_records)
    if unsafe_count:
        blockers.append(
            ReadinessBlocker(
                code="UNSAFE_F1_LABEL_BASIS",
                message=(
                    "Some F1 labels rely on forbidden or unregistered evidence. "
                    "Compilation, proof search, and LLM agreement cannot label faithfulness."
                ),
                observed=f"{unsafe_count} unsafe records",
                required="every record uses only registered human/certified F1 label bases",
            )
        )

    effective_records = [record for record in safe_records if record.duplicate_of is None]
    (
        generator_holdout_present,
        generator_holdout_valid,
        generator_holdout_sha256,
        heldout_generator_family,
    ) = _audit_generator_holdout(
        repo_root=repo_root,
        policy=policy,
        records=effective_records,
        blockers=blockers,
    )
    (
        missing_lf022,
        invalid_lf022,
        lf022_promoted_pair_ids,
        lf022_pairs_by_source,
        lf022_family_by_pair,
        lf022_artifact_sha256s,
    ) = _audit_lf022_artifacts(
        repo_root=repo_root,
        policy=policy,
        blockers=blockers,
    )
    for record in effective_records:
        if record.negative_source not in {"G_sci", "G_open"}:
            continue
        expected_pairs = lf022_pairs_by_source[record.negative_source]
        expected_family = lf022_family_by_pair.get(record.pair_id)
        if record.pair_id not in expected_pairs or record.source_family != expected_family:
            invalid_lf022 = (
                *invalid_lf022,
                f"{record.pair_id}: {record.negative_source} inventory provenance is not "
                "bound to its promoted LF022 pair/family",
            )
    if any("inventory provenance" in error for error in invalid_lf022):
        blockers.append(
            ReadinessBlocker(
                code="LF022_TRAINING_LINEAGE_INVALID",
                message="Training G_sci/G_open records do not derive from admitted LF-022 data.",
                observed="; ".join(
                    error for error in invalid_lf022 if "inventory provenance" in error
                )[:2000],
                required="every G_sci/G_open training pair and family bound to its source manifest",
            )
        )

    human_results, human_manifests = _audit_human_products(
        repo_root=repo_root,
        policy=policy,
        blockers=blockers,
    )
    (
        lineage_present,
        lineage_valid,
        lineage_error_count,
        lineage_artifact_sha256s,
    ) = _audit_inventory_lineage(
        records=records,
        repo_root=repo_root,
        policy=policy,
        human_manifests=human_manifests,
        ambiguity_records=ambiguity_records,
        lf022_promoted_pair_ids=lf022_promoted_pair_ids,
        blockers=blockers,
    )

    human_component_sets = {
        product: {record.split_component_id for record in manifest.records}
        for product, manifest in human_manifests.items()
    }
    human_disjoint = True
    for index, left in enumerate(_HUMAN_PRODUCTS):
        for right in _HUMAN_PRODUCTS[index + 1 :]:
            if human_component_sets.get(left, set()) & human_component_sets.get(right, set()):
                human_disjoint = False
    if human_manifests and not human_disjoint:
        blockers.append(
            ReadinessBlocker(
                code="HUMAN_PARTITION_ANCESTRY_OVERLAP",
                message="Human products share connected split components.",
                observed="at least one cross-product component overlap",
                required="zero overlap across all four human products",
            )
        )
    training_components = {record.split_component_id for record in effective_records}
    forbidden_training_overlap = set().union(
        *(
            human_component_sets.get(product, set())
            for product in ("selection_gold", "calibration_gold", "final_human_test")
        )
    )
    training_vs_nontraining_disjoint = not (training_components & forbidden_training_overlap)
    if human_manifests and not training_vs_nontraining_disjoint:
        blockers.append(
            ReadinessBlocker(
                code="TRAINING_SELECTION_CALIBRATION_TEST_OVERLAP",
                message="Training records overlap protected human products by ancestry.",
                observed="at least one connected component overlaps",
                required="zero training overlap with selection/calibration/final products",
            )
        )

    selection = human_manifests.get("selection_gold")
    if selection is not None:
        faithful = {
            record.split_component_id for record in selection.records if record.same_claim is True
        }
        unfaithful = {
            record.split_component_id for record in selection.records if record.same_claim is False
        }
        relation_counts = Counter(
            record.relation
            for record in selection.records
            if record.relation not in (None, "ambiguous")
        )
        failures: list[str] = []
        if len(faithful) < policy.selection_gold_minimum_groups.faithful:
            failures.append(f"faithful={len(faithful)}")
        if len(unfaithful) < policy.selection_gold_minimum_groups.unfaithful:
            failures.append(f"unfaithful={len(unfaithful)}")
        for relation in _CONFIRMATORY_RELATIONS:
            count = relation_counts[relation]
            if count < policy.selection_gold_minimum_groups.per_included_relation_class:
                failures.append(f"{relation}={count}")
        if failures:
            blockers.append(
                ReadinessBlocker(
                    code="SELECTION_GOLD_MINIMA_NOT_MET",
                    message="selection_gold lacks preregistered group-level support.",
                    observed=", ".join(failures),
                    required=(
                        f"{policy.selection_gold_minimum_groups.faithful} faithful, "
                        f"{policy.selection_gold_minimum_groups.unfaithful} unfaithful, "
                        f"{policy.selection_gold_minimum_groups.per_included_relation_class} "
                        "for every canonical non-ambiguous confirmatory relation"
                    ),
                )
            )

    d5_records = [record for record in effective_records if "D5" in record.arm_memberships]
    d5_gold_records = [
        record for record in d5_records if record.training_gold_record_id is not None
    ]
    d5_violations: list[str] = []
    if policy.d5.training_gold_required and not d5_gold_records:
        d5_violations.append("no D5 record is bound to training_gold")
    training_gold_manifest = human_manifests.get("training_gold")
    if policy.d5.training_gold_required and training_gold_manifest is not None:
        expected_training_gold_ids = {
            record.record_id
            for record in training_gold_manifest.records
            if record.same_claim is not None
        }
        expected_ambiguity_gold_ids = {
            record.record_id
            for record in training_gold_manifest.records
            if record.same_claim is None and record.relation == "ambiguous"
        }
        observed_training_gold_ids = {
            record.training_gold_record_id
            for record in d5_gold_records
            if record.training_gold_record_id is not None
        }
        if observed_training_gold_ids != expected_training_gold_ids:
            d5_violations.append(
                "D5 training_gold bindings do not cover the complete frozen training_gold product"
            )
        observed_ambiguity_gold_ids = {
            record.training_gold_record_id for record in ambiguity_records
        }
        if observed_ambiguity_gold_ids != expected_ambiguity_gold_ids:
            d5_violations.append(
                "D5 ambiguity inventory does not cover every terminal-ambiguity "
                "training_gold record"
            )
        if len(observed_training_gold_ids) != len(d5_gold_records):
            d5_violations.append("D5 contains duplicate training_gold bindings")
        if len(observed_ambiguity_gold_ids) != len(ambiguity_records):
            d5_violations.append("D5 ambiguity inventory contains duplicate gold bindings")
        training_gold_by_id = {
            record.record_id: record for record in training_gold_manifest.records
        }
        for ambiguity in ambiguity_records:
            gold = training_gold_by_id.get(ambiguity.training_gold_record_id)
            if (
                gold is None
                or gold.target_id != ambiguity.target_id
                or gold.resolved_label_id != ambiguity.resolved_label_id
                or not gold.ambiguity_head_eligible
            ):
                d5_violations.append(
                    f"{ambiguity.record_id}: invalid ambiguity-only D5 lineage/weight"
                )
    for record in d5_records:
        expected_weight = (
            policy.d5.human_gold_loss_weight if record.training_gold_record_id is not None else 1.0
        )
        observed_weight = record.arm_loss_weights.get("D5", 1.0)
        if not math.isclose(observed_weight, expected_weight):
            d5_violations.append(
                f"{record.pair_id}: D5 weight {observed_weight} != {expected_weight}"
            )
        if "D5" in record.ancestry_oversampled_arms:
            d5_violations.append(f"{record.pair_id}: D5 ancestry oversampling is enabled")
    for record in effective_records:
        for raw_arm in record.arm_memberships:
            expected_raw = (
                policy.d5.human_gold_loss_weight
                if raw_arm == "D5" and record.training_gold_record_id is not None
                else 1.0
            )
            if not math.isclose(
                record.arm_loss_weights.get(raw_arm, 1.0),
                expected_raw,
            ):
                d5_violations.append(
                    f"{record.pair_id}: {raw_arm} raw loss weight is not preregistered"
                )
    for audit_arm in _ARMS:
        by_component: dict[str, list[TrainingAuditRecord]] = {}
        for record in effective_records:
            if audit_arm in record.arm_memberships:
                by_component.setdefault(record.split_component_id, []).append(record)
        ambiguity_by_component: dict[str, list[AmbiguityTrainingRecord]] = {}
        if audit_arm == "D5":
            for ambiguity in ambiguity_records:
                ambiguity_by_component.setdefault(
                    ambiguity.split_component_id,
                    [],
                ).append(ambiguity)
            for component in ambiguity_by_component:
                by_component.setdefault(component, [])
        for component, component_records in by_component.items():
            raw_total = sum(
                record.arm_loss_weights.get(audit_arm, 1.0) for record in component_records
            ) + sum(
                ambiguity.raw_human_gold_loss_weight
                for ambiguity in ambiguity_by_component.get(component, [])
            )
            observed_total = sum(
                record.normalized_arm_loss_weights[audit_arm] for record in component_records
            ) + sum(
                ambiguity.normalized_component_weight
                for ambiguity in ambiguity_by_component.get(component, [])
            )
            if not math.isclose(observed_total, 1.0):
                d5_violations.append(
                    f"{component}: {audit_arm} ancestry-normalized weights do not sum to one"
                )
            for record in component_records:
                expected_normalized = record.arm_loss_weights.get(audit_arm, 1.0) / raw_total
                if not math.isclose(
                    record.normalized_arm_loss_weights[audit_arm],
                    expected_normalized,
                ):
                    d5_violations.append(
                        f"{record.pair_id}: {audit_arm} normalized loss weight is incorrect"
                    )
            for ambiguity in ambiguity_by_component.get(component, []):
                expected_normalized = ambiguity.raw_human_gold_loss_weight / raw_total
                if not math.isclose(
                    ambiguity.normalized_component_weight,
                    expected_normalized,
                ):
                    d5_violations.append(
                        f"{ambiguity.record_id}: D5 normalized ambiguity weight is incorrect"
                    )
    if d5_violations:
        blockers.append(
            ReadinessBlocker(
                code="D5_HUMAN_GOLD_CONTRACT_INVALID",
                message=(
                    "D5 must include training_gold at loss weight 2 with no ancestry oversampling."
                ),
                observed="; ".join(d5_violations[:8]),
                required=(
                    "training_gold present; human-gold D5 loss weight 2; ordinary D5 "
                    "weight 1; ancestry oversampling disabled"
                ),
            )
        )

    arm_results: list[ArmReadiness] = []
    positive_sets: dict[str, set[str]] = {}
    arm_target_counts = {
        candidate_arm: sum(candidate_arm in record.arm_memberships for record in effective_records)
        for candidate_arm in _ARMS
    }
    if reduced_data_ablation:
        nonzero_counts = [count for count in arm_target_counts.values() if count > 0]
        target = min(nonzero_counts) if nonzero_counts else 0
        if target % 2:
            target -= 1
    else:
        target = policy.pilot.confirmatory_pair_count
    for audit_arm in _ARMS:
        arm_records = [
            record for record in effective_records if audit_arm in record.arm_memberships
        ]
        positives = [record for record in arm_records if record.same_claim]
        negatives = [record for record in arm_records if not record.same_claim]
        positive_sets[audit_arm] = {record.pair_id for record in positives}
        component_counts = Counter(record.split_component_id for record in arm_records)
        cap_violations = sum(
            count > policy.pilot.maximum_unique_variants_per_component_per_arm
            for count in component_counts.values()
        )
        class_balance_ok = (
            len(arm_records) == target
            and len(positives) == target // 2
            and len(negatives) == target // 2
        )
        negative_counts = Counter(
            record.negative_source for record in negatives if record.negative_source
        )
        expected_negative = _allocation(
            target // 2,
            policy.negative_arms[audit_arm],
        )
        source_mix_ok = dict(negative_counts) == expected_negative
        expected_positive = _allocation(target // 2, policy.full_arm_positive_mix)
        positive_counts = Counter(
            record.positive_source for record in positives if record.positive_source
        )
        positive_mix_ok = dict(positive_counts) == expected_positive
        family_ok = _family_controls_ok(
            arm_records,
            arm=audit_arm,
            negative_total=target // 2,
            policy=policy,
        )
        ready = (
            target > 0
            and class_balance_ok
            and source_mix_ok
            and positive_mix_ok
            and family_ok
            and cap_violations == 0
        )
        arm_results.append(
            ArmReadiness(
                arm=audit_arm,
                selected_pair_count=len(arm_records),
                positive_count=len(positives),
                negative_count=len(negatives),
                negative_source_counts={
                    source: sum(record.negative_source == source for record in negatives)
                    for source in _NEGATIVE_SOURCES
                },
                component_cap_violations=cap_violations,
                source_mix_ok=source_mix_ok,
                class_balance_ok=class_balance_ok,
                family_controls_ok=family_ok,
                positive_pool_ok=positive_mix_ok,
                ready=ready,
            )
        )
    positive_pool_identical = (
        bool(positive_sets["D0"])
        and len({frozenset(values) for values in positive_sets.values()}) == 1
    )
    if inventory_present and (
        any(not result.ready for result in arm_results) or not positive_pool_identical
    ):
        blockers.append(
            ReadinessBlocker(
                code="PILOT_ARM_REQUIREMENTS_NOT_MET",
                message=(
                    "One or more D0-D5 selections fail pair count, 50/50 balance, "
                    "source mixture, family caps, ancestry cap, or common-positive-pool rules."
                ),
                observed=(
                    ", ".join(result.arm for result in arm_results if not result.ready)
                    or "positive pools differ"
                ),
                required=(f"{target} pairs per arm under the frozen training-data contract"),
            )
        )

    required_products_present = len(human_manifests) == len(_HUMAN_PRODUCTS)
    selection_minima_ok = not any(
        blocker.code == "SELECTION_GOLD_MINIMA_NOT_MET" for blocker in blockers
    )
    human_admission_ready = (
        policy.artifact_class == "test_fixture" or policy.human_gold_admission_enabled
    )
    common_integrity = (
        inventory_present
        and not parse_errors
        and not ambiguity_parse_errors
        and not duplicate_pair_ids
        and unsafe_count == 0
        and not missing_lf022
        and not invalid_lf022
        and generator_holdout_present
        and generator_holdout_valid
        and lineage_present
        and lineage_valid
        and all(result.ready for result in arm_results)
        and positive_pool_identical
        and required_products_present
        and human_disjoint
        and training_vs_nontraining_disjoint
        and selection_minima_ok
        and not d5_violations
        and human_admission_ready
    )
    confirmatory_ready = common_integrity and target == policy.pilot.confirmatory_pair_count
    reduced_ready = (
        common_integrity
        and reduced_data_ablation
        and target >= policy.pilot.reduced_data_minimum_pair_count
        and target < policy.pilot.confirmatory_pair_count
    )
    return TrainingReadiness(
        inventory_present=inventory_present,
        inventory_sha256=hash_file(inventory_path) if inventory_present else None,
        inventory_record_count=len(records),
        effective_nonduplicate_record_count=len(effective_records),
        safe_f1_label_count=len(safe_records),
        unsafe_f1_label_count=unsafe_count,
        forbidden_label_basis_counts=dict(sorted(forbidden_counts.items())),
        unknown_label_basis_counts=dict(sorted(unknown_counts.items())),
        lf022_artifacts_present=not missing_lf022 and not invalid_lf022,
        missing_lf022_artifacts=missing_lf022,
        invalid_lf022_artifacts=invalid_lf022,
        lf022_artifact_sha256s=lf022_artifact_sha256s,
        generator_holdout_manifest_present=generator_holdout_present,
        generator_holdout_manifest_valid=generator_holdout_valid,
        generator_holdout_manifest_sha256=generator_holdout_sha256,
        heldout_generator_family=heldout_generator_family,
        lineage_artifacts_present=lineage_present,
        lineage_integrity_valid=lineage_valid,
        lineage_error_count=lineage_error_count,
        lineage_artifact_sha256s=lineage_artifact_sha256s,
        arm_results=tuple(arm_results),
        positive_pool_identical_across_arms=positive_pool_identical,
        human_products=human_results,
        human_partitions_component_disjoint=human_disjoint,
        training_vs_nontraining_gold_disjoint=training_vs_nontraining_disjoint,
        confirmatory_ready=confirmatory_ready,
        reduced_data_ablation_ready=reduced_ready,
    )


def audit_training_data_readiness(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[TrainingDataReadinessPolicy],
    reduced_data_ablation: bool = False,
) -> TrainingDataReadinessReport:
    """Audit current immutable artifacts without training or synthesizing labels."""
    blockers: list[ReadinessBlocker] = []
    if (
        loaded_policy.config.artifact_class == "production"
        and not loaded_policy.config.human_gold_admission_enabled
    ):
        blockers.append(
            ReadinessBlocker(
                code="HUMAN_GOLD_ADMISSION_DISABLED",
                message=(
                    "Current operator attestations authenticate integrity only and "
                    "cannot admit human-gold labels."
                ),
                observed=(
                    "origin_assurance=operator_attested, backend_origin_verified=false, "
                    "human_gold_eligible=false"
                ),
                required=(
                    "a future registered backend adapter and independently verified "
                    "backend-origin trust record under a revised admission policy"
                ),
            )
        )
    prevalence = _audit_prevalence(
        repo_root=repo_root,
        policy=loaded_policy.config,
        blockers=blockers,
    )
    training = _audit_training(
        repo_root=repo_root,
        policy=loaded_policy.config,
        reduced_data_ablation=reduced_data_ablation,
        blockers=blockers,
    )
    if reduced_data_ablation and not training.reduced_data_ablation_ready:
        blockers.append(
            ReadinessBlocker(
                code="REDUCED_DATA_ABLATION_NOT_READY",
                message="Explicit reduced-data mode does not waive integrity or gold-split rules.",
                observed="reduced-data readiness requirements failed",
                required="all non-scale requirements plus a balanced reduced D0-D5 inventory",
            )
        )
    if not reduced_data_ablation and not training.confirmatory_ready:
        blockers.append(
            ReadinessBlocker(
                code="CONFIRMATORY_TRAINING_NOT_READY",
                message="The preregistered 50,000-pair confirmatory pilot cannot start.",
                observed=(
                    f"{training.effective_nonduplicate_record_count} effective "
                    "training inventory records"
                ),
                required=(
                    f"{loaded_policy.config.pilot.confirmatory_pair_count} "
                    "ancestry-controlled pairs per D0-D5 arm"
                ),
            )
        )
    blockers = sorted(
        {hash_canonical(blocker.model_dump(mode="json")): blocker for blocker in blockers}.values(),
        key=lambda blocker: (blocker.code, blocker.observed, blocker.required),
    )
    mode: Literal["confirmatory", "reduced_data_ablation"] = (
        "reduced_data_ablation" if reduced_data_ablation else "confirmatory"
    )
    status: Literal[
        "READY",
        "READY_REDUCED_DATA_ABLATION",
        "READY_TEST_FIXTURE",
        "READY_REDUCED_DATA_TEST_FIXTURE",
        "NOT_READY",
    ]
    if not blockers and training.confirmatory_ready and not reduced_data_ablation:
        status = (
            "READY" if loaded_policy.config.artifact_class == "production" else "READY_TEST_FIXTURE"
        )
    elif not blockers and training.reduced_data_ablation_ready and reduced_data_ablation:
        status = (
            "READY_REDUCED_DATA_ABLATION"
            if loaded_policy.config.artifact_class == "production"
            else "READY_REDUCED_DATA_TEST_FIXTURE"
        )
    else:
        status = "NOT_READY"
    payload = {
        "schema_version": 1,
        "policy_id": loaded_policy.config.policy_id,
        "policy_sha256": loaded_policy.config_hash,
        "artifact_class": loaded_policy.config.artifact_class,
        "mode": mode,
        "status": status,
        "prevalence": prevalence.model_dump(mode="json"),
        "training": training.model_dump(mode="json"),
        "blockers": [blocker.model_dump(mode="json") for blocker in blockers],
        "training_execution_authorized": status in {"READY", "READY_REDUCED_DATA_ABLATION"},
        "model_execution_performed": False,
        "semantic_labels_created": False,
        "semantic_labels_inferred_from_compilation": False,
        "semantic_labels_inferred_from_proof_search": False,
        "semantic_labels_inferred_from_llm_agreement": False,
    }
    return TrainingDataReadinessReport.model_validate(
        {
            **payload,
            "audit_id": "training_data_readiness_v1:" + hash_canonical(payload),
        }
    )


def render_training_data_readiness_markdown(
    report: TrainingDataReadinessReport,
) -> str:
    """Render a deterministic, concise human-readable companion report."""
    lines = [
        "# Training-data readiness audit v1",
        "",
        f"**Status:** `{report.status}`  ",
        f"**Mode:** `{report.mode}`  ",
        f"**Audit ID:** `{report.audit_id}`",
        "",
        "## Separation of claims",
        "",
        (
            f"- Prevalence frame adequate for human annotation: "
            f"**{str(report.prevalence.frame_adequate_for_annotation).upper()}** "
            f"({report.prevalence.frame_item_count} items, "
            f"{len(report.prevalence.generator_family_counts)} generator families)."
        ),
        (
            f"- Human terminal prevalence labels: "
            f"**{report.prevalence.human_terminal_label_count}**; prevalence estimate ready: "
            f"**{str(report.prevalence.prevalence_estimate_ready).upper()}**."
        ),
        (
            f"- Confirmatory flagship training/model selection ready: "
            f"**{str(report.training.confirmatory_ready).upper()}**."
        ),
        (
            f"- Reduced-data ablation ready: "
            f"**{str(report.training.reduced_data_ablation_ready).upper()}**."
        ),
        "",
        "Mechanical compilation and Gate 5G admission are not semantic labels and "
        "are not counted as training supervision.",
        "",
        "## Current inventory",
        "",
        (
            "- Effective nonduplicate training records: "
            f"{report.training.effective_nonduplicate_record_count}"
        ),
        f"- Safe F1 labels: {report.training.safe_f1_label_count}",
        f"- Unsafe F1 labels: {report.training.unsafe_f1_label_count}",
        (
            "- LF-022 SCI/open artifacts present: "
            f"{str(report.training.lf022_artifacts_present).upper()}"
        ),
        (
            "- Generator holdout manifest valid: "
            f"{str(report.training.generator_holdout_manifest_valid).upper()}"
        ),
        (
            "- Human gold products present: "
            f"{sum(item.present for item in report.training.human_products)}/4"
        ),
        "",
        "## Blockers",
        "",
    ]
    if report.blockers:
        for blocker in report.blockers:
            lines.append(
                f"- `{blocker.code}` — {blocker.message} "
                f"Observed: {blocker.observed}. Required: {blocker.required}."
            )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Safety boundary",
            "",
            "- Model execution performed: false",
            "- Semantic labels created by this audit: false",
            "- Compilation-derived F1 labels: false",
            "- Proof-search-derived F1 labels: false",
            "- LLM-agreement-derived F1 labels: false",
            "",
        ]
    )
    return "\n".join(lines)


def write_training_data_readiness_reports(
    report: TrainingDataReadinessReport,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Write deterministic current-state reports (no timestamps, no hidden state)."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(canonical_json_bytes(report.model_dump(mode="json")) + b"\n")
    markdown_path.write_text(
        render_training_data_readiness_markdown(report),
        encoding="utf-8",
    )


def report_input_hashes(
    *,
    repo_root: Path,
    policy: TrainingDataReadinessPolicy,
) -> dict[str, str | None]:
    """Return deterministic hashes of declared inputs for external manifests/tests."""
    paths: list[tuple[str, str]] = [
        ("prevalence_frame", policy.inputs.prevalence_frame),
        ("prevalence_human_labels", policy.inputs.prevalence_human_labels),
        ("training_inventory", policy.inputs.training_inventory),
        ("training_ambiguity_inventory", policy.inputs.training_ambiguity_inventory),
        ("generator_holdout_manifest", policy.inputs.generator_holdout_manifest),
        *sorted(policy.inputs.human_products.items()),
        *(
            (f"lf022_{source}", path)
            for source, path in sorted(policy.inputs.lf022_required_artifacts.items())
        ),
        ("lineage_theorem_records", policy.inputs.lineage.theorem_records),
        ("lineage_pair_records", policy.inputs.lineage.pair_records),
        ("lineage_resolved_labels", policy.inputs.lineage.resolved_labels),
        ("lineage_evidence_records", policy.inputs.lineage.evidence_records),
        ("lineage_promotion_decisions", policy.inputs.lineage.promotion_decisions),
        ("lineage_annotation_records", policy.inputs.lineage.annotation_records),
        ("lineage_adjudication_records", policy.inputs.lineage.adjudication_records),
        ("lineage_split_assignments", policy.inputs.lineage.split_assignments),
        *(
            (f"lineage_source_manifest_{index}", path)
            for index, path in enumerate(policy.inputs.lineage.source_manifests)
        ),
    ]
    return {
        name: hash_file(repo_root / relative) if (repo_root / relative).is_file() else None
        for name, relative in paths
    }


__all__ = [
    "AmbiguityTrainingRecord",
    "GeneratorHoldoutManifest",
    "GoldGroupRecord",
    "GoldPartitionManifest",
    "HumanAdjudicationRecord",
    "PrevalenceHumanLabelRecord",
    "SplitAssignmentRecord",
    "StatisticalAdequacyAssessment",
    "TrainingAuditRecord",
    "TrainingDataReadinessPolicy",
    "TrainingDataReadinessReport",
    "audit_training_data_readiness",
    "load_training_data_readiness_policy",
    "render_training_data_readiness_markdown",
    "report_input_hashes",
    "write_training_data_readiness_reports",
]

"""Fail-closed, non-training audit for scientific training-data readiness.

This module deliberately separates three statements that are easy to conflate:

* a frozen real-output frame is large/diverse enough to annotate;
* enough of that frame has genuine human terminal labels to estimate prevalence;
* a complete, ancestry-disjoint corpus exists for the preregistered model pilot.

Compilation, proof search, type correctness, and LLM agreement are never accepted
as F1 labels.  The audit reads metadata only and never trains or executes a model.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel

_NEGATIVE_SOURCES = ("G_rule", "G_sci", "G_open", "G_real")
_ARMS = ("D0", "D1", "D2", "D3", "D4", "D5")
_HUMAN_PRODUCTS = (
    "training_gold",
    "selection_gold",
    "calibration_gold",
    "final_human_test",
)
_RELATIONS = (
    "equivalent",
    "A_stronger",
    "B_stronger",
    "incomparable",
    "unrelated",
    "ambiguous",
)


def _relative_repo_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError("artifact paths must be nonempty repository-relative paths")
    return value


class ReadinessInputs(StrictModel):
    prevalence_frame: str
    prevalence_human_labels: str
    training_inventory: str
    human_products: dict[str, str]
    lf022_required_artifacts: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "prevalence_frame",
        "prevalence_human_labels",
        "training_inventory",
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
    def _lf022_paths_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_repo_path(path) for path in value)


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
    inputs: ReadinessInputs
    prevalence: PrevalenceRequirements
    pilot: PilotRequirements
    selection_gold_minimum_groups: SelectionGoldRequirements
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
        return self


class PrevalenceHumanLabelRecord(StrictModel):
    """Adjudicated human label bound to one immutable prevalence-frame row."""

    schema_version: Literal[1]
    frame_record_id: str = Field(min_length=1)
    adjudicated: Literal[True]
    label_basis: Literal["human_adjudication"]
    same_claim: bool | None
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
        return self


class GoldGroupRecord(StrictModel):
    record_id: str = Field(min_length=1)
    split_component_id: str = Field(min_length=1)
    adjudicated: bool
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
        if self.same_claim is True and self.relation != "equivalent":
            raise ValueError("faithful gold group must use relation=equivalent")
        if self.same_claim is False and self.relation in (None, "equivalent", "ambiguous"):
            raise ValueError("unfaithful gold group requires a non-equivalent terminal relation")
        if self.relation == "ambiguous" and self.same_claim is not None:
            raise ValueError("ambiguous relation requires same_claim=null")
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
    records: tuple[GoldGroupRecord, ...] = Field(min_length=1)

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
            if any(
                record.same_claim is not None or record.relation is not None or record.label_bases
                for record in self.records
            ):
                raise ValueError("sealed final_human_test readiness records cannot expose labels")
        elif not self.labels_exposed_to_audit:
            raise ValueError("non-final gold readiness manifests must expose audit labels")
        if self.partition == "calibration_gold" and self.distribution != "compiling_real_outputs":
            raise ValueError("calibration_gold must use compiling_real_outputs")
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
    mode: Literal["confirmatory", "reduced_data_ablation"]
    status: Literal["READY", "READY_REDUCED_DATA_ABLATION", "NOT_READY"]
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
        expected = (
            "READY"
            if self.mode == "confirmatory" and self.training.confirmatory_ready
            else (
                "READY_REDUCED_DATA_ABLATION"
                if self.mode == "reduced_data_ablation"
                and self.training.reduced_data_ablation_ready
                else "NOT_READY"
            )
        )
        if self.status != expected:
            raise ValueError("readiness status does not match audited training state")
        if self.training_execution_authorized != (self.status != "NOT_READY"):
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
        human_terminal = sum(
            label.same_claim is not None and label.frame_record_id in frame_record_ids
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
        if not valid:
            blockers.append(
                ReadinessBlocker(
                    code=f"{product.upper()}_INVALID",
                    message=f"The {product} readiness manifest fails its contract.",
                    observed=error or "partition or human-label basis mismatch",
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
    missing_lf022 = tuple(
        path for path in policy.inputs.lf022_required_artifacts if not (repo_root / path).is_file()
    )
    if missing_lf022:
        blockers.append(
            ReadinessBlocker(
                code="LF022_ARTIFACTS_MISSING",
                message="SCI-conditioned/open-ended LF-022 data do not yet exist.",
                observed=", ".join(missing_lf022),
                required="all registered LF-022 readiness artifacts",
            )
        )

    human_results, human_manifests = _audit_human_products(
        repo_root=repo_root,
        policy=policy,
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
        for relation, count in sorted(relation_counts.items()):
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
                        "per included confirmatory relation"
                    ),
                )
            )

    arm_results: list[ArmReadiness] = []
    positive_sets: dict[str, set[str]] = {}
    arm_target_counts = {
        arm: sum(arm in record.arm_memberships for record in effective_records) for arm in _ARMS
    }
    if reduced_data_ablation:
        nonzero_counts = [count for count in arm_target_counts.values() if count > 0]
        target = min(nonzero_counts) if nonzero_counts else 0
        if target % 2:
            target -= 1
    else:
        target = policy.pilot.confirmatory_pair_count
    for arm in _ARMS:
        arm_records = [record for record in effective_records if arm in record.arm_memberships]
        positives = [record for record in arm_records if record.same_claim]
        negatives = [record for record in arm_records if not record.same_claim]
        positive_sets[arm] = {record.pair_id for record in positives}
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
        expected_negative = _allocation(target // 2, policy.negative_arms[arm])
        source_mix_ok = dict(negative_counts) == expected_negative
        expected_positive = _allocation(target // 2, policy.full_arm_positive_mix)
        positive_counts = Counter(
            record.positive_source for record in positives if record.positive_source
        )
        positive_mix_ok = dict(positive_counts) == expected_positive
        family_ok = _family_controls_ok(
            arm_records,
            arm=arm,
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
                arm=arm,
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
    common_integrity = (
        inventory_present
        and not parse_errors
        and not duplicate_pair_ids
        and unsafe_count == 0
        and not missing_lf022
        and all(result.ready for result in arm_results)
        and positive_pool_identical
        and required_products_present
        and human_disjoint
        and training_vs_nontraining_disjoint
        and selection_minima_ok
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
        lf022_artifacts_present=not missing_lf022,
        missing_lf022_artifacts=missing_lf022,
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
    status: Literal["READY", "READY_REDUCED_DATA_ABLATION", "NOT_READY"]
    if not blockers and training.confirmatory_ready and not reduced_data_ablation:
        status = "READY"
    elif not blockers and training.reduced_data_ablation_ready and reduced_data_ablation:
        status = "READY_REDUCED_DATA_ABLATION"
    else:
        status = "NOT_READY"
    payload = {
        "schema_version": 1,
        "policy_id": loaded_policy.config.policy_id,
        "policy_sha256": loaded_policy.config_hash,
        "mode": mode,
        "status": status,
        "prevalence": prevalence.model_dump(mode="json"),
        "training": training.model_dump(mode="json"),
        "blockers": [blocker.model_dump(mode="json") for blocker in blockers],
        "training_execution_authorized": status != "NOT_READY",
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
        *sorted(policy.inputs.human_products.items()),
        *(
            (f"lf022_{index}", path)
            for index, path in enumerate(policy.inputs.lf022_required_artifacts)
        ),
    ]
    return {
        name: hash_file(repo_root / relative) if (repo_root / relative).is_file() else None
        for name, relative in paths
    }


__all__ = [
    "GoldGroupRecord",
    "GoldPartitionManifest",
    "TrainingAuditRecord",
    "TrainingDataReadinessPolicy",
    "TrainingDataReadinessReport",
    "audit_training_data_readiness",
    "load_training_data_readiness_policy",
    "render_training_data_readiness_markdown",
    "report_input_hashes",
    "write_training_data_readiness_reports",
]

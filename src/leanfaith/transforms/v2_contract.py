"""Strict LF-031 design contract for the additive deterministic-v2 portfolio.

LF-031 is deliberately non-executable.  This module validates the complete
P05--P17/N11--N17 design inventory, its evidence classes, overlap ownership,
mechanism holdouts, and its immutable binding to the accepted v1 registry.
It contains no transformation rule factory and cannot emit a ``VariantDraft``
or a semantic label.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import IntendedRelation, Polarity

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
FamilyId = Annotated[str, Field(pattern=r"^[pn][0-9]{2}_[a-z0-9_]+$", strict=True)]
MechanismId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]+$", strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]
ErrorCode = Annotated[
    str,
    Field(pattern=r"^E(0[1-9]|[12][0-9]|30)$", strict=True),
]


class V2ContractError(ValueError):
    """The LF-031 portfolio or its immutable v1 binding is invalid."""


class V2EvidenceClass(StrEnum):
    """PLAN.md section 15.11 mechanical evidence classes; never labels."""

    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    D0 = "D0"


class V2CoverageDetector(StrEnum):
    """Read-only upper-bound signals used by the LF-031 coverage probe."""

    QUALIFIED_IDENTIFIER = "qualified_identifier"
    EXPLICIT_APPLICATION = "explicit_application"
    COERCION_CONSTANT = "coercion_constant"
    TYPE_ASCRIPTION = "type_ascription"
    PROJECTION_EXPR = "projection_expr"
    CONSTRUCTOR_SURFACE = "constructor_surface"
    BOUNDED_QUANTIFIER = "bounded_quantifier"
    PROOF_ARROW = "proof_arrow"
    ETA_SURFACE = "eta_surface"
    ADJACENT_EXPLICIT_BINDERS = "adjacent_explicit_binders"
    IFF_SURFACE = "iff_surface"
    CONJUNCTION_CHAIN = "conjunction_chain"
    MULTIPLE_PROPOSITIONAL_HYPOTHESES = "multiple_propositional_hypotheses"
    SAME_TYPED_BINDERS = "same_typed_binders"
    IMPLICATION_SURFACE = "implication_surface"
    FORALL_EXISTS_NESTING = "forall_exists_nesting"
    NEGATION_QUANTIFIER = "negation_quantifier"
    CONJUNCTION_SURFACE = "conjunction_surface"
    BOUNDED_GUARD = "bounded_guard"
    ROLE_ARGUMENT_SLOTS = "role_argument_slots"


EXPECTED_V2_FAMILY_IDS: tuple[str, ...] = (
    "n11_bound_variable_substitution",
    "n12_implication_converse",
    "n13_witness_dependency",
    "n14_negation_scope",
    "n15_conjunct_omission",
    "n16_domain_guard_removal",
    "n17_role_sensitive_arguments",
    "p05_resolved_names",
    "p06_implicit_arguments",
    "p07_coercion_surface",
    "p08_type_ascriptions",
    "p09_projections",
    "p10_constructors",
    "p11_bounded_quantifiers",
    "p12_proof_arrow_binder",
    "p13_restricted_eta",
    "p14_independent_binder_permutation",
    "p15_root_iff_reversal",
    "p16_conjunction_reassociation",
    "p17_hypothesis_packing",
)

_EXPECTED_EVIDENCE: dict[str, V2EvidenceClass] = {
    **{f"p{index:02d}": V2EvidenceClass.E0 for index in range(5, 13)},
    "p13": V2EvidenceClass.E1,
    **{f"p{index:02d}": V2EvidenceClass.E2 for index in range(14, 18)},
    **{f"n{index:02d}": V2EvidenceClass.D0 for index in range(11, 18)},
}

EXPECTED_HOLDOUT_PARTITION: dict[str, tuple[str, ...]] = {
    "connective_scope_delta": (
        "n12_implication_converse",
        "n14_negation_scope",
        "n15_conjunct_omission",
        "n16_domain_guard_removal",
    ),
    "local_eta": ("p13_restricted_eta",),
    "proposition_packaging": (
        "p14_independent_binder_permutation",
        "p15_root_iff_reversal",
        "p16_conjunction_reassociation",
        "p17_hypothesis_packing",
    ),
    "quantifier_dependency_delta": ("n13_witness_dependency",),
    "surface_presentation": (
        "p05_resolved_names",
        "p06_implicit_arguments",
        "p07_coercion_surface",
        "p08_type_ascriptions",
        "p09_projections",
        "p10_constructors",
        "p11_bounded_quantifiers",
        "p12_proof_arrow_binder",
    ),
    "variable_role_delta": (
        "n11_bound_variable_substitution",
        "n17_role_sensitive_arguments",
    ),
}

EXPECTED_OVERLAP_OWNERS: dict[str, str] = {
    "bounded_quantifier_presentation": "p11_bounded_quantifiers",
    "bound_variable_occurrence": "n11_bound_variable_substitution",
    "conjunction_reassociation": "p16_conjunction_reassociation",
    "conjunct_omission": "n15_conjunct_omission",
    "constructor_surface": "p10_constructors",
    "domain_guard_removal": "n16_domain_guard_removal",
    "elaborated_coercion_hop": "p07_coercion_surface",
    "projection_surface": "p09_projections",
    "proof_arrow_presentation": "p12_proof_arrow_binder",
    "role_sensitive_argument_slot": "n17_role_sensitive_arguments",
    "root_iff_reversal": "p15_root_iff_reversal",
    "root_implication_converse": "n12_implication_converse",
    "syntactic_type_ascription": "p08_type_ascriptions",
}

EXPECTED_OVERLAP_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "bound_variable_occurrence": ("n17_role_sensitive_arguments",),
    "bounded_quantifier_presentation": ("n16_domain_guard_removal",),
    "conjunct_omission": ("p16_conjunction_reassociation",),
    "conjunction_reassociation": ("n15_conjunct_omission",),
    "constructor_surface": ("p09_projections",),
    "domain_guard_removal": (
        "n15_conjunct_omission",
        "p11_bounded_quantifiers",
    ),
    "elaborated_coercion_hop": ("p08_type_ascriptions",),
    "projection_surface": (
        "p07_coercion_surface",
        "p10_constructors",
    ),
    "proof_arrow_presentation": (
        "n12_implication_converse",
        "p17_hypothesis_packing",
    ),
    "role_sensitive_argument_slot": ("n11_bound_variable_substitution",),
    "root_iff_reversal": ("n12_implication_converse",),
    "root_implication_converse": ("p15_root_iff_reversal",),
    "syntactic_type_ascription": ("p07_coercion_surface",),
}


def _family_number(family_id: str) -> str:
    return family_id.split("_", 1)[0]


class V1ImmutableBinding(StrictModel):
    """Byte and effective-hash binding to the accepted v1 decision."""

    registry_path: Literal["configs/transformations/registry.yaml"]
    registry_sha256: Sha256
    profile_path: Literal["configs/transformations/v1.yaml"]
    profile_sha256: Sha256
    promotion_policy_path: Literal["policies/transformation_promotion_v1.yaml"]
    promotion_policy_sha256: Sha256
    effective_registry_hash: Sha256


class V2FamilyDesign(StrictModel):
    """One reserved, disabled family design.  It is not a runtime rule."""

    family_id: FamilyId
    family_version: SemanticVersion
    polarity: Polarity
    status: Literal["disabled"] = "disabled"
    implementation_status: Literal["design_only"] = "design_only"
    evidence_class: V2EvidenceClass
    intended_relation: IntendedRelation
    intended_error_types: tuple[ErrorCode, ...] = ()
    detector: V2CoverageDetector
    narrow_scope: NonEmptyStr
    excluded_cases: tuple[NonEmptyStr, ...] = Field(min_length=1)
    executable: Literal[False] = False
    draft_emission_authorized: Literal[False] = False
    label_emission_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _closed_design(self) -> V2FamilyDesign:
        if self.excluded_cases != tuple(sorted(set(self.excluded_cases))):
            raise ValueError("excluded_cases must be sorted and unique")
        prefix = _family_number(self.family_id)
        expected_evidence = _EXPECTED_EVIDENCE.get(prefix)
        if expected_evidence is None or self.evidence_class != expected_evidence:
            raise ValueError(
                f"{self.family_id} must use evidence class "
                f"{expected_evidence.value if expected_evidence else 'unknown'}"
            )
        expected_polarity = Polarity.POSITIVE if prefix.startswith("p") else Polarity.NEGATIVE
        if self.polarity != expected_polarity:
            raise ValueError(f"{self.family_id} has the wrong polarity")
        expected_intention = (
            IntendedRelation.EQUIVALENT
            if self.polarity == Polarity.POSITIVE
            else IntendedRelation.NEAR_MISS
        )
        if self.intended_relation != expected_intention:
            raise ValueError(f"{self.family_id} has the wrong generation intention")
        if self.polarity == Polarity.POSITIVE and self.intended_error_types:
            raise ValueError("positive v2 families cannot declare intended error types")
        if self.polarity == Polarity.NEGATIVE and not self.intended_error_types:
            raise ValueError("negative v2 families require at least one intended E-code")
        if self.intended_error_types != tuple(sorted(set(self.intended_error_types))):
            raise ValueError("intended_error_types must be sorted and unique")
        return self


class V2OverlapOwnership(StrictModel):
    """One mutually exclusive mechanism owner and its denied competitors."""

    mechanism_id: MechanismId
    owner_family_id: FamilyId
    excluded_family_ids: tuple[FamilyId, ...] = Field(min_length=1)
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def _canonical(self) -> V2OverlapOwnership:
        if self.excluded_family_ids != tuple(sorted(set(self.excluded_family_ids))):
            raise ValueError("excluded_family_ids must be sorted and unique")
        if self.owner_family_id in self.excluded_family_ids:
            raise ValueError("overlap owner cannot also be excluded")
        return self


class V2MechanismHoldout(StrictModel):
    """A complete family superclass held out as one evaluation mechanism."""

    mechanism_superclass: MechanismId
    family_ids: tuple[FamilyId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical(self) -> V2MechanismHoldout:
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("holdout family_ids must be sorted and unique")
        return self


class V2CoverageProbePolicy(StrictModel):
    """A design-only signal probe; signals are never applicability evidence."""

    detector_version: Literal["v2_coverage_signals_v1"]
    interpretation: Literal["upper_bound_signal_not_applicability"]
    required_views: tuple[Literal["headless", "raw_proof_stripped"], ...]
    probe_only: Literal[True] = True
    executes_lean: Literal[False] = False
    mutates_input_data: Literal[False] = False
    emits_drafts: Literal[False] = False
    emits_labels: Literal[False] = False

    @model_validator(mode="after")
    def _views(self) -> V2CoverageProbePolicy:
        if self.required_views != ("headless", "raw_proof_stripped"):
            raise ValueError("LF-031 coverage requires exactly headless and raw_proof_stripped")
        return self


class DeterministicV2PortfolioConfig(StrictModel):
    """Complete disabled LF-031 portfolio loaded from ``v2.yaml``."""

    schema_version: Literal[1] = 1
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_version: SemanticVersion
    status: Literal["design_only"]
    accepted_v1: V1ImmutableBinding
    coverage_probe: V2CoverageProbePolicy
    families: tuple[V2FamilyDesign, ...]
    overlap_ownership: tuple[V2OverlapOwnership, ...]
    mechanism_holdouts: tuple[V2MechanismHoldout, ...]
    runtime_registry_created: Literal[False] = False
    any_family_executable: Literal[False] = False
    draft_emission_authorized: Literal[False] = False
    label_emission_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _closed_portfolio(self) -> DeterministicV2PortfolioConfig:
        family_ids = tuple(family.family_id for family in self.families)
        if family_ids != EXPECTED_V2_FAMILY_IDS:
            raise ValueError("families must be the exact sorted P05-P17/N11-N17 portfolio")

        overlaps = tuple(item.mechanism_id for item in self.overlap_ownership)
        if overlaps != tuple(sorted(EXPECTED_OVERLAP_OWNERS)):
            raise ValueError("overlap_ownership must contain the exact frozen mechanisms")
        for item in self.overlap_ownership:
            if item.owner_family_id != EXPECTED_OVERLAP_OWNERS[item.mechanism_id]:
                raise ValueError(f"wrong owner for overlap mechanism {item.mechanism_id}")
            if item.excluded_family_ids != EXPECTED_OVERLAP_EXCLUSIONS[item.mechanism_id]:
                raise ValueError(f"wrong exclusions for overlap mechanism {item.mechanism_id}")
            unknown = set(item.excluded_family_ids) - set(family_ids)
            if unknown:
                raise ValueError(
                    f"overlap mechanism {item.mechanism_id} names unknown families {unknown}"
                )

        holdouts = {item.mechanism_superclass: item.family_ids for item in self.mechanism_holdouts}
        if holdouts != EXPECTED_HOLDOUT_PARTITION:
            raise ValueError("mechanism_holdouts must equal the frozen superclass partition")
        flattened = tuple(family for group in holdouts.values() for family in group)
        if set(flattened) != set(family_ids) or len(flattened) != len(set(flattened)):
            raise ValueError("mechanism holdouts must partition every v2 family exactly once")
        return self


class V2GenerationAddendum(StrictModel):
    """Future version-specific generation evidence; never a promotion record."""

    schema_version: Literal[1] = 1
    addendum_id: NonEmptyStr
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_version: SemanticVersion
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    execution_profile_id: NonEmptyStr
    execution_profile_hash: Sha256
    family_ids: tuple[FamilyId, ...] = Field(min_length=1)
    evidence_classes: dict[FamilyId, V2EvidenceClass]
    source_inventory_hash: Sha256
    coverage_report_hash: Sha256
    clean_replay_hash_a: Sha256
    clean_replay_hash_b: Sha256
    failure_accounting_hash: Sha256
    lean_validation_report_hash: Sha256
    overlap_audit_hash: Sha256
    split_audit_hash: Sha256
    denylist_audit_hash: Sha256
    emitted_draft_count: int = Field(ge=0)
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    grants_generation_credit_only: Literal[True] = True
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> V2GenerationAddendum:
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("addendum family_ids must be sorted and unique")
        if set(self.evidence_classes) != set(self.family_ids):
            raise ValueError("addendum evidence_classes must cover every family exactly")
        if self.clean_replay_hash_a != self.clean_replay_hash_b:
            raise ValueError("clean v2 generation replays must match exactly")
        return self


def load_v2_portfolio(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[DeterministicV2PortfolioConfig]:
    """Load LF-031 and fail if any accepted v1 byte or hash has changed."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2ContractError("v2 portfolio path escapes the repository")
    loaded = load_config(resolved, DeterministicV2PortfolioConfig)
    binding = loaded.config.accepted_v1
    for relative, expected in (
        (binding.registry_path, binding.registry_sha256),
        (binding.profile_path, binding.profile_sha256),
        (binding.promotion_policy_path, binding.promotion_policy_sha256),
    ):
        actual = hash_file(root / relative)
        if actual != expected:
            raise V2ContractError(
                f"accepted v1 artifact changed: {relative}; {actual} != {expected}"
            )

    # Imported lazily so the LF-031 schema remains independent of runtime rule
    # construction and can never accidentally register a family.
    from leanfaith.transforms.registry import load_transformation_registry

    effective = load_transformation_registry(root)
    if effective.registry_hash != binding.effective_registry_hash:
        raise V2ContractError(
            "accepted v1 effective registry hash changed: "
            f"{effective.registry_hash} != {binding.effective_registry_hash}"
        )
    return loaded


__all__ = [
    "EXPECTED_HOLDOUT_PARTITION",
    "EXPECTED_OVERLAP_EXCLUSIONS",
    "EXPECTED_OVERLAP_OWNERS",
    "EXPECTED_V2_FAMILY_IDS",
    "DeterministicV2PortfolioConfig",
    "V1ImmutableBinding",
    "V2ContractError",
    "V2CoverageDetector",
    "V2CoverageProbePolicy",
    "V2EvidenceClass",
    "V2FamilyDesign",
    "V2GenerationAddendum",
    "V2MechanismHoldout",
    "V2OverlapOwnership",
    "load_v2_portfolio",
]

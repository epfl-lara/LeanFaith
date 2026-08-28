"""Separate experimental runtime for conservative deterministic-v2 E0 slices.

Every executable profile is exact and code-owned.  None modifies the accepted
v1 registry or can resolve labels, promote families, or mark output
training-eligible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import TransformationAudit, VariantDraft
from leanfaith.transforms.protocol import (
    TransformationRule,
    build_transformation_attempt,
    verify_variant_draft_id,
)
from leanfaith.transforms.registry import TransformationExecution
from leanfaith.transforms.v2_contract import V2EvidenceClass, load_v2_portfolio

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]

V2E0RuleId = Literal[
    "p05_resolved_names",
    "p06_implicit_arguments",
    "p07_coercion_surface",
    "p08_type_ascriptions",
    "p09_projections",
    "p10_constructors",
    "p11_bounded_quantifiers",
    "p12_proof_arrow_binder",
]
V2E0ProfileId = Literal[
    "deterministic_v2_e0_experimental",
    "deterministic_v2_e0_lf032_experimental",
    "deterministic_v2_e0_lf033_surface_experimental",
    "deterministic_v2_e0_p12_v110_experimental",
]

_PROFILE_RULE_IDS: dict[str, tuple[str, ...]] = {
    "deterministic_v2_e0_experimental": (
        "p11_bounded_quantifiers",
        "p12_proof_arrow_binder",
    ),
    "deterministic_v2_e0_lf032_experimental": (
        "p06_implicit_arguments",
        "p07_coercion_surface",
        "p09_projections",
        "p10_constructors",
        "p11_bounded_quantifiers",
        "p12_proof_arrow_binder",
    ),
    "deterministic_v2_e0_lf033_surface_experimental": (
        "p05_resolved_names",
        "p08_type_ascriptions",
    ),
    "deterministic_v2_e0_p12_v110_experimental": ("p12_proof_arrow_binder",),
}

_PROFILE_RULE_VERSIONS: dict[str, tuple[str, ...]] = {
    profile_id: tuple("1.0.0" for _ in rule_ids)
    for profile_id, rule_ids in _PROFILE_RULE_IDS.items()
}
_PROFILE_RULE_VERSIONS["deterministic_v2_e0_p12_v110_experimental"] = ("1.1.0",)


class V2E0ExecutionError(ValueError):
    """The experimental v2 execution profile or dispatch failed closed."""


class V2E0RuleBinding(StrictModel):
    family_id: V2E0RuleId
    rule_id: V2E0RuleId
    rule_version: SemanticVersion
    implementation_key: V2E0RuleId
    evidence_class: Literal[V2EvidenceClass.E0] = V2EvidenceClass.E0

    @model_validator(mode="after")
    def _same_identity(self) -> V2E0RuleBinding:
        if not (self.family_id == self.rule_id == self.implementation_key):
            raise ValueError("v2 E0 rule identity fields must be identical")
        return self


class P12V110Addendum(StrictModel):
    """Immutable authorization for P12's additive v1.1 matcher expansion."""

    schema_version: Literal[1] = 1
    addendum_id: Literal["deterministic_v2_p12_v110_matcher_expansion"]
    addendum_version: Literal["1.0.0"]
    status: Literal["experimental"]
    base_portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    base_portfolio_config_hash: Sha256
    family_id: Literal["p12_proof_arrow_binder"]
    base_family_version: Literal["1.0.0"]
    expanded_family_version: Literal["1.1.0"]
    change_kind: Literal["matcher_expansion"]
    scope: str = Field(min_length=1, strict=True)
    excluded_cases: tuple[str, ...] = Field(min_length=1)
    require_exact_inverse_replay: Literal[True] = True
    require_existing_e0_audit: Literal[True] = True
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> P12V110Addendum:
        if self.excluded_cases != tuple(sorted(set(self.excluded_cases))):
            raise ValueError("P12 v1.1 excluded cases must be sorted and unique")
        return self


class V2E0ExecutionConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: V2E0ProfileId
    profile_version: SemanticVersion
    status: Literal["experimental"]
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    candidate_pool: Literal[
        "deterministic_v2_e0_experimental",
        "deterministic_v2_e0_lf032_experimental",
        "deterministic_v2_e0_lf033_surface_experimental",
        "deterministic_v2_e0_p12_v110_experimental",
    ]
    active_rules: tuple[V2E0RuleBinding, ...]
    required_candidate_views: tuple[
        Literal[
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ],
        ...,
    ]
    require_same_context_reelaboration: Literal[True] = True
    require_exact_inverse_replay: Literal[True] = True
    require_alpha_canonical_identity: Literal[True] = True
    require_semantic_atom_identity: Literal[True] = True
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    output_quality_tier: Literal[QualityTier.PROVISIONAL] = QualityTier.PROVISIONAL

    @model_validator(mode="after")
    def _exact_slice(self) -> V2E0ExecutionConfig:
        expected = _PROFILE_RULE_IDS[self.profile_id]
        if tuple(item.rule_id for item in self.active_rules) != expected:
            raise ValueError(f"v2 E0 profile {self.profile_id} must contain exactly {expected}")
        expected_versions = _PROFILE_RULE_VERSIONS[self.profile_id]
        if tuple(item.rule_version for item in self.active_rules) != expected_versions:
            raise ValueError(
                f"v2 E0 profile {self.profile_id} must use rule versions {expected_versions}"
            )
        if self.candidate_pool != self.profile_id:
            raise ValueError("v2 E0 candidate_pool must equal profile_id")
        if self.required_candidate_views != (
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ):
            raise ValueError("v2 E0 required_candidate_views changed")
        return self


class V2E0P12V110ExecutionConfig(V2E0ExecutionConfig):
    """P12 v1.1 profile with an explicit immutable design addendum."""

    profile_id: Literal["deterministic_v2_e0_p12_v110_experimental"]
    candidate_pool: Literal["deterministic_v2_e0_p12_v110_experimental"]
    version_addendum_path: Literal["configs/transformations/v2_p12_v110_addendum.yaml"]
    version_addendum_sha256: Sha256


def load_v2_e0_execution_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[V2E0ExecutionConfig]:
    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2_e0_experimental.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2E0ExecutionError("v2 E0 execution config escapes the repository")
    raw = load_yaml_mapping(resolved)
    config_type: type[V2E0ExecutionConfig] = (
        V2E0P12V110ExecutionConfig
        if raw.get("profile_id") == "deterministic_v2_e0_p12_v110_experimental"
        else V2E0ExecutionConfig
    )
    loaded = load_config(resolved, config_type)
    portfolio = load_v2_portfolio(root)
    if loaded.config.portfolio_config_hash != portfolio.config_hash:
        raise V2E0ExecutionError("v2 E0 profile does not bind the current v2 portfolio")
    if (
        loaded.config.accepted_v1_effective_registry_hash
        != portfolio.config.accepted_v1.effective_registry_hash
    ):
        raise V2E0ExecutionError("v2 E0 profile does not bind the accepted v1 replay")
    designs = {item.family_id: item for item in portfolio.config.families}
    for binding in loaded.config.active_rules:
        design = designs[binding.family_id]
        if design.evidence_class != V2EvidenceClass.E0:
            raise V2E0ExecutionError(f"{binding.family_id} is not an E0 design")
        if design.executable or design.draft_emission_authorized:
            raise V2E0ExecutionError("LF-031 design config was mutated into an executor")
    if loaded.config.profile_id == "deterministic_v2_e0_p12_v110_experimental":
        if not isinstance(loaded.config, V2E0P12V110ExecutionConfig):
            raise V2E0ExecutionError("P12 v1.1 profile lacks its versioned config schema")
        addendum_path = (root / loaded.config.version_addendum_path).resolve()
        if not addendum_path.is_relative_to(root.resolve()):
            raise V2E0ExecutionError("P12 v1.1 addendum escapes the repository")
        if hash_file(addendum_path) != loaded.config.version_addendum_sha256:
            raise V2E0ExecutionError("P12 v1.1 addendum byte hash changed")
        addendum = load_config(addendum_path, P12V110Addendum).config
        design = designs[addendum.family_id]
        binding = loaded.config.active_rules[0]
        if not (
            addendum.base_portfolio_config_hash == portfolio.config_hash
            and design.family_version == addendum.base_family_version
            and binding.rule_version == addendum.expanded_family_version
        ):
            raise V2E0ExecutionError("P12 v1.1 addendum does not bind its base design/version")
    return loaded


class V2E0Runtime:
    """Code-owned dispatcher for exactly one selected E0 profile."""

    def __init__(self, loaded: LoadedConfig[V2E0ExecutionConfig]) -> None:
        from leanfaith.transforms.positives.p05_p08_surface import (
            P05ResolvedGlobalNamesRule,
            P08TypeAscriptionsRule,
        )
        from leanfaith.transforms.positives.p06_p10_surface import (
            P06ImplicitArgumentsRule,
            P10ConstructorsRule,
        )
        from leanfaith.transforms.positives.v2_e0 import (
            P11BoundedQuantifierRule,
            P12ProofArrowBinderRule,
            P12ProofArrowBinderV110Rule,
        )
        from leanfaith.transforms.positives.v2_e0_p07_p09 import (
            P07CoercionSurfaceRule,
            P09ProjectionSurfaceRule,
        )

        self.loaded = loaded
        self.generation_config_hash = loaded.config_hash
        self.portfolio_hash = loaded.config.portfolio_config_hash
        constructor_args = {
            "generation_config_hash": loaded.config_hash,
            "candidate_pool": loaded.config.candidate_pool,
        }
        supported = {
            "p05_resolved_names": P05ResolvedGlobalNamesRule(**constructor_args),
            "p06_implicit_arguments": P06ImplicitArgumentsRule(**constructor_args),
            "p07_coercion_surface": P07CoercionSurfaceRule(**constructor_args),
            "p08_type_ascriptions": P08TypeAscriptionsRule(**constructor_args),
            "p09_projections": P09ProjectionSurfaceRule(**constructor_args),
            "p10_constructors": P10ConstructorsRule(**constructor_args),
            "p11_bounded_quantifiers": P11BoundedQuantifierRule(
                **constructor_args,
            ),
            "p12_proof_arrow_binder": P12ProofArrowBinderRule(
                **constructor_args,
            ),
        }
        if loaded.config.profile_id == "deterministic_v2_e0_p12_v110_experimental":
            supported["p12_proof_arrow_binder"] = P12ProofArrowBinderV110Rule(
                **constructor_args,
            )
        self._rules: dict[str, TransformationRule] = {
            rule_id: supported[rule_id]
            for rule_id in (binding.rule_id for binding in loaded.config.active_rules)
        }

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(self._rules)

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise V2E0ExecutionError(
                f"rule {rule_id!r} is outside profile {self.loaded.config.profile_id}"
            )
        if theorem.theorem_id != representation.theorem_id:
            raise V2E0ExecutionError("source theorem/representation lineage mismatch")
        if theorem.context_id != representation.context_id:
            raise V2E0ExecutionError("source theorem/representation context mismatch")
        applicability = rule.assess(theorem, representation)
        drafts = (
            tuple(rule.generate(theorem, representation, seed)) if applicability.applicable else ()
        )
        for draft in drafts:
            try:
                verify_variant_draft_id(draft)
            except ValueError as exc:
                raise V2E0ExecutionError("v2 E0 rule emitted an invalid draft ID") from exc
            if (
                draft.rule_id != rule_id
                or draft.family_id != rule_id
                or draft.generation_config_hash != self.generation_config_hash
                or draft.source_theorem_ids != (theorem.theorem_id,)
                or draft.source_representation_ids != (representation.representation_id,)
            ):
                raise V2E0ExecutionError("v2 E0 rule emitted a draft outside its binding")
        outcome: Literal["generated", "not_applicable", "no_output"]
        if not applicability.applicable:
            outcome = "not_applicable"
        else:
            outcome = "generated" if drafts else "no_output"
        attempt = build_transformation_attempt(
            family_id=rule_id,
            rule_id=rule_id,
            rule_version=rule.rule_version,
            source_theorem_ids=(theorem.theorem_id,),
            source_representation_ids=(representation.representation_id,),
            context_id=theorem.context_id,
            registry_hash=self.portfolio_hash,
            generation_config_hash=self.generation_config_hash,
            seed=seed,
            applicability=applicability,
            terminal_outcome=outcome,
            draft_ids=tuple(item.draft_id for item in drafts),
        )
        return TransformationExecution(attempt=attempt, drafts=drafts)

    def audit(
        self,
        rule_id: str,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise V2E0ExecutionError(
                f"rule {rule_id!r} is outside profile {self.loaded.config.profile_id}"
            )
        return rule.audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )


def build_v2_e0_runtime(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> V2E0Runtime:
    return V2E0Runtime(load_v2_e0_execution_config(repo_root, path=path))


__all__ = [
    "V2E0ExecutionConfig",
    "V2E0ExecutionError",
    "V2E0RuleBinding",
    "V2E0Runtime",
    "build_v2_e0_runtime",
    "load_v2_e0_execution_config",
]

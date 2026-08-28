"""Experimental deterministic-v2 E2 runtime for P18 v1.0."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import TransformationAudit, VariantDraft
from leanfaith.transforms.positives.p18_equality_symmetry import P18EqualitySymmetryRule
from leanfaith.transforms.protocol import build_transformation_attempt, verify_variant_draft_id
from leanfaith.transforms.registry import TransformationExecution
from leanfaith.transforms.v2_contract import load_v2_portfolio

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]
P18RuleId = Literal["p18_root_equality_symmetry"]


class V2E2P18ExecutionError(ValueError):
    """The P18 profile, addendum, dispatch, or output violated its scope."""


class P18V100Addendum(StrictModel):
    """Immutable additive-family design bound to the frozen v2 portfolio."""

    schema_version: Literal[1] = 1
    addendum_id: Literal["deterministic_v2_p18_v100_family_addition"]
    addendum_version: Literal["1.0.0"]
    status: Literal["experimental"]
    base_portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    base_portfolio_config_hash: Sha256
    family_id: Literal["p18_root_equality_symmetry"]
    family_version: Literal["1.0.0"]
    change_kind: Literal["additive_family"]
    evidence_class: Literal["E2"]
    scope: str = Field(min_length=1, strict=True)
    excluded_cases: tuple[str, ...] = Field(min_length=1)
    require_exact_inverse_replay: Literal[True] = True
    require_exact_root_equality_symmetry: Literal[True] = True
    require_same_context_reelaboration: Literal[True] = True
    require_admission_free_local_certificate_smoke: Literal[True] = True
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> P18V100Addendum:
        if self.excluded_cases != tuple(sorted(set(self.excluded_cases))):
            raise ValueError("P18 excluded cases must be sorted and unique")
        return self


class V2E2P18RuleBinding(StrictModel):
    family_id: Literal["p18_root_equality_symmetry"]
    rule_id: Literal["p18_root_equality_symmetry"]
    rule_version: Literal["1.0.0"]
    implementation_key: Literal["p18_root_equality_symmetry"]
    evidence_class: Literal["E2"]

    @model_validator(mode="after")
    def _closed_binding(self) -> V2E2P18RuleBinding:
        if not (
            self.family_id
            == self.rule_id
            == self.implementation_key
            == "p18_root_equality_symmetry"
        ):
            raise ValueError("P18 binding identity fields must be identical")
        return self


class V2E2P18ExecutionConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: Literal["deterministic_v2_e2_p18_experimental"]
    profile_version: SemanticVersion
    status: Literal["experimental"]
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    candidate_pool: Literal["deterministic_v2_e2_p18_experimental"]
    active_rules: tuple[V2E2P18RuleBinding, ...]
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
    require_exact_root_equality_symmetry: Literal[True] = True
    require_distinct_equality_sides: Literal[True] = True
    require_admission_free_local_certificate_smoke: Literal[True] = True
    version_addendum_path: Literal["configs/transformations/v2_p18_v100_addendum.yaml"]
    version_addendum_sha256: Sha256
    failed_proof_search_is_negative_evidence: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    output_quality_tier: Literal[QualityTier.PROVISIONAL] = QualityTier.PROVISIONAL

    @model_validator(mode="after")
    def _closed_profile(self) -> V2E2P18ExecutionConfig:
        if tuple(binding.rule_id for binding in self.active_rules) != (
            "p18_root_equality_symmetry",
        ):
            raise ValueError("the P18 profile must contain only P18")
        if self.required_candidate_views != (
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ):
            raise ValueError("P18 required_candidate_views changed")
        return self


def load_v2_e2_p18_execution_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[V2E2P18ExecutionConfig]:
    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2_e2_p18_experimental.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2E2P18ExecutionError("v2 P18 config escapes the repository")
    loaded = load_config(resolved, V2E2P18ExecutionConfig)
    portfolio = load_v2_portfolio(root)
    if loaded.config.portfolio_config_hash != portfolio.config_hash:
        raise V2E2P18ExecutionError("P18 profile does not bind the frozen v2 portfolio")
    if (
        loaded.config.accepted_v1_effective_registry_hash
        != portfolio.config.accepted_v1.effective_registry_hash
    ):
        raise V2E2P18ExecutionError("P18 profile does not bind accepted v1 replay")
    addendum_path = (root / loaded.config.version_addendum_path).resolve()
    if not addendum_path.is_relative_to(root.resolve()):
        raise V2E2P18ExecutionError("P18 addendum escapes the repository")
    if hash_file(addendum_path) != loaded.config.version_addendum_sha256:
        raise V2E2P18ExecutionError("P18 addendum byte hash changed")
    addendum = load_config(addendum_path, P18V100Addendum).config
    binding = loaded.config.active_rules[0]
    if not (
        addendum.base_portfolio_config_hash == portfolio.config_hash
        and addendum.family_id == binding.family_id
        and addendum.family_version == binding.rule_version
    ):
        raise V2E2P18ExecutionError("P18 addendum does not bind its base portfolio/profile")
    return loaded


class V2E2P18Runtime:
    """Code-owned unary dispatcher for only P18."""

    def __init__(self, loaded: LoadedConfig[V2E2P18ExecutionConfig]) -> None:
        self.loaded = loaded
        self.generation_config_hash = loaded.config_hash
        self.portfolio_hash = loaded.config.portfolio_config_hash
        self._rule = P18EqualitySymmetryRule(
            generation_config_hash=loaded.config_hash,
            candidate_pool=loaded.config.candidate_pool,
        )

    @property
    def rule_ids(self) -> tuple[P18RuleId, ...]:
        return ("p18_root_equality_symmetry",)

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        if rule_id != self._rule.rule_id:
            raise V2E2P18ExecutionError(f"rule {rule_id!r} is outside the P18 profile")
        if theorem.theorem_id != representation.theorem_id:
            raise V2E2P18ExecutionError("source theorem/representation lineage mismatch")
        if theorem.context_id != representation.context_id:
            raise V2E2P18ExecutionError("source theorem/representation context mismatch")
        applicability = self._rule.assess(theorem, representation)
        drafts = (
            tuple(self._rule.generate(theorem, representation, seed))
            if applicability.applicable
            else ()
        )
        for draft in drafts:
            try:
                verify_variant_draft_id(draft)
            except ValueError as exc:
                raise V2E2P18ExecutionError("P18 emitted an invalid draft ID") from exc
            if (
                draft.rule_id != rule_id
                or draft.family_id != rule_id
                or draft.generation_config_hash != self.generation_config_hash
                or draft.source_theorem_ids != (theorem.theorem_id,)
                or draft.source_representation_ids != (representation.representation_id,)
            ):
                raise V2E2P18ExecutionError("P18 emitted a draft outside its binding")
        terminal_outcome: Literal["generated", "not_applicable", "no_output"]
        if not applicability.applicable:
            terminal_outcome = "not_applicable"
        else:
            terminal_outcome = "generated" if drafts else "no_output"
        attempt = build_transformation_attempt(
            family_id=rule_id,
            rule_id=rule_id,
            rule_version=self._rule.rule_version,
            source_theorem_ids=(theorem.theorem_id,),
            source_representation_ids=(representation.representation_id,),
            context_id=theorem.context_id,
            registry_hash=self.portfolio_hash,
            generation_config_hash=self.generation_config_hash,
            seed=seed,
            applicability=applicability,
            terminal_outcome=terminal_outcome,
            draft_ids=tuple(draft.draft_id for draft in drafts),
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
        if rule_id != self._rule.rule_id:
            raise V2E2P18ExecutionError(f"rule {rule_id!r} is outside the P18 profile")
        return self._rule.audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )


def build_v2_e2_p18_runtime(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> V2E2P18Runtime:
    return V2E2P18Runtime(load_v2_e2_p18_execution_config(repo_root, path=path))


__all__ = [
    "P18RuleId",
    "P18V100Addendum",
    "V2E2P18ExecutionConfig",
    "V2E2P18ExecutionError",
    "V2E2P18RuleBinding",
    "V2E2P18Runtime",
    "build_v2_e2_p18_runtime",
    "load_v2_e2_p18_execution_config",
]

"""Experimental deterministic-v2 E2 runtime for LF-033 P15."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import TransformationAudit, VariantDraft
from leanfaith.transforms.positives.p15_iff_reversal import P15IffReversalRule
from leanfaith.transforms.protocol import build_transformation_attempt, verify_variant_draft_id
from leanfaith.transforms.registry import TransformationExecution
from leanfaith.transforms.v2_contract import V2EvidenceClass, load_v2_portfolio

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]
P15RuleId = Literal["p15_root_iff_reversal"]


class V2E2P15ExecutionError(ValueError):
    """The P15 profile, dispatch, or output violated its closed scope."""


class V2E2P15RuleBinding(StrictModel):
    family_id: Literal["p15_root_iff_reversal"]
    rule_id: Literal["p15_root_iff_reversal"]
    rule_version: Literal["1.0.0"]
    implementation_key: Literal["p15_root_iff_reversal"]
    evidence_class: Literal["E2"]

    @model_validator(mode="after")
    def _closed_binding(self) -> V2E2P15RuleBinding:
        if not (
            self.family_id == self.rule_id == self.implementation_key == "p15_root_iff_reversal"
        ):
            raise ValueError("P15 binding identity fields must be identical")
        return self


class V2E2P15ExecutionConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: Literal["deterministic_v2_e2_p15_experimental"]
    profile_version: SemanticVersion
    status: Literal["experimental"]
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    candidate_pool: Literal["deterministic_v2_e2_p15_experimental"]
    active_rules: tuple[V2E2P15RuleBinding, ...]
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
    require_exact_root_iff_reversal: Literal[True] = True
    require_distinct_iff_sides: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    output_quality_tier: Literal[QualityTier.PROVISIONAL] = QualityTier.PROVISIONAL

    @model_validator(mode="after")
    def _closed_profile(self) -> V2E2P15ExecutionConfig:
        if tuple(binding.rule_id for binding in self.active_rules) != ("p15_root_iff_reversal",):
            raise ValueError("the P15 profile must contain only P15")
        if self.required_candidate_views != (
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ):
            raise ValueError("P15 required_candidate_views changed")
        return self


def load_v2_e2_p15_execution_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[V2E2P15ExecutionConfig]:
    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2_e2_p15_experimental.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2E2P15ExecutionError("v2 P15 config escapes the repository")
    loaded = load_config(resolved, V2E2P15ExecutionConfig)
    portfolio = load_v2_portfolio(root)
    if loaded.config.portfolio_config_hash != portfolio.config_hash:
        raise V2E2P15ExecutionError("P15 profile does not bind the v2 portfolio")
    if (
        loaded.config.accepted_v1_effective_registry_hash
        != portfolio.config.accepted_v1.effective_registry_hash
    ):
        raise V2E2P15ExecutionError("P15 profile does not bind accepted v1 replay")
    designs = {item.family_id: item for item in portfolio.config.families}
    design = designs["p15_root_iff_reversal"]
    if design.evidence_class != V2EvidenceClass.E2:
        raise V2E2P15ExecutionError("P15 is not an E2 design")
    if design.executable or design.draft_emission_authorized:
        raise V2E2P15ExecutionError("LF-031 design config was mutated into an executor")
    return loaded


class V2E2P15Runtime:
    """Code-owned unary dispatcher for only P15."""

    def __init__(self, loaded: LoadedConfig[V2E2P15ExecutionConfig]) -> None:
        self.loaded = loaded
        self.generation_config_hash = loaded.config_hash
        self.portfolio_hash = loaded.config.portfolio_config_hash
        self._rule = P15IffReversalRule(
            generation_config_hash=loaded.config_hash,
            candidate_pool=loaded.config.candidate_pool,
        )

    @property
    def rule_ids(self) -> tuple[P15RuleId, ...]:
        return ("p15_root_iff_reversal",)

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        if rule_id != self._rule.rule_id:
            raise V2E2P15ExecutionError(f"rule {rule_id!r} is outside the P15 profile")
        if theorem.theorem_id != representation.theorem_id:
            raise V2E2P15ExecutionError("source theorem/representation lineage mismatch")
        if theorem.context_id != representation.context_id:
            raise V2E2P15ExecutionError("source theorem/representation context mismatch")
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
                raise V2E2P15ExecutionError("P15 emitted an invalid draft ID") from exc
            if (
                draft.rule_id != rule_id
                or draft.family_id != rule_id
                or draft.generation_config_hash != self.generation_config_hash
                or draft.source_theorem_ids != (theorem.theorem_id,)
                or draft.source_representation_ids != (representation.representation_id,)
            ):
                raise V2E2P15ExecutionError("P15 emitted a draft outside its binding")
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
            raise V2E2P15ExecutionError(f"rule {rule_id!r} is outside the P15 profile")
        return self._rule.audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )


def build_v2_e2_p15_runtime(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> V2E2P15Runtime:
    return V2E2P15Runtime(load_v2_e2_p15_execution_config(repo_root, path=path))


__all__ = [
    "P15RuleId",
    "V2E2P15ExecutionConfig",
    "V2E2P15ExecutionError",
    "V2E2P15RuleBinding",
    "V2E2P15Runtime",
    "build_v2_e2_p15_runtime",
    "load_v2_e2_p15_execution_config",
]

"""Experimental deterministic-v2 D0 runtime for LF-034 N12.

N12 is isolated from the already pinned N11 profile so extending the negative
portfolio cannot reinterpret any N11 config or output hash. The runtime emits
only provisional drafts and audits; it has no label or promotion path.
"""

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
from leanfaith.transforms.negatives.n12_implication_converse import (
    N12ImplicationConverseRule,
)
from leanfaith.transforms.protocol import (
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
N12RuleId = Literal["n12_implication_converse"]


class V2D0N12ExecutionError(ValueError):
    """The N12 profile, dispatch, or output violated its closed scope."""


class V2D0N12RuleBinding(StrictModel):
    family_id: Literal["n12_implication_converse"]
    rule_id: Literal["n12_implication_converse"]
    rule_version: Literal["1.1.0"]
    implementation_key: Literal["n12_implication_converse"]
    evidence_class: Literal["D0"]
    intended_error_types: tuple[Literal["E26", "E30"], ...]

    @model_validator(mode="after")
    def _closed_binding(self) -> V2D0N12RuleBinding:
        if not (
            self.family_id == self.rule_id == self.implementation_key == "n12_implication_converse"
        ):
            raise ValueError("N12 binding identity fields must be identical")
        if self.intended_error_types != ("E26", "E30"):
            raise ValueError("N12 intended_error_types must be exactly [E26, E30]")
        return self


class V2D0N12ExecutionConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: Literal["deterministic_v2_d0_n12_experimental"]
    profile_version: SemanticVersion
    status: Literal["experimental"]
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    candidate_pool: Literal["deterministic_v2_d0_n12_experimental"]
    active_rules: tuple[V2D0N12RuleBinding, ...]
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
    require_exact_root_converse: Literal[True] = True
    require_candidate_prop_reelaboration: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    output_quality_tier: Literal[QualityTier.PROVISIONAL] = QualityTier.PROVISIONAL

    @model_validator(mode="after")
    def _closed_profile(self) -> V2D0N12ExecutionConfig:
        if tuple(binding.rule_id for binding in self.active_rules) != ("n12_implication_converse",):
            raise ValueError("the N12 profile must contain only N12")
        if self.required_candidate_views != (
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ):
            raise ValueError("N12 required_candidate_views changed")
        return self


def load_v2_d0_n12_execution_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[V2D0N12ExecutionConfig]:
    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2_d0_n12_experimental.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2D0N12ExecutionError("v2 N12 config escapes the repository")
    loaded = load_config(resolved, V2D0N12ExecutionConfig)
    portfolio = load_v2_portfolio(root)
    if loaded.config.portfolio_config_hash != portfolio.config_hash:
        raise V2D0N12ExecutionError("N12 profile does not bind the v2 portfolio")
    if (
        loaded.config.accepted_v1_effective_registry_hash
        != portfolio.config.accepted_v1.effective_registry_hash
    ):
        raise V2D0N12ExecutionError("N12 profile does not bind accepted v1 replay")
    designs = {item.family_id: item for item in portfolio.config.families}
    design = designs["n12_implication_converse"]
    if design.evidence_class != V2EvidenceClass.D0:
        raise V2D0N12ExecutionError("N12 is not a D0 design")
    if design.executable or design.draft_emission_authorized:
        raise V2D0N12ExecutionError("LF-031 design config was mutated into an executor")
    return loaded


class V2D0N12Runtime:
    """Code-owned unary dispatcher for only N12."""

    def __init__(self, loaded: LoadedConfig[V2D0N12ExecutionConfig]) -> None:
        self.loaded = loaded
        self.generation_config_hash = loaded.config_hash
        self.portfolio_hash = loaded.config.portfolio_config_hash
        self._rule = N12ImplicationConverseRule(
            generation_config_hash=loaded.config_hash,
            candidate_pool=loaded.config.candidate_pool,
        )

    @property
    def rule_ids(self) -> tuple[N12RuleId, ...]:
        return ("n12_implication_converse",)

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        if rule_id != self._rule.rule_id:
            raise V2D0N12ExecutionError(f"rule {rule_id!r} is outside the N12 profile")
        if theorem.theorem_id != representation.theorem_id:
            raise V2D0N12ExecutionError("source theorem/representation lineage mismatch")
        if theorem.context_id != representation.context_id:
            raise V2D0N12ExecutionError("source theorem/representation context mismatch")
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
                raise V2D0N12ExecutionError("N12 emitted an invalid draft ID") from exc
            if (
                draft.rule_id != rule_id
                or draft.family_id != rule_id
                or draft.generation_config_hash != self.generation_config_hash
                or draft.source_theorem_ids != (theorem.theorem_id,)
                or draft.source_representation_ids != (representation.representation_id,)
            ):
                raise V2D0N12ExecutionError("N12 emitted a draft outside its binding")
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
            raise V2D0N12ExecutionError(f"rule {rule_id!r} is outside the N12 profile")
        return self._rule.audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )


def build_v2_d0_n12_runtime(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> V2D0N12Runtime:
    return V2D0N12Runtime(load_v2_d0_n12_execution_config(repo_root, path=path))


__all__ = [
    "N12RuleId",
    "V2D0N12ExecutionConfig",
    "V2D0N12ExecutionError",
    "V2D0N12RuleBinding",
    "V2D0N12Runtime",
    "build_v2_d0_n12_runtime",
    "load_v2_d0_n12_execution_config",
]

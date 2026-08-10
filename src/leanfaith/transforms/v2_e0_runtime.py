"""Separate experimental runtime for the first executable deterministic-v2 slice.

This module activates only P11 and P12.  It does not modify or extend the
accepted v1 registry, and it cannot resolve labels, promote families, or mark
its output training-eligible.
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

_ACTIVE_RULE_IDS = ("p11_bounded_quantifiers", "p12_proof_arrow_binder")


class V2E0ExecutionError(ValueError):
    """The experimental v2 execution profile or dispatch failed closed."""


class V2E0RuleBinding(StrictModel):
    family_id: Literal["p11_bounded_quantifiers", "p12_proof_arrow_binder"]
    rule_id: Literal["p11_bounded_quantifiers", "p12_proof_arrow_binder"]
    rule_version: SemanticVersion
    implementation_key: Literal["p11_bounded_quantifiers", "p12_proof_arrow_binder"]
    evidence_class: Literal[V2EvidenceClass.E0] = V2EvidenceClass.E0

    @model_validator(mode="after")
    def _same_identity(self) -> V2E0RuleBinding:
        if not (self.family_id == self.rule_id == self.implementation_key):
            raise ValueError("v2 E0 rule identity fields must be identical")
        return self


class V2E0ExecutionConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: Literal["deterministic_v2_e0_experimental"]
    profile_version: SemanticVersion
    status: Literal["experimental"]
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_config_hash: Sha256
    accepted_v1_effective_registry_hash: Sha256
    candidate_pool: Literal["deterministic_v2_e0_experimental"]
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
        if tuple(item.rule_id for item in self.active_rules) != _ACTIVE_RULE_IDS:
            raise ValueError("v2 E0 slice must contain exactly P11 then P12")
        if self.required_candidate_views != (
            "alpha_identity_fingerprint",
            "operator_tree",
            "semantic_atoms",
            "signature_explicit",
        ):
            raise ValueError("v2 E0 required_candidate_views changed")
        return self


def load_v2_e0_execution_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[V2E0ExecutionConfig]:
    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/v2_e0_experimental.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise V2E0ExecutionError("v2 E0 execution config escapes the repository")
    loaded = load_config(resolved, V2E0ExecutionConfig)
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
    return loaded


class V2E0Runtime:
    """Code-owned dispatcher for exactly P11 and P12."""

    def __init__(self, loaded: LoadedConfig[V2E0ExecutionConfig]) -> None:
        from leanfaith.transforms.positives.v2_e0 import (
            P11BoundedQuantifierRule,
            P12ProofArrowBinderRule,
        )

        self.loaded = loaded
        self.generation_config_hash = loaded.config_hash
        self.portfolio_hash = loaded.config.portfolio_config_hash
        self._rules = {
            "p11_bounded_quantifiers": P11BoundedQuantifierRule(
                generation_config_hash=loaded.config_hash,
                candidate_pool=loaded.config.candidate_pool,
            ),
            "p12_proof_arrow_binder": P12ProofArrowBinderRule(
                generation_config_hash=loaded.config_hash,
                candidate_pool=loaded.config.candidate_pool,
            ),
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
            raise V2E0ExecutionError(f"rule {rule_id!r} is outside the P11/P12 slice")
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
            raise V2E0ExecutionError(f"rule {rule_id!r} is outside the P11/P12 slice")
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

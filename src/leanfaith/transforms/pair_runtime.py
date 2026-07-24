"""Fail-closed dispatcher for explicit two-source transformation rules.

The unary :mod:`leanfaith.transforms.registry` remains intentionally strict
about one source theorem.  N10 instead executes through this small pair-aware
boundary, which binds the implementation to the same effective registry hash,
validates both source lineages and every returned draft, and emits the ordinary
persistent :class:`TransformationAttempt` / :class:`TransformationExecution`
records with two aligned sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from leanfaith.schemas.enums import QualityTier, TransformationFamilyStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    Applicability,
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
)
from leanfaith.transforms.protocol import (
    PairTransformationRule,
    build_transformation_attempt,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RuleImplementationStatus,
    TransformationExecution,
    TransformationFamilyConfig,
    TransformationRuleConfig,
)


class PairTransformationDispatchError(RuntimeError):
    """A pair execution failed closed, with a terminal attempt when possible."""

    def __init__(
        self,
        message: str,
        *,
        execution: TransformationExecution | None = None,
        stage: str,
    ) -> None:
        suffix = "" if execution is None else f"; attempt={execution.attempt.attempt_id}"
        super().__init__(f"pair transformation {stage} failed: {message}{suffix}")
        self.execution = execution
        self.stage = stage


@dataclass(frozen=True, slots=True)
class _ConfiguredPairRule:
    family: TransformationFamilyConfig
    rule: TransformationRuleConfig


def _find_config(
    loaded: LoadedTransformationRegistry,
    rule_id: str,
) -> _ConfiguredPairRule | None:
    for family in loaded.config.families:
        for rule in family.rules:
            if rule.rule_id == rule_id:
                return _ConfiguredPairRule(family=family, rule=rule)
    return None


def _source_pairs(
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pairs = tuple(
        sorted(
            (
                (primary.theorem_id, primary_representation.representation_id),
                (donor.theorem_id, donor_representation.representation_id),
            )
        )
    )
    return (
        tuple(theorem_id for theorem_id, _ in pairs),
        tuple(representation_id for _, representation_id in pairs),
    )


def _attempt(
    *,
    loaded: LoadedTransformationRegistry,
    configured: _ConfiguredPairRule,
    source_theorem_ids: tuple[str, ...],
    source_representation_ids: tuple[str, ...],
    context_id: str,
    seed: int,
    applicability: Applicability | None,
    terminal_outcome: Literal[
        "not_applicable",
        "generated",
        "no_output",
        "rejected_disabled",
        "generation_error",
        "infrastructure_error",
    ],
    draft_ids: tuple[str, ...] = (),
    failure_codes: tuple[str, ...] = (),
) -> TransformationAttempt:
    return build_transformation_attempt(
        family_id=configured.family.family_id,
        rule_id=configured.rule.rule_id,
        rule_version=configured.rule.rule_version,
        source_theorem_ids=source_theorem_ids,
        source_representation_ids=source_representation_ids,
        context_id=context_id,
        registry_hash=loaded.registry_hash,
        generation_config_hash=loaded.registry_hash,
        seed=seed,
        applicability=applicability,
        terminal_outcome=terminal_outcome,
        draft_ids=draft_ids,
        failure_codes=failure_codes,
        metadata={"source_arity": 2},
    )


def _failure(
    *,
    loaded: LoadedTransformationRegistry,
    configured: _ConfiguredPairRule,
    source_theorem_ids: tuple[str, ...],
    source_representation_ids: tuple[str, ...],
    context_id: str,
    seed: int,
    applicability: Applicability | None,
    stage: str,
    code: str,
    cause: Exception | None = None,
) -> PairTransformationDispatchError:
    outcome: Literal["generation_error", "infrastructure_error"] = (
        "infrastructure_error" if isinstance(cause, TimeoutError | OSError) else "generation_error"
    )
    attempt = _attempt(
        loaded=loaded,
        configured=configured,
        source_theorem_ids=source_theorem_ids,
        source_representation_ids=source_representation_ids,
        context_id=context_id,
        seed=seed,
        applicability=applicability,
        terminal_outcome=outcome,
        failure_codes=(f"{stage}_{code}",),
    )
    execution = TransformationExecution(attempt=attempt, drafts=())
    return PairTransformationDispatchError(
        code,
        execution=execution,
        stage=stage,
    )


def execute_pair_transformation(
    loaded: LoadedTransformationRegistry,
    implementation: PairTransformationRule,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
    seed: int,
) -> TransformationExecution:
    """Assess and generate through a registry-bound, two-source contract."""

    loaded = LoadedTransformationRegistry.model_validate(loaded.model_dump(mode="json"))
    raw_rule_id = getattr(implementation, "rule_id", None)
    if not isinstance(raw_rule_id, str):
        raise PairTransformationDispatchError(
            "rule_id_missing",
            execution=None,
            stage="configure",
        )
    configured = _find_config(loaded, raw_rule_id)
    if configured is None:
        raise PairTransformationDispatchError(
            "rule_unlisted",
            execution=None,
            stage="configure",
        )
    if primary.theorem_id == donor.theorem_id:
        raise PairTransformationDispatchError(
            "source_theorems_not_distinct",
            execution=None,
            stage="input",
        )
    if primary_representation.representation_id == donor_representation.representation_id:
        raise PairTransformationDispatchError(
            "source_representations_not_distinct",
            execution=None,
            stage="input",
        )
    source_theorem_ids, source_representation_ids = _source_pairs(
        primary,
        primary_representation,
        donor,
        donor_representation,
    )
    context_id = primary.context_id

    if (
        configured.family.status == TransformationFamilyStatus.DISABLED
        or configured.family.family_id not in loaded.profile.active_family_ids
        or configured.rule.implementation_status != RuleImplementationStatus.AVAILABLE
    ):
        attempt = _attempt(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            terminal_outcome="rejected_disabled",
            failure_codes=("configure_implementation_unavailable",),
        )
        execution = TransformationExecution(attempt=attempt, drafts=())
        raise PairTransformationDispatchError(
            "implementation_unavailable",
            execution=execution,
            stage="configure",
        )
    if not isinstance(implementation, PairTransformationRule):
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            stage="configure",
            code="pair_protocol_mismatch",
        )
    metadata_mismatches = tuple(
        name
        for name, actual, expected in (
            ("family_id", implementation.family_id, configured.family.family_id),
            ("rule_version", implementation.rule_version, configured.rule.rule_version),
            ("polarity", implementation.polarity, configured.rule.polarity),
            (
                "implementation_key",
                implementation.implementation_key,
                configured.rule.implementation_key,
            ),
            (
                "generation_config_hash",
                getattr(implementation, "generation_config_hash", None),
                loaded.registry_hash,
            ),
        )
        if actual != expected
    )
    if metadata_mismatches:
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            stage="configure",
            code="metadata_mismatch_" + "_".join(sorted(metadata_mismatches)),
        )

    lineage_violations: list[str] = []
    if primary_representation.theorem_id != primary.theorem_id:
        lineage_violations.append("primary_representation_theorem_id")
    if donor_representation.theorem_id != donor.theorem_id:
        lineage_violations.append("donor_representation_theorem_id")
    if (
        primary.context_id != primary_representation.context_id
        or donor.context_id != donor_representation.context_id
        or primary.context_id != donor.context_id
    ):
        lineage_violations.append("context_id")
    if lineage_violations:
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            stage="input",
            code="lineage_" + "_".join(sorted(lineage_violations)),
        )

    try:
        applicability = implementation.assess_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
    except Exception as exc:
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            stage="assess",
            code=type(exc).__name__,
            cause=exc,
        ) from exc
    if not isinstance(applicability, Applicability):
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=None,
            stage="assess",
            code="applicability_type",
        )
    if not applicability.applicable:
        attempt = _attempt(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=applicability,
            terminal_outcome="not_applicable",
        )
        return TransformationExecution(attempt=attempt, drafts=())

    try:
        drafts = tuple(
            implementation.generate_pair(
                primary,
                primary_representation,
                donor,
                donor_representation,
                seed,
            )
        )
    except Exception as exc:
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=applicability,
            stage="generate",
            code=type(exc).__name__,
            cause=exc,
        ) from exc

    violations: list[str] = []
    seen: set[str] = set()
    for draft in drafts:
        if not isinstance(draft, VariantDraft):
            violations.append("draft_type")
            continue
        try:
            verify_variant_draft_id(draft)
        except ValueError:
            violations.append("draft_id")
        if draft.draft_id in seen:
            violations.append("duplicate_draft_id")
        seen.add(draft.draft_id)
        for name, actual, expected in (
            ("rule_id", draft.rule_id, configured.rule.rule_id),
            ("rule_version", draft.rule_version, configured.rule.rule_version),
            ("family_id", draft.family_id, configured.family.family_id),
            ("seed", draft.seed, seed),
            ("context_id", draft.context_id, context_id),
            ("generation_config_hash", draft.generation_config_hash, loaded.registry_hash),
            ("source_theorem_ids", draft.source_theorem_ids, source_theorem_ids),
            (
                "source_representation_ids",
                draft.source_representation_ids,
                source_representation_ids,
            ),
        ):
            if actual != expected:
                violations.append(name)
        if draft.intended_relation not in configured.rule.allowed_intentions:
            violations.append("intended_relation")
        if not set(draft.intended_error_types).issubset(configured.rule.allowed_error_types):
            violations.append("intended_error_types")
    if violations:
        raise _failure(
            loaded=loaded,
            configured=configured,
            source_theorem_ids=source_theorem_ids,
            source_representation_ids=source_representation_ids,
            context_id=context_id,
            seed=seed,
            applicability=applicability,
            stage="generate",
            code="draft_" + "_".join(sorted(set(violations))),
        )

    outcome: Literal["generated", "no_output"] = "generated" if drafts else "no_output"
    attempt = _attempt(
        loaded=loaded,
        configured=configured,
        source_theorem_ids=source_theorem_ids,
        source_representation_ids=source_representation_ids,
        context_id=context_id,
        seed=seed,
        applicability=applicability,
        terminal_outcome=outcome,
        draft_ids=tuple(draft.draft_id for draft in drafts),
    )
    return TransformationExecution(attempt=attempt, drafts=drafts)


def audit_pair_transformation(
    loaded: LoadedTransformationRegistry,
    implementation: PairTransformationRule,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
    candidate: TheoremRecord,
    candidate_representation: RepresentationRecord,
    draft: VariantDraft,
) -> TransformationAudit:
    """Registry-bind and validate a pair rule's mechanical draft audit."""

    loaded = LoadedTransformationRegistry.model_validate(loaded.model_dump(mode="json"))
    configured = _find_config(loaded, getattr(implementation, "rule_id", ""))
    if configured is None:
        raise PairTransformationDispatchError(
            "rule_unlisted",
            execution=None,
            stage="audit",
        )
    if (
        configured.family.status == TransformationFamilyStatus.DISABLED
        or configured.family.family_id not in loaded.profile.active_family_ids
        or configured.rule.implementation_status != RuleImplementationStatus.AVAILABLE
    ):
        raise PairTransformationDispatchError(
            "implementation_unavailable",
            execution=None,
            stage="audit",
        )
    if not isinstance(implementation, PairTransformationRule):
        raise PairTransformationDispatchError(
            "pair_protocol_mismatch",
            execution=None,
            stage="audit",
        )
    metadata_mismatches = tuple(
        name
        for name, actual, expected in (
            ("family_id", implementation.family_id, configured.family.family_id),
            ("rule_version", implementation.rule_version, configured.rule.rule_version),
            ("polarity", implementation.polarity, configured.rule.polarity),
            (
                "implementation_key",
                implementation.implementation_key,
                configured.rule.implementation_key,
            ),
            (
                "generation_config_hash",
                implementation.generation_config_hash,
                loaded.registry_hash,
            ),
        )
        if actual != expected
    )
    if metadata_mismatches:
        raise PairTransformationDispatchError(
            "metadata_mismatch_" + "_".join(sorted(metadata_mismatches)),
            execution=None,
            stage="audit",
        )
    if primary.theorem_id == donor.theorem_id:
        raise PairTransformationDispatchError(
            "source_theorems_not_distinct",
            execution=None,
            stage="audit",
        )
    if primary_representation.representation_id == donor_representation.representation_id:
        raise PairTransformationDispatchError(
            "source_representations_not_distinct",
            execution=None,
            stage="audit",
        )
    source_theorem_ids, source_representation_ids = _source_pairs(
        primary,
        primary_representation,
        donor,
        donor_representation,
    )
    preflight: list[str] = []
    if primary_representation.theorem_id != primary.theorem_id:
        preflight.append("primary_representation_theorem_id")
    if donor_representation.theorem_id != donor.theorem_id:
        preflight.append("donor_representation_theorem_id")
    if candidate_representation.theorem_id != candidate.theorem_id:
        preflight.append("candidate_representation_theorem_id")
    if (
        primary.context_id != primary_representation.context_id
        or donor.context_id != donor_representation.context_id
        or candidate.context_id != candidate_representation.context_id
        or primary.context_id != donor.context_id
        or primary.context_id != candidate.context_id
    ):
        preflight.append("context_id")
    try:
        verify_variant_draft_id(draft)
    except ValueError:
        preflight.append("draft_id")
    for name, actual, expected in (
        ("draft_rule_id", draft.rule_id, configured.rule.rule_id),
        ("draft_rule_version", draft.rule_version, configured.rule.rule_version),
        ("draft_family_id", draft.family_id, configured.family.family_id),
        ("draft_context_id", draft.context_id, primary.context_id),
        (
            "draft_generation_config_hash",
            draft.generation_config_hash,
            loaded.registry_hash,
        ),
        ("draft_source_theorem_ids", draft.source_theorem_ids, source_theorem_ids),
        (
            "draft_source_representation_ids",
            draft.source_representation_ids,
            source_representation_ids,
        ),
    ):
        if actual != expected:
            preflight.append(name)
    if draft.intended_relation not in configured.rule.allowed_intentions:
        preflight.append("draft_intended_relation")
    if not set(draft.intended_error_types).issubset(configured.rule.allowed_error_types):
        preflight.append("draft_intended_error_types")
    if preflight:
        raise PairTransformationDispatchError(
            "preflight_" + "_".join(sorted(set(preflight))),
            execution=None,
            stage="audit",
        )

    try:
        audit = implementation.audit_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
            candidate,
            candidate_representation,
            draft,
        )
    except Exception as exc:
        raise PairTransformationDispatchError(
            type(exc).__name__,
            execution=None,
            stage="audit",
        ) from exc
    if not isinstance(audit, TransformationAudit):
        raise PairTransformationDispatchError(
            "audit_type",
            execution=None,
            stage="audit",
        )
    violations: list[str] = []
    audit_comparisons: tuple[tuple[str, object, object], ...] = (
        ("audit_draft_id", audit.draft_id, draft.draft_id),
        ("audit_family_id", audit.family_id, configured.family.family_id),
        ("audit_rule_id", audit.rule_id, configured.rule.rule_id),
        ("audit_rule_version", audit.rule_version, configured.rule.rule_version),
        ("audit_context_id", audit.context_id, primary.context_id),
        ("candidate_theorem_id", audit.candidate_theorem_id, candidate.theorem_id),
        (
            "candidate_representation_id",
            audit.candidate_representation_id,
            candidate_representation.representation_id,
        ),
        ("candidate_code_hash", audit.candidate_code_hash, draft.candidate_code_hash),
    )
    for audit_name, audit_actual, audit_expected in audit_comparisons:
        if audit_actual != audit_expected:
            violations.append(audit_name)
    try:
        verify_transformation_audit_id(audit)
    except ValueError:
        violations.append("audit_id")
    if audit.recommended_quality_tier not in (
        QualityTier.PROVISIONAL,
        QualityTier.UNKNOWN,
    ):
        violations.append("self_promotion")
    if violations:
        raise PairTransformationDispatchError(
            "result_" + "_".join(sorted(set(violations))),
            execution=None,
            stage="audit",
        )
    return audit


__all__ = [
    "PairTransformationDispatchError",
    "audit_pair_transformation",
    "execute_pair_transformation",
]

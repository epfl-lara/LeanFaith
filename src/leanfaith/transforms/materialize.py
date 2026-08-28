"""Deterministic Phase-4 candidate and pair materialization.

Transformation rules emit intentions and mechanical audit recommendations,
never semantic labels.  This module turns one audited draft into the derived
``TheoremRecord`` and unlabeled ``PairRecord`` needed by the Phase-4 data
pipeline while enforcing complete ancestry and immutable semantic IDs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from leanfaith.schemas.enums import ValidationStatus
from leanfaith.schemas.ids import ANCESTRY_PREFIX, PAIR_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.pair import PairRecord, check_pair_groups
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.schemas.variant import TransformationAudit, VariantDraft
from leanfaith.transforms.protocol import (
    TransformationIdentityError,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)

_ELABORATING_STATUSES = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_DERIVED_THEOREM_SCHEMA = "deterministic_transform_theorem_v1"
_DETERMINISTIC_PAIR_SCHEMA = "deterministic_transform_pair_v1"
_SEMANTIC_LABEL_METADATA_KEYS = frozenset(
    {
        "decision",
        "error_types",
        "eval_eligibility",
        "faithfulness_levels",
        "quality_tier",
        "relation",
        "requires_adjudication",
        "resolution_method",
        "resolution_outcome",
        "resolved_label_id",
        "same_claim",
        "train_eligibility",
        "truth_A_implies_B",
        "truth_B_implies_A",
    }
)


def _source_map(sources: Sequence[TheoremRecord]) -> dict[str, TheoremRecord]:
    mapped = {source.theorem_id: source for source in sources}
    if not mapped:
        raise TransformationIdentityError("at least one source theorem is required")
    if len(mapped) != len(sources):
        raise TransformationIdentityError("source theorem IDs must be unique")
    return mapped


def _merge_provenance_metadata(
    *,
    fixed: Mapping[str, str | int | float | bool | None],
    supplied: Mapping[str, str | int | float | bool | None] | None,
    record_kind: str,
) -> dict[str, str | int | float | bool | None]:
    """Merge caller metadata without allowing lineage or label reinterpretation."""

    extra = dict(supplied or {})
    forbidden = set(fixed) | _SEMANTIC_LABEL_METADATA_KEYS
    collisions = sorted(set(extra) & forbidden)
    if collisions:
        raise TransformationIdentityError(
            f"{record_kind} metadata cannot override lineage or carry resolved-label "
            f"fields: {', '.join(collisions)}"
        )
    return {**fixed, **extra}


def _expected_derived_theorem_id(
    *,
    draft: VariantDraft,
    primary_source_id: str,
) -> str:
    return make_id(
        THEOREM_PREFIX,
        {
            "schema": _DERIVED_THEOREM_SCHEMA,
            "context_id": draft.context_id,
            "draft_id": draft.draft_id,
            "source_theorem_ids": draft.source_theorem_ids,
            "primary_source_id": primary_source_id,
            "candidate_code_hash": draft.candidate_code_hash,
        },
    )


def _expected_derived_ancestry_id(
    *,
    draft: VariantDraft,
    root_ancestry_ids: tuple[str, ...],
) -> str:
    return make_id(
        ANCESTRY_PREFIX,
        {
            "schema": _DERIVED_THEOREM_SCHEMA,
            "draft_id": draft.draft_id,
            "parent_theorem_ids": draft.source_theorem_ids,
            "root_ancestry_ids": root_ancestry_ids,
        },
    )


def _check_n10_sources(
    draft: VariantDraft,
    source_by_id: Mapping[str, TheoremRecord],
    *,
    primary_source_id: str,
) -> None:
    if draft.family_id != "n10_nearby_theorem":
        return
    if len(source_by_id) != 2:
        raise TransformationIdentityError("N10 requires exactly two source theorems")
    first, second = (source_by_id[theorem_id] for theorem_id in draft.source_theorem_ids)
    if set(first.root_ancestry_ids) & set(second.root_ancestry_ids):
        raise TransformationIdentityError("N10 source root ancestries must be disjoint")
    trace_primary_ids = {
        value
        for step in draft.transformation_trace
        if isinstance((value := step.get("primary_theorem_id")), str)
    }
    trace_donor_ids = {
        value
        for step in draft.transformation_trace
        if isinstance((value := step.get("donor_theorem_id")), str)
    }
    expected_donor_ids = set(source_by_id) - {primary_source_id}
    if trace_primary_ids != {primary_source_id} or trace_donor_ids != expected_donor_ids:
        raise TransformationIdentityError(
            "N10 trace must identify exactly the supplied primary and donor sources"
        )


def _verify_audit_matches_draft(
    *,
    audit: TransformationAudit,
    draft: VariantDraft,
) -> None:
    comparisons = (
        ("draft_id", audit.draft_id, draft.draft_id),
        ("family_id", audit.family_id, draft.family_id),
        ("rule_id", audit.rule_id, draft.rule_id),
        ("rule_version", audit.rule_version, draft.rule_version),
        ("context_id", audit.context_id, draft.context_id),
        ("candidate_code_hash", audit.candidate_code_hash, draft.candidate_code_hash),
    )
    mismatches = [name for name, actual, expected in comparisons if actual != expected]
    if mismatches:
        raise TransformationIdentityError(
            "audit does not match draft fields: " + ", ".join(mismatches)
        )


def build_derived_theorem_record(
    *,
    draft: VariantDraft,
    sources: Sequence[TheoremRecord],
    primary_source_id: str,
    elaboration_status: ValidationStatus,
    elaboration_diagnostics: Sequence[str] = (),
    inline_elaboration_source: str | None = None,
    metadata: Mapping[str, str | int | float | bool | None] | None = None,
) -> TheoremRecord:
    """Build one deterministic child theorem without inferring a label.

    ``primary_source_id`` identifies the declaration interface retained by the
    candidate.  Unary rules use their only source; N10 explicitly supplies its
    primary rather than relying on the lexicographic ordering of its two
    parents.
    """

    verify_variant_draft_id(draft)
    source_by_id = _source_map(sources)
    expected_source_ids = tuple(sorted(source_by_id))
    if expected_source_ids != draft.source_theorem_ids:
        raise TransformationIdentityError(
            "source theorem IDs must equal every draft source theorem ID"
        )
    if primary_source_id not in source_by_id:
        raise TransformationIdentityError("primary source theorem is not linked by the draft")
    primary = source_by_id[primary_source_id]
    if any(source.context_id != draft.context_id for source in sources):
        raise TransformationIdentityError("all source theorem contexts must match the draft")
    if any(not source.is_proposition for source in sources):
        raise TransformationIdentityError("all source theorems must be propositions")
    if any(source.elaboration_status not in _ELABORATING_STATUSES for source in sources):
        raise TransformationIdentityError("all source theorems must have an elaborating status")
    _check_n10_sources(
        draft,
        source_by_id,
        primary_source_id=primary_source_id,
    )
    if elaboration_status not in _ELABORATING_STATUSES:
        raise TransformationIdentityError(
            "a materialized deterministic theorem must have an elaborating status"
        )

    parent_ids = draft.source_theorem_ids
    root_ancestry_ids = tuple(
        sorted({root for source in sources for root in source.root_ancestry_ids})
    )
    candidate_hash = hashlib.sha256(draft.candidate_code.encode("utf-8")).hexdigest()
    if candidate_hash != draft.candidate_code_hash:
        raise TransformationIdentityError("draft candidate hash does not match candidate code")
    effective_inline_source = inline_elaboration_source or draft.candidate_code
    if effective_inline_source.count(draft.candidate_code) != 1:
        raise TransformationIdentityError(
            "candidate inline elaboration source must contain candidate code exactly once"
        )

    theorem_id = _expected_derived_theorem_id(
        draft=draft,
        primary_source_id=primary_source_id,
    )
    ancestry_id = _expected_derived_ancestry_id(
        draft=draft,
        root_ancestry_ids=root_ancestry_ids,
    )
    record_metadata = _merge_provenance_metadata(
        fixed={
            "candidate_pool": draft.candidate_pool,
            "draft_id": draft.draft_id,
            "family_id": draft.family_id,
            "generation_intention_only": True,
            "primary_source_id": primary_source_id,
            "resolved_semantic_label": False,
            "rule_id": draft.rule_id,
            "rule_version": draft.rule_version,
        },
        supplied=metadata,
        record_kind="derived theorem",
    )

    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=root_ancestry_ids,
        parent_theorem_ids=parent_ids,
        source="deterministic_transform",
        source_revision=draft.generation_config_hash,
        source_record=draft.draft_id,
        context_id=draft.context_id,
        declaration_kind=primary.declaration_kind,
        declaration_name=primary.declaration_name,
        declaration_full_name=primary.declaration_full_name,
        declaration_ordinal=primary.declaration_ordinal,
        proof_stripped_declaration=draft.candidate_code,
        inline_elaboration_source=effective_inline_source,
        is_proposition=primary.is_proposition,
        elaboration_status=elaboration_status,
        elaboration_diagnostics=tuple(elaboration_diagnostics),
        statement_content_hash=candidate_hash,
        metadata=record_metadata,
    )


def build_deterministic_pair_record(
    *,
    source: TheoremRecord,
    candidate: TheoremRecord,
    draft: VariantDraft,
    audit: TransformationAudit,
    all_sources: Sequence[TheoremRecord] | None = None,
    metadata: Mapping[str, str | int | float | bool | None] | None = None,
) -> PairRecord:
    """Build an unlabeled source-candidate pair with complete split groups.

    Unary drafts may omit ``all_sources`` because ``source`` is their only
    parent. Multi-parent drafts (currently N10) must provide every source so
    donor ancestry cannot be lost by trusting a preconstructed candidate.
    """

    verify_variant_draft_id(draft)
    verify_transformation_audit_id(audit)
    sources = tuple(all_sources) if all_sources is not None else (source,)
    source_by_id = _source_map(sources)
    if len(draft.source_theorem_ids) > 1 and all_sources is None:
        raise TransformationIdentityError(
            "multi-parent pair materialization requires every source theorem"
        )
    if tuple(sorted(source_by_id)) != draft.source_theorem_ids:
        raise TransformationIdentityError("pair sources must equal every draft source theorem ID")
    if source.theorem_id not in source_by_id:
        raise TransformationIdentityError("pair source is not present in all_sources")
    if candidate.parent_theorem_ids != draft.source_theorem_ids:
        raise TransformationIdentityError(
            "candidate parents must equal every draft source theorem ID"
        )
    if any(parent.context_id != draft.context_id for parent in sources):
        raise TransformationIdentityError("pair source contexts must match the draft")
    if candidate.context_id != draft.context_id:
        raise TransformationIdentityError("pair candidate context must match the draft")
    _check_n10_sources(
        draft,
        source_by_id,
        primary_source_id=source.theorem_id,
    )
    expected_roots = tuple(
        sorted({root for parent in sources for root in parent.root_ancestry_ids})
    )
    if candidate.root_ancestry_ids != expected_roots:
        raise TransformationIdentityError(
            "candidate root ancestries must equal the union of every draft source"
        )
    primary_source_id = candidate.metadata.get("primary_source_id")
    if primary_source_id != source.theorem_id:
        raise TransformationIdentityError(
            "pair theorem A must be the candidate's recorded primary source"
        )
    if candidate.theorem_id != _expected_derived_theorem_id(
        draft=draft,
        primary_source_id=source.theorem_id,
    ):
        raise TransformationIdentityError("candidate theorem ID does not match draft lineage")
    if candidate.ancestry_id != _expected_derived_ancestry_id(
        draft=draft,
        root_ancestry_ids=expected_roots,
    ):
        raise TransformationIdentityError("candidate ancestry ID does not match draft lineage")
    if candidate.source != "deterministic_transform":
        raise TransformationIdentityError("candidate theorem source is not deterministic_transform")
    if candidate.source_revision != draft.generation_config_hash:
        raise TransformationIdentityError("candidate source revision does not match draft config")
    if candidate.source_record != draft.draft_id:
        raise TransformationIdentityError("candidate source record does not link to the draft")
    primary = source_by_id[source.theorem_id]
    declaration_identity = (
        candidate.declaration_kind,
        candidate.declaration_name,
        candidate.declaration_full_name,
        candidate.declaration_ordinal,
    )
    expected_declaration_identity = (
        primary.declaration_kind,
        primary.declaration_name,
        primary.declaration_full_name,
        primary.declaration_ordinal,
    )
    if declaration_identity != expected_declaration_identity:
        raise TransformationIdentityError(
            "candidate declaration identity does not match the primary source"
        )
    if candidate.proof_stripped_declaration != draft.candidate_code:
        raise TransformationIdentityError("candidate theorem text does not match draft code")
    if (
        candidate.inline_elaboration_source is None
        or candidate.inline_elaboration_source.count(draft.candidate_code) != 1
    ):
        raise TransformationIdentityError(
            "candidate inline elaboration source does not contain draft code exactly once"
        )
    if candidate.statement_content_hash != draft.candidate_code_hash:
        raise TransformationIdentityError("candidate theorem hash does not match draft code hash")
    if not candidate.is_proposition:
        raise TransformationIdentityError("candidate theorem must be proposition-valued")
    if candidate.elaboration_status not in _ELABORATING_STATUSES:
        raise TransformationIdentityError("candidate theorem must have an elaborating status")
    expected_candidate_metadata = {
        "candidate_pool": draft.candidate_pool,
        "draft_id": draft.draft_id,
        "family_id": draft.family_id,
        "generation_intention_only": True,
        "primary_source_id": source.theorem_id,
        "resolved_semantic_label": False,
        "rule_id": draft.rule_id,
        "rule_version": draft.rule_version,
    }
    mismatched_candidate_metadata = sorted(
        key
        for key, expected in expected_candidate_metadata.items()
        if candidate.metadata.get(key) != expected
    )
    injected_label_keys = sorted(_SEMANTIC_LABEL_METADATA_KEYS & set(candidate.metadata))
    if mismatched_candidate_metadata or injected_label_keys:
        details = sorted(set(mismatched_candidate_metadata) | set(injected_label_keys))
        raise TransformationIdentityError(
            "candidate metadata violates lineage/no-label invariants: " + ", ".join(details)
        )
    _verify_audit_matches_draft(audit=audit, draft=draft)
    if audit.candidate_theorem_id != candidate.theorem_id:
        raise TransformationIdentityError("audit does not reference the candidate theorem")
    if audit.recommended_validation_status not in _ELABORATING_STATUSES:
        raise TransformationIdentityError(
            "a persisted deterministic pair requires an elaborating candidate audit"
        )
    if audit.recommended_validation_status != candidate.elaboration_status:
        raise TransformationIdentityError(
            "audit validation status does not match candidate theorem status"
        )
    if audit.violation_codes:
        raise TransformationIdentityError(
            "a deterministic pair cannot be materialized from a quarantined audit"
        )

    split_group_ids = expected_roots
    payload = {
        "schema": _DETERMINISTIC_PAIR_SCHEMA,
        "theorem_a_id": source.theorem_id,
        "theorem_b_id": candidate.theorem_id,
        "draft_id": draft.draft_id,
        "audit_id": audit.audit_id,
        "split_group_ids": split_group_ids,
    }
    pair_metadata = _merge_provenance_metadata(
        fixed={
            "audit_id": audit.audit_id,
            "candidate_pool": draft.candidate_pool,
            "draft_id": draft.draft_id,
            "generation_intention_only": True,
            "near_miss": draft.intended_relation.value == "near_miss",
            "primary_source_id": source.theorem_id,
            "resolved_semantic_label": False,
        },
        supplied=metadata,
        record_kind="pair",
    )

    pair = PairRecord(
        pair_id=make_id(PAIR_PREFIX, payload),
        theorem_a_id=source.theorem_id,
        theorem_b_id=candidate.theorem_id,
        pair_source="deterministic_transform",
        split_group_ids=split_group_ids,
        generator_id=draft.rule_id,
        transformation_family=draft.family_id,
        intended_relation=draft.intended_relation,
        resolved_label_id=None,
        split_eligible=True,
        metadata=pair_metadata,
    )
    violations = check_pair_groups(pair, source, candidate)
    if violations:
        raise TransformationIdentityError(
            "deterministic pair split-group mismatch: " + ", ".join(violations)
        )
    return pair


__all__ = [
    "build_derived_theorem_record",
    "build_deterministic_pair_record",
]

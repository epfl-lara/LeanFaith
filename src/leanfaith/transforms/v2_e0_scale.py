"""Pooled scale materialization for the experimental deterministic-v2 E0 slice.

This module is deliberately an orchestration layer, not a semantic resolver.
It preserves one terminal :class:`V2E0MaterializationResult` per input while
using the configured LeanInteract pool for the two expensive stages:

1. independent candidate elaboration requests;
2. independent candidate representation requests.

Every output remains provisional, unlabeled, unpromoted, and ineligible for
training.  Mixed Lean contexts are rejected before rule execution or Lean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import Polarity, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import TransformationAttempt, VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.protocol import build_deterministic_variant_record
from leanfaith.transforms.v2_e0_materializer import (
    V2E0MaterializationResult,
    _build_result,
    _inline_candidate_source,
)
from leanfaith.transforms.v2_e0_runtime import V2E0RuleId, V2E0Runtime


class V2E0ScaleError(ValueError):
    """The pooled E0 request violated a fail-closed orchestration invariant."""


@dataclass(frozen=True, slots=True)
class V2E0MaterializationInput:
    """One ordered source/rule attempt in a homogeneous Lean context."""

    theorem: TheoremRecord
    representation: RepresentationRecord
    rule_id: V2E0RuleId
    seed: int


@dataclass(frozen=True, slots=True)
class _GeneratedCandidate:
    index: int
    item: V2E0MaterializationInput
    attempt: TransformationAttempt
    draft: VariantDraft
    inline_source: str


def _common(
    runtime: V2E0Runtime,
    item: V2E0MaterializationInput,
    attempt: TransformationAttempt,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_id": runtime.loaded.config.profile_id,
        "profile_config_hash": runtime.generation_config_hash,
        "rule_id": item.rule_id,
        "attempt": attempt,
        "resolved_label_count": 0,
        "promoted_item_count": 0,
        "training_eligible": False,
    }


def _candidate_request(candidate: _GeneratedCandidate) -> LeanRequest:
    return LeanRequest(
        request_id=f"v2-e0-{candidate.draft.draft_id.removeprefix('draft:')[:24]}",
        context_id=candidate.draft.context_id,
        code=candidate.inline_source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={
            "artifact_kind": "v2_e0_candidate",
            "draft_id": candidate.draft.draft_id,
        },
    )


def _candidate_representation_input(candidate: TheoremRecord) -> TheoremForRepresentation:
    if candidate.declaration_full_name is None:
        raise V2E0ScaleError("candidate has no declaration_full_name")
    return TheoremForRepresentation(
        theorem_id=candidate.theorem_id,
        full_name=candidate.declaration_full_name,
        proof_stripped=candidate.proof_stripped_declaration,
        context_id=candidate.context_id,
        inline_declaration=True,
        inline_source=candidate.inline_elaboration_source,
    )


def materialize_v2_e0_batch(
    *,
    backend: LeanInteractBackend,
    runtime: V2E0Runtime,
    inputs: Sequence[V2E0MaterializationInput],
    context_id: str,
    project_dir: Path,
    import_header: str,
) -> tuple[V2E0MaterializationResult, ...]:
    """Materialize one homogeneous batch through pooled LeanInteract calls.

    Input order and cardinality are preserved, including not-applicable,
    invalid, and quarantined entries.  A backend cardinality mismatch is an
    orchestration failure rather than evidence about any candidate.
    """

    if not inputs:
        return ()
    mismatches: list[str] = []
    for item in inputs:
        if item.theorem.theorem_id != item.representation.theorem_id:
            mismatches.append(f"{item.theorem.theorem_id}:lineage")
        if item.theorem.context_id != item.representation.context_id:
            mismatches.append(f"{item.theorem.theorem_id}:source_context")
        if item.theorem.context_id != context_id:
            mismatches.append(f"{item.theorem.theorem_id}:batch_context")
    if mismatches:
        raise V2E0ScaleError(
            "mixed or inconsistent contexts/lineage rejected before Lean execution: "
            + ", ".join(mismatches)
        )

    ordered: list[V2E0MaterializationResult | None] = [None] * len(inputs)
    generated: list[_GeneratedCandidate] = []
    for index, item in enumerate(inputs):
        execution = runtime.execute(
            item.rule_id,
            item.theorem,
            item.representation,
            item.seed,
        )
        common = _common(runtime, item, execution.attempt)
        if execution.attempt.terminal_outcome == "not_applicable":
            ordered[index] = _build_result(terminal_status="not_applicable", **common)
            continue
        if not execution.drafts:
            ordered[index] = _build_result(terminal_status="no_output", **common)
            continue
        if len(execution.drafts) != 1:
            raise V2E0ScaleError("v2 E0 rules must emit at most one draft per input")
        draft = execution.drafts[0]
        generated.append(
            _GeneratedCandidate(
                index=index,
                item=item,
                attempt=execution.attempt,
                draft=draft,
                inline_source=_inline_candidate_source(
                    item.theorem,
                    draft.candidate_code,
                    project_dir=project_dir,
                    import_header=import_header,
                ),
            )
        )

    lean_results = (
        backend.run_batch([_candidate_request(item) for item in generated]) if generated else []
    )
    if len(lean_results) != len(generated):
        raise V2E0ScaleError(
            "candidate Lean batch cardinality mismatch: "
            f"expected {len(generated)}, received {len(lean_results)}"
        )

    valid: list[tuple[_GeneratedCandidate, TheoremRecord]] = []
    for generated_item, lean_result in zip(generated, lean_results, strict=True):
        common = _common(runtime, generated_item.item, generated_item.attempt)
        if lean_result.request_id != _candidate_request(generated_item).request_id:
            raise V2E0ScaleError("candidate Lean batch did not preserve request order")
        if lean_result.context_id != context_id:
            raise V2E0ScaleError("candidate Lean result context does not match batch context")
        if lean_result.status not in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
            ordered[generated_item.index] = _build_result(
                terminal_status="candidate_invalid",
                draft=generated_item.draft,
                failure_codes=(f"lean_{lean_result.status.value}",),
                **common,
            )
            continue
        validation_status = (
            ValidationStatus.ELABORATES
            if lean_result.status == LeanStatus.VALID
            else ValidationStatus.ELABORATES_WITH_PLACEHOLDER
        )
        theorem = generated_item.item.theorem
        candidate = build_derived_theorem_record(
            draft=generated_item.draft,
            sources=(theorem,),
            primary_source_id=theorem.theorem_id,
            elaboration_status=validation_status,
            elaboration_diagnostics=tuple(
                str(message.get("data", "")) for message in lean_result.messages
            ),
            inline_elaboration_source=generated_item.inline_source,
            metadata={
                "profile_id": runtime.loaded.config.profile_id,
                "training_eligible": False,
                "validation_request_hash": lean_result.request_hash,
            },
        )
        valid.append((generated_item, candidate))

    candidate_representations: list[RepresentationRecord] = []
    if valid:
        candidate_representations = build_representations(
            backend,
            [_candidate_representation_input(candidate) for _, candidate in valid],
            imports=import_header,
            created_at=valid[0][0].item.representation.created_at,
        )
        if len(candidate_representations) != len(valid):
            raise V2E0ScaleError(
                "candidate representation batch cardinality mismatch: "
                f"expected {len(valid)}, received {len(candidate_representations)}"
            )

    for (generated_item, candidate), candidate_representation in zip(
        valid,
        candidate_representations,
        strict=True,
    ):
        common = _common(runtime, generated_item.item, generated_item.attempt)
        if candidate_representation.theorem_id != candidate.theorem_id:
            raise V2E0ScaleError("candidate representation batch did not preserve input order")
        if candidate_representation.context_id != context_id:
            raise V2E0ScaleError("candidate representation context does not match batch context")
        audit = runtime.audit(
            generated_item.item.rule_id,
            generated_item.item.theorem,
            generated_item.item.representation,
            candidate,
            candidate_representation,
            generated_item.draft,
        )
        if audit.violation_codes:
            ordered[generated_item.index] = _build_result(
                terminal_status="audit_quarantined",
                draft=generated_item.draft,
                candidate_theorem=candidate,
                candidate_representation=candidate_representation,
                audit=audit,
                failure_codes=audit.violation_codes,
                **common,
            )
            continue
        variant = build_deterministic_variant_record(
            attempt=generated_item.attempt,
            draft=generated_item.draft,
            audit=audit,
            candidate=candidate,
            candidate_representation=candidate_representation,
            polarity=Polarity.POSITIVE,
            metadata={
                "evidence_class": "E0",
                "profile_id": runtime.loaded.config.profile_id,
                "resolved_semantic_label": False,
                "training_eligible": False,
            },
        )
        ordered[generated_item.index] = _build_result(
            terminal_status="provisional_variant",
            draft=generated_item.draft,
            candidate_theorem=candidate,
            candidate_representation=candidate_representation,
            audit=audit,
            variant=variant,
            **common,
        )

    if any(result is None for result in ordered):
        raise V2E0ScaleError("pooled materialization left an input without a terminal result")
    return tuple(result for result in ordered if result is not None)


__all__ = [
    "V2E0MaterializationInput",
    "V2E0ScaleError",
    "materialize_v2_e0_batch",
]

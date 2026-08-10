"""LeanInteract-backed one-source materializer for experimental P11/P12.

Draft generation is syntactic and provisional.  A ``VariantRecord`` is
created only after independent Lean re-elaboration and the rule's clean E0
audit.  No label, pair resolution, promotion, or training eligibility is
created here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import Polarity, QualityTier, ValidationStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
)
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.protocol import build_deterministic_variant_record
from leanfaith.transforms.v2_e0_runtime import V2E0ProfileId, V2E0RuleId, V2E0Runtime


class V2E0MaterializationError(ValueError):
    """Input, Lean execution, persistence, or lineage failed closed."""


class V2E0MaterializationResult(StrictModel):
    schema_version: Literal[1] = 1
    result_id: str
    profile_id: V2E0ProfileId
    profile_config_hash: str
    rule_id: V2E0RuleId
    terminal_status: Literal[
        "not_applicable",
        "no_output",
        "candidate_invalid",
        "candidate_representation_failed",
        "audit_quarantined",
        "provisional_variant",
    ]
    attempt: TransformationAttempt
    draft: VariantDraft | None = None
    candidate_theorem: TheoremRecord | None = None
    candidate_representation: RepresentationRecord | None = None
    audit: TransformationAudit | None = None
    variant: VariantRecord | None = None
    failure_codes: tuple[str, ...] = ()
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> V2E0MaterializationResult:
        if self.failure_codes != tuple(sorted(set(self.failure_codes))):
            raise ValueError("failure_codes must be sorted and unique")
        if self.terminal_status == "provisional_variant":
            if any(
                item is None
                for item in (
                    self.draft,
                    self.candidate_theorem,
                    self.candidate_representation,
                    self.audit,
                    self.variant,
                )
            ):
                raise ValueError("provisional_variant requires complete mechanical lineage")
            assert self.audit is not None
            assert self.variant is not None
            if self.audit.violation_codes:
                raise ValueError("a provisional variant cannot carry audit violations")
            if self.audit.recommended_quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("v2 E0 output must remain provisional")
            if self.variant.quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("v2 E0 VariantRecord must remain provisional")
        elif self.variant is not None:
            raise ValueError("only a clean provisional result may carry a VariantRecord")
        expected = make_id("v2e0_result", _result_payload(self))
        if self.result_id != expected:
            raise ValueError("v2 E0 result_id does not match its semantic payload")
        return self


def _result_payload(result: V2E0MaterializationResult) -> dict[str, object]:
    return {
        key: value for key, value in result.model_dump(mode="json").items() if key != "result_id"
    }


def _build_result(**data: object) -> V2E0MaterializationResult:
    placeholder = V2E0MaterializationResult.model_construct(
        _fields_set=None,
        result_id=f"v2e0_result:{'0' * 64}",
        **data,
    )
    return V2E0MaterializationResult.model_validate(
        {"result_id": make_id("v2e0_result", _result_payload(placeholder)), **data}
    )


def _inline_candidate_source(
    source: TheoremRecord,
    candidate_code: str,
    *,
    project_dir: Path,
    import_header: str,
) -> str:
    if source.inline_elaboration_source is not None:
        if source.inline_elaboration_source.count(source.proof_stripped_declaration) != 1:
            raise V2E0MaterializationError(
                "inline source must contain the source declaration exactly once"
            )
        return source.inline_elaboration_source.replace(
            source.proof_stripped_declaration,
            candidate_code,
            1,
        )
    if source.source_file is not None and source.source_range is not None:
        source_path = (project_dir / source.source_file).resolve()
        if not source_path.is_relative_to(project_dir.resolve()):
            raise V2E0MaterializationError("source_file escapes the Lean project")
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
        start_line = source.source_range[0]
        if start_line < 1 or start_line > len(lines) + 1:
            raise V2E0MaterializationError("source_range starts outside source_file")
        return "".join(lines[: start_line - 1]) + candidate_code + "\n"
    return "\n".join(part for part in (import_header.strip(), candidate_code) if part)


def _candidate_representation(
    backend: LeanInteractBackend,
    candidate: TheoremRecord,
    *,
    created_at: object,
) -> RepresentationRecord:
    if candidate.declaration_full_name is None:
        raise V2E0MaterializationError("candidate has no declaration_full_name")
    records = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=candidate.theorem_id,
                full_name=candidate.declaration_full_name,
                proof_stripped=candidate.proof_stripped_declaration,
                context_id=candidate.context_id,
                inline_declaration=True,
                inline_source=candidate.inline_elaboration_source,
            )
        ],
        imports="",
        created_at=created_at,  # type: ignore[arg-type]
    )
    if len(records) != 1:
        raise V2E0MaterializationError("candidate representation did not return exactly one record")
    return records[0]


def materialize_v2_e0_candidate(
    *,
    backend: LeanInteractBackend,
    runtime: V2E0Runtime,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    rule_id: V2E0RuleId,
    seed: int,
    project_dir: Path,
    import_header: str,
) -> V2E0MaterializationResult:
    execution = runtime.execute(rule_id, theorem, representation, seed)
    common: dict[str, object] = {
        "schema_version": 1,
        "profile_id": runtime.loaded.config.profile_id,
        "profile_config_hash": runtime.generation_config_hash,
        "rule_id": rule_id,
        "attempt": execution.attempt,
        "resolved_label_count": 0,
        "promoted_item_count": 0,
        "training_eligible": False,
    }
    if execution.attempt.terminal_outcome == "not_applicable":
        return _build_result(terminal_status="not_applicable", **common)
    if not execution.drafts:
        return _build_result(terminal_status="no_output", **common)
    if len(execution.drafts) != 1:
        raise V2E0MaterializationError("P11/P12 must emit at most one draft")
    draft = execution.drafts[0]
    inline_source = _inline_candidate_source(
        theorem,
        draft.candidate_code,
        project_dir=project_dir,
        import_header=import_header,
    )
    request = LeanRequest(
        request_id=f"v2-e0-{draft.draft_id.removeprefix('draft:')[:24]}",
        context_id=draft.context_id,
        code=inline_source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={"artifact_kind": "v2_e0_candidate", "draft_id": draft.draft_id},
    )
    result = backend.run(request)
    if result.status not in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
        return _build_result(
            terminal_status="candidate_invalid",
            draft=draft,
            failure_codes=(f"lean_{result.status.value}",),
            **common,
        )
    validation_status = (
        ValidationStatus.ELABORATES
        if result.status == LeanStatus.VALID
        else ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    )
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(theorem,),
        primary_source_id=theorem.theorem_id,
        elaboration_status=validation_status,
        elaboration_diagnostics=tuple(str(message.get("data", "")) for message in result.messages),
        inline_elaboration_source=inline_source,
        metadata={
            "profile_id": runtime.loaded.config.profile_id,
            "training_eligible": False,
            "validation_request_hash": result.request_hash,
        },
    )
    try:
        candidate_representation = _candidate_representation(
            backend,
            candidate,
            created_at=representation.created_at,
        )
    except (OSError, ValueError) as exc:
        return _build_result(
            terminal_status="candidate_representation_failed",
            draft=draft,
            candidate_theorem=candidate,
            failure_codes=(f"representation_{type(exc).__name__}",),
            **common,
        )
    audit = runtime.audit(
        rule_id,
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    if audit.violation_codes:
        return _build_result(
            terminal_status="audit_quarantined",
            draft=draft,
            candidate_theorem=candidate,
            candidate_representation=candidate_representation,
            audit=audit,
            failure_codes=audit.violation_codes,
            **common,
        )
    variant = build_deterministic_variant_record(
        attempt=execution.attempt,
        draft=draft,
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
    return _build_result(
        terminal_status="provisional_variant",
        draft=draft,
        candidate_theorem=candidate,
        candidate_representation=candidate_representation,
        audit=audit,
        variant=variant,
        **common,
    )


def read_single_record(
    path: Path,
    model: type[TheoremRecord] | type[RepresentationRecord],
) -> TheoremRecord | RepresentationRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise V2E0MaterializationError(f"input must be one JSON object: {path}") from exc
    return model.model_validate(payload)


def write_v2_e0_result(result: V2E0MaterializationResult, output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(result.model_dump(mode="json")) + b"\n"
    temporary = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output_path)
    except FileExistsError as exc:
        raise V2E0MaterializationError(f"output already exists: {output_path}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return hash_file(output_path)


__all__ = [
    "V2E0MaterializationError",
    "V2E0MaterializationResult",
    "materialize_v2_e0_candidate",
    "read_single_record",
    "write_v2_e0_result",
]

"""Strict sidecar and view schemas for the corrected SFT2B source bundle."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.sft2b.schemas import NonEmpty, Sha256, SourceRecord, StableId

WorkbookFlag = Literal[
    "answer_header",
    "boxed_or_fboxed_answer",
    "solution_or_proof_header",
]
WorkbookDisposition = Literal[
    "quarantine_solution_or_answer_discourse",
    "retain_explicit_claim_or_question",
]
SemanticSampleReason = Literal[
    "deterministic_100_per_release_class",
    "workbook_heuristic_hit",
]


class SourceSelectionAuditV2(StrictModel):
    """Evidence needed to replay screening, deduplication, and matched selection."""

    schema_version: Literal["sft2b_source_selection_audit_v2"] = "sft2b_source_selection_audit_v2"
    source_id: StableId
    release_class: NonEmpty
    trust_tier: NonEmpty
    domain: NonEmpty
    upstream_source: NonEmpty
    selection_hash: Sha256
    complexity_score: Annotated[int, Field(ge=0)]
    headless_signature: NonEmpty
    headless_signature_sha256: Sha256
    near_duplicate_hash: Sha256
    problem_identity: NonEmpty
    reference_proposition_sha256: Sha256
    nl_sha256: Sha256
    benchmark_exact_hit: Literal[False]
    benchmark_near_hit: Literal[False]
    benchmark_problem_identity_hit: Literal[False]
    existing_301_overlap: Literal[False]
    denied_family_hit: Literal[False]


class WorkbookDiscourseAuditV2(StrictModel):
    """One row for every flagged Workbook item, including retained claims."""

    schema_version: Literal["sft2b_workbook_discourse_audit_v2"] = (
        "sft2b_workbook_discourse_audit_v2"
    )
    source_id: StableId
    flags: tuple[WorkbookFlag, ...]
    disposition: WorkbookDisposition
    rationale: NonEmpty
    nl_sha256: Sha256
    reference_proposition_sha256: Sha256
    headless_signature: NonEmpty
    headless_signature_sha256: Sha256
    near_duplicate_hash: Sha256
    problem_identity: NonEmpty
    selection_hash: Sha256

    @model_validator(mode="after")
    def validate_flags(self) -> WorkbookDiscourseAuditV2:
        if not self.flags or tuple(sorted(set(self.flags))) != self.flags:
            raise ValueError("Workbook flags must be nonempty, unique, and sorted")
        return self


class WorkbookQuarantineV2(StrictModel):
    """Full auxiliary row for a Workbook source excluded from model-facing core."""

    schema_version: Literal["sft2b_workbook_quarantine_v2"] = "sft2b_workbook_quarantine_v2"
    source: SourceRecord
    discourse_audit: WorkbookDiscourseAuditV2

    @model_validator(mode="after")
    def validate_identity(self) -> WorkbookQuarantineV2:
        if self.source.source_id != self.discourse_audit.source_id:
            raise ValueError("Workbook quarantine source/audit IDs differ")
        if self.discourse_audit.disposition != "quarantine_solution_or_answer_discourse":
            raise ValueError("Workbook quarantine contains a retained row")
        return self


class SemanticAlignmentAuditV2(StrictModel):
    """Deterministic source-contract semantic audit evidence."""

    schema_version: Literal["sft2b_semantic_alignment_audit_v2"] = (
        "sft2b_semantic_alignment_audit_v2"
    )
    source_id: StableId
    release_class: NonEmpty
    sample_reasons: tuple[SemanticSampleReason, ...]
    alignment_disposition: Literal["admit", "quarantine"]
    source_present_in_core: bool
    review_method: Literal["deterministic_source_contract_and_lexical_alignment_v2"]
    evidence_basis: NonEmpty
    nl_sha256: Sha256
    reference_proposition_sha256: Sha256
    headless_signature_sha256: Sha256
    near_duplicate_hash: Sha256
    problem_identity: NonEmpty

    @model_validator(mode="after")
    def validate_reasons_and_disposition(self) -> SemanticAlignmentAuditV2:
        if (
            not self.sample_reasons
            or tuple(sorted(set(self.sample_reasons))) != self.sample_reasons
        ):
            raise ValueError("semantic audit reasons must be nonempty, unique, and sorted")
        if self.source_present_in_core != (self.alignment_disposition == "admit"):
            raise ValueError("semantic audit core-presence/disposition mismatch")
        return self


class LibraryDocstringCorrectionV2(StrictModel):
    """Frozen impact record for one corrupt v1 library source."""

    schema_version: Literal["sft2b_library_docstring_correction_v2"] = (
        "sft2b_library_docstring_correction_v2"
    )
    v1_source_id: StableId
    release_class: Literal["library_mathlib", "library_physlib", "library_cslib"]
    reference_theorem_id: NonEmpty
    source_locator: NonEmpty
    source_file_sha256: Sha256
    v1_nl_sha256: Sha256
    v1_contains_open_delimiter: bool
    v1_contains_close_delimiter: bool
    corrected_disposition: Literal["excluded_no_adjacent_strict_docstring"]
    corrected_nl_sha256: None = None

    @model_validator(mode="after")
    def validate_detection(self) -> LibraryDocstringCorrectionV2:
        if not (self.v1_contains_open_delimiter or self.v1_contains_close_delimiter):
            raise ValueError("correction row must contain at least one raw comment delimiter")
        return self


class SourceIdViewV2(StrictModel):
    """Strict ordered ID view used by full-source consumers."""

    schema_version: Literal["sft2b_source_id_view_v2"] = "sft2b_source_id_view_v2"
    view_id: Literal["corrected_core_50000", "legacy_tail"]
    source_count: Annotated[int, Field(ge=0)]
    selection_rule: NonEmpty
    parent_sources_sha256: Sha256
    source_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def validate_view(self) -> SourceIdViewV2:
        if len(self.source_ids) != self.source_count:
            raise ValueError("source ID view count mismatch")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source ID view contains duplicates")
        return self

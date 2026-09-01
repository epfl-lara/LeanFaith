"""Fail-closed row-level review intake for the additive SFT2B v3 source release.

The v2 ``workbook_discourse_audit`` and ``semantic_alignment_audit`` files are
deterministic program outputs.  This module uses them only to reproduce the
required review population and to bind source bytes.  They never satisfy the
human-review contract implemented here.
"""

from __future__ import annotations

import argparse
import datetime
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    NonEmpty,
    Sha256,
    SourceProvenance,
    SourceRecord,
    StableId,
    stable_id,
)
from leanfaith.sft2b.source_bundle_schemas import (
    SemanticAlignmentAuditV2,
    SourceSelectionAuditV2,
    WorkbookDiscourseAuditV2,
    WorkbookDisposition,
    WorkbookFlag,
    WorkbookQuarantineV2,
)

ReleaseClass = Literal[
    "lean_workbook",
    "library_cslib",
    "library_mathlib",
    "library_physlib",
    "numina_current_auto",
    "numina_current_human",
    "numina_legacy_owner",
]
ReviewReason = Literal[
    "deterministic_100_per_release_class",
    "workbook_heuristic_hit",
]
ReviewedField = Literal[
    "nl_statement",
    "reference_proposition",
    "reference_theorem_id",
    "reference_declaration_name",
    "headless_signature",
    "problem_identity",
    "compile_context",
    "provenance",
]
ReviewVerdict = Literal[
    "admit_standalone_aligned",
    "quarantine_solution_or_proof_fragment",
    "quarantine_incomplete_or_nonstandalone",
    "quarantine_misaligned",
    "quarantine_other_quality_failure",
    "needs_escalation",
]
HubRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

REVIEWED_FIELDS: tuple[ReviewedField, ...] = (
    "nl_statement",
    "reference_proposition",
    "reference_theorem_id",
    "reference_declaration_name",
    "headless_signature",
    "problem_identity",
    "compile_context",
    "provenance",
)
PACKET_ENTRY_COUNT: Literal[992] = 992
AUTOMATIC_DISPOSITION_COUNT: Literal[293] = 293
DETERMINISTIC_SAMPLE_COUNT: Literal[700] = 700
WORKBOOK_HIT_COUNT: Literal[293] = 293
OVERLAP_COUNT: Literal[1] = 1


class SourceReviewContractError(RuntimeError):
    """Raised when packet or completed-review evidence fails closed."""


class PinnedFileV3(StrictModel):
    path: NonEmpty
    sha256: Sha256


class SourceReviewSelectionV3(StrictModel):
    workbook_hit_count: Literal[293]
    deterministic_rows_per_release_class: Literal[100]
    release_classes: tuple[ReleaseClass, ...]
    deterministic_sample_count: Literal[700]
    required_unique_source_count: Literal[992]
    expected_overlap_count: Literal[1]
    expected_overlap_source_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def validate_selection(self) -> SourceReviewSelectionV3:
        if tuple(sorted(set(self.release_classes))) != self.release_classes:
            raise ValueError("release classes must be nonempty, unique, and sorted")
        if len(self.release_classes) * self.deterministic_rows_per_release_class != (
            self.deterministic_sample_count
        ):
            raise ValueError("deterministic sample count does not match class contract")
        if len(self.expected_overlap_source_ids) != self.expected_overlap_count:
            raise ValueError("overlap ID count does not match overlap contract")
        expected_unique = (
            self.workbook_hit_count + self.deterministic_sample_count - self.expected_overlap_count
        )
        if expected_unique != self.required_unique_source_count:
            raise ValueError("unique review count does not conserve required reasons")
        return self


class HumanReviewRequirementV3(StrictModel):
    reviewer_kind: Literal["human"]
    method: Literal["manual_row_level_source_alignment_v1"]
    reviewed_fields: tuple[ReviewedField, ...]
    personal_review_attestation_required: Literal[True]
    final_verdict_required: Literal[True]
    external_reviews_sha256_pin_required: Literal[True]
    external_reviewer_identity_allowlist_required: Literal[True]
    external_human_attestation_pin_required: Literal[True]

    @model_validator(mode="after")
    def validate_reviewed_fields(self) -> HumanReviewRequirementV3:
        if self.reviewed_fields != REVIEWED_FIELDS:
            raise ValueError("human review must cover the exact frozen field set")
        return self


class SourceReviewConfigV3(StrictModel):
    schema_version: Literal["sft2b_source_review_contract_v3"]
    source_bundle_revision: HubRevision
    source_bundle_prefix: NonEmpty
    source_bundle_path: NonEmpty
    source_files: dict[str, PinnedFileV3]
    selection: SourceReviewSelectionV3
    human_review: HumanReviewRequirementV3
    instructions: PinnedFileV3

    @model_validator(mode="after")
    def validate_file_set(self) -> SourceReviewConfigV3:
        expected = {
            "deterministic_review_selection",
            "source_audit",
            "source_manifest",
            "sources",
            "workbook_dispositions",
            "workbook_quarantine",
        }
        if set(self.source_files) != expected:
            raise ValueError("source-review config must pin the exact required v2 file set")
        return self


class ReviewedSourceSnapshotV3(StrictModel):
    """Exact values shown to and adjudicated by the human reviewer."""

    nl_statement: NonEmpty
    reference_proposition: NonEmpty
    reference_theorem_id: NonEmpty
    reference_declaration_name: NonEmpty | None
    headless_signature: NonEmpty
    problem_identity: NonEmpty
    compile_context: CompileContextRecord
    provenance: SourceProvenance


class ReviewedSourceFieldHashesV3(StrictModel):
    nl_statement_sha256: Sha256
    reference_proposition_sha256: Sha256
    reference_theorem_id_sha256: Sha256
    reference_declaration_name_sha256: Sha256 | None
    headless_signature_sha256: Sha256
    problem_identity_sha256: Sha256
    compile_context_sha256: Sha256
    provenance_sha256: Sha256


def _field_hashes(snapshot: ReviewedSourceSnapshotV3) -> ReviewedSourceFieldHashesV3:
    declaration = snapshot.reference_declaration_name
    return ReviewedSourceFieldHashesV3(
        nl_statement_sha256=sha256_hex(snapshot.nl_statement.encode("utf-8")),
        reference_proposition_sha256=sha256_hex(snapshot.reference_proposition.encode("utf-8")),
        reference_theorem_id_sha256=sha256_hex(snapshot.reference_theorem_id.encode("utf-8")),
        reference_declaration_name_sha256=(
            sha256_hex(declaration.encode("utf-8")) if declaration is not None else None
        ),
        headless_signature_sha256=sha256_hex(snapshot.headless_signature.encode("utf-8")),
        problem_identity_sha256=sha256_hex(snapshot.problem_identity.encode("utf-8")),
        compile_context_sha256=hash_canonical(snapshot.compile_context.model_dump(mode="json")),
        provenance_sha256=hash_canonical(snapshot.provenance.model_dump(mode="json")),
    )


class SourceReviewPacketEntryV3(StrictModel):
    """One immutable, reviewable source snapshot; it contains no review verdict."""

    schema_version: Literal["sft2b_source_review_packet_entry_v3"] = (
        "sft2b_source_review_packet_entry_v3"
    )
    packet_entry_id: StableId
    source_id: StableId
    release_class: ReleaseClass
    required_reasons: tuple[ReviewReason, ...]
    reviewed_fields: tuple[ReviewedField, ...]
    reviewed_source: ReviewedSourceSnapshotV3
    reviewed_field_sha256: ReviewedSourceFieldHashesV3
    reviewed_source_sha256: Sha256

    def binding_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "release_class": self.release_class,
            "required_reasons": self.required_reasons,
            "reviewed_fields": self.reviewed_fields,
            "reviewed_source": self.reviewed_source.model_dump(mode="json"),
            "reviewed_field_sha256": self.reviewed_field_sha256.model_dump(mode="json"),
            "reviewed_source_sha256": self.reviewed_source_sha256,
        }

    @model_validator(mode="after")
    def validate_binding(self) -> SourceReviewPacketEntryV3:
        if (
            not self.required_reasons
            or tuple(sorted(set(self.required_reasons))) != self.required_reasons
        ):
            raise ValueError("review reasons must be nonempty, unique, and sorted")
        if self.reviewed_fields != REVIEWED_FIELDS:
            raise ValueError("packet entry does not contain the exact reviewed fields")
        expected_field_hashes = _field_hashes(self.reviewed_source)
        if self.reviewed_field_sha256 != expected_field_hashes:
            raise ValueError("reviewed field hash mismatch")
        source_payload = self.reviewed_source.model_dump(mode="json")
        if self.reviewed_source_sha256 != hash_canonical(source_payload):
            raise ValueError("reviewed source hash mismatch")
        if self.packet_entry_id != stable_id("sft2b_review_packet", self.binding_payload()):
            raise ValueError("packet entry ID does not replay")
        return self


class AutomaticDispositionV3(StrictModel):
    """Deterministic v2 rule output, explicitly not a human or semantic review."""

    schema_version: Literal["sft2b_automatic_disposition_v3"] = "sft2b_automatic_disposition_v3"
    source_id: StableId
    packet_entry_id: StableId
    reviewed_source_sha256: Sha256
    evidence_kind: Literal["deterministic_automatic_disposition"]
    method: Literal["workbook_solution_or_answer_discourse_v1"]
    flags: tuple[WorkbookFlag, ...]
    automatic_disposition: WorkbookDisposition
    automatic_rationale: NonEmpty
    satisfies_human_review_contract: Literal[False]

    @model_validator(mode="after")
    def validate_flags(self) -> AutomaticDispositionV3:
        if not self.flags or tuple(sorted(set(self.flags))) != self.flags:
            raise ValueError("automatic-disposition flags must be nonempty, unique, and sorted")
        return self


class HumanSourceReviewV3(StrictModel):
    """Authentic human review intake bound to one exact packet entry."""

    schema_version: Literal["sft2b_human_source_review_v3"] = "sft2b_human_source_review_v3"
    review_id: StableId
    packet_entry_id: StableId
    source_id: StableId
    reviewed_fields: tuple[ReviewedField, ...]
    reviewed_field_sha256: ReviewedSourceFieldHashesV3
    reviewed_source_sha256: Sha256
    reviewer_identity: Annotated[str, Field(min_length=3, max_length=200)]
    reviewer_kind: Literal["human"]
    method: Literal["manual_row_level_source_alignment_v1"]
    review_timestamp_utc: datetime.datetime
    verdict: ReviewVerdict
    rationale: Annotated[str, Field(min_length=20, max_length=4000)]
    personally_reviewed_exact_fields: Literal[True]

    @field_validator("reviewer_identity", "rationale")
    @classmethod
    def validate_trimmed_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("review identity and rationale must be trimmed")
        return value

    @field_validator("review_timestamp_utc")
    @classmethod
    def validate_utc_timestamp(cls, value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
            raise ValueError("review timestamp must be timezone-aware UTC")
        return value

    def identity_payload(self) -> dict[str, object]:
        return {
            "packet_entry_id": self.packet_entry_id,
            "source_id": self.source_id,
            "reviewed_source_sha256": self.reviewed_source_sha256,
            "reviewer_identity": self.reviewer_identity,
            "review_timestamp_utc": self.review_timestamp_utc.isoformat(),
            "verdict": self.verdict,
        }

    @model_validator(mode="after")
    def validate_review_identity(self) -> HumanSourceReviewV3:
        if self.reviewed_fields != REVIEWED_FIELDS:
            raise ValueError("human review does not attest the exact reviewed fields")
        if self.review_id != stable_id("sft2b_human_review", self.identity_payload()):
            raise ValueError("human review ID does not replay")
        return self


class ReviewPacketManifestV3(StrictModel):
    schema_version: Literal["sft2b_source_review_packet_manifest_v3"] = (
        "sft2b_source_review_packet_manifest_v3"
    )
    source_bundle_revision: HubRevision
    source_bundle_prefix: NonEmpty
    packet_entry_count: Literal[992]
    automatic_disposition_count: Literal[293]
    deterministic_sample_count: Literal[700]
    workbook_hit_count: Literal[293]
    overlap_count: Literal[1]
    release_class_counts: dict[str, int]
    reason_counts: dict[str, int]
    overlap_source_ids: tuple[StableId, ...]
    review_status: Literal["awaiting_authentic_human_review"]
    review_contract_schema_sha256: Sha256
    packet_sha256: Sha256
    automatic_dispositions_sha256: Sha256


class HumanReviewVerificationReceiptV3(StrictModel):
    schema_version: Literal["sft2b_human_review_verification_receipt_v3"] = (
        "sft2b_human_review_verification_receipt_v3"
    )
    packet_sha256: Sha256
    reviews_sha256: Sha256
    review_count: Literal[992]
    reviewer_identities: tuple[NonEmpty, ...]
    verdict_counts: dict[str, int]
    escalation_count: Annotated[int, Field(ge=0)]
    schema_coverage_binding_passed: Literal[True]
    authenticity_scope: Literal["not_authenticated_by_schema_verifier"]


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReviewContractError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise SourceReviewContractError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SourceReviewContractError(f"cannot read JSONL {path}: {error}") from error
    if not lines or any(not line.strip() for line in lines):
        raise SourceReviewContractError(f"JSONL must be nonempty with no blank rows: {path}")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise SourceReviewContractError(
                f"invalid JSONL row {path}:{line_number}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise SourceReviewContractError(f"non-object JSONL row {path}:{line_number}")
        result.append(cast(dict[str, Any], value))
    return tuple(result)


def load_config(path: Path) -> SourceReviewConfigV3:
    config = SourceReviewConfigV3.model_validate(_object(path))
    instructions = Path(config.instructions.path)
    if not instructions.is_absolute():
        instructions = path.resolve().parents[2] / instructions
    if not instructions.is_file() or hash_file(instructions) != config.instructions.sha256:
        raise SourceReviewContractError("human review instructions hash mismatch")
    return config


def _source_paths(config: SourceReviewConfigV3) -> dict[str, Path]:
    bundle = Path(config.source_bundle_path)
    result: dict[str, Path] = {}
    for name, pin in config.source_files.items():
        path = bundle / pin.path
        if not path.is_file():
            raise SourceReviewContractError(f"missing pinned v2 source file: {path}")
        if hash_file(path) != pin.sha256:
            raise SourceReviewContractError(f"pinned v2 source file hash mismatch: {name}")
        result[name] = path
    return result


def _unique_by_id(rows: tuple[Any, ...], *, key: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in rows:
        value = row
        for part in key.split("."):
            value = getattr(value, part)
        if value in result:
            raise SourceReviewContractError(f"duplicate {label} ID: {value}")
        result[value] = row
    return result


def expected_packet_entries(
    config: SourceReviewConfigV3,
) -> tuple[tuple[SourceReviewPacketEntryV3, ...], tuple[AutomaticDispositionV3, ...]]:
    """Reproduce the exact v3 review population from immutable v2 evidence."""

    paths = _source_paths(config)
    sources = tuple(SourceRecord.model_validate(row) for row in _jsonl(paths["sources"]))
    audits = tuple(
        SourceSelectionAuditV2.model_validate(row) for row in _jsonl(paths["source_audit"])
    )
    discourse = tuple(
        WorkbookDiscourseAuditV2.model_validate(row)
        for row in _jsonl(paths["workbook_dispositions"])
    )
    quarantines = tuple(
        WorkbookQuarantineV2.model_validate(row) for row in _jsonl(paths["workbook_quarantine"])
    )
    selections = tuple(
        SemanticAlignmentAuditV2.model_validate(row)
        for row in _jsonl(paths["deterministic_review_selection"])
    )

    source_by_id = _unique_by_id(sources, key="source_id", label="core source")
    audit_by_id = _unique_by_id(audits, key="source_id", label="source audit")
    discourse_by_id = _unique_by_id(discourse, key="source_id", label="Workbook disposition")
    quarantine_by_id = _unique_by_id(
        quarantines, key="source.source_id", label="Workbook quarantine"
    )
    selection_by_id = _unique_by_id(selections, key="source_id", label="review selection")
    if set(source_by_id) != set(audit_by_id):
        raise SourceReviewContractError("core sources and source-audit IDs differ")
    if set(quarantine_by_id) != {
        source_id
        for source_id, row in discourse_by_id.items()
        if row.disposition == "quarantine_solution_or_answer_discourse"
    }:
        raise SourceReviewContractError("Workbook automatic quarantine/disposition IDs differ")

    requirement = config.selection
    reason_counts = Counter(reason for row in selections for reason in row.sample_reasons)
    if reason_counts != Counter(
        {
            "workbook_heuristic_hit": requirement.workbook_hit_count,
            "deterministic_100_per_release_class": requirement.deterministic_sample_count,
        }
    ):
        raise SourceReviewContractError("required review reason counts drifted")
    if len(selection_by_id) != requirement.required_unique_source_count:
        raise SourceReviewContractError("required unique review population drifted")
    class_counts = Counter(
        row.release_class
        for row in selections
        if "deterministic_100_per_release_class" in row.sample_reasons
    )
    if class_counts != Counter(
        dict.fromkeys(
            requirement.release_classes,
            requirement.deterministic_rows_per_release_class,
        )
    ):
        raise SourceReviewContractError("deterministic per-release-class sample drifted")
    overlap_ids = tuple(sorted(row.source_id for row in selections if len(row.sample_reasons) == 2))
    if overlap_ids != requirement.expected_overlap_source_ids:
        raise SourceReviewContractError("Workbook/sample overlap identity drifted")
    if set(discourse_by_id) != {
        row.source_id for row in selections if "workbook_heuristic_hit" in row.sample_reasons
    }:
        raise SourceReviewContractError("all Workbook hits must be in the review population")

    entries: list[SourceReviewPacketEntryV3] = []
    entry_by_source: dict[str, SourceReviewPacketEntryV3] = {}
    for selected in sorted(selections, key=lambda row: (row.release_class, row.source_id)):
        quarantine = quarantine_by_id.get(selected.source_id)
        source = (
            quarantine.source if quarantine is not None else source_by_id.get(selected.source_id)
        )
        if source is None:
            raise SourceReviewContractError(
                f"selected source bytes are unavailable: {selected.source_id}"
            )
        audit = audit_by_id.get(selected.source_id)
        workbook = discourse_by_id.get(selected.source_id)
        evidence = audit if audit is not None else workbook
        if evidence is None:
            raise SourceReviewContractError(
                f"selected source replay evidence is unavailable: {selected.source_id}"
            )
        if (
            selected.nl_sha256 != sha256_hex(source.nl_statement.encode("utf-8"))
            or selected.reference_proposition_sha256 != source.reference_proposition_sha256
            or selected.headless_signature_sha256
            != sha256_hex(evidence.headless_signature.encode("utf-8"))
            or selected.problem_identity != evidence.problem_identity
        ):
            raise SourceReviewContractError(
                f"v2 review-selection/source binding mismatch: {selected.source_id}"
            )
        snapshot = ReviewedSourceSnapshotV3(
            nl_statement=source.nl_statement,
            reference_proposition=source.reference_proposition,
            reference_theorem_id=source.reference_theorem_id,
            reference_declaration_name=source.reference_declaration_name,
            headless_signature=evidence.headless_signature,
            problem_identity=evidence.problem_identity,
            compile_context=source.compile_context,
            provenance=source.provenance,
        )
        source_hash = hash_canonical(snapshot.model_dump(mode="json"))
        field_hashes = _field_hashes(snapshot)
        reasons = cast(tuple[ReviewReason, ...], tuple(sorted(selected.sample_reasons)))
        payload = {
            "schema_version": "sft2b_source_review_packet_entry_v3",
            "source_id": source.source_id,
            "release_class": selected.release_class,
            "required_reasons": reasons,
            "reviewed_fields": REVIEWED_FIELDS,
            "reviewed_source": snapshot.model_dump(mode="json"),
            "reviewed_field_sha256": field_hashes.model_dump(mode="json"),
            "reviewed_source_sha256": source_hash,
        }
        entry = SourceReviewPacketEntryV3(
            packet_entry_id=stable_id("sft2b_review_packet", payload),
            source_id=source.source_id,
            release_class=cast(ReleaseClass, selected.release_class),
            required_reasons=reasons,
            reviewed_fields=REVIEWED_FIELDS,
            reviewed_source=snapshot,
            reviewed_field_sha256=field_hashes,
            reviewed_source_sha256=source_hash,
        )
        entries.append(entry)
        entry_by_source[entry.source_id] = entry

    automatic: list[AutomaticDispositionV3] = []
    for row in sorted(discourse, key=lambda value: value.source_id):
        entry = entry_by_source[row.source_id]
        automatic.append(
            AutomaticDispositionV3(
                source_id=row.source_id,
                packet_entry_id=entry.packet_entry_id,
                reviewed_source_sha256=entry.reviewed_source_sha256,
                evidence_kind="deterministic_automatic_disposition",
                method="workbook_solution_or_answer_discourse_v1",
                flags=row.flags,
                automatic_disposition=row.disposition,
                automatic_rationale=row.rationale,
                satisfies_human_review_contract=False,
            )
        )
    return tuple(entries), tuple(automatic)


def _canonical_jsonl(rows: tuple[StrictModel, ...]) -> bytes:
    return b"".join(canonical_json_bytes(row.model_dump(mode="json")) + b"\n" for row in rows)


def _packet_manifest(
    config: SourceReviewConfigV3,
    entries: tuple[SourceReviewPacketEntryV3, ...],
    automatic: tuple[AutomaticDispositionV3, ...],
    *,
    packet_sha256: str,
    automatic_dispositions_sha256: str,
) -> ReviewPacketManifestV3:
    reason_counts = Counter(reason for row in entries for reason in row.required_reasons)
    class_counts = Counter(row.release_class for row in entries)
    overlap_ids = tuple(sorted(row.source_id for row in entries if len(row.required_reasons) == 2))
    observed_counts = (
        len(entries),
        len(automatic),
        reason_counts["deterministic_100_per_release_class"],
        reason_counts["workbook_heuristic_hit"],
        len(overlap_ids),
    )
    expected_counts = (
        PACKET_ENTRY_COUNT,
        AUTOMATIC_DISPOSITION_COUNT,
        DETERMINISTIC_SAMPLE_COUNT,
        WORKBOOK_HIT_COUNT,
        OVERLAP_COUNT,
    )
    if observed_counts != expected_counts:
        raise SourceReviewContractError(
            f"review packet count contract drifted: {observed_counts} != {expected_counts}"
        )
    return ReviewPacketManifestV3(
        source_bundle_revision=config.source_bundle_revision,
        source_bundle_prefix=config.source_bundle_prefix,
        packet_entry_count=PACKET_ENTRY_COUNT,
        automatic_disposition_count=AUTOMATIC_DISPOSITION_COUNT,
        deterministic_sample_count=DETERMINISTIC_SAMPLE_COUNT,
        workbook_hit_count=WORKBOOK_HIT_COUNT,
        overlap_count=OVERLAP_COUNT,
        release_class_counts=dict(sorted(class_counts.items())),
        reason_counts=dict(sorted(reason_counts.items())),
        overlap_source_ids=overlap_ids,
        review_status="awaiting_authentic_human_review",
        review_contract_schema_sha256=hash_canonical(HumanSourceReviewV3.model_json_schema()),
        packet_sha256=packet_sha256,
        automatic_dispositions_sha256=automatic_dispositions_sha256,
    )


def build_review_packet(config_path: Path, output_dir: Path) -> ReviewPacketManifestV3:
    """Build the immutable packet only; never synthesize a completed review."""

    config = load_config(config_path)
    entries, automatic = expected_packet_entries(config)
    output_dir.mkdir(parents=True, exist_ok=False)
    packet_bytes = _canonical_jsonl(cast(tuple[StrictModel, ...], entries))
    automatic_bytes = _canonical_jsonl(cast(tuple[StrictModel, ...], automatic))
    (output_dir / "review_packet.jsonl").write_bytes(packet_bytes)
    (output_dir / "automatic_dispositions.jsonl").write_bytes(automatic_bytes)
    manifest = _packet_manifest(
        config,
        entries,
        automatic,
        packet_sha256=sha256_hex(packet_bytes),
        automatic_dispositions_sha256=sha256_hex(automatic_bytes),
    )
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    (output_dir / "review_packet_manifest.json").write_bytes(manifest_bytes)
    checksums = {
        "automatic_dispositions.jsonl": sha256_hex(automatic_bytes),
        "review_packet.jsonl": sha256_hex(packet_bytes),
        "review_packet_manifest.json": sha256_hex(manifest_bytes),
    }
    checksum_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
    (output_dir / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")
    verify_review_packet(config_path, output_dir)
    return manifest


def verify_review_packet(config_path: Path, packet_dir: Path) -> ReviewPacketManifestV3:
    """Replay packet selection, every reviewed field, and all byte hashes."""

    config = load_config(config_path)
    expected_entries, expected_automatic = expected_packet_entries(config)
    manifest = ReviewPacketManifestV3.model_validate(
        _object(packet_dir / "review_packet_manifest.json")
    )
    observed_entries = tuple(
        SourceReviewPacketEntryV3.model_validate(row)
        for row in _jsonl(packet_dir / "review_packet.jsonl")
    )
    observed_automatic = tuple(
        AutomaticDispositionV3.model_validate(row)
        for row in _jsonl(packet_dir / "automatic_dispositions.jsonl")
    )
    if observed_entries != expected_entries:
        raise SourceReviewContractError("review packet does not replay from frozen v2 bytes")
    if observed_automatic != expected_automatic:
        raise SourceReviewContractError("automatic dispositions do not replay from v2 bytes")
    packet_hash = hash_file(packet_dir / "review_packet.jsonl")
    automatic_hash = hash_file(packet_dir / "automatic_dispositions.jsonl")
    expected_manifest = _packet_manifest(
        config,
        observed_entries,
        observed_automatic,
        packet_sha256=packet_hash,
        automatic_dispositions_sha256=automatic_hash,
    )
    if manifest != expected_manifest:
        raise SourceReviewContractError("review packet manifest does not replay")
    checksums = (packet_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected_checksums = {
        name: hash_file(packet_dir / name)
        for name in (
            "automatic_dispositions.jsonl",
            "review_packet.jsonl",
            "review_packet_manifest.json",
        )
    }
    observed_checksums: dict[str, str] = {}
    for line in checksums:
        digest, separator, name = line.partition("  ")
        if not separator or name in observed_checksums:
            raise SourceReviewContractError("malformed or duplicate SHA256SUMS row")
        observed_checksums[name] = digest
    if observed_checksums != expected_checksums:
        raise SourceReviewContractError("review packet SHA256SUMS mismatch")
    return manifest


def verify_completed_human_reviews(
    config_path: Path,
    packet_dir: Path,
    reviews_path: Path,
    *,
    require_final_verdicts: bool = True,
) -> HumanReviewVerificationReceiptV3:
    """Verify exact review schema, coverage, and source-byte bindings.

    This function deliberately cannot authenticate that a claimed reviewer is
    human.  The release builder must additionally require user-supplied file
    hashes, an explicit reviewer-identity allowlist, and a separately pinned
    out-of-band human attestation before treating these rows as admissible.
    """

    config = load_config(config_path)
    manifest = verify_review_packet(config_path, packet_dir)
    packet_rows = tuple(
        SourceReviewPacketEntryV3.model_validate(row)
        for row in _jsonl(packet_dir / "review_packet.jsonl")
    )
    try:
        review_raw = _jsonl(reviews_path)
    except SourceReviewContractError as error:
        raise SourceReviewContractError(
            "completed human-review rows are required and were not provided"
        ) from error
    reviews = tuple(HumanSourceReviewV3.model_validate(row) for row in review_raw)
    review_by_packet = _unique_by_id(reviews, key="packet_entry_id", label="human review packet")
    packet_by_id = _unique_by_id(packet_rows, key="packet_entry_id", label="review packet")
    if set(review_by_packet) != set(packet_by_id):
        missing = len(set(packet_by_id) - set(review_by_packet))
        unexpected = len(set(review_by_packet) - set(packet_by_id))
        raise SourceReviewContractError(
            f"human review coverage incomplete: missing={missing}, unexpected={unexpected}"
        )
    review_ids = {row.review_id for row in reviews}
    if len(review_ids) != len(reviews):
        raise SourceReviewContractError("duplicate human review ID")
    requirement = config.human_review
    for packet_id, review in review_by_packet.items():
        packet = packet_by_id[packet_id]
        if (
            review.source_id != packet.source_id
            or review.reviewed_fields != packet.reviewed_fields
            or review.reviewed_field_sha256 != packet.reviewed_field_sha256
            or review.reviewed_source_sha256 != packet.reviewed_source_sha256
            or review.reviewer_kind != requirement.reviewer_kind
            or review.method != requirement.method
        ):
            raise SourceReviewContractError(
                f"human review/source binding mismatch: {packet.source_id}"
            )
    verdict_counts = Counter(review.verdict for review in reviews)
    escalation_count = verdict_counts["needs_escalation"]
    if require_final_verdicts and escalation_count:
        raise SourceReviewContractError(
            f"human review has {escalation_count} unresolved escalation verdicts"
        )
    if len(reviews) != manifest.packet_entry_count:
        raise SourceReviewContractError("human review count differs from packet manifest")
    return HumanReviewVerificationReceiptV3(
        packet_sha256=manifest.packet_sha256,
        reviews_sha256=hash_file(reviews_path),
        review_count=PACKET_ENTRY_COUNT,
        reviewer_identities=tuple(sorted({row.reviewer_identity for row in reviews})),
        verdict_counts=dict(sorted(verdict_counts.items())),
        escalation_count=escalation_count,
        schema_coverage_binding_passed=True,
        authenticity_scope="not_authenticated_by_schema_verifier",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build-packet", "verify-packet"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", type=Path, required=True)
        subparser.add_argument("--packet-dir", type=Path, required=True)
    review = subparsers.add_parser("verify-reviews")
    review.add_argument("--config", type=Path, required=True)
    review.add_argument("--packet-dir", type=Path, required=True)
    review.add_argument("--reviews", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build-packet":
        result: StrictModel = build_review_packet(args.config, args.packet_dir)
    elif args.command == "verify-packet":
        result = verify_review_packet(args.config, args.packet_dir)
    else:
        result = verify_completed_human_reviews(args.config, args.packet_dir, args.reviews)
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

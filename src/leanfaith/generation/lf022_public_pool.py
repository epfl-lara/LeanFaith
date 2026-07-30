"""Materialize a bounded, public-only LF-022 allocation pool.

This module is deliberately offline.  It validates already-extracted theorem,
representation, context, benchmark-registry, source-license, and family-matrix
records; deterministically selects an exact public subset; and emits the
content-addressed artifacts consumed by :mod:`leanfaith.generation.lf022_production`.
It never contacts a model provider, creates semantic labels, or promotes data.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.lf022_extraction_reuse import (
    LF022ExtractionReuseArtifactBinding,
    LF022ExtractionReuseAttestationV1,
    verify_lf022_extraction_reuse_attestation,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022AuthorizedExtractionMember,
    LF022JSONLArtifactBinding,
    LF022PlanProfile,
    LF022ProductionAdmission,
    LF022ProductionArtifactSet,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanManifest,
    LF022ProductionSourceRecord,
    LF022PublicSourceAuthorization,
    build_lf022_production_plan,
    lf022_source_locator_id,
    make_lf022_authorized_extraction_manifest,
    make_lf022_benchmark_registry_manifest,
    make_lf022_denylist_clearance_record,
    make_lf022_production_admission,
    make_lf022_production_source_record,
    make_lf022_public_source_authorization,
    make_lf022_public_source_authorization_registry,
    write_lf022_production_plan,
)
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.lean.project_registry import ContextPayload, context_fingerprint
from leanfaith.representations.views import representation_content_hash
from leanfaith.schemas.enums import ArtifactClass, DataStage, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import HEX64_PATTERN, REPRESENTATION_PREFIX, id_pattern, make_id
from leanfaith.schemas.manifest import OutputManifest
from leanfaith.schemas.source import make_source_ancestry_id
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.sources.mathlib_frame import MathlibFileFrame

LF022_PUBLIC_POOL_SELECTION_VERSION = "lf022_public_pool_hash_rank_v1"
_PRIVATE_SOURCE_PATTERNS = (
    re.compile(r"(?:^|[:/])formalmathatepfl/sft_classic(?:$|[@/#?])"),
    re.compile(r"(?:^|[:/])sft_classic(?:$|[@/#?])"),
)
_PROFILE_MINIMUMS: dict[LF022PlanProfile, int] = {
    "diagnostic_scaffold": 1,
    "pilot_scaffold": 12,
    "scientific_production_scaffold": 15_000,
}
_REJECTION_REASONS = (
    "private_source",
    "unapproved_source",
    "not_fully_elaborated_proposition",
    "transform_source_ineligible",
    "not_source_ancestry",
    "ancestry_binding_mismatch",
    "missing_representation",
    "representation_binding_mismatch",
    "representation_content_hash_mismatch",
    "missing_or_mismatched_context",
    "required_view_unavailable",
    "unstable_source_locator",
    "denylist_identifier_hit",
    "denylist_content_hit",
)


class LF022PublicPoolError(RuntimeError):
    """A public-pool input or immutable output failed closed."""


class LF022PublicPoolCapacityError(LF022PublicPoolError):
    """The exact requested count cannot be selected from the eligible pool."""

    def __init__(
        self,
        *,
        requested_count: int,
        eligible_count: int,
        eligible_unique_ancestry_count: int,
        rejection_counts: dict[str, int],
    ) -> None:
        self.requested_count = requested_count
        self.eligible_count = eligible_count
        self.eligible_unique_ancestry_count = eligible_unique_ancestry_count
        self.rejection_counts = dict(rejection_counts)
        super().__init__(
            f"requested {requested_count} public LF-022 sources but only "
            f"{eligible_unique_ancestry_count} distinct source ancestries "
            f"({eligible_count} theorem records) are eligible; "
            f"rejections={rejection_counts}"
        )


def _is_private_source(source: str) -> bool:
    normalized = source.strip().lower()
    return any(pattern.search(normalized) for pattern in _PRIVATE_SOURCE_PATTERNS)


class LF022ApprovedPublicSource(StrictModel):
    """Reviewed public-source metadata supplied before materialization."""

    schema_version: Literal[1] = 1
    source: str = Field(min_length=1)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    license_id: str = Field(min_length=1)
    license_evidence_uri: str = Field(min_length=1)
    approval_status: Literal["approved_public_research_compatible"]
    source_is_public: Literal[True]
    redistribution_allowed: Literal[True]
    external_transmission_allowed: Literal[True]
    context_project_kind: str = Field(min_length=1)
    context_project_uri: str = Field(min_length=1)
    context_project_registry_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _public_only(self) -> Self:
        if _is_private_source(self.source):
            raise ValueError("private sft_classic cannot be approved for LF-022 transmission")
        return self


class LF022PublicPoolOutputArtifacts(StrictModel):
    """All canonical artifacts emitted before the audit record itself."""

    family_matrix: LF022ArtifactBinding
    upstream_extraction_output_manifest: LF022ArtifactBinding
    upstream_representation_output_manifest: LF022ArtifactBinding
    mathlib_source_frame: LF022ArtifactBinding
    extraction_manifests: dict[str, LF022ArtifactBinding]
    source_authorizations: dict[str, LF022ArtifactBinding]
    public_source_authorization_registry: LF022ArtifactBinding
    benchmark_registry_manifest: LF022ArtifactBinding
    denylist_clearance_records: LF022JSONLArtifactBinding
    source_pool: LF022JSONLArtifactBinding
    theorem_records: LF022JSONLArtifactBinding
    representation_records: LF022JSONLArtifactBinding
    context_records: LF022JSONLArtifactBinding
    admission: LF022ArtifactBinding
    production_plan: LF022ArtifactBinding

    @model_validator(mode="after")
    def _maps_are_canonical(self) -> Self:
        for field_name in ("extraction_manifests", "source_authorizations"):
            keys = list(getattr(self, field_name))
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError(f"{field_name} keys must be sorted and unique")
        return self


class LF022PublicPoolAudit(StrictModel):
    """Mechanically reconciled eligibility, selection, and output audit."""

    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    selection_version: Literal["lf022_public_pool_hash_rank_v1"]
    profile: LF022PlanProfile
    requested_count: int = Field(ge=1)
    input_theorems: LF022JSONLArtifactBinding
    input_representations: LF022JSONLArtifactBinding
    input_contexts: LF022JSONLArtifactBinding
    input_extraction_output_manifest: LF022ArtifactBinding
    input_representation_output_manifest: LF022ArtifactBinding
    input_mathlib_source_frame: LF022ArtifactBinding
    extraction_run_id: str
    representation_run_id: str
    mathlib_source_frame_id: str
    active_benchmark_registry: LF022ArtifactBinding
    active_benchmark_registry_content_hash: str = Field(pattern=HEX64_PATTERN)
    input_theorem_count: int = Field(ge=1)
    input_representation_count: int = Field(ge=1)
    input_context_count: int = Field(ge=1)
    orphan_representation_count: int = Field(ge=0)
    unused_context_count: int = Field(ge=0)
    eligible_count: int = Field(ge=1)
    eligible_unique_ancestry_count: int = Field(ge=1)
    eligible_not_selected_count: int = Field(ge=0)
    selected_count: int = Field(ge=1)
    selected_unique_ancestry_count: int = Field(ge=1)
    rejection_counts: dict[str, int]
    selected_source_counts: dict[str, int]
    selection_order_theorem_ids: tuple[str, ...] = Field(min_length=1)
    outputs: LF022PublicPoolOutputArtifacts
    public_sources_only: Literal[True]
    private_sft_classic_forbidden: Literal[True]
    network_execution_authorized: Literal[False]
    semantic_labels_created: Literal[False]

    @model_validator(mode="after")
    def _reconciles(self) -> Self:
        if set(self.rejection_counts) != set(_REJECTION_REASONS):
            raise ValueError("rejection_counts must contain every canonical reason")
        if any(value < 0 for value in self.rejection_counts.values()):
            raise ValueError("rejection counts cannot be negative")
        if sum(self.rejection_counts.values()) + self.eligible_count != self.input_theorem_count:
            raise ValueError("theorem eligibility accounting does not reconcile")
        if self.selected_count != self.requested_count:
            raise ValueError("selected_count must equal requested_count")
        if self.selected_unique_ancestry_count != self.selected_count:
            raise ValueError("every selected theorem must have a distinct source ancestry")
        if self.eligible_unique_ancestry_count < self.selected_unique_ancestry_count:
            raise ValueError("selected source ancestries exceed eligible source ancestries")
        if self.eligible_count != self.selected_count + self.eligible_not_selected_count:
            raise ValueError("eligible selection accounting does not reconcile")
        if len(self.selection_order_theorem_ids) != self.selected_count:
            raise ValueError("selection_order_theorem_ids does not match selected_count")
        if len(set(self.selection_order_theorem_ids)) != self.selected_count:
            raise ValueError("selection_order_theorem_ids must be unique")
        if list(self.selected_source_counts) != sorted(self.selected_source_counts):
            raise ValueError("selected_source_counts keys must be sorted")
        if sum(self.selected_source_counts.values()) != self.selected_count:
            raise ValueError("selected_source_counts does not reconcile")
        expected = make_id(
            "lf022_public_pool_audit",
            self.model_dump(mode="json", exclude={"audit_id"}),
        )
        if self.audit_id != expected:
            raise ValueError("audit_id does not match canonical audit content")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedLF022PublicPool:
    """In-memory result plus exact persisted bindings."""

    audit: LF022PublicPoolAudit
    audit_binding: LF022ArtifactBinding
    admission: LF022ProductionAdmission
    plan: LF022ProductionPlanManifest


@dataclass(frozen=True, slots=True)
class _Eligible:
    theorem: TheoremRecord
    representation: RepresentationRecord
    context: ContextRecord
    root_ancestry_id: str
    source_locator_id: str
    selection_rank: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _inside_repo_regular_file(repo_root: Path, path: Path, *, label: str) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022PublicPoolError(f"{label} must be inside the repository") from exc
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LF022PublicPoolError(f"{label} contains a symlinked path component")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022PublicPoolError(f"{label} is missing: {candidate}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022PublicPoolError(f"{label} must be a regular repository file")
    return resolved


def _relative(repo_root: Path, path: Path) -> str:
    return str(PurePosixPath(path.resolve().relative_to(repo_root.resolve()).as_posix()))


def _load_jsonl[RecordT: StrictModel](
    repo_root: Path,
    path: Path,
    model: type[RecordT],
    *,
    label: str,
    extraction_envelope_key: str | None = None,
) -> tuple[tuple[RecordT, ...], LF022JSONLArtifactBinding]:
    resolved = _inside_repo_regular_file(repo_root, path, label=label)
    try:
        raw_lines = resolved.read_bytes().splitlines()
    except OSError as exc:  # pragma: no cover - resolved regular file already read-probed
        raise LF022PublicPoolError(f"cannot read {label}") from exc
    if not raw_lines or any(not line for line in raw_lines):
        raise LF022PublicPoolError(f"{label} must be nonempty and contain no blank rows")
    records: list[RecordT] = []
    for line_number, line in enumerate(raw_lines, start=1):
        try:
            document = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant {value}")
                ),
            )
            if (
                extraction_envelope_key is not None
                and isinstance(document, dict)
                and extraction_envelope_key in document
            ):
                if set(document) != {"theorem", "representation"}:
                    raise ValueError(
                        "extraction theorem envelope must contain exactly "
                        "'theorem' and 'representation'"
                    )
                document = document[extraction_envelope_key]
            records.append(model.model_validate(cast(object, document)))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise LF022PublicPoolError(f"invalid {label} row {line_number}: {exc}") from exc
    return (
        tuple(records),
        LF022JSONLArtifactBinding(
            path=_relative(repo_root, resolved),
            sha256=hash_file(resolved),
            record_count=len(records),
        ),
    )


def _resolve_bound_registry(
    *,
    repo_root: Path,
    registry: FrozenRegistry,
    binding: LF022ArtifactBinding,
) -> DenylistIndex:
    path = _inside_repo_regular_file(
        repo_root,
        Path(binding.path),
        label="active benchmark registry",
    )
    if hash_file(path) != binding.sha256:
        raise LF022PublicPoolError("active benchmark registry hash mismatch")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        persisted = FrozenRegistry.model_validate(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022PublicPoolError(f"invalid active benchmark registry: {exc}") from exc
    if persisted != registry:
        raise LF022PublicPoolError(
            "supplied active benchmark registry differs from its exact binding"
        )
    return DenylistIndex(registry)


def _resolve_bound_model[RecordT: StrictModel](
    *,
    repo_root: Path,
    supplied: RecordT,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
) -> RecordT:
    """Replay one exact JSON binding and require it to equal the supplied model."""

    path = _inside_repo_regular_file(repo_root, Path(binding.path), label=label)
    if hash_file(path) != binding.sha256:
        raise LF022PublicPoolError(f"{label} hash mismatch")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
        persisted = model.model_validate(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022PublicPoolError(f"invalid {label}: {exc}") from exc
    if persisted != supplied:
        raise LF022PublicPoolError(f"supplied {label} differs from its exact binding")
    return persisted


def _validate_upstream_extraction(
    *,
    theorems: tuple[TheoremRecord, ...],
    theorem_binding: LF022JSONLArtifactBinding,
    context_binding: LF022JSONLArtifactBinding,
    extraction_manifest: OutputManifest,
    extraction_manifest_binding: LF022ArtifactBinding,
    mathlib_source_frame: MathlibFileFrame,
    mathlib_source_frame_binding: LF022ArtifactBinding,
    profile: LF022PlanProfile,
) -> None:
    """Bind the exact theorem partition to its extraction run and source frame."""

    if (
        extraction_manifest.stage is not DataStage.ELABORATED
        or extraction_manifest.source != "mathlib"
        or extraction_manifest.source_revision != mathlib_source_frame.revision
        or extraction_manifest.row_count != theorem_binding.record_count
        or extraction_manifest.context_hash != context_binding.sha256
    ):
        raise LF022PublicPoolError(
            "upstream extraction manifest source/revision/count/context does not match inputs"
        )
    if profile == "scientific_production_scaffold" and (
        extraction_manifest.environment_hash is None
        or extraction_manifest.code_tree_hash is None
        or extraction_manifest.code.code_tree_hash is None
        or extraction_manifest.code_tree_hash != extraction_manifest.code.code_tree_hash
    ):
        raise LF022PublicPoolError(
            "scientific upstream extraction requires exact environment and code-tree hashes"
        )
    if (
        extraction_manifest.output_partition_checksums.get(theorem_binding.path)
        != theorem_binding.sha256
        or extraction_manifest.file_checksums.get(theorem_binding.path) != theorem_binding.sha256
    ):
        raise LF022PublicPoolError(
            "upstream extraction manifest does not bind the exact theorem JSONL"
        )
    if (
        extraction_manifest.input_partition_checksums.get(mathlib_source_frame_binding.path)
        != mathlib_source_frame_binding.sha256
    ):
        raise LF022PublicPoolError(
            "upstream extraction manifest does not bind the exact mathlib source frame"
        )
    if not extraction_manifest_binding.path or not mathlib_source_frame_binding.path:
        raise LF022PublicPoolError("upstream extraction artifacts require exact paths")

    frame_members = {member.relative_path for member in mathlib_source_frame.members}
    invalid = tuple(
        theorem.theorem_id
        for theorem in theorems
        if theorem.source != "mathlib"
        or theorem.source_revision != mathlib_source_frame.revision
        or theorem.source_file is None
        or theorem.source_file not in frame_members
    )
    if invalid:
        raise LF022PublicPoolError(
            "public theorem partition contains records outside the bound mathlib source frame"
        )


def _manifest_checksum_matches_binding(
    *,
    repo_root: Path,
    checksums: dict[str, str],
    binding: LF022JSONLArtifactBinding,
) -> bool:
    """Match a run-manifest path written as either repository-relative or absolute."""

    candidates = {
        binding.path,
        str((repo_root.resolve() / PurePosixPath(binding.path)).resolve()),
    }
    observed = tuple(checksums[key] for key in sorted(candidates) if key in checksums)
    return bool(observed) and all(value == binding.sha256 for value in observed)


def _validate_upstream_representation(
    *,
    repo_root: Path,
    theorems: tuple[TheoremRecord, ...],
    theorem_binding: LF022JSONLArtifactBinding,
    representations: tuple[RepresentationRecord, ...],
    representation_binding: LF022JSONLArtifactBinding,
    extraction_manifest: OutputManifest,
    extraction_manifest_binding: LF022ArtifactBinding,
    representation_manifest: OutputManifest,
    representation_manifest_binding: LF022ArtifactBinding,
    profile: LF022PlanProfile,
    extraction_reuse_attestation: LF022ExtractionReuseAttestationV1 | None,
    extraction_reuse_attestation_binding: LF022ArtifactBinding | None,
) -> None:
    """Bind model-visible views to the exact LeanInteract representation run."""

    context_ids = {theorem.context_id for theorem in theorems}
    if len(context_ids) != 1:
        raise LF022PublicPoolError(
            "upstream representation input must contain exactly one Lean context"
        )
    context_id = next(iter(context_ids))
    if (
        representation_manifest.stage is not DataStage.REPRESENTED
        or representation_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or representation_manifest.source_revision != "from_theorem_partition"
        or representation_manifest.row_count != representation_binding.record_count
        or representation_manifest.attempted_row_count != theorem_binding.record_count
        or representation_manifest.context_hash != hash_canonical({"context_id": context_id})
    ):
        raise LF022PublicPoolError(
            "upstream representation manifest stage/count/context does not match inputs"
        )
    representation_output_binding_exact = _manifest_checksum_matches_binding(
        repo_root=repo_root,
        checksums=representation_manifest.output_partition_checksums,
        binding=representation_binding,
    ) and _manifest_checksum_matches_binding(
        repo_root=repo_root,
        checksums=representation_manifest.file_checksums,
        binding=representation_binding,
    )
    representation_input_binding_exact = _manifest_checksum_matches_binding(
        repo_root=repo_root,
        checksums=representation_manifest.input_partition_checksums,
        binding=theorem_binding,
    )
    representation_provenance_exact = (
        representation_manifest.environment_hash is not None
        and representation_manifest.environment_hash == extraction_manifest.environment_hash
        and representation_manifest.code_tree_hash is not None
        and representation_manifest.code.code_tree_hash is not None
        and representation_manifest.code_tree_hash == representation_manifest.code.code_tree_hash
        and representation_manifest.code_tree_hash == extraction_manifest.code_tree_hash
        and representation_manifest.code == extraction_manifest.code
    )
    mismatch_present = (
        not representation_output_binding_exact
        or not representation_input_binding_exact
        or not representation_provenance_exact
    )
    if (extraction_reuse_attestation is None) != (extraction_reuse_attestation_binding is None):
        raise LF022PublicPoolError(
            "extraction-reuse attestation record and binding must be supplied together"
        )
    reuse_verified = False
    if (
        extraction_reuse_attestation is not None
        and extraction_reuse_attestation_binding is not None
    ):
        try:
            verify_lf022_extraction_reuse_attestation(
                repo_root=repo_root,
                attestation=extraction_reuse_attestation,
                attestation_binding=LF022ExtractionReuseArtifactBinding.model_validate(
                    extraction_reuse_attestation_binding.model_dump(mode="json")
                ),
                extraction_manifest=extraction_manifest,
                extraction_manifest_binding=LF022ExtractionReuseArtifactBinding.model_validate(
                    extraction_manifest_binding.model_dump(mode="json")
                ),
                theorem_records_binding=LF022ExtractionReuseArtifactBinding.model_validate(
                    theorem_binding.model_dump(mode="json")
                ),
                representation_manifest=representation_manifest,
                representation_manifest_binding=(
                    LF022ExtractionReuseArtifactBinding.model_validate(
                        representation_manifest_binding.model_dump(mode="json")
                    )
                ),
                representation_records_binding=(
                    LF022ExtractionReuseArtifactBinding.model_validate(
                        representation_binding.model_dump(mode="json")
                    )
                ),
            )
        except ValueError as exc:
            raise LF022PublicPoolError(
                f"reviewed extraction-reuse attestation failed: {exc}"
            ) from exc
        reuse_verified = True
    if mismatch_present and not reuse_verified:
        if not representation_output_binding_exact:
            raise LF022PublicPoolError(
                "upstream representation manifest does not bind the exact representation JSONL"
            )
        if not representation_input_binding_exact:
            raise LF022PublicPoolError(
                "upstream representation manifest does not bind the exact extraction theorem JSONL"
            )
        raise LF022PublicPoolError(
            "upstream representation environment/code provenance differs from extraction"
        )
    if profile == "scientific_production_scaffold" and (
        representation_manifest.environment_hash is None
        or representation_manifest.code_tree_hash is None
    ):
        raise LF022PublicPoolError(
            "scientific upstream representation requires exact environment and code provenance"
        )
    if len(representations) != representation_binding.record_count:
        raise LF022PublicPoolError(
            "representation JSONL record count differs from its exact artifact binding"
        )


def _unique_index[RecordT: StrictModel](
    records: tuple[RecordT, ...],
    *,
    attribute: str,
    label: str,
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        key = cast(str, getattr(record, attribute))
        if key in result:
            raise LF022PublicPoolError(f"duplicate {label}: {key}")
        result[key] = record
    return result


def _selection_rank(
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    source_locator_id: str,
) -> str:
    return hash_canonical(
        {
            "schema": LF022_PUBLIC_POOL_SELECTION_VERSION,
            "source_locator_id": source_locator_id,
            "theorem_id": theorem.theorem_id,
            "representation_id": representation.representation_id,
        }
    )


def _representation_hash_matches(record: RepresentationRecord) -> bool:
    """Recompute the canonical representation hash before pool admission."""

    return record.content_hash == representation_content_hash(
        {
            "raw_proof_stripped": record.raw_proof_stripped,
            "headless": record.headless,
            "signature_pp": record.signature_pp,
            "signature_explicit": record.signature_explicit,
            "semantic_atoms": (
                list(record.semantic_atoms) if record.semantic_atoms is not None else None
            ),
            "operator_tree": record.operator_tree,
            "alpha_identity_fingerprint": record.alpha_identity_fingerprint,
        }
    )


def _representation_matches_theorem(
    record: RepresentationRecord,
    theorem: TheoremRecord,
) -> bool:
    expected_id = make_id(
        REPRESENTATION_PREFIX,
        {
            "theorem_id": theorem.theorem_id,
            "normalization_version": record.normalization_version,
        },
    )
    return (
        record.theorem_id == theorem.theorem_id
        and record.context_id == theorem.context_id
        and record.representation_id == expected_id
        and record.raw_proof_stripped == theorem.proof_stripped_declaration
    )


def _context_matches_approved_source(
    *,
    theorem: TheoremRecord,
    context: ContextRecord,
    approval: LF022ApprovedPublicSource,
) -> bool:
    """Require the Lean context to identify the approved source revision."""

    payload = ContextPayload(
        environment_schema_version=context.environment_schema_version,
        lean_version=context.lean_version,
        lean_interact_version=context.lean_interact_version,
        repl_revision=context.repl_revision,
        project_uri=context.project_uri,
        project_revision=context.project_revision,
        imports=context.imports,
        namespace_context=context.namespace_context,
        open_context=context.open_context,
        scoped_context=context.scoped_context,
        options=context.options,
        notation_context=context.notation_context,
        header_text=context.header_text,
    )
    return (
        context.context_id == theorem.context_id
        and context.context_fingerprint == context_fingerprint(payload)
        and context.header_hash == sha256_hex(context.header_text.encode("utf-8"))
        and context.project_revision == theorem.source_revision
        and context.project_kind == approval.context_project_kind
        and context.project_uri == approval.context_project_uri
        and context.project_registry_key == approval.context_project_registry_key
    )


def _source_ancestry_matches(theorem: TheoremRecord, source_locator_id: str) -> bool:
    """Verify serialized root ancestry against the extractor's constructor."""

    if theorem.declaration_full_name is None:
        return False
    try:
        expected = make_source_ancestry_id(
            source=theorem.source,
            revision=theorem.source_revision,
            source_locator=(
                theorem.source_record_id
                if theorem.source_record_id is not None
                else theorem.source_record or ""
            ),
            declaration_full_name=theorem.declaration_full_name,
        )
    except ValueError:
        return False
    return (
        source_locator_id == lf022_source_locator_id(theorem)
        and theorem.ancestry_id == expected
        and theorem.root_ancestry_ids == (expected,)
    )


def _output_directory(repo_root: Path, output_directory: Path) -> Path:
    root = repo_root.resolve(strict=True)
    output = output_directory if output_directory.is_absolute() else root / output_directory
    try:
        relative = output.relative_to(root)
    except ValueError as exc:
        raise LF022PublicPoolError("output directory must be inside the repository") from exc
    if ".." in relative.parts or "." in relative.parts:
        raise LF022PublicPoolError(
            "output directory must be a normalized path inside the repository"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LF022PublicPoolError("output directory contains a symlinked component")
        if current.exists() and not current.is_dir():
            raise LF022PublicPoolError("output path component is not a directory")
        current.mkdir(exist_ok=True)
    return output


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LF022PublicPoolError(f"immutable output cannot be a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022PublicPoolError(f"immutable output conflict: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022PublicPoolError(
                    f"concurrent immutable output conflict: {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(
    *,
    repo_root: Path,
    path: Path,
    record: StrictModel,
) -> LF022ArtifactBinding:
    digest = _write_immutable(path, canonical_json_bytes(record.model_dump(mode="json")))
    return LF022ArtifactBinding(path=_relative(repo_root, path), sha256=digest)


def _write_jsonl(
    *,
    repo_root: Path,
    path: Path,
    records: tuple[StrictModel, ...],
) -> LF022JSONLArtifactBinding:
    if not records:
        raise LF022PublicPoolError(f"cannot write empty production artifact: {path}")
    payload = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )
    digest = _write_immutable(path, payload)
    return LF022JSONLArtifactBinding(
        path=_relative(repo_root, path),
        sha256=digest,
        record_count=len(records),
    )


def _source_key(source: str, revision: str) -> str:
    return f"{source}@{revision}"


def _source_artifact_stem(source: str, revision: str) -> str:
    return hash_canonical({"source": source, "source_revision": revision})


def materialize_lf022_public_pool(
    *,
    repo_root: Path,
    theorem_records_path: Path,
    representation_records_path: Path,
    context_records_path: Path,
    active_registry: FrozenRegistry,
    active_registry_binding: LF022ArtifactBinding,
    extraction_output_manifest: OutputManifest,
    extraction_output_manifest_binding: LF022ArtifactBinding,
    representation_output_manifest: OutputManifest,
    representation_output_manifest_binding: LF022ArtifactBinding,
    mathlib_source_frame: MathlibFileFrame,
    mathlib_source_frame_binding: LF022ArtifactBinding,
    family_matrix: LF022ProductionFamilyMatrix,
    approved_sources: tuple[LF022ApprovedPublicSource, ...],
    output_directory: Path,
    requested_count: int = 15_000,
    profile: LF022PlanProfile = "scientific_production_scaffold",
    extraction_reuse_attestation: LF022ExtractionReuseAttestationV1 | None = None,
    extraction_reuse_attestation_binding: LF022ArtifactBinding | None = None,
) -> MaterializedLF022PublicPool:
    """Create or byte-identically replay one bounded public LF-022 pool."""

    if requested_count < _PROFILE_MINIMUMS[profile]:
        raise LF022PublicPoolError(
            f"{profile} requires at least {_PROFILE_MINIMUMS[profile]} selected theorems"
        )
    if not approved_sources:
        raise LF022PublicPoolError("at least one reviewed public source is required")
    ordered_approvals = tuple(
        sorted(approved_sources, key=lambda item: (item.source, item.source_revision))
    )
    approval_keys = [(item.source, item.source_revision) for item in ordered_approvals]
    if len(approval_keys) != len(set(approval_keys)):
        raise LF022PublicPoolError("approved public source/revision entries must be unique")
    approvals = {(item.source, item.source_revision): item for item in ordered_approvals}

    theorems, theorem_input_binding = _load_jsonl(
        repo_root,
        theorem_records_path,
        TheoremRecord,
        label="public theorem records",
        extraction_envelope_key="theorem",
    )
    representations, representation_input_binding = _load_jsonl(
        repo_root,
        representation_records_path,
        RepresentationRecord,
        label="public representation records",
    )
    contexts, context_input_binding = _load_jsonl(
        repo_root,
        context_records_path,
        ContextRecord,
        label="public context records",
    )
    persisted_extraction_manifest = _resolve_bound_model(
        repo_root=repo_root,
        supplied=extraction_output_manifest,
        binding=extraction_output_manifest_binding,
        model=OutputManifest,
        label="upstream extraction output manifest",
    )
    persisted_mathlib_frame = _resolve_bound_model(
        repo_root=repo_root,
        supplied=mathlib_source_frame,
        binding=mathlib_source_frame_binding,
        model=MathlibFileFrame,
        label="mathlib source frame",
    )
    _validate_upstream_extraction(
        theorems=theorems,
        theorem_binding=theorem_input_binding,
        context_binding=context_input_binding,
        extraction_manifest=persisted_extraction_manifest,
        extraction_manifest_binding=extraction_output_manifest_binding,
        mathlib_source_frame=persisted_mathlib_frame,
        mathlib_source_frame_binding=mathlib_source_frame_binding,
        profile=profile,
    )
    persisted_representation_manifest = _resolve_bound_model(
        repo_root=repo_root,
        supplied=representation_output_manifest,
        binding=representation_output_manifest_binding,
        model=OutputManifest,
        label="upstream representation output manifest",
    )
    _validate_upstream_representation(
        repo_root=repo_root,
        theorems=theorems,
        theorem_binding=theorem_input_binding,
        representations=representations,
        representation_binding=representation_input_binding,
        extraction_manifest=persisted_extraction_manifest,
        extraction_manifest_binding=extraction_output_manifest_binding,
        representation_manifest=persisted_representation_manifest,
        representation_manifest_binding=representation_output_manifest_binding,
        profile=profile,
        extraction_reuse_attestation=extraction_reuse_attestation,
        extraction_reuse_attestation_binding=extraction_reuse_attestation_binding,
    )
    denylist_index = _resolve_bound_registry(
        repo_root=repo_root,
        registry=active_registry,
        binding=active_registry_binding,
    )

    theorem_index = _unique_index(
        theorems,
        attribute="theorem_id",
        label="theorem ID",
    )
    _unique_index(
        representations,
        attribute="representation_id",
        label="representation ID",
    )
    representations_by_theorem = _unique_index(
        representations,
        attribute="theorem_id",
        label="representation theorem binding",
    )
    context_index = _unique_index(
        contexts,
        attribute="context_id",
        label="context ID",
    )

    locator_index: dict[str, str] = {}
    locator_by_theorem: dict[str, str] = {}
    for theorem in theorems:
        try:
            locator = lf022_source_locator_id(theorem)
        except ValueError:
            continue
        previous = locator_index.get(locator)
        if previous is not None:
            raise LF022PublicPoolError(
                f"duplicate source locator {locator} for {previous} and {theorem.theorem_id}"
            )
        locator_index[locator] = theorem.theorem_id
        locator_by_theorem[theorem.theorem_id] = locator

    rejections: Counter[str] = Counter()
    eligible: list[_Eligible] = []
    for theorem in theorems:
        source_key = (theorem.source, theorem.source_revision)
        approval = approvals.get(source_key)
        representation = representations_by_theorem.get(theorem.theorem_id)
        context = context_index.get(theorem.context_id)

        reason: str | None = None
        if _is_private_source(theorem.source):
            reason = "private_source"
        elif approval is None:
            reason = "unapproved_source"
        elif (
            not theorem.is_proposition
            or theorem.elaboration_status is not ValidationStatus.ELABORATES
        ):
            reason = "not_fully_elaborated_proposition"
        elif theorem.metadata.get("transform_source_eligible") is not True:
            reason = "transform_source_ineligible"
        elif (
            theorem.parent_theorem_ids
            or len(theorem.root_ancestry_ids) != 1
            or theorem.ancestry_id != theorem.root_ancestry_ids[0]
        ):
            reason = "not_source_ancestry"
        elif not _source_ancestry_matches(
            theorem,
            locator_by_theorem.get(theorem.theorem_id, ""),
        ):
            reason = "ancestry_binding_mismatch"
        elif representation is None:
            reason = "missing_representation"
        elif not _representation_matches_theorem(representation, theorem):
            reason = "representation_binding_mismatch"
        elif not _representation_hash_matches(representation):
            reason = "representation_content_hash_mismatch"
        elif context is None or not _context_matches_approved_source(
            theorem=theorem,
            context=context,
            approval=approval,
        ):
            reason = "missing_or_mismatched_context"
        elif any(
            getattr(representation, view) is None
            or representation.view_status[view] is not ViewStatus.OK
            for view in ("headless", "signature_explicit")
        ):
            reason = "required_view_unavailable"
        else:
            source_locator_id = locator_by_theorem.get(theorem.theorem_id)
            if source_locator_id is None:
                reason = "unstable_source_locator"
            else:
                identifier_hits = tuple(
                    identifier
                    for identifier in (
                        source_locator_id,
                        theorem.theorem_id,
                        representation.representation_id,
                    )
                    if denylist_index.contains_row_id(identifier)
                )
                if identifier_hits:
                    reason = "denylist_identifier_hit"
                elif candidate_benchmark_hits(
                    denylist_index=denylist_index,
                    theorem=theorem,
                    representation=representation,
                ):
                    reason = "denylist_content_hit"
                else:
                    eligible.append(
                        _Eligible(
                            theorem=theorem,
                            representation=representation,
                            context=context,
                            root_ancestry_id=theorem.root_ancestry_ids[0],
                            source_locator_id=source_locator_id,
                            selection_rank=_selection_rank(
                                theorem,
                                representation,
                                source_locator_id,
                            ),
                        )
                    )
        if reason is not None:
            rejections[reason] += 1

    eligible.sort(
        key=lambda item: (
            item.selection_rank,
            item.source_locator_id,
            item.theorem.theorem_id,
            item.representation.representation_id,
        )
    )
    eligible_by_ancestry: dict[str, _Eligible] = {}
    for item in eligible:
        eligible_by_ancestry.setdefault(item.root_ancestry_id, item)
    unique_ancestry_eligible = tuple(eligible_by_ancestry.values())
    if len(unique_ancestry_eligible) < requested_count:
        counts = {reason: rejections[reason] for reason in _REJECTION_REASONS}
        raise LF022PublicPoolCapacityError(
            requested_count=requested_count,
            eligible_count=len(eligible),
            eligible_unique_ancestry_count=len(unique_ancestry_eligible),
            rejection_counts=counts,
        )
    selected = unique_ancestry_eligible[:requested_count]

    output = _output_directory(repo_root, output_directory)
    family_matrix_binding = _write_json(
        repo_root=repo_root,
        path=output / "family_matrix.json",
        record=family_matrix,
    )

    selected_by_source: dict[tuple[str, str], list[_Eligible]] = {}
    for item in selected:
        selected_by_source.setdefault(
            (item.theorem.source, item.theorem.source_revision),
            [],
        ).append(item)

    extraction_bindings: dict[str, LF022ArtifactBinding] = {}
    authorization_bindings: dict[str, LF022ArtifactBinding] = {}
    authorizations: list[LF022PublicSourceAuthorization] = []
    authorization_by_source: dict[tuple[str, str], LF022PublicSourceAuthorization] = {}
    for source_key in sorted(selected_by_source):
        source, revision = source_key
        source_items = selected_by_source[source_key]
        extraction = make_lf022_authorized_extraction_manifest(
            source=source,
            source_revision=revision,
            members=tuple(
                LF022AuthorizedExtractionMember(
                    source_locator_id=item.source_locator_id,
                    theorem_id=item.theorem.theorem_id,
                    statement_content_hash=item.theorem.statement_content_hash,
                )
                for item in source_items
            ),
        )
        stem = _source_artifact_stem(source, revision)
        extraction_binding = _write_json(
            repo_root=repo_root,
            path=output / "extractions" / f"{stem}.json",
            record=extraction,
        )
        approval = approvals[source_key]
        authorization = make_lf022_public_source_authorization(
            source=source,
            source_revision=revision,
            license_id=approval.license_id,
            license_evidence_uri=approval.license_evidence_uri,
            context_project_uri=approval.context_project_uri,
            upstream_theorem_records=theorem_input_binding,
            upstream_context_records=context_input_binding,
            upstream_extraction_output_manifest=extraction_output_manifest_binding,
            upstream_representation_records=representation_input_binding,
            upstream_representation_output_manifest=representation_output_manifest_binding,
            extraction_reuse_attestation=extraction_reuse_attestation_binding,
            mathlib_source_frame=mathlib_source_frame_binding,
            extraction_manifest=extraction_binding,
        )
        authorization_binding = _write_json(
            repo_root=repo_root,
            path=output / "source_authorizations" / f"{authorization.authorization_id}.json",
            record=authorization,
        )
        key = _source_key(source, revision)
        extraction_bindings[key] = extraction_binding
        authorization_bindings[authorization.authorization_id] = authorization_binding
        authorizations.append(authorization)
        authorization_by_source[source_key] = authorization

    source_registry = make_lf022_public_source_authorization_registry(
        policy_version="lf022_public_source_authorization_v1",
        authorizations=tuple(authorizations),
    )
    source_registry_binding = _write_json(
        repo_root=repo_root,
        path=output / "public_source_authorization_registry.json",
        record=source_registry,
    )
    benchmark_manifest = make_lf022_benchmark_registry_manifest(
        policy_version=active_registry.policy_version,
        active_registry=active_registry_binding,
    )
    benchmark_manifest_binding = _write_json(
        repo_root=repo_root,
        path=output / "benchmark_registry_manifest.json",
        record=benchmark_manifest,
    )

    clearances = []
    source_records: list[LF022ProductionSourceRecord] = []
    for item in selected:
        authorization = authorization_by_source[(item.theorem.source, item.theorem.source_revision)]
        clearance = make_lf022_denylist_clearance_record(
            benchmark_manifest_id=benchmark_manifest.manifest_id,
            active_registry_file_sha256=active_registry_binding.sha256,
            active_registry_content_hash=denylist_index.registry_content_hash,
            source_locator_id=item.source_locator_id,
            theorem_id=item.theorem.theorem_id,
            theorem_statement_content_hash=item.theorem.statement_content_hash,
            representation_id=item.representation.representation_id,
            representation_content_hash=item.representation.content_hash,
            identifier_hits=(),
            content_hits=(),
        )
        clearances.append(clearance)
        source_records.append(
            make_lf022_production_source_record(
                source_locator_id=item.source_locator_id,
                source=item.theorem.source,
                source_revision=item.theorem.source_revision,
                theorem_id=item.theorem.theorem_id,
                theorem_statement_content_hash=item.theorem.statement_content_hash,
                representation_id=item.representation.representation_id,
                representation_content_hash=item.representation.content_hash,
                normalization_version=item.representation.normalization_version,
                context_id=item.context.context_id,
                context_fingerprint=item.context.context_fingerprint,
                context_header_hash=item.context.header_hash,
                public_source_authorization_id=authorization.authorization_id,
                denylist_clearance_id=clearance.clearance_id,
            )
        )

    ordered_clearances = tuple(sorted(clearances, key=lambda item: item.clearance_id))
    ordered_source_records = tuple(
        sorted(source_records, key=lambda item: item.admission_record_id)
    )
    ordered_theorems = tuple(
        sorted((item.theorem for item in selected), key=lambda item: item.theorem_id)
    )
    ordered_representations = tuple(
        sorted(
            (item.representation for item in selected),
            key=lambda item: item.representation_id,
        )
    )
    selected_context_ids = {item.context.context_id for item in selected}
    ordered_contexts = tuple(
        sorted(
            (context_index[context_id] for context_id in selected_context_ids),
            key=lambda item: item.context_id,
        )
    )

    clearance_binding = _write_jsonl(
        repo_root=repo_root,
        path=output / "denylist_clearances.jsonl",
        records=ordered_clearances,
    )
    source_pool_binding = _write_jsonl(
        repo_root=repo_root,
        path=output / "source_pool.jsonl",
        records=ordered_source_records,
    )
    theorem_binding = _write_jsonl(
        repo_root=repo_root,
        path=output / "theorems.jsonl",
        records=ordered_theorems,
    )
    representation_binding = _write_jsonl(
        repo_root=repo_root,
        path=output / "representations.jsonl",
        records=ordered_representations,
    )
    context_binding = _write_jsonl(
        repo_root=repo_root,
        path=output / "contexts.jsonl",
        records=ordered_contexts,
    )
    production_artifacts = LF022ProductionArtifactSet(
        family_matrix=family_matrix_binding,
        public_source_authorization_registry=source_registry_binding,
        benchmark_registry_manifest=benchmark_manifest_binding,
        active_benchmark_registry=active_registry_binding,
        denylist_clearance_records=clearance_binding,
        source_pool=source_pool_binding,
        theorem_records=theorem_binding,
        representation_records=representation_binding,
        context_records=context_binding,
    )
    admission = make_lf022_production_admission(
        family_matrix=family_matrix,
        artifacts=production_artifacts,
        profile=profile,
    )
    admission_binding = _write_json(
        repo_root=repo_root,
        path=output / "admission.json",
        record=admission,
    )
    plan = build_lf022_production_plan(
        repo_root=repo_root,
        admission=admission,
        family_matrix=family_matrix,
    )
    plan_binding = write_lf022_production_plan(
        repo_root=repo_root,
        relative_path=_relative(repo_root, output / "production_plan.json"),
        plan=plan,
    )

    output_artifacts = LF022PublicPoolOutputArtifacts(
        family_matrix=family_matrix_binding,
        upstream_extraction_output_manifest=extraction_output_manifest_binding,
        upstream_representation_output_manifest=representation_output_manifest_binding,
        mathlib_source_frame=mathlib_source_frame_binding,
        extraction_manifests=dict(sorted(extraction_bindings.items())),
        source_authorizations=dict(sorted(authorization_bindings.items())),
        public_source_authorization_registry=source_registry_binding,
        benchmark_registry_manifest=benchmark_manifest_binding,
        denylist_clearance_records=clearance_binding,
        source_pool=source_pool_binding,
        theorem_records=theorem_binding,
        representation_records=representation_binding,
        context_records=context_binding,
        admission=admission_binding,
        production_plan=plan_binding,
    )
    source_counts = Counter(item.theorem.source for item in selected)
    rejection_counts = {reason: rejections[reason] for reason in _REJECTION_REASONS}
    audit_payload: dict[str, object] = {
        "schema_version": 1,
        "selection_version": LF022_PUBLIC_POOL_SELECTION_VERSION,
        "profile": profile,
        "requested_count": requested_count,
        "input_theorems": theorem_input_binding.model_dump(mode="json"),
        "input_representations": representation_input_binding.model_dump(mode="json"),
        "input_contexts": context_input_binding.model_dump(mode="json"),
        "input_extraction_output_manifest": extraction_output_manifest_binding.model_dump(
            mode="json"
        ),
        "input_representation_output_manifest": (
            representation_output_manifest_binding.model_dump(mode="json")
        ),
        "input_mathlib_source_frame": mathlib_source_frame_binding.model_dump(mode="json"),
        "extraction_run_id": persisted_extraction_manifest.run_id,
        "representation_run_id": persisted_representation_manifest.run_id,
        "mathlib_source_frame_id": persisted_mathlib_frame.frame_id,
        "active_benchmark_registry": active_registry_binding.model_dump(mode="json"),
        "active_benchmark_registry_content_hash": denylist_index.registry_content_hash,
        "input_theorem_count": len(theorems),
        "input_representation_count": len(representations),
        "input_context_count": len(contexts),
        "orphan_representation_count": sum(
            theorem_id not in theorem_index for theorem_id in representations_by_theorem
        ),
        "unused_context_count": sum(
            context_id not in selected_context_ids for context_id in context_index
        ),
        "eligible_count": len(eligible),
        "eligible_unique_ancestry_count": len(unique_ancestry_eligible),
        "eligible_not_selected_count": len(eligible) - requested_count,
        "selected_count": len(selected),
        "selected_unique_ancestry_count": len({item.root_ancestry_id for item in selected}),
        "rejection_counts": rejection_counts,
        "selected_source_counts": dict(sorted(source_counts.items())),
        "selection_order_theorem_ids": [item.theorem.theorem_id for item in selected],
        "outputs": output_artifacts.model_dump(mode="json"),
        "public_sources_only": True,
        "private_sft_classic_forbidden": True,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
    }
    audit = LF022PublicPoolAudit.model_validate(
        {
            **audit_payload,
            "audit_id": make_id("lf022_public_pool_audit", audit_payload),
        }
    )
    audit_binding = _write_json(
        repo_root=repo_root,
        path=output / "audit.json",
        record=audit,
    )
    return MaterializedLF022PublicPool(
        audit=audit,
        audit_binding=audit_binding,
        admission=admission,
        plan=plan,
    )


__all__ = [
    "LF022_PUBLIC_POOL_SELECTION_VERSION",
    "LF022ApprovedPublicSource",
    "LF022PublicPoolAudit",
    "LF022PublicPoolCapacityError",
    "LF022PublicPoolError",
    "LF022PublicPoolOutputArtifacts",
    "MaterializedLF022PublicPool",
    "materialize_lf022_public_pool",
]

"""Fail-closed LF-022 candidate inventory for later two-family judging.

This stage turns content-bound, mechanically Lean-valid public LF-022 checks
into a compact dispatch inventory.  A completed and replay-verified Codex audit
may be bound as optional diagnostic metadata, but is not an admission
prerequisite and is never counted as either weak-supervision judge.

Every dispatch-eligible pair still requires four independent calls:
``judge_A`` and ``judge_B`` in both ``AB`` and ``BA`` orientations.  Exact
judge-visible payload duplicates are retained for provenance but only one canonical
member is dispatch-eligible.  The resulting records are schema-barred from
labels, silver promotion, training, evaluation, and gate credit.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import (
    LF022BatchFreezeRequest,
    LF022PublicBatchManifest,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditError,
    LF022CodexAuditInput,
    LF022CodexAuditManifest,
    LF022VerifiedCodexAuditJudgment,
    load_lean_valid_audit_inputs,
    verify_completed_lf022_codex_audit,
)
from leanfaith.generation.lf022_execution import (
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
)
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckManifest,
    LF022LeanCheckRecord,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding, canonical_model_family
from leanfaith.generation.weak_supervision import (
    FamilySeparationMatrix,
    PublicLeanJudgePair,
    validate_family_separation,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_SUPERVISION_CANDIDATE_VERSION: Literal["lf022_supervision_candidate_inventory_v3"] = (
    "lf022_supervision_candidate_inventory_v3"
)
_REQUIRED_CELLS = ("judge_A:AB", "judge_A:BA", "judge_B:AB", "judge_B:BA")
_VALID_LEAN_CHECK_OUTCOMES = frozenset({"elaborates", "elaborates_with_placeholder"})


class LF022SupervisionCandidateError(RuntimeError):
    """A source binding or provisional-only invariant failed."""


class CandidateArtifactBinding(StrictModel):
    """Exact path and byte hash of one immutable input artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


def _judge_visible_payload_hash(pair: PublicLeanJudgePair) -> str:
    """Hash exactly the semantic fields rendered to every blinded judge.

    The per-call opaque token is deliberately excluded because it is a dispatch
    nonce rather than semantic input.  Natural language is included, including
    the distinction between ``null`` and a string.
    """

    return hash_canonical(
        {
            "schema": "lf022_judge_visible_payload_v2",
            "lean_a": pair.canonical_lean_a,
            "lean_b": pair.canonical_lean_b,
            "optional_natural_language": pair.optional_natural_language,
        }
    )


def _source_candidate_item_id(item: LF022CodexAuditInput) -> str:
    """Build the v3 source-neutral identity from one verified Lean-check projection."""

    return make_id(
        "lf022_supervision_source",
        {
            "schema": "lf022_supervision_source_v1",
            "lean_check_id": item.lean_check_id,
            "variant_id": item.variant_id,
            "pair_id": item.pair.pair_id,
            "pair_admission_sha256": item.pair.admission_sha256,
        },
    )


class LF022SupervisionCandidateSpec(StrictModel):
    """Frozen request for one candidate inventory."""

    schema_version: Literal[2, 3] = 3
    method_version: Literal[
        "lf022_supervision_candidate_inventory_v2",
        "lf022_supervision_candidate_inventory_v3",
    ] = LF022_SUPERVISION_CANDIDATE_VERSION
    collection_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    proposer_family_id: str = Field(min_length=1)
    proposer_model: str = Field(min_length=1)
    judge_a_family_id: str = Field(min_length=1)
    judge_b_family_id: str = Field(min_length=1)
    primary_eval_judge_family_id: str = Field(min_length=1)
    checks: CandidateArtifactBinding
    lean_check_manifest: CandidateArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    codex_audit_manifest: CandidateArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    public_sources_only: Literal[True] = True
    codex_is_diagnostic_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _family_separation(self) -> Self:
        expected_method = f"lf022_supervision_candidate_inventory_v{self.schema_version}"
        if self.method_version != expected_method:
            raise ValueError("candidate spec schema and method versions differ")
        if self.schema_version == 2 and self.codex_audit_manifest is None:
            raise ValueError("candidate spec v2 requires a Codex audit manifest")
        if self.schema_version == 2 and self.lean_check_manifest is not None:
            raise ValueError("candidate spec v2 cannot carry a v3 Lean-check manifest binding")
        if self.schema_version == 3 and self.lean_check_manifest is None:
            raise ValueError("candidate spec v3 requires a Lean-check manifest")
        validate_family_separation(
            FamilySeparationMatrix(
                proposer_family=self.proposer_family_id,
                judge_a_family=self.judge_a_family_id,
                judge_b_family=self.judge_b_family_id,
                primary_eval_judge_family=self.primary_eval_judge_family_id,
            )
        )
        return self


class PriorCodexDiagnostic(StrictModel):
    """Replay-verified one-pass diagnostic that cannot become supervision."""

    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    same_claim_answer: Literal["same_claim", "not_same_claim", "ambiguous", "uncertain"]
    relation: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_expert_review: bool
    parsed_response_sha256: str = Field(pattern=HEX64_PATTERN)
    source_candidate_item_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_supervision_source"),
        exclude_if=lambda value: value is None,
    )
    diagnostic_only: Literal[True] = True
    weak_judge_vote: Literal[False] = False


CandidateDispatchStatus = Literal[
    "ready_for_two_family_judging",
    "exact_duplicate_not_dispatched",
]


class LF022SupervisionCandidateRecord(StrictModel):
    """One public pair awaiting the complete two-family swapped-order audit."""

    schema_version: Literal[2, 3] = 3
    candidate_inventory_record_id: str = Field(pattern=id_pattern("lf022_supervision_candidate"))
    collection_id: str = Field(min_length=1)
    pair_id: str = Field(pattern=id_pattern("pair"))
    variant_id: str = Field(pattern=id_pattern("var"))
    lean_check_id: str = Field(pattern=id_pattern("lf022_lean_check"))
    proposer_family_id: str = Field(min_length=1)
    proposer_model: str = Field(min_length=1)
    pair: PublicLeanJudgePair
    pair_admission_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_visible_payload_sha256: str = Field(pattern=HEX64_PATTERN)
    dispatch_status: CandidateDispatchStatus
    canonical_dispatch_pair_id: str = Field(pattern=id_pattern("pair"))
    canonical_dispatch_audit_item_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_codex_audit_item"),
        exclude_if=lambda value: value is None,
    )
    source_candidate_item_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_supervision_source"),
        exclude_if=lambda value: value is None,
    )
    canonical_dispatch_source_item_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_supervision_source"),
        exclude_if=lambda value: value is None,
    )
    required_judgment_cells: tuple[str, ...]
    prior_codex_diagnostic: PriorCodexDiagnostic | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    promotion_blockers: tuple[str, ...] = Field(min_length=1)
    candidate_state: Literal["unresolved_awaiting_two_family_judging"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.schema_version == 2:
            if self.prior_codex_diagnostic is None:
                raise ValueError("candidate record v2 requires its prior Codex diagnostic")
            if (
                self.canonical_dispatch_audit_item_id is None
                or self.source_candidate_item_id is not None
                or self.canonical_dispatch_source_item_id is not None
                or self.candidate_state is not None
                or self.prior_codex_diagnostic.source_candidate_item_id is not None
            ):
                raise ValueError("candidate record v2 contains v3-only source lineage")
            source_item_id = self.prior_codex_diagnostic.audit_item_id
            canonical_source_item_id = self.canonical_dispatch_audit_item_id
        else:
            if (
                self.source_candidate_item_id is None
                or self.canonical_dispatch_source_item_id is None
                or self.candidate_state != "unresolved_awaiting_two_family_judging"
            ):
                raise ValueError("candidate record v3 requires unresolved source-neutral lineage")
            if self.canonical_dispatch_audit_item_id is not None:
                raise ValueError("candidate record v3 cannot use Codex audit lineage for dispatch")
            source_item_id = self.source_candidate_item_id
            canonical_source_item_id = self.canonical_dispatch_source_item_id
            if (
                self.prior_codex_diagnostic is not None
                and self.prior_codex_diagnostic.source_candidate_item_id != source_item_id
            ):
                raise ValueError("optional Codex diagnostic differs from source candidate item")
        if self.pair_id != self.pair.pair_id:
            raise ValueError("pair_id differs from embedded public pair")
        if self.pair_admission_sha256 != self.pair.admission_sha256:
            raise ValueError("pair admission hash differs from embedded public pair")
        if self.judge_visible_payload_sha256 != _judge_visible_payload_hash(self.pair):
            raise ValueError("judge-visible payload hash differs from embedded pair")
        if self.dispatch_status == "ready_for_two_family_judging":
            if source_item_id != canonical_source_item_id:
                raise ValueError("ready record must be the canonical dispatch source item")
            if self.pair_id != self.canonical_dispatch_pair_id:
                raise ValueError("ready record must bind the canonical dispatch pair")
            if self.required_judgment_cells != _REQUIRED_CELLS:
                raise ValueError("ready record requires all four judge/orientation cells")
        else:
            if source_item_id == canonical_source_item_id:
                raise ValueError("duplicate record cannot be the canonical dispatch source item")
            if self.required_judgment_cells:
                raise ValueError("duplicate record cannot schedule redundant judge calls")
        if tuple(sorted(set(self.promotion_blockers))) != self.promotion_blockers:
            raise ValueError("promotion blockers must be sorted and unique")
        expected_id = make_id(
            "lf022_supervision_candidate",
            self.model_dump(mode="json", exclude={"candidate_inventory_record_id"}),
        )
        if self.candidate_inventory_record_id != expected_id:
            raise ValueError("candidate inventory record ID differs from content")
        return self


class LF022SupervisionCandidateManifest(StrictModel):
    """Exact inventory summary and immutable output binding."""

    schema_version: Literal[2, 3, 4] = 3
    method_version: Literal[
        "lf022_supervision_candidate_inventory_v2",
        "lf022_supervision_candidate_inventory_v3",
        "lf022_supervision_candidate_inventory_v4",
    ] = LF022_SUPERVISION_CANDIDATE_VERSION
    inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    collection_id: str
    spec_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    selection_spec_seed_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    lean_check_manifest_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    codex_audit_manifest_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    logical_input_binding_sha256: str = Field(pattern=HEX64_PATTERN)
    codex_response_artifact_set_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    proposer_family_id: str
    proposer_model: str
    judge_a_family_id: str
    judge_b_family_id: str
    primary_eval_judge_family_id: str
    records_artifact: Literal["candidates.jsonl"] = "candidates.jsonl"
    records_sha256: str = Field(pattern=HEX64_PATTERN)
    public_sample_artifact: Literal["public_sample.jsonl"] = "public_sample.jsonl"
    public_sample_sha256: str = Field(pattern=HEX64_PATTERN)
    public_sample_count: int = Field(ge=0, le=10, strict=True)
    summary_artifact: Literal["summary.md"] = "summary.md"
    summary_sha256: str = Field(pattern=HEX64_PATTERN)
    record_count: int = Field(ge=0, strict=True)
    unique_judge_visible_payload_count: int = Field(ge=0, strict=True)
    exact_duplicate_record_count: int = Field(ge=0, strict=True)
    dispatch_eligible_count: int = Field(ge=0, strict=True)
    required_future_judge_call_count: int = Field(ge=0, strict=True)
    codex_diagnostic_status: Literal["absent", "complete"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    codex_diagnostic_record_count: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    codex_same_claim_counts: dict[str, int]
    dispatch_status_counts: dict[str, int]
    codex_is_diagnostic_only: Literal[True] = True
    two_family_judgments_completed: Literal[False] = False
    human_pilot_bound: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _reconcile(self) -> Self:
        expected_method = f"lf022_supervision_candidate_inventory_v{self.schema_version}"
        if self.method_version != expected_method:
            raise ValueError("candidate manifest schema and method versions differ")
        if self.schema_version in {2, 3}:
            if self.spec_sha256 is None or self.selection_spec_seed_sha256 is not None:
                raise ValueError("candidate manifest v2/v3 requires only spec_sha256")
        elif self.spec_sha256 is not None or self.selection_spec_seed_sha256 is None:
            raise ValueError("candidate manifest v4 requires only selection_spec_seed_sha256")
        if self.schema_version == 2:
            if self.lean_check_manifest_sha256 is not None:
                raise ValueError("candidate manifest v2 cannot carry a v3 Lean-check binding")
            if (
                self.codex_audit_manifest_sha256 is None
                or self.codex_response_artifact_set_sha256 is None
            ):
                raise ValueError("candidate manifest v2 requires Codex diagnostic bindings")
            if self.codex_diagnostic_record_count is not None:
                raise ValueError("candidate manifest v2 cannot carry the v3 diagnostic count")
            if self.codex_diagnostic_status is not None:
                raise ValueError("candidate manifest v2 cannot carry v3 diagnostic status")
            diagnostic_count = self.record_count
        else:
            if self.lean_check_manifest_sha256 is None:
                raise ValueError("candidate manifest v3 requires a Lean-check manifest binding")
            if self.codex_diagnostic_record_count is None:
                raise ValueError("candidate manifest v3 requires the diagnostic count")
            if self.codex_diagnostic_status is None:
                raise ValueError("candidate manifest v3 requires diagnostic status")
            diagnostic_count = self.codex_diagnostic_record_count
            diagnostic_bindings = (
                self.codex_audit_manifest_sha256,
                self.codex_response_artifact_set_sha256,
            )
            if any(value is None for value in diagnostic_bindings) != all(
                value is None for value in diagnostic_bindings
            ):
                raise ValueError("optional Codex diagnostic bindings must be complete or absent")
            if self.codex_audit_manifest_sha256 is None and diagnostic_count != 0:
                raise ValueError("unbound Codex diagnostics require a zero diagnostic count")
            expected_status = "absent" if self.codex_audit_manifest_sha256 is None else "complete"
            if self.codex_diagnostic_status != expected_status:
                raise ValueError("Codex diagnostic status differs from its bindings")
            if (
                self.codex_audit_manifest_sha256 is not None
                and diagnostic_count != self.record_count
            ):
                raise ValueError("completed Codex diagnostic must cover every candidate record")
        if self.record_count != sum(self.dispatch_status_counts.values()):
            raise ValueError("dispatch status counts do not reconcile")
        if self.dispatch_eligible_count != self.dispatch_status_counts.get(
            "ready_for_two_family_judging", 0
        ):
            raise ValueError("dispatch-eligible count does not reconcile")
        if self.exact_duplicate_record_count != (
            self.record_count - self.unique_judge_visible_payload_count
        ):
            raise ValueError("exact duplicate count does not reconcile")
        if self.required_future_judge_call_count != 4 * self.dispatch_eligible_count:
            raise ValueError("future judge-call count must be four per dispatch pair")
        if sum(self.codex_same_claim_counts.values()) != diagnostic_count:
            raise ValueError("Codex diagnostic counts do not reconcile")
        expected = make_id(
            "lf022_supervision_inventory",
            self.model_dump(
                mode="json",
                exclude={
                    "inventory_id",
                    "records_artifact",
                    "public_sample_artifact",
                    "summary_artifact",
                    "spec_sha256",
                    "selection_spec_seed_sha256",
                },
            ),
        )
        if self.inventory_id != expected:
            raise ValueError("inventory ID differs from manifest content")
        return self


def _render_summary(values: dict[str, object]) -> bytes:
    """Render a logical/hash-only provisional inventory report."""

    codex_counts = values["codex_same_claim_counts"]
    status_counts = values["dispatch_status_counts"]
    assert isinstance(codex_counts, dict)
    assert isinstance(status_counts, dict)

    def counts(mapping: dict[object, object]) -> str:
        return ", ".join(f"`{key}` {value}" for key, value in sorted(mapping.items())) or "none"

    diagnostic_manifest_sha = values.get("codex_audit_manifest_sha256")
    diagnostic_count = values.get("codex_diagnostic_record_count", values["record_count"])
    diagnostic_bound = isinstance(diagnostic_manifest_sha, str)
    lines = [
        "# LF-022 provisional supervision candidate inventory",
        "",
        "This report contains public source/candidate pairs admitted from exact, replayed",
        "Lean-valid check records. Any bound Codex result remains diagnostic-only and",
        "contributes zero weak-supervision votes. No semantic label, silver record, training",
        "record, evaluation record, or gate credit is created here.",
        "",
        "## Exact logical input bindings",
        "",
        f"- Collection: `{values['collection_id']}`",
        f"- Lean checks SHA-256: `{values['checks_sha256']}`",
        f"- Lean-check manifest SHA-256: `{values.get('lean_check_manifest_sha256')}`",
        "- Codex diagnostic binding: "
        + (
            f"manifest `{diagnostic_manifest_sha}`, response set "
            f"`{values.get('codex_response_artifact_set_sha256')}`"
            if diagnostic_bound
            else "none"
        ),
        f"- Logical input binding SHA-256: `{values['logical_input_binding_sha256']}`",
        "",
        "## Candidate counts",
        "",
        f"- Lean-valid candidate records: {values['record_count']}",
        f"- Records carrying an optional Codex diagnostic: {diagnostic_count}",
        f"- Exact unique judge-visible payloads: {values['unique_judge_visible_payload_count']}",
        "- Exact duplicate records retained but not dispatched: "
        f"{values['exact_duplicate_record_count']}",
        f"- Ready for later two-family judging: {values['dispatch_eligible_count']}",
        f"- Future judge calls required: {values['required_future_judge_call_count']}",
        f"- Dispatch statuses: {counts(status_counts)}",
        f"- Optional Codex diagnostic verdicts: {counts(codex_counts)}",
        "",
        "Each dispatch-eligible pair still requires four independent blinded calls:",
        "`judge_A:AB`, `judge_A:BA`, `judge_B:AB`, and `judge_B:BA`.",
        "The configured weak judges are distinct from the proposer and from the reserved",
        "primary evaluation judge. Human-pilot and promotion audits remain blockers.",
        "",
        "## Inspectable sample",
        "",
        f"`public_sample.jsonl` contains {values['public_sample_count']} public-only records",
        "with both Lean statements and, when bound, optional audit-only Codex metadata.",
        "The complete inventory is in `candidates.jsonl`.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _lexical_no_symlink_components(
    path: Path,
    *,
    base: Path,
    label: str,
    allow_missing_leaf: bool = False,
) -> Path:
    """Return an absolute lexical path without resolving through symlinks."""

    if not path.is_absolute() and (
        not path.parts or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LF022SupervisionCandidateError(f"{label} must be a normalized path")
    lexical = path if path.is_absolute() else base / path
    if not lexical.is_absolute():
        raise LF022SupervisionCandidateError(f"{label} base must make the path absolute")
    current = Path(lexical.anchor)
    parts = lexical.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise LF022SupervisionCandidateError(
                f"{label} contains a symlinked component: {current}"
            )
        if not current.exists():
            if allow_missing_leaf and index == len(parts) - 1:
                return current
            raise LF022SupervisionCandidateError(f"{label} is missing: {current}")
        if index < len(parts) - 1 and not current.is_dir():
            raise LF022SupervisionCandidateError(
                f"{label} has a non-directory parent component: {current}"
            )
    return current


def _resolve_bound_path(binding: CandidateArtifactBinding, *, repo_root: Path) -> Path:
    path = _lexical_no_symlink_components(
        Path(binding.path),
        base=repo_root,
        label="bound artifact",
    )
    if not path.is_file():
        raise LF022SupervisionCandidateError(f"bound artifact is not a regular file: {path}")
    if hash_file(path) != binding.sha256:
        raise LF022SupervisionCandidateError(f"bound artifact hash mismatch: {path}")
    return path


def _load_spec(
    *,
    repo_root: Path,
    spec_path: Path,
    expected_spec_sha256: str,
) -> LF022SupervisionCandidateSpec:
    spec_path = _lexical_no_symlink_components(
        spec_path,
        base=repo_root,
        label="candidate inventory spec",
    )
    if not spec_path.is_file():
        raise LF022SupervisionCandidateError(f"spec is missing or unsafe: {spec_path}")
    if hash_file(spec_path) != expected_spec_sha256:
        raise LF022SupervisionCandidateError("spec hash differs from expected SHA-256")
    raw = spec_path.read_bytes()
    try:
        spec = LF022SupervisionCandidateSpec.model_validate_json(raw)
    except ValueError as exc:
        raise LF022SupervisionCandidateError(f"invalid candidate inventory spec: {exc}") from exc
    if raw not in {
        canonical_json_bytes(spec.model_dump(mode="json")),
        canonical_json_bytes(spec.model_dump(mode="json")) + b"\n",
    }:
        raise LF022SupervisionCandidateError("spec is not canonical JSON")
    return spec


def _load_canonical_checks(checks_path: Path) -> tuple[LF022LeanCheckRecord, ...]:
    """Load every content-addressed check without changing the manifest denominator."""

    raw = checks_path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise LF022SupervisionCandidateError("Lean checks JSONL lacks a terminal newline")
    records: list[LF022LeanCheckRecord] = []
    seen_check_ids: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        try:
            record = LF022LeanCheckRecord.model_validate_json(line)
        except ValueError as exc:
            raise LF022SupervisionCandidateError(
                f"invalid Lean check at line {line_number}: {exc}"
            ) from exc
        expected = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        if line != expected:
            raise LF022SupervisionCandidateError(
                f"Lean check at line {line_number} is not canonical JSONL"
            )
        if record.check_id in seen_check_ids:
            raise LF022SupervisionCandidateError(f"duplicate Lean check ID {record.check_id}")
        seen_check_ids.add(record.check_id)
        records.append(record)
    return tuple(records)


@dataclass(frozen=True, slots=True)
class _VerifiedLeanCheckSelector:
    """Durably replayed selector lineage without current-policy eligibility replay."""

    expected_variants: tuple[tuple[str, str, str], ...]
    frozen_tasks_by_id: dict[str, LF022GOpenExecutionTask]


def _load_bound_canonical_model[ModelT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[ModelT],
    label: str,
) -> ModelT:
    """Hash-check and canonical-parse one historical frozen artifact."""

    path = _resolve_bound_path(
        CandidateArtifactBinding(path=binding.path, sha256=binding.sha256),
        repo_root=repo_root,
    )
    raw = path.read_bytes()
    try:
        parsed = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022SupervisionCandidateError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(parsed.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022SupervisionCandidateError(f"{label} is not canonical JSON")
    return parsed


def _load_historical_batch_tasks(
    *,
    repo_root: Path,
    batch: LF022PublicBatchManifest,
) -> dict[str, LF022GOpenExecutionTask]:
    """Replay durable freeze/admission/task bindings without today's eligibility policy."""

    request = _load_bound_canonical_model(
        repo_root=repo_root,
        binding=batch.freeze_request,
        model=LF022BatchFreezeRequest,
        label="frozen LF-022 batch request",
    )
    if (
        request.request_id != batch.freeze_request_id
        or request.batch_directory != batch.batch_directory
        or request.executor_output_root != batch.executor_output_root
        or len(request.routes) != len(batch.routes)
    ):
        raise LF022SupervisionCandidateError("batch manifest differs from its frozen request")

    frozen_tasks: dict[str, LF022GOpenExecutionTask] = {}
    for route, route_request in zip(batch.routes, request.routes, strict=True):
        if (
            route.proposer_family_id != route_request.proposer_family_id
            or route.model_id != route_request.route.model_id
            or route.execution_scope != route_request.route.execution_scope
            or route.public_pool_audit_id != route_request.public_pool_audit_id
            or route.allocation_plan_id != route_request.allocation_plan_id
        ):
            raise LF022SupervisionCandidateError("batch route differs from its frozen request")
        admission = _load_bound_canonical_model(
            repo_root=repo_root,
            binding=route.admission,
            model=LF022GOpenExecutionAdmission,
            label=f"{route.proposer_family_id} frozen admission",
        )
        if (
            admission.admission_id != route.admission_id
            or admission.public_pool_audit_id != route.public_pool_audit_id
            or admission.allocation_plan_id != route.allocation_plan_id
            or admission.artifacts != route_request.execution_artifacts
            or admission.route != route_request.route
            or admission.retry_policy != route_request.retry_policy
            or admission.code_tree_hash != route_request.code_tree_hash
            or admission.route.proposer_family_id != route.proposer_family_id
            or admission.route.model_id != route.model_id
            or admission.route.execution_scope != route.execution_scope
        ):
            raise LF022SupervisionCandidateError("batch route differs from its frozen admission")

        allocation_task_ids: list[str] = []
        for task_binding in route.tasks:
            task = _load_bound_canonical_model(
                repo_root=repo_root,
                binding=task_binding.task,
                model=LF022GOpenExecutionTask,
                label=f"frozen batch task {task_binding.execution_task_id}",
            )
            if (
                task.execution_task_id != task_binding.execution_task_id
                or task.allocation_task.task_id != task_binding.allocation_task_id
                or task.execution_admission_id != admission.admission_id
                or task.allocation_plan_id != admission.allocation_plan_id
                or task.allocation_task.proposer_family_id != route.proposer_family_id
                or task.proposal_count != route_request.proposal_count
                or task.requested_relations != route_request.requested_relations
            ):
                raise LF022SupervisionCandidateError(
                    "batch task binding differs from frozen task content"
                )
            task_digest = task.execution_task_id.split(":", 1)[1]
            adjacent_task_path = _lexical_no_symlink_components(
                Path(batch.executor_output_root)
                / "tasks"
                / task_digest[:2]
                / task_digest
                / "task.json",
                base=repo_root,
                label=f"adjacent executor task {task.execution_task_id}",
            )
            if not adjacent_task_path.is_file():
                raise LF022SupervisionCandidateError(
                    f"adjacent executor task is missing: {task.execution_task_id}"
                )
            raw_adjacent_task = adjacent_task_path.read_bytes()
            try:
                adjacent_task = LF022GOpenExecutionTask.model_validate_json(raw_adjacent_task)
            except ValueError as exc:
                raise LF022SupervisionCandidateError(
                    f"invalid adjacent executor task {task.execution_task_id}: {exc}"
                ) from exc
            canonical_adjacent_task = canonical_json_bytes(adjacent_task.model_dump(mode="json"))
            if raw_adjacent_task not in {
                canonical_adjacent_task,
                canonical_adjacent_task + b"\n",
            }:
                raise LF022SupervisionCandidateError(
                    f"adjacent executor task is not canonical: {task.execution_task_id}"
                )
            if adjacent_task != task:
                raise LF022SupervisionCandidateError(
                    f"adjacent executor task differs from frozen task: {task.execution_task_id}"
                )
            if task.execution_task_id in frozen_tasks:
                raise LF022SupervisionCandidateError(
                    f"batch repeats frozen execution task {task.execution_task_id}"
                )
            frozen_tasks[task.execution_task_id] = task
            allocation_task_ids.append(task.allocation_task.task_id)
        if tuple(sorted(allocation_task_ids)) != route_request.allocation_task_ids:
            raise LF022SupervisionCandidateError(
                "batch tasks differ from the frozen request allocation selection"
            )
    if len(frozen_tasks) != batch.total_task_count:
        raise LF022SupervisionCandidateError("loaded frozen task count differs from batch manifest")
    return frozen_tasks


def _verify_lean_check_selector(
    *,
    repo_root: Path,
    manifest: LF022LeanCheckManifest,
) -> _VerifiedLeanCheckSelector:
    """Replay the one exact batch or post-generation selector bound by the checks."""

    has_batch = manifest.selection_batch_manifest is not None
    has_postgen = manifest.selection_postgen_selector is not None
    if has_batch == has_postgen:
        raise LF022SupervisionCandidateError(
            "Lean-check manifest must bind exactly one batch or postgen selector lineage"
        )
    if has_batch:
        assert manifest.selection_batch_manifest is not None
        assert manifest.selection_batch_manifest_sha256 is not None
        selector_path = _resolve_bound_path(
            CandidateArtifactBinding(
                path=manifest.selection_batch_manifest,
                sha256=manifest.selection_batch_manifest_sha256,
            ),
            repo_root=repo_root,
        )
        raw_selector = selector_path.read_bytes()
        try:
            batch = LF022PublicBatchManifest.model_validate_json(raw_selector)
        except ValueError as exc:
            raise LF022SupervisionCandidateError(
                f"invalid Lean-check batch selector: {exc}"
            ) from exc
        canonical_selector = canonical_json_bytes(batch.model_dump(mode="json"))
        if raw_selector not in {canonical_selector, canonical_selector + b"\n"}:
            raise LF022SupervisionCandidateError("Lean-check batch selector is not canonical JSON")
        if batch.batch_id != manifest.selection_batch_id:
            raise LF022SupervisionCandidateError("Lean-check batch selector ID differs")
        frozen_tasks_by_id = _load_historical_batch_tasks(repo_root=repo_root, batch=batch)
        selected_task_ids = tuple(
            sorted(task.execution_task_id for route in batch.routes for task in route.tasks)
        )
        output_root = Path(batch.executor_output_root)
        if not output_root.is_absolute():
            output_root = repo_root / output_root
        terminal_paths = {
            task_id: output_root
            / "tasks"
            / task_id.removeprefix("lf022_execution_task:")[:2]
            / task_id.removeprefix("lf022_execution_task:")
            / "terminal.json"
            for task_id in selected_task_ids
        }
    else:
        from leanfaith.generation.lf022_postgen_reconcile import (
            LF022PostgenReconciliationError,
            verify_lf022_postgen_terminal_selector_selected_only,
        )

        assert manifest.selection_postgen_selector is not None
        assert manifest.selection_postgen_selector_sha256 is not None
        assert manifest.selection_postgen_selector_id is not None
        selector_path = _resolve_bound_path(
            CandidateArtifactBinding(
                path=manifest.selection_postgen_selector,
                sha256=manifest.selection_postgen_selector_sha256,
            ),
            repo_root=repo_root,
        )
        try:
            verified = verify_lf022_postgen_terminal_selector_selected_only(
                repo_root=repo_root,
                selector_path=selector_path,
            )
        except LF022PostgenReconciliationError as exc:
            raise LF022SupervisionCandidateError(
                f"Lean-check postgen selector replay failed: {exc}"
            ) from exc
        if (
            verified.selector.selector_id != manifest.selection_postgen_selector_id
            or verified.manifest.batch_id != manifest.selection_batch_id
        ):
            raise LF022SupervisionCandidateError("Lean-check postgen selector identity differs")
        frozen_tasks_by_id = verified.frozen_tasks_by_id
        selected_task_ids = verified.execution_task_ids
        terminal_paths = verified.terminal_paths
    selected_count = len(selected_task_ids)
    if manifest.selected_execution_task_count != selected_count:
        raise LF022SupervisionCandidateError(
            "Lean-check selector count differs from its frozen selected-task count"
        )
    if len(set(selected_task_ids)) != selected_count:
        raise LF022SupervisionCandidateError("Lean-check selector repeats one task ID")

    from leanfaith.generation.lf022_executor import LF022ExecutionTerminalRecord

    expected_variants: list[tuple[str, str, str]] = []
    for task_id in selected_task_ids:
        terminal_path = _lexical_no_symlink_components(
            terminal_paths[task_id],
            base=repo_root,
            label=f"selector terminal {task_id}",
        )
        if not terminal_path.is_file():
            raise LF022SupervisionCandidateError(
                f"selected execution task lacks terminal artifact: {task_id}"
            )
        raw = terminal_path.read_bytes()
        try:
            terminal = LF022ExecutionTerminalRecord.model_validate_json(raw)
        except ValueError as exc:
            raise LF022SupervisionCandidateError(
                f"invalid selected execution terminal {task_id}: {exc}"
            ) from exc
        canonical = canonical_json_bytes(terminal.model_dump(mode="json"))
        if raw not in {canonical, canonical + b"\n"}:
            raise LF022SupervisionCandidateError(
                f"selected execution terminal is not canonical: {task_id}"
            )
        if terminal.execution_task_id != task_id:
            raise LF022SupervisionCandidateError(
                f"selected execution terminal task ID differs: {task_id}"
            )
        frozen_task = frozen_tasks_by_id.get(task_id)
        if frozen_task is None:
            raise LF022SupervisionCandidateError(
                f"selected execution task is absent from frozen batch tasks: {task_id}"
            )
        if terminal.execution_admission_id != frozen_task.execution_admission_id:
            raise LF022SupervisionCandidateError(
                f"selected execution terminal admission differs: {task_id}"
            )
        if terminal.status != "provisional_variants_created":
            continue
        assert terminal.variants_artifact is not None
        assert terminal.variants_sha256 is not None
        variants_path = _resolve_bound_path(
            CandidateArtifactBinding(
                path=terminal.variants_artifact,
                sha256=terminal.variants_sha256,
            ),
            repo_root=repo_root,
        )
        if variants_path != terminal_path.with_name("provisional_variants.jsonl"):
            raise LF022SupervisionCandidateError(
                f"selected variants are not colocated with their terminal: {task_id}"
            )
        lines = variants_path.read_bytes().splitlines(keepends=True)
        if len(lines) != terminal.provisional_variant_count:
            raise LF022SupervisionCandidateError(
                f"selected provisional variant count differs: {task_id}"
            )
        for line_number, line in enumerate(lines, start=1):
            if not line.endswith(b"\n"):
                raise LF022SupervisionCandidateError(
                    f"selected variant line lacks newline: {task_id}:{line_number}"
                )
            try:
                variant = VariantRecord.model_validate_json(line)
            except ValueError as exc:
                raise LF022SupervisionCandidateError(
                    f"invalid selected variant {task_id}:{line_number}: {exc}"
                ) from exc
            if line != canonical_json_bytes(variant.model_dump(mode="json")) + b"\n":
                raise LF022SupervisionCandidateError(
                    f"selected variant is not canonical: {task_id}:{line_number}"
                )
            expected_variants.append((task_id, variant.variant_id, sha256_hex(line)))
    return _VerifiedLeanCheckSelector(
        expected_variants=tuple(expected_variants),
        frozen_tasks_by_id=frozen_tasks_by_id,
    )


def _verify_lean_check_manifest(
    *,
    repo_root: Path,
    manifest_path: Path,
    checks_path: Path,
    checks: tuple[LF022LeanCheckRecord, ...],
) -> tuple[LF022LeanCheckManifest, _VerifiedLeanCheckSelector]:
    """Replay one canonical Lean-check manifest and its exact selector lineage."""

    raw = manifest_path.read_bytes()
    try:
        manifest = LF022LeanCheckManifest.model_validate_json(raw)
    except ValueError as exc:
        raise LF022SupervisionCandidateError(f"invalid Lean-check manifest: {exc}") from exc
    expected = canonical_json_bytes(manifest.model_dump(mode="json"))
    if raw not in {expected, expected + b"\n"}:
        raise LF022SupervisionCandidateError("Lean-check manifest is not canonical JSON")
    if manifest.checks_sha256 != hash_file(checks_path):
        raise LF022SupervisionCandidateError("Lean-check manifest differs from bound checks")
    if manifest.record_count != len(checks):
        raise LF022SupervisionCandidateError("Lean-check manifest record count differs")
    if manifest.ordered_variant_ids_hash != hash_canonical(
        [record.variant_id for record in checks]
    ):
        raise LF022SupervisionCandidateError("Lean-check ordered variant IDs differ")
    if manifest.status_counts != dict(
        sorted(Counter(record.lean_status.value for record in checks).items())
    ):
        raise LF022SupervisionCandidateError("Lean-check status counts differ")
    if manifest.outcome_counts != dict(
        sorted(Counter(record.outcome for record in checks).items())
    ):
        raise LF022SupervisionCandidateError("Lean-check outcome counts differ")
    expected_input_set_hash = hash_canonical(
        [
            {
                "variant_id": record.variant_id,
                "line_sha256": record.source_variant_line_sha256,
                "context_id": record.context_id,
                "import_header_sha256": record.import_header_sha256,
                "project_dir": str(Path(record.project_dir).resolve()),
            }
            for record in checks
        ]
    )
    if manifest.input_set_hash != expected_input_set_hash:
        raise LF022SupervisionCandidateError("Lean-check input-set hash differs")
    selector = _verify_lean_check_selector(repo_root=repo_root, manifest=manifest)
    return manifest, selector


def _load_check_source_lineage(
    *,
    repo_root: Path,
    check: LF022LeanCheckRecord,
    spec: LF022SupervisionCandidateSpec,
) -> tuple[VariantRecord, LF022GOpenExecutionTask]:
    """Replay one check's exact variant line and adjacent frozen execution task."""

    variant_path = _resolve_bound_path(
        CandidateArtifactBinding(
            path=check.source_variant_artifact,
            sha256=check.source_variant_artifact_sha256,
        ),
        repo_root=repo_root,
    )
    lines = variant_path.read_bytes().splitlines(keepends=True)
    try:
        line = lines[check.source_variant_line_number - 1]
    except IndexError as exc:
        raise LF022SupervisionCandidateError(
            f"variant source line is missing for {check.check_id}"
        ) from exc
    if sha256_hex(line) != check.source_variant_line_sha256:
        raise LF022SupervisionCandidateError(
            f"variant source line hash differs for {check.check_id}"
        )
    try:
        variant = VariantRecord.model_validate_json(line)
    except ValueError as exc:
        raise LF022SupervisionCandidateError(
            f"invalid source variant for {check.check_id}: {exc}"
        ) from exc
    if (
        line != canonical_json_bytes(variant.model_dump(mode="json")) + b"\n"
        or variant.variant_id != check.variant_id
        or variant.candidate_code_hash != check.candidate_code_hash
        or variant.extracted_statement is None
        or variant.context_id != check.context_id
    ):
        raise LF022SupervisionCandidateError(
            f"Lean check differs from its source variant for {check.check_id}"
        )
    task_path = _lexical_no_symlink_components(
        variant_path.with_name("task.json"),
        base=repo_root,
        label=f"source task for {check.check_id}",
    )
    if not task_path.is_file():
        raise LF022SupervisionCandidateError(f"source task is missing for {check.check_id}")
    raw_task = task_path.read_bytes()
    try:
        task = LF022GOpenExecutionTask.model_validate_json(raw_task)
    except ValueError as exc:
        raise LF022SupervisionCandidateError(
            f"invalid source task for {check.check_id}: {exc}"
        ) from exc
    canonical_task = canonical_json_bytes(task.model_dump(mode="json"))
    if raw_task not in {canonical_task, canonical_task + b"\n"}:
        raise LF022SupervisionCandidateError(f"source task is not canonical for {check.check_id}")
    if (
        task.source.source_id != check.source_id
        or task.source.source_revision != check.source_revision
        or task.source.context_id != check.context_id
    ):
        raise LF022SupervisionCandidateError(
            f"source task differs from Lean check for {check.check_id}"
        )
    expected_import_header = "\n".join(f"import {module}" for module in task.source.imports) + "\n"
    if check.import_header != expected_import_header or check.import_header_sha256 != sha256_hex(
        expected_import_header.encode("utf-8")
    ):
        raise LF022SupervisionCandidateError(
            f"source task imports differ from Lean check for {check.check_id}"
        )
    _validate_check_source_proposer_binding(variant=variant, task=task, spec=spec)
    return variant, task


def _validate_check_source_proposer_binding(
    *,
    variant: VariantRecord,
    task: LF022GOpenExecutionTask,
    spec: LF022SupervisionCandidateSpec,
) -> None:
    """Bind a direct v3 check to its frozen allocation family and proposer model."""

    if task.allocation_task.proposer_family_id != spec.proposer_family_id:
        raise LF022SupervisionCandidateError(
            "source task proposer family differs from frozen candidate spec"
        )
    expected_representation_ids = (
        (task.source.source_representation_id,)
        if task.source.source_representation_id is not None
        else ()
    )
    if (
        variant.source_theorem_ids != (task.source.source_theorem_id,)
        or variant.source_representation_ids != expected_representation_ids
        or variant.context_id != task.source.context_id
    ):
        raise LF022SupervisionCandidateError(
            "source variant lineage differs from its adjacent execution task"
        )
    if variant.generator_id != spec.proposer_model or variant.metadata.get(
        "proposer_family"
    ) != canonical_model_family(spec.proposer_model):
        raise LF022SupervisionCandidateError(
            "source variant proposer model/family differs from frozen candidate spec"
        )


def _record_values(
    *,
    spec: LF022SupervisionCandidateSpec,
    item: object,
    judgment: object | None,
    canonical_pair_id: str,
    canonical_source_item_id: str,
    codex_model: str | None,
    codex_reasoning_effort: str | None,
) -> dict[str, object]:
    # Local imports keep the public verification dataclasses as the source of truth.
    from leanfaith.generation.lf022_codex_audit import (
        LF022CodexAuditInput,
        LF022VerifiedCodexAuditJudgment,
    )

    if not isinstance(item, LF022CodexAuditInput):
        raise TypeError("candidate materialization requires a verified LF-022 source item")
    if judgment is not None and not isinstance(judgment, LF022VerifiedCodexAuditJudgment):
        raise TypeError("optional diagnostic must be one verified LF-022 Codex judgment")
    if (judgment is None) != (codex_model is None or codex_reasoning_effort is None):
        raise TypeError("optional Codex judgment and model metadata must be present together")
    pair = item.pair
    source_candidate_item_id = _source_candidate_item_id(item)
    visible_payload_hash = _judge_visible_payload_hash(pair)
    ready = source_candidate_item_id == canonical_source_item_id
    blockers = {
        "human_pilot_not_bound",
        "promotion_audit_missing",
        "silver_not_promoted",
        "swapped_order_judgments_missing",
        "two_family_judgments_missing",
    }
    if not ready:
        blockers.add("exact_duplicate_not_dispatched")
    values: dict[str, object] = {
        "schema_version": 3,
        "collection_id": spec.collection_id,
        "pair_id": pair.pair_id,
        "variant_id": item.variant_id,
        "lean_check_id": item.lean_check_id,
        "proposer_family_id": spec.proposer_family_id,
        "proposer_model": spec.proposer_model,
        "pair": pair.model_dump(mode="json"),
        "pair_admission_sha256": pair.admission_sha256,
        "judge_visible_payload_sha256": visible_payload_hash,
        "dispatch_status": (
            "ready_for_two_family_judging" if ready else "exact_duplicate_not_dispatched"
        ),
        "canonical_dispatch_pair_id": canonical_pair_id,
        "source_candidate_item_id": source_candidate_item_id,
        "canonical_dispatch_source_item_id": canonical_source_item_id,
        "required_judgment_cells": _REQUIRED_CELLS if ready else (),
        "promotion_blockers": tuple(sorted(blockers)),
        "candidate_state": "unresolved_awaiting_two_family_judging",
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    if judgment is not None:
        assert codex_model is not None
        assert codex_reasoning_effort is not None
        response = judgment.response
        values["prior_codex_diagnostic"] = PriorCodexDiagnostic(
            audit_item_id=judgment.audit_item_id,
            source_candidate_item_id=source_candidate_item_id,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
            same_claim_answer=response.same_claim_answer,
            relation=response.relation.value if response.relation is not None else None,
            confidence=response.confidence,
            needs_expert_review=response.needs_expert_review,
            parsed_response_sha256=judgment.parsed_response_sha256,
        ).model_dump(mode="json")
    return values


def _validate_variant_proposer_binding(
    *,
    variant: VariantRecord,
    judgment_proposer_family_id: str,
    judgment_variant_id: str,
    spec: LF022SupervisionCandidateSpec,
) -> None:
    """Bind the declared family/model to replayed audit and variant lineages."""

    if judgment_proposer_family_id != spec.proposer_family_id:
        raise LF022SupervisionCandidateError("audit proposer family differs from frozen spec")
    if (
        variant.variant_id != judgment_variant_id
        or variant.generator_id != spec.proposer_model
        or variant.metadata.get("proposer_family") != canonical_model_family(spec.proposer_model)
    ):
        raise LF022SupervisionCandidateError(
            "variant proposer model/family differs from frozen candidate spec"
        )


def build_lf022_supervision_candidate_inventory(
    *,
    repo_root: Path,
    spec_path: Path,
    expected_spec_sha256: str,
) -> tuple[tuple[LF022SupervisionCandidateRecord, ...], LF022SupervisionCandidateManifest]:
    """Verify all inputs and build a deterministic candidate-only inventory."""

    repo_root = repo_root.resolve()
    spec = _load_spec(
        repo_root=repo_root,
        spec_path=spec_path,
        expected_spec_sha256=expected_spec_sha256,
    )
    if spec.schema_version != 3 or spec.lean_check_manifest is None:
        raise LF022SupervisionCandidateError(
            "new candidate inventories require a schema-v3 spec and Lean-check manifest"
        )
    checks_path = _resolve_bound_path(spec.checks, repo_root=repo_root)
    checks = _load_canonical_checks(checks_path)
    lean_check_manifest_path = _resolve_bound_path(
        spec.lean_check_manifest,
        repo_root=repo_root,
    )
    _, selector = _verify_lean_check_manifest(
        repo_root=repo_root,
        manifest_path=lean_check_manifest_path,
        checks_path=checks_path,
        checks=checks,
    )
    source_variants_by_check: dict[str, VariantRecord] = {}
    check_variant_lineage: list[tuple[str, str, str]] = []
    for check in checks:
        variant, executor_task = _load_check_source_lineage(
            repo_root=repo_root,
            check=check,
            spec=spec,
        )
        frozen_task = selector.frozen_tasks_by_id.get(executor_task.execution_task_id)
        if frozen_task is None or frozen_task != executor_task:
            raise LF022SupervisionCandidateError(
                "adjacent executor task differs from its frozen batch task"
            )
        source_variants_by_check[check.check_id] = variant
        check_variant_lineage.append(
            (executor_task.execution_task_id, variant.variant_id, check.source_variant_line_sha256)
        )
    if tuple(check_variant_lineage) != selector.expected_variants:
        raise LF022SupervisionCandidateError(
            "Lean checks do not exactly cover selector-created provisional variants"
        )
    try:
        direct_items = load_lean_valid_audit_inputs(
            checks_path=checks_path,
            repo_root=repo_root,
        )
    except LF022CodexAuditError as exc:
        raise LF022SupervisionCandidateError(
            f"Lean-valid candidate projection failed: {exc}"
        ) from exc
    expected_valid_check_ids = {
        check.check_id
        for check in checks
        if check.outcome in _VALID_LEAN_CHECK_OUTCOMES and check.declaration_verified
    }
    projected_check_ids = {item.lean_check_id for item in direct_items}
    if (
        len(projected_check_ids) != len(direct_items)
        or projected_check_ids != expected_valid_check_ids
    ):
        raise LF022SupervisionCandidateError(
            "candidate projection does not contain all and only declaration-verified valid checks"
        )
    items_by_id = {item.audit_item_id: item for item in direct_items}
    if len(items_by_id) != len(direct_items):
        raise LF022SupervisionCandidateError("direct Lean-check projection has duplicate items")

    judgments_by_id: dict[str, LF022VerifiedCodexAuditJudgment] = {}
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    codex_response_artifact_set_sha256: str | None = None
    if spec.codex_audit_manifest is not None:
        audit_manifest_path = _resolve_bound_path(
            spec.codex_audit_manifest,
            repo_root=repo_root,
        )
        try:
            audit_manifest = LF022CodexAuditManifest.model_validate_json(
                audit_manifest_path.read_bytes()
            )
        except ValueError as exc:
            raise LF022SupervisionCandidateError(f"invalid Codex audit manifest: {exc}") from exc
        try:
            verified = verify_completed_lf022_codex_audit(
                repo_root=repo_root,
                checks_path=checks_path,
                audit_root=audit_manifest_path.parent,
                require_complete_clean=True,
            )
        except LF022CodexAuditError as exc:
            raise LF022SupervisionCandidateError(f"Codex audit replay failed: {exc}") from exc
        if verified.manifest != audit_manifest:
            raise LF022SupervisionCandidateError("verified Codex audit differs from bound manifest")
        verified_items_by_id = {item.audit_item_id: item for item in verified.items}
        judgments_by_id = {item.audit_item_id: item for item in verified.judgments}
        if (
            len(verified_items_by_id) != len(verified.items)
            or len(judgments_by_id) != len(verified.judgments)
            or verified_items_by_id != items_by_id
            or set(judgments_by_id) != set(items_by_id)
            or tuple(verified.checks) != checks
        ):
            raise LF022SupervisionCandidateError(
                "completed Codex diagnostic does not exactly cover the verified Lean checks"
            )
        codex_model = verified.manifest.model
        codex_reasoning_effort = verified.manifest.reasoning_effort
        codex_response_artifact_set_sha256 = verified.response_artifact_set_sha256

    checks_by_id = {check.check_id: check for check in checks}
    for item in direct_items:
        bound_check = checks_by_id.get(item.lean_check_id)
        if bound_check is None:
            raise LF022SupervisionCandidateError(
                f"candidate source item lacks Lean check {item.lean_check_id}"
            )
        variant = source_variants_by_check[bound_check.check_id]
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id=(
                judgments_by_id[item.audit_item_id].proposer_family_id
                if judgments_by_id
                else spec.proposer_family_id
            ),
            judgment_variant_id=item.variant_id,
            spec=spec,
        )

    payload_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    payload_hash_by_item: dict[str, str] = {}
    for audit_item_id, item in items_by_id.items():
        payload_hash = _judge_visible_payload_hash(item.pair)
        payload_hash_by_item[audit_item_id] = payload_hash
        payload_groups[payload_hash].append((_source_candidate_item_id(item), item.pair.pair_id))
    canonical_by_hash = {key: min(values) for key, values in payload_groups.items()}

    records: list[LF022SupervisionCandidateRecord] = []
    for audit_item_id in sorted(items_by_id):
        item = items_by_id[audit_item_id]
        judgment = judgments_by_id.get(audit_item_id)
        canonical_source_item_id, canonical_pair_id = canonical_by_hash[
            payload_hash_by_item[audit_item_id]
        ]
        record_values = _record_values(
            spec=spec,
            item=item,
            judgment=judgment,
            canonical_pair_id=canonical_pair_id,
            canonical_source_item_id=canonical_source_item_id,
            codex_model=codex_model,
            codex_reasoning_effort=codex_reasoning_effort,
        )
        record_id = make_id("lf022_supervision_candidate", record_values)
        records.append(
            LF022SupervisionCandidateRecord.model_validate(
                {**record_values, "candidate_inventory_record_id": record_id}
            )
        )
    records.sort(key=lambda item: (item.judge_visible_payload_sha256, item.pair_id))
    record_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records
    )
    public_sample = tuple(
        item for item in records if item.dispatch_status == "ready_for_two_family_judging"
    )[:10]
    public_sample_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in public_sample
    )
    status_counts = dict(sorted(Counter(item.dispatch_status for item in records).items()))
    codex_counts = dict(
        sorted(
            Counter(
                item.prior_codex_diagnostic.same_claim_answer
                for item in records
                if item.prior_codex_diagnostic is not None
            ).items()
        )
    )
    dispatch_count = status_counts.get("ready_for_two_family_judging", 0)
    manifest_values: dict[str, object] = {
        "schema_version": 3,
        "method_version": LF022_SUPERVISION_CANDIDATE_VERSION,
        "collection_id": spec.collection_id,
        "spec_sha256": expected_spec_sha256,
        "checks_sha256": spec.checks.sha256,
        "lean_check_manifest_sha256": spec.lean_check_manifest.sha256,
        "codex_audit_manifest_sha256": (
            spec.codex_audit_manifest.sha256 if spec.codex_audit_manifest is not None else None
        ),
        "logical_input_binding_sha256": hash_canonical(
            {
                "schema": "lf022_supervision_logical_input_v3",
                "collection_id": spec.collection_id,
                "checks_sha256": spec.checks.sha256,
                "lean_check_manifest_sha256": spec.lean_check_manifest.sha256,
                "codex_audit_manifest_sha256": (
                    spec.codex_audit_manifest.sha256
                    if spec.codex_audit_manifest is not None
                    else None
                ),
                "proposer_family_id": spec.proposer_family_id,
                "proposer_model": spec.proposer_model,
                "judge_a_family_id": spec.judge_a_family_id,
                "judge_b_family_id": spec.judge_b_family_id,
                "primary_eval_judge_family_id": spec.primary_eval_judge_family_id,
            }
        ),
        "codex_response_artifact_set_sha256": codex_response_artifact_set_sha256,
        "proposer_family_id": spec.proposer_family_id,
        "proposer_model": spec.proposer_model,
        "judge_a_family_id": spec.judge_a_family_id,
        "judge_b_family_id": spec.judge_b_family_id,
        "primary_eval_judge_family_id": spec.primary_eval_judge_family_id,
        "records_artifact": "candidates.jsonl",
        "records_sha256": sha256_hex(record_bytes),
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": sha256_hex(public_sample_bytes),
        "public_sample_count": len(public_sample),
        "record_count": len(records),
        "unique_judge_visible_payload_count": len(payload_groups),
        "exact_duplicate_record_count": len(records) - len(payload_groups),
        "dispatch_eligible_count": dispatch_count,
        "required_future_judge_call_count": 4 * dispatch_count,
        "codex_diagnostic_status": (
            "complete" if spec.codex_audit_manifest is not None else "absent"
        ),
        "codex_diagnostic_record_count": len(judgments_by_id),
        "codex_same_claim_counts": codex_counts,
        "dispatch_status_counts": status_counts,
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    summary_bytes = _render_summary(manifest_values)
    manifest_values["summary_artifact"] = "summary.md"
    manifest_values["summary_sha256"] = sha256_hex(summary_bytes)
    inventory_id = make_id(
        "lf022_supervision_inventory",
        {
            key: value
            for key, value in manifest_values.items()
            if key
            not in {
                "records_artifact",
                "public_sample_artifact",
                "summary_artifact",
                "spec_sha256",
            }
        },
    )
    manifest = LF022SupervisionCandidateManifest.model_validate(
        {**manifest_values, "inventory_id": inventory_id}
    )
    return tuple(records), manifest


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LF022SupervisionCandidateError(f"output cannot be a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022SupervisionCandidateError(f"immutable output conflict: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
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
                raise LF022SupervisionCandidateError(
                    f"concurrent immutable output conflict: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def write_lf022_supervision_candidate_inventory(
    *,
    output_dir: Path,
    records: tuple[LF022SupervisionCandidateRecord, ...],
    manifest: LF022SupervisionCandidateManifest,
) -> tuple[Path, Path, Path, Path]:
    """Write canonical immutable JSONL and manifest artifacts."""

    output_dir = _lexical_no_symlink_components(
        output_dir,
        base=Path.cwd(),
        label="candidate inventory output directory",
        allow_missing_leaf=True,
    )
    if output_dir.exists() and not output_dir.is_dir():
        raise LF022SupervisionCandidateError(
            f"candidate inventory output is not a directory: {output_dir}"
        )
    if not output_dir.exists():
        output_dir.mkdir()
    records_path = output_dir / manifest.records_artifact
    sample_path = output_dir / manifest.public_sample_artifact
    summary_path = output_dir / manifest.summary_artifact
    manifest_path = output_dir / "manifest.json"
    record_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records
    )
    if sha256_hex(record_bytes) != manifest.records_sha256:
        raise LF022SupervisionCandidateError("records differ from manifest hash")
    sample = tuple(
        item for item in records if item.dispatch_status == "ready_for_two_family_judging"
    )[:10]
    sample_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in sample
    )
    if (
        len(sample) != manifest.public_sample_count
        or sha256_hex(sample_bytes) != manifest.public_sample_sha256
    ):
        raise LF022SupervisionCandidateError("public sample differs from manifest binding")
    summary_bytes = _render_summary(manifest.model_dump(mode="json"))
    if sha256_hex(summary_bytes) != manifest.summary_sha256:
        raise LF022SupervisionCandidateError("summary differs from manifest binding")
    _write_immutable(records_path, record_bytes)
    _write_immutable(sample_path, sample_bytes)
    _write_immutable(summary_path, summary_bytes)
    _write_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    return records_path, sample_path, summary_path, manifest_path


__all__ = [
    "CandidateArtifactBinding",
    "LF022SupervisionCandidateError",
    "LF022SupervisionCandidateManifest",
    "LF022SupervisionCandidateRecord",
    "LF022SupervisionCandidateSpec",
    "PriorCodexDiagnostic",
    "build_lf022_supervision_candidate_inventory",
    "write_lf022_supervision_candidate_inventory",
]

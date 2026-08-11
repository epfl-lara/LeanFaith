"""Fail-closed LF-022 candidate inventory for later two-family judging.

This stage turns one *completed and replay-verified* public LF-022 Codex audit
into a compact dispatch inventory.  The prior Codex answer is retained only as
an audit diagnostic; it is never counted as either weak-supervision judge.

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
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditError,
    LF022CodexAuditManifest,
    verify_completed_lf022_codex_audit,
)
from leanfaith.generation.lf022_production import canonical_model_family
from leanfaith.generation.weak_supervision import (
    FamilySeparationMatrix,
    PublicLeanJudgePair,
    validate_family_separation,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_SUPERVISION_CANDIDATE_VERSION: Literal["lf022_supervision_candidate_inventory_v2"] = (
    "lf022_supervision_candidate_inventory_v2"
)
_REQUIRED_CELLS = ("judge_A:AB", "judge_A:BA", "judge_B:AB", "judge_B:BA")


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


class LF022SupervisionCandidateSpec(StrictModel):
    """Frozen request for one candidate inventory."""

    schema_version: Literal[2] = 2
    method_version: Literal["lf022_supervision_candidate_inventory_v2"] = (
        LF022_SUPERVISION_CANDIDATE_VERSION
    )
    collection_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    proposer_family_id: str = Field(min_length=1)
    proposer_model: str = Field(min_length=1)
    judge_a_family_id: str = Field(min_length=1)
    judge_b_family_id: str = Field(min_length=1)
    primary_eval_judge_family_id: str = Field(min_length=1)
    checks: CandidateArtifactBinding
    codex_audit_manifest: CandidateArtifactBinding
    public_sources_only: Literal[True] = True
    codex_is_diagnostic_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _family_separation(self) -> Self:
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
    diagnostic_only: Literal[True] = True
    weak_judge_vote: Literal[False] = False


CandidateDispatchStatus = Literal[
    "ready_for_two_family_judging",
    "exact_duplicate_not_dispatched",
]


class LF022SupervisionCandidateRecord(StrictModel):
    """One public pair awaiting the complete two-family swapped-order audit."""

    schema_version: Literal[2] = 2
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
    canonical_dispatch_audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    required_judgment_cells: tuple[str, ...]
    prior_codex_diagnostic: PriorCodexDiagnostic
    promotion_blockers: tuple[str, ...] = Field(min_length=1)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.pair_id != self.pair.pair_id:
            raise ValueError("pair_id differs from embedded public pair")
        if self.pair_admission_sha256 != self.pair.admission_sha256:
            raise ValueError("pair admission hash differs from embedded public pair")
        if self.judge_visible_payload_sha256 != _judge_visible_payload_hash(self.pair):
            raise ValueError("judge-visible payload hash differs from embedded pair")
        if self.dispatch_status == "ready_for_two_family_judging":
            if self.prior_codex_diagnostic.audit_item_id != self.canonical_dispatch_audit_item_id:
                raise ValueError("ready record must be the canonical dispatch audit item")
            if self.pair_id != self.canonical_dispatch_pair_id:
                raise ValueError("ready record must bind the canonical dispatch pair")
            if self.required_judgment_cells != _REQUIRED_CELLS:
                raise ValueError("ready record requires all four judge/orientation cells")
        else:
            if self.prior_codex_diagnostic.audit_item_id == self.canonical_dispatch_audit_item_id:
                raise ValueError("duplicate record cannot be the canonical dispatch audit item")
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

    schema_version: Literal[2] = 2
    method_version: Literal["lf022_supervision_candidate_inventory_v2"] = (
        LF022_SUPERVISION_CANDIDATE_VERSION
    )
    inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    collection_id: str
    spec_sha256: str = Field(pattern=HEX64_PATTERN)
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    codex_audit_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    logical_input_binding_sha256: str = Field(pattern=HEX64_PATTERN)
    codex_response_artifact_set_sha256: str = Field(pattern=HEX64_PATTERN)
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
        if sum(self.codex_same_claim_counts.values()) != self.record_count:
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

    lines = [
        "# LF-022 provisional supervision candidate inventory",
        "",
        "This report contains public, Lean-valid source/candidate pairs from one exact",
        "replay-verified Codex audit. Codex remains a diagnostic-only audit and contributes",
        "zero weak-supervision votes. No semantic label, silver record, training record,",
        "evaluation record, or gate credit is created here.",
        "",
        "## Exact logical input bindings",
        "",
        f"- Collection: `{values['collection_id']}`",
        f"- Lean checks SHA-256: `{values['checks_sha256']}`",
        f"- Codex audit manifest SHA-256: `{values['codex_audit_manifest_sha256']}`",
        f"- Verified response set SHA-256: `{values['codex_response_artifact_set_sha256']}`",
        f"- Logical input binding SHA-256: `{values['logical_input_binding_sha256']}`",
        "",
        "## Candidate counts",
        "",
        f"- Replay-verified records: {values['record_count']}",
        f"- Exact unique judge-visible payloads: {values['unique_judge_visible_payload_count']}",
        "- Exact duplicate records retained but not dispatched: "
        f"{values['exact_duplicate_record_count']}",
        f"- Ready for later two-family judging: {values['dispatch_eligible_count']}",
        f"- Future judge calls required: {values['required_future_judge_call_count']}",
        f"- Dispatch statuses: {counts(status_counts)}",
        f"- Prior Codex diagnostic verdicts: {counts(codex_counts)}",
        "",
        "Each dispatch-eligible pair still requires four independent blinded calls:",
        "`judge_A:AB`, `judge_A:BA`, `judge_B:AB`, and `judge_B:BA`.",
        "The configured weak judges are distinct from the proposer and from the reserved",
        "primary evaluation judge. Human-pilot and promotion audits remain blockers.",
        "",
        "## Inspectable sample",
        "",
        f"`public_sample.jsonl` contains {values['public_sample_count']} public-only records",
        "with both Lean statements and the audit-only Codex diagnostic. The complete",
        "inventory is in `candidates.jsonl`.",
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


def _record_values(
    *,
    spec: LF022SupervisionCandidateSpec,
    item: object,
    judgment: object,
    canonical_pair_id: str,
    canonical_audit_item_id: str,
    codex_model: str,
    codex_reasoning_effort: str,
) -> dict[str, object]:
    # Local imports keep the public verification dataclasses as the source of truth.
    from leanfaith.generation.lf022_codex_audit import (
        LF022CodexAuditInput,
        LF022VerifiedCodexAuditJudgment,
    )

    if not isinstance(item, LF022CodexAuditInput) or not isinstance(
        judgment, LF022VerifiedCodexAuditJudgment
    ):
        raise TypeError("candidate materialization requires verified LF-022 audit records")
    pair = item.pair
    response = judgment.response
    visible_payload_hash = _judge_visible_payload_hash(pair)
    ready = judgment.audit_item_id == canonical_audit_item_id
    blockers = {
        "human_pilot_not_bound",
        "promotion_audit_missing",
        "silver_not_promoted",
        "swapped_order_judgments_missing",
        "two_family_judgments_missing",
    }
    if not ready:
        blockers.add("exact_duplicate_not_dispatched")
    return {
        "schema_version": 2,
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
        "canonical_dispatch_audit_item_id": canonical_audit_item_id,
        "required_judgment_cells": _REQUIRED_CELLS if ready else (),
        "prior_codex_diagnostic": PriorCodexDiagnostic(
            audit_item_id=judgment.audit_item_id,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
            same_claim_answer=response.same_claim_answer,
            relation=response.relation.value if response.relation is not None else None,
            confidence=response.confidence,
            needs_expert_review=response.needs_expert_review,
            parsed_response_sha256=judgment.parsed_response_sha256,
        ).model_dump(mode="json"),
        "promotion_blockers": tuple(sorted(blockers)),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }


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
    checks_path = _resolve_bound_path(spec.checks, repo_root=repo_root)
    audit_manifest_path = _resolve_bound_path(spec.codex_audit_manifest, repo_root=repo_root)
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
    if len(verified.items) != len(verified.judgments):
        raise LF022SupervisionCandidateError("completed Codex audit lacks one judgment per item")

    items_by_id = {item.audit_item_id: item for item in verified.items}
    judgments_by_id = {item.audit_item_id: item for item in verified.judgments}
    if set(items_by_id) != set(judgments_by_id):
        raise LF022SupervisionCandidateError("Codex items and judgments do not match exactly")
    checks_by_id = {check.check_id: check for check in verified.checks}
    for judgment in verified.judgments:
        check = checks_by_id.get(judgment.lean_check_id)
        if check is None:
            raise LF022SupervisionCandidateError(
                f"verified judgment lacks Lean check {judgment.lean_check_id}"
            )
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
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id=judgment.proposer_family_id,
            judgment_variant_id=judgment.variant_id,
            spec=spec,
        )

    payload_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    payload_hash_by_item: dict[str, str] = {}
    for audit_item_id, item in items_by_id.items():
        payload_hash = _judge_visible_payload_hash(item.pair)
        payload_hash_by_item[audit_item_id] = payload_hash
        payload_groups[payload_hash].append((audit_item_id, item.pair.pair_id))
    canonical_by_hash = {key: min(values) for key, values in payload_groups.items()}

    records: list[LF022SupervisionCandidateRecord] = []
    for audit_item_id in sorted(items_by_id):
        item = items_by_id[audit_item_id]
        judgment = judgments_by_id[audit_item_id]
        canonical_audit_item_id, canonical_pair_id = canonical_by_hash[
            payload_hash_by_item[audit_item_id]
        ]
        record_values = _record_values(
            spec=spec,
            item=item,
            judgment=judgment,
            canonical_pair_id=canonical_pair_id,
            canonical_audit_item_id=canonical_audit_item_id,
            codex_model=verified.manifest.model,
            codex_reasoning_effort=verified.manifest.reasoning_effort,
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
        sorted(Counter(item.prior_codex_diagnostic.same_claim_answer for item in records).items())
    )
    dispatch_count = status_counts.get("ready_for_two_family_judging", 0)
    manifest_values: dict[str, object] = {
        "schema_version": 2,
        "method_version": LF022_SUPERVISION_CANDIDATE_VERSION,
        "collection_id": spec.collection_id,
        "spec_sha256": expected_spec_sha256,
        "checks_sha256": spec.checks.sha256,
        "codex_audit_manifest_sha256": spec.codex_audit_manifest.sha256,
        "logical_input_binding_sha256": hash_canonical(
            {
                "schema": "lf022_supervision_logical_input_v2",
                "collection_id": spec.collection_id,
                "checks_sha256": spec.checks.sha256,
                "codex_audit_manifest_sha256": spec.codex_audit_manifest.sha256,
                "proposer_family_id": spec.proposer_family_id,
                "proposer_model": spec.proposer_model,
                "judge_a_family_id": spec.judge_a_family_id,
                "judge_b_family_id": spec.judge_b_family_id,
                "primary_eval_judge_family_id": spec.primary_eval_judge_family_id,
            }
        ),
        "codex_response_artifact_set_sha256": verified.response_artifact_set_sha256,
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

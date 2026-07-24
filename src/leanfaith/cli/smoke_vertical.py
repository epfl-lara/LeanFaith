"""LF-019 integrated smoke vertical slice and Gate-4G mechanical audit.

The slice intentionally exercises the real Lean-backed record boundaries while
remaining unusable as scientific data.  It runs every active deterministic
family, resolves only the mechanically certified P01 alpha pair under the
explicit smoke exemption, builds a component-atomic smoke split, invokes a
tiny nonproduction classifier, and proves that centralized artifact guards
reject every downstream use.
"""

from __future__ import annotations

import datetime
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.cli.doctor import run_doctor
from leanfaith.cli.negative_pre_scale import NegativePreScaleConfig
from leanfaith.cli.transformations import _validate_authorization
from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets import load_active_benchmark_registry
from leanfaith.datasets.denylist import ActiveBenchmarkRegistry
from leanfaith.lean.api_probe import run_api_probe
from leanfaith.lean.extraction import (
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.project_registry import (
    ContextPayload,
    EnvironmentLock,
    build_context_record,
    check_project_toolchain,
    load_environment_lock,
    load_project_registry,
)
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.release.guard import (
    ArtifactUse,
    ReleaseGuardError,
    assess_artifact,
    require_artifact_allowed,
)
from leanfaith.representations.pipeline import (
    TheoremForRepresentation,
    build_representations,
)
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.schemas import (
    ArtifactClass,
    AuditValue,
    ContextRecord,
    DataStage,
    Decision,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceTargetKind,
    FaithfulnessLevels,
    OutputManifest,
    PairRecord,
    Polarity,
    QualityTier,
    RelationLabel,
    RepresentationRecord,
    ResolutionOutcome,
    ResolvedLabel,
    RunManifest,
    SemanticLabelTargetKind,
    TheoremRecord,
    TransformationAttempt,
    TransformationAudit,
    ValidationStatus,
    VariantDraft,
    VariantRecord,
    ViewStatus,
    collect_code_state,
    make_id,
    new_run_id,
    run_manifest_path,
    write_manifest,
)
from leanfaith.schemas.manifest import CodeState
from leanfaith.transforms import (
    build_negative_rule_runtime,
    build_positive_rule_runtime,
    load_transformation_registry,
)
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.pair_runtime import (
    audit_pair_transformation,
    execute_pair_transformation,
)
from leanfaith.transforms.positive_fixtures import (
    PositiveFixtureCase,
    load_lf019_positive_fixture_profile,
)
from leanfaith.transforms.protocol import build_deterministic_variant_record
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    TransformationRegistry,
    TransformationRejected,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_CONFIG = Path("configs/transformations/lf019_smoke_v1.yaml")
_DEFAULT_OUTPUT = Path("data/generated/deterministic/lf019_smoke_v1")
_DEFAULT_REPORT_DIR = Path("reports/transformation_audits/lf019_smoke")
_POSITIVE_RULES = ("p01_alpha", "p02_binders", "p04_notation_lite")
_NEGATIVE_RULES = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
)
_ACTIVE_RULES = tuple(sorted((*_POSITIVE_RULES, *_NEGATIVE_RULES)))
_REQUIRED_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "semantic_atoms",
    "operator_tree",
)
_GATE_4G_CHECKS = (
    "current_registry_snapshot_frozen",
    "active_family_inventory_exact",
    "all_eight_active_families_executed",
    "disabled_family_dispatch_rejected",
    "ten_or_more_fixture_sources_extracted",
    "all_candidates_reelaborated",
    "complete_attempt_draft_audit_variant_pair_lineage",
    "all_candidates_have_validation_and_audit_status",
    "n10_dual_ancestry_persisted",
    "transformation_evidence_linked",
    "p01_only_smoke_resolution",
    "non_p01_semantics_unresolved",
    "zero_intention_to_label_inference",
    "zero_gold_labels",
    "zero_promotions",
    "zero_protected_benchmark_overlap",
    "zero_connected_split_leakage",
    "batch_failure_isolation_passed",
    "deterministic_semantic_replay_passed",
    "smoke_release_guard_passed",
    "smoke_selection_guard_passed",
)


class SmokeStatement(StrictModel):
    """One source input used only by the LF-019 fixture pipeline."""

    case_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    source_name: Annotated[
        str,
        Field(pattern=r"^[A-Za-z_][A-Za-z0-9_']*$", strict=True),
    ]
    source_code: str = Field(min_length=1)

    @model_validator(mode="after")
    def _name_matches_code(self) -> SmokeStatement:
        if not self.source_code.startswith(f"theorem {self.source_name} "):
            raise ValueError("source_code must begin with its configured theorem name")
        return self


class LF019SmokeConfig(StrictModel):
    """Closed, versioned LF-019 integrated-run configuration."""

    schema_version: Literal[1] = 1
    smoke_profile_id: Literal["lf019_smoke_v1"] = "lf019_smoke_v1"
    smoke_profile_version: Literal["1.0.0"] = "1.0.0"
    artifact_class: Literal["smoke"] = "smoke"
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    positive_fixture_path: Literal["configs/transformations/lf019_positive_fixtures_v1.yaml"]
    negative_fixture_path: Literal["configs/transformations/lf018_pre_scale_v1.yaml"]
    record_timestamp_utc: str = Field(min_length=1)
    split_seed: int = Field(ge=0, strict=True)
    inventory_only_statements: tuple[SmokeStatement, ...] = Field(min_length=1)
    expected_failure_statements: tuple[SmokeStatement, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_inventory(self) -> LF019SmokeConfig:
        parsed = datetime.datetime.fromisoformat(self.record_timestamp_utc)
        if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
            raise ValueError("record_timestamp_utc must be timezone-aware UTC")
        cases = (*self.inventory_only_statements, *self.expected_failure_statements)
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("smoke statement case IDs must be unique")
        if len({case.source_name for case in cases}) != len(cases):
            raise ValueError("smoke statement source names must be unique")
        return self

    @property
    def record_timestamp(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.record_timestamp_utc)


class SmokeFailureRecord(StrictModel):
    """Explicit terminal failure; expected malformed input is never discarded."""

    schema_version: Literal[1] = 1
    case_id: str
    stage: str
    failure_type: str
    detail: str
    expected: bool = False
    rule_id: str | None = None
    request_id: str | None = None
    attempt_id: str | None = None


class SmokeCaseResult(StrictModel):
    """Cross-partition index for one successfully generated family case."""

    case_id: str
    rule_id: str
    source_theorem_ids: tuple[str, ...]
    source_representation_ids: tuple[str, ...]
    attempt_id: str
    draft_id: str
    candidate_theorem_id: str
    candidate_representation_id: str
    audit_id: str
    variant_id: str
    pair_id: str
    evidence_id: str
    resolved_label_id: str | None = None


class SmokeSplitAssignment(StrictModel):
    pair_id: str
    component_id: str = Field(pattern=_HEX64)
    split: Literal["train", "validation"]
    split_group_ids: tuple[str, ...] = Field(min_length=1)


class SmokeSplitManifest(StrictModel):
    """Smoke-only component assignment; not the general LF-025 splitter."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    split_version: Literal["lf019_connected_smoke_v1"] = "lf019_connected_smoke_v1"
    seed: int
    assignments: tuple[SmokeSplitAssignment, ...]
    component_count: int = Field(ge=2)
    train_component_count: int = Field(ge=1)
    validation_component_count: int = Field(ge=1)
    group_overlap_count: Literal[0] = 0


class SmokePlumbingMetrics(StrictModel):
    """Structural metrics only; no semantic-quality statistic is permitted."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    prediction_count: int = Field(ge=1)
    schema_valid_count: int = Field(ge=1)
    finite_score_count: int = Field(ge=1)
    labeled_training_count: int = Field(ge=1)
    component_count: int = Field(ge=2)
    zero_split_leakage: Literal[True] = True
    descriptive_only: Literal[True] = True

    @model_validator(mode="after")
    def _counts(self) -> SmokePlumbingMetrics:
        if not (self.prediction_count == self.schema_valid_count == self.finite_score_count):
            raise ValueError("every smoke prediction must be schema-valid with finite scores")
        return self


class SmokeArtifactCatalog(StrictModel):
    """Root eligibility catalog bound to every LF-019 partition."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    run_id: str
    artifact_paths: tuple[str, ...]
    artifact_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]

    @model_validator(mode="after")
    def _bound_paths(self) -> SmokeArtifactCatalog:
        if self.artifact_paths != tuple(sorted(set(self.artifact_paths))):
            raise ValueError("artifact_paths must be sorted and unique")
        if set(self.artifact_paths) != set(self.artifact_hashes):
            raise ValueError("catalog hashes must bind every artifact path exactly")
        return self


class LF019SmokeReport(StrictModel):
    """Machine-checkable smoke result and Gate-4G decision."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf019_smoke_vertical_audit"] = "lf019_smoke_vertical_audit"
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    mechanical_pass: bool
    clean_checkout_pass: bool
    lf019_accepted: bool
    gate_4g_closed: bool
    gate_4a_closed: Literal[False] = False
    gate_4b_closed: Literal[False] = False
    run_id: str
    registry_hash: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_input_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    context_ids: tuple[str, ...]
    configured_source_count: int = Field(ge=10)
    accepted_source_count: int = Field(ge=0)
    expected_failure_count: int = Field(ge=1)
    unexpected_failure_count: int = Field(ge=0)
    family_results: tuple[SmokeCaseResult, ...]
    generated_pair_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    smoke_label_count: int = Field(ge=0)
    gold_label_count: int = Field(ge=0)
    promoted_item_count: int = Field(ge=0)
    split_component_count: int = Field(ge=0)
    check_results: dict[str, bool]
    output_manifest_path: str
    output_manifest_sha256: str = Field(pattern=_HEX64)
    artifact_catalog_path: str
    artifact_catalog_sha256: str = Field(pattern=_HEX64)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _report_consistency(self) -> LF019SmokeReport:
        if set(self.check_results) != set(_GATE_4G_CHECKS):
            raise ValueError("check_results must contain exactly the Gate-4G smoke checks")
        count_contracts = (
            (
                "ten_or_more_fixture_sources_extracted",
                self.accepted_source_count >= 10,
            ),
            (
                "all_eight_active_families_executed",
                self.generated_pair_count == 8 and len(self.family_results) == 8,
            ),
            ("transformation_evidence_linked", self.evidence_count == 8),
            ("p01_only_smoke_resolution", self.smoke_label_count == 1),
            ("zero_gold_labels", self.gold_label_count == 0),
            ("zero_promotions", self.promoted_item_count == 0),
        )
        for check_name, observed in count_contracts:
            if self.check_results[check_name] != observed:
                raise ValueError(f"{check_name} disagrees with its persisted counts")
        expected_mechanical = self.unexpected_failure_count == 0 and all(
            self.check_results[name]
            for name in _GATE_4G_CHECKS
            if name != "deterministic_semantic_replay_passed"
        )
        # Replay is performed by the paired runner after two independent runs;
        # a single run records its own deterministic-semantic fingerprint.
        if self.mechanical_pass != expected_mechanical:
            raise ValueError("mechanical_pass does not match the non-replay smoke checks")
        expected_acceptance = (
            self.mechanical_pass
            and self.clean_checkout_pass
            and self.check_results["deterministic_semantic_replay_passed"]
        )
        if self.lf019_accepted != expected_acceptance:
            raise ValueError("lf019_accepted requires mechanics, clean checkout, and replay")
        if self.gate_4g_closed != self.lf019_accepted:
            raise ValueError("Gate 4G closes exactly when LF-019 acceptance closes")
        return self


@dataclass(frozen=True, slots=True)
class LF019SmokeArtifacts:
    output_dir: Path
    report_path: Path
    output_manifest_path: Path
    run_manifest_path: Path
    catalog_path: Path
    semantic_fingerprint: str
    mechanical_pass: bool
    gate_4g_closed: bool


class LF019SmokeError(RuntimeError):
    def __init__(self, detail: str, *, artifacts: LF019SmokeArtifacts) -> None:
        super().__init__(detail)
        self.artifacts = artifacts


def _write_jsonl(records: Sequence[StrictModel], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records)
    )
    return hash_file(path)


def _write_json(value: StrictModel, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value.model_dump(mode="json")) + b"\n")
    return hash_file(path)


def _fixture_project_revision(project_dir: Path) -> str:
    candidates = {
        project_dir / name
        for name in (
            "lean-toolchain",
            "lakefile.lean",
            "lakefile.toml",
            "lake-manifest.json",
            "LeanFaithFixtures.lean",
        )
    }
    candidates.update((project_dir / "LeanFaithFixtures").rglob("*.lean"))
    entries = tuple(
        (str(path.relative_to(project_dir)), hash_file(path))
        for path in sorted(candidates)
        if path.is_file()
    )
    if not entries:
        raise ValueError("fixture project has no context-defining files")
    return f"workspace:{hash_canonical(entries)}"


def _context_for_fixture(
    paths: RepoPaths,
    *,
    project_dir: Path,
    header_text: str,
) -> tuple[ContextRecord, EnvironmentLock]:
    projects = load_project_registry(paths)
    spec = projects.get("fixtures")
    if spec is None:
        raise ValueError("project registry has no fixtures entry")
    expected_dir = spec.local_directory(paths)
    if expected_dir is None or expected_dir.resolve() != project_dir:
        raise ValueError("LF-019 project_dir differs from the fixtures registry")
    lock = load_environment_lock(paths)
    lean_version = check_project_toolchain(spec, project_dir, lock.toolchain_lock)
    imports = tuple(
        module
        for line in header_text.splitlines()
        if line.strip().startswith("import ")
        for module in line.strip().removeprefix("import ").split()
    )
    payload = ContextPayload(
        environment_schema_version=lock.environment_schema_version,
        lean_version=str(lean_version),
        lean_interact_version=lock.lean_interact.version,
        repl_revision=(
            f"{lock.lean_interact.repl_fork}@lean-interact-{lock.lean_interact.version}"
        ),
        project_uri=spec.uri,
        project_revision=_fixture_project_revision(project_dir),
        imports=imports,
        header_text=header_text,
    )
    return (
        build_context_record(
            payload,
            project_kind=spec.kind.value,
            project_registry_key=spec.registry_key,
        ),
        lock,
    )


@dataclass(frozen=True, slots=True)
class _ExtractedSource:
    theorem: TheoremRecord
    representation: RepresentationRecord


def _validation_status(status: LeanStatus) -> ValidationStatus:
    if status == LeanStatus.VALID:
        return ValidationStatus.ELABORATES
    if status == LeanStatus.VALID_WITH_SORRY:
        return ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    if status == LeanStatus.TIMEOUT:
        return ValidationStatus.TIMEOUT
    if status in {LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.SETUP_ERROR}:
        return ValidationStatus.INFRASTRUCTURE_ERROR
    return ValidationStatus.INVALID


def _extract_fixture_source(
    backend: LeanInteractBackend,
    *,
    case_id: str,
    source_name: str,
    source_code: str,
    source_revision: str,
    context: ContextRecord,
    imports: str,
    created_at: datetime.datetime,
) -> _ExtractedSource:
    full_source = "\n".join(part for part in (imports.strip(), source_code) if part)
    request = LeanRequest(
        request_id=f"lf019-extract-{case_id}",
        context_id=context.context_id,
        code=full_source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={"artifact_class": "smoke", "case_id": case_id},
    )
    result = backend.run(request)
    if result.status not in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
        raise RuntimeError(f"source did not elaborate: {result.status.value}")
    extraction = extract_from_declarations(
        SourceIdentity(
            source="lf019_smoke_fixture",
            source_revision=source_revision,
            source_record=case_id,
            context_id=context.context_id,
            extraction_route="inline_fixture",
        ),
        full_source,
        list(result.declarations),
        created_at=created_at,
        elaboration_status=_validation_status(result.status),
        lean_result_id=result.request_hash,
    )
    if extraction.failures:
        details = "; ".join(
            f"{failure.code.value}:{failure.detail}" for failure in extraction.failures
        )
        raise RuntimeError(f"source extraction failed: {details}")
    if len(extraction.accepted) != 1:
        raise RuntimeError(
            f"source extraction expected one theorem, got {len(extraction.accepted)}"
        )
    extracted = extraction.accepted[0]
    if extracted.theorem.declaration_full_name != source_name:
        raise RuntimeError(
            f"extracted name {extracted.theorem.declaration_full_name!r} != {source_name!r}"
        )
    inline_source = reconstruct_for_revalidation(
        full_source,
        extracted.declaration,
        extracted.proof_stripped,
    )
    theorem = TheoremRecord.model_validate(
        {
            **extracted.theorem.model_dump(mode="python"),
            "inline_elaboration_source": inline_source,
            "metadata": {
                **extracted.theorem.metadata,
                "artifact_class": "smoke",
                "smoke_case_id": case_id,
                "transform_source_eligible": True,
            },
        }
    )
    representation = _build_representation(
        backend,
        theorem,
        imports=imports,
        created_at=created_at,
        source_signature=str((extracted.declaration.get("signature") or {}).get("pp", "")).strip()
        or None,
    )
    return _ExtractedSource(theorem=theorem, representation=representation)


def _build_representation(
    backend: LeanInteractBackend,
    theorem: TheoremRecord,
    *,
    imports: str,
    created_at: datetime.datetime,
    source_signature: str | None = None,
) -> RepresentationRecord:
    full_name = theorem.declaration_full_name
    if full_name is None:
        raise ValueError("inline fixture theorem has no full name")
    (record,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=theorem.theorem_id,
                full_name=full_name,
                proof_stripped=theorem.proof_stripped_declaration,
                context_id=theorem.context_id,
                source_signature=source_signature,
                inline_declaration=True,
                inline_source=theorem.inline_elaboration_source,
            )
        ],
        imports=imports,
        created_at=created_at,
    )
    failed = tuple(view for view in _REQUIRED_VIEWS if record.view_status[view] != ViewStatus.OK)
    if failed:
        raise RuntimeError(f"required representation views failed: {','.join(failed)}")
    if record.alpha_identity_fingerprint is None:
        raise RuntimeError("alpha identity fingerprint missing")
    return record


def _candidate_status(
    backend: LeanInteractBackend,
    *,
    case_id: str,
    context_id: str,
    imports: str,
    code: str,
) -> tuple[ValidationStatus, tuple[str, ...]]:
    source = "\n".join(part for part in (imports.strip(), code) if part)
    result = backend.run(
        LeanRequest(
            request_id=f"lf019-candidate-{case_id}",
            context_id=context_id,
            code=source,
            declarations=True,
            allow_sorry=True,
            timeout_seconds=300.0,
            metadata={"artifact_class": "smoke", "case_id": case_id},
        )
    )
    diagnostics = tuple(str(message.get("data", "")) for message in result.messages)
    status = _validation_status(result.status)
    if status not in {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }:
        raise RuntimeError(f"candidate did not elaborate: {result.status.value}")
    return status, diagnostics


def _make_audit_evidence(
    *,
    pair: PairRecord,
    audit: TransformationAudit,
    config_hash: str,
    created_at: datetime.datetime,
) -> EvidenceRecord:
    evidence_id = make_id(
        "ev",
        {
            "schema": "lf019_transform_audit_evidence_v1",
            "pair_id": pair.pair_id,
            "audit_id": audit.audit_id,
        },
    )
    return EvidenceRecord(
        evidence_id=evidence_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair.pair_id,
        kind=EvidenceKind.TRANSFORMATION_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=AuditValue(
            checks={
                "applicable": audit.applicability.applicable,
                "candidate_linked": audit.candidate_theorem_id is not None,
                "candidate_representation_linked": (audit.candidate_representation_id is not None),
                "atom_mapping_ok": audit.atom_mapping_ok,
                "structural_diff_ok": audit.structural_diff_ok,
                "inverse_or_roundtrip_ok": audit.inverse_or_roundtrip_ok,
            },
            violation_codes=audit.violation_codes,
        ),
        method_version="lf019_transform_audit_evidence_v1",
        config_hash=config_hash,
        created_at=created_at,
        metadata={"artifact_class": "smoke", "audit_id": audit.audit_id},
    )


def _attach_evidence(pair: PairRecord, evidence: EvidenceRecord) -> PairRecord:
    return PairRecord.model_validate(
        {
            **pair.model_dump(mode="python"),
            "evidence_ids": tuple(sorted((*pair.evidence_ids, evidence.evidence_id))),
        }
    )


def _make_p01_smoke_label(
    pair: PairRecord,
    evidence: EvidenceRecord,
) -> tuple[PairRecord, ResolvedLabel]:
    label_id = make_id(
        "lbl",
        {
            "schema": "lf019_smoke_alpha_label_v1",
            "pair_id": pair.pair_id,
            "evidence_id": evidence.evidence_id,
        },
    )
    linked_pair = PairRecord.model_validate(
        {
            **pair.model_dump(mode="python"),
            "resolved_label_id": label_id,
        }
    )
    label = ResolvedLabel(
        label_id=label_id,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=pair.pair_id,
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
        faithfulness_levels=FaithfulnessLevels(
            F0_representation_equivalent=True,
            F1_same_claim=True,
            F2_truth_equivalent=None,
        ),
        quality_tier=QualityTier.PROVISIONAL,
        resolution_method="smoke_alpha_certificate",
        evidence_ids_used=(evidence.evidence_id,),
        requires_adjudication=False,
        train_eligibility=True,
        eval_eligibility=False,
        policy_version="lf019_smoke_alpha_v1",
        decision=Decision.REVIEW,
        relation_provenance=("p01_alpha",),
        adjudication_notes="Smoke plumbing exemption only; never scientific evidence.",
    )
    return linked_pair, label


def _component_assignments(
    pairs: Sequence[PairRecord],
    *,
    seed: int,
) -> SmokeSplitManifest:
    """Assign connected split-group components atomically without LF-025."""

    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for pair in pairs:
        first, *rest = pair.split_group_ids
        find(first)
        for group in rest:
            union(first, group)

    component_groups: dict[str, set[str]] = {}
    for group in sorted(parent):
        component_groups.setdefault(find(group), set()).add(group)
    component_ids = {
        root: hash_canonical(
            {
                "schema": "lf019_connected_component_v1",
                "groups": tuple(sorted(groups)),
                "seed": seed,
            }
        )
        for root, groups in component_groups.items()
    }
    ordered_components = sorted(component_ids.items(), key=lambda item: item[1])
    if len(ordered_components) < 2:
        raise RuntimeError("smoke split requires at least two connected components")
    split_for_root: dict[str, Literal["train", "validation"]] = {
        root: ("validation" if index % 4 == 0 else "train")
        for index, (root, _) in enumerate(ordered_components)
    }
    if "train" not in split_for_root.values():
        split_for_root[ordered_components[-1][0]] = "train"
    if "validation" not in split_for_root.values():
        split_for_root[ordered_components[0][0]] = "validation"

    assignments: list[SmokeSplitAssignment] = []
    split_groups: dict[str, set[str]] = {"train": set(), "validation": set()}
    for pair in sorted(pairs, key=lambda record: record.pair_id):
        roots = {find(group) for group in pair.split_group_ids}
        if len(roots) != 1:
            raise AssertionError("one pair spans multiple connected roots after union")
        root = roots.pop()
        split = split_for_root[root]
        split_groups[split].update(pair.split_group_ids)
        assignments.append(
            SmokeSplitAssignment(
                pair_id=pair.pair_id,
                component_id=component_ids[root],
                split=split,
                split_group_ids=pair.split_group_ids,
            )
        )
    overlap = split_groups["train"] & split_groups["validation"]
    if overlap:
        raise RuntimeError(f"smoke split leaked groups: {sorted(overlap)}")
    train_components = {
        assignment.component_id for assignment in assignments if assignment.split == "train"
    }
    validation_components = {
        assignment.component_id for assignment in assignments if assignment.split == "validation"
    }
    return SmokeSplitManifest(
        seed=seed,
        assignments=tuple(assignments),
        component_count=len(component_ids),
        train_component_count=len(train_components),
        validation_component_count=len(validation_components),
    )


def _protected_overlap(
    benchmark: ActiveBenchmarkRegistry,
    theorems: Sequence[TheoremRecord],
    representations: Sequence[RepresentationRecord],
) -> tuple[str, ...]:
    index = benchmark.index
    overlaps: list[str] = []
    for theorem in theorems:
        if index.contains_lean(theorem.proof_stripped_declaration):
            overlaps.append(f"lean:{theorem.theorem_id}")
    for representation in representations:
        candidates = (
            (
                signature_near_dup_hash(representation.headless)
                if representation.headless is not None
                else None
            ),
            (
                signature_near_dup_hash(representation.signature_pp)
                if representation.signature_pp is not None
                else None
            ),
            (
                signature_near_dup_hash(representation.signature_explicit)
                if representation.signature_explicit is not None
                else None
            ),
            representation.alpha_identity_fingerprint,
        )
        if any(
            candidate is not None and index.contains_representation(candidate)
            for candidate in candidates
        ):
            overlaps.append(f"representation:{representation.representation_id}")
    return tuple(sorted(overlaps))


def _disabled_dispatch_rejected(
    loaded_registry: LoadedTransformationRegistry,
    source: TheoremRecord,
    representation: RepresentationRecord,
) -> bool:
    runtime = TransformationRegistry(loaded_registry)
    try:
        runtime.execute("p00_cosmetic", source, representation, 0)
    except TransformationRejected:
        return True
    return False


def _semantic_fingerprint(
    *,
    contexts: Sequence[ContextRecord],
    partition_records: Mapping[str, Sequence[StrictModel]],
    split_manifest: SmokeSplitManifest,
) -> str:
    """Hash deterministic semantic records; exclude run IDs and timestamps."""

    return hash_canonical(
        {
            "contexts": [
                {
                    "context_id": context.context_id,
                    "fingerprint": context.context_fingerprint,
                }
                for context in sorted(contexts, key=lambda value: value.context_id)
            ],
            "partitions": {
                name: [
                    record.model_dump(mode="json", exclude={"created_at"})
                    for record in sorted(
                        records,
                        key=lambda value: canonical_json_bytes(value.model_dump(mode="json")),
                    )
                ]
                for name, records in sorted(partition_records.items())
            },
            "split": split_manifest.model_dump(mode="json"),
        }
    )


def _registry_validation_is_current(paths: RepoPaths, registry_hash: str) -> bool:
    reports = (
        paths.reports / "transformation_audits" / "lf016_registry_validation.json",
        paths.reports / "transformation_audits" / "lf017_positive_validation.json",
        paths.reports / "transformation_audits" / "lf018_negative_validation.json",
    )
    snapshot = (
        paths.reports / "transformation_audits" / "registry_snapshots" / f"{registry_hash}.json"
    )
    if not snapshot.is_file() or any(not path.is_file() for path in reports):
        return False
    try:
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in reports]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return all(
        payload.get("registry_hash") == registry_hash and payload.get("mechanical_pass") is True
        for payload in payloads
    )


def _pair_features(
    pair: PairRecord,
    *,
    representation_by_theorem: Mapping[str, RepresentationRecord],
) -> dict[str, float]:
    left = representation_by_theorem[pair.theorem_a_id]
    right = representation_by_theorem[pair.theorem_b_id]
    left_atoms = set(left.semantic_atoms or ())
    right_atoms = set(right.semantic_atoms or ())
    union = left_atoms | right_atoms
    atom_jaccard = len(left_atoms & right_atoms) / len(union) if union else 1.0
    left_length = len(left.signature_explicit or "")
    right_length = len(right.signature_explicit or "")
    length_ratio = min(left_length, right_length) / max(left_length, right_length, 1)
    return {
        "alpha_identity_equal": float(
            left.alpha_identity_fingerprint == right.alpha_identity_fingerprint
        ),
        "semantic_atom_jaccard": float(atom_jaccard),
        "signature_length_ratio": float(length_ratio),
    }


def _guard_rejects_bound_smoke_catalog(
    *,
    paths: RepoPaths,
    catalog: SmokeArtifactCatalog,
    use: ArtifactUse,
) -> bool:
    """Verify catalog bytes first, then require the centralized guard to reject."""

    for relative in catalog.artifact_paths:
        path = paths.root / relative
        if not path.is_file() or hash_file(path) != catalog.artifact_hashes[relative]:
            return False
        if path.name == "manifest.json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            if payload.get("artifact_class") != ArtifactClass.SMOKE.value:
                return False
    decision = assess_artifact(catalog, use=use)
    if decision.allowed or "smoke_artifact_forbidden" not in decision.reason_codes:
        return False
    try:
        require_artifact_allowed(catalog, use=use)
    except ReleaseGuardError:
        return True
    return False


def _output_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "contexts": output_dir / "contexts.jsonl",
        "source_theorems": output_dir / "source_theorems.jsonl",
        "source_representations": output_dir / "source_representations.jsonl",
        "attempts": output_dir / "attempts.jsonl",
        "drafts": output_dir / "drafts.jsonl",
        "candidate_theorems": output_dir / "candidate_theorems.jsonl",
        "candidate_representations": output_dir / "candidate_representations.jsonl",
        "audits": output_dir / "audits.jsonl",
        "variants": output_dir / "variants.jsonl",
        "pairs": output_dir / "pairs.jsonl",
        "failures": output_dir / "failures.jsonl",
    }


def _record_expected_failure(
    backend: LeanInteractBackend,
    *,
    statement: SmokeStatement,
    context: ContextRecord,
    imports: str,
) -> SmokeFailureRecord:
    full_source = "\n".join(part for part in (imports.strip(), statement.source_code) if part)
    request_id = f"lf019-expected-failure-{statement.case_id}"
    result = backend.run(
        LeanRequest(
            request_id=request_id,
            context_id=context.context_id,
            code=full_source,
            declarations=True,
            allow_sorry=True,
            timeout_seconds=300.0,
            metadata={"artifact_class": "smoke", "expected_failure": "true"},
        )
    )
    if result.status in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
        return SmokeFailureRecord(
            case_id=statement.case_id,
            stage="expected_failure_contract",
            failure_type="UnexpectedSuccess",
            detail="malformed fixture unexpectedly elaborated",
            expected=False,
            request_id=request_id,
        )
    return SmokeFailureRecord(
        case_id=statement.case_id,
        stage="source_elaboration",
        failure_type=result.status.value,
        detail="deliberately malformed fixture rejected without affecting siblings",
        expected=True,
        request_id=request_id,
    )


def _partition_manifest(
    *,
    artifact_class: ArtifactClass,
    run_id: str,
    source: str,
    source_revision: str,
    config_hash: str,
    row_count: int,
    file_checksums: Mapping[str, str],
    input_manifest_hashes: Mapping[str, str],
    input_partition_checksums: Mapping[str, str],
    output_partition_checksums: Mapping[str, str],
    failure_partition_checksums: Mapping[str, str],
    environment_hash: str,
    context_hash: str,
    code_state: CodeState,
    created_at: datetime.datetime,
    notes: str,
) -> OutputManifest:
    return OutputManifest(
        stage=(
            DataStage.GENERATED
            if source == "lf019_smoke_fixture"
            else (
                DataStage.EVIDENCE_COLLECTED
                if source == "lf019_smoke_evidence"
                else DataStage.LABELED
            )
        ),
        artifact_class=artifact_class,
        run_id=run_id,
        source=source,
        source_revision=source_revision,
        config_hash=config_hash,
        record_schema_version=1,
        row_count=row_count,
        file_checksums=dict(file_checksums),
        input_manifest_hashes=dict(input_manifest_hashes),
        input_partition_checksums=dict(input_partition_checksums),
        output_partition_checksums=dict(output_partition_checksums),
        failure_partition_checksums=dict(failure_partition_checksums),
        environment_hash=environment_hash,
        context_hash=context_hash,
        code_tree_hash=code_state.code_tree_hash,
        code=code_state,
        created_at=created_at,
        notes=notes,
    )


def run_lf019_smoke_once(
    *,
    paths: RepoPaths,
    output_dir: Path | None = None,
    report_path: Path | None = None,
    expected_semantic_fingerprint: str | None = None,
    code_bundle_path: Path | None = None,
) -> LF019SmokeArtifacts:
    """Run one immutable LF-019 slice.

    Callers perform two independent runs; the second receives the first
    semantic fingerprint and may close the replay check.
    """

    from leanfaith.models.smoke import (
        SmokeArtifactBoundary,
        SmokeMetrics,
        SmokeTrainingExample,
        TinySmokeTrainingConfig,
        predict_tiny_smoke_classifier,
        train_tiny_smoke_classifier,
    )

    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    config_path = (paths.root / _DEFAULT_CONFIG).resolve()
    loaded_config = load_config(config_path, LF019SmokeConfig)
    config = loaded_config.config
    resolved_output = (output_dir or paths.root / _DEFAULT_OUTPUT / run_id).resolve()
    resolved_report = (report_path or paths.root / _DEFAULT_REPORT_DIR / f"{run_id}.json").resolve()
    for candidate in (config_path, resolved_output, resolved_report):
        if not candidate.is_relative_to(paths.root.resolve()):
            raise ValueError("LF-019 paths must remain inside the repository")
    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise FileExistsError(f"LF-019 output directory is not empty: {resolved_output}")
    if resolved_report.exists():
        raise FileExistsError(f"LF-019 report already exists: {resolved_report}")

    positive_profile = load_lf019_positive_fixture_profile(
        paths.root,
        path=paths.root / config.positive_fixture_path,
    )
    negative_loaded = load_config(
        paths.root / config.negative_fixture_path,
        NegativePreScaleConfig,
    )
    negative_profile = negative_loaded.config
    loaded_registry = load_transformation_registry(paths.root)
    positive_runtime = build_positive_rule_runtime(loaded_registry)
    negative_runtime = build_negative_rule_runtime(loaded_registry)
    if positive_runtime.registered_rule_ids != _POSITIVE_RULES:
        raise RuntimeError("positive runtime inventory differs from LF-019")
    if negative_runtime.registered_rule_ids != _NEGATIVE_RULES[:4]:
        raise RuntimeError("unary negative runtime inventory differs from LF-019")
    if tuple(rule.rule_id for rule in negative_runtime.pair_rules) != (_NEGATIVE_RULES[4],):
        raise RuntimeError("pair negative runtime inventory differs from LF-019")

    authorization_path, authorization_hash, _, _ = _validate_authorization(paths.root)
    benchmark = load_active_benchmark_registry(
        repo_root=paths.root,
        authorization_path=authorization_path,
    )
    benchmark_hash = hash_file(benchmark.manifest_path)
    doctor = run_doctor(paths)
    api_probe = run_api_probe()
    if not doctor.ok or not api_probe.ok:
        raise RuntimeError("LF-019 doctor/API preflight failed")

    code_state = collect_code_state(paths.root)
    code_bundle_hash: str | None = None
    if code_bundle_path is not None:
        if code_state.code_tree_hash is None:
            raise RuntimeError("code state has no tree hash for bundle validation")
        code_bundle_hash = validate_code_bundle(code_bundle_path, code_state.code_tree_hash)

    positive_project_dir = (paths.root / positive_profile.config.project_dir).resolve()
    negative_project_dir = (paths.root / negative_profile.project_dir).resolve()
    if positive_project_dir != negative_project_dir:
        raise RuntimeError("positive and negative smoke fixtures use different projects")
    positive_context, environment_lock = _context_for_fixture(
        paths,
        project_dir=positive_project_dir,
        header_text=positive_profile.config.imports,
    )
    negative_context, negative_lock = _context_for_fixture(
        paths,
        project_dir=negative_project_dir,
        header_text=negative_profile.imports,
    )
    if environment_lock != negative_lock:
        raise RuntimeError("fixture contexts loaded different environment locks")
    contexts = tuple(
        sorted(
            {
                context.context_id: context for context in (positive_context, negative_context)
            }.values(),
            key=lambda value: value.context_id,
        )
    )
    context_bundle_hash = hash_canonical([context.model_dump(mode="json") for context in contexts])
    environment_lock_path = paths.configs / "environment.lock.yaml"
    environment_lock_hash = hash_file(environment_lock_path)

    resolved_output.mkdir(parents=True, exist_ok=True)
    raw_response_dir = resolved_output / "raw_lean_responses"
    backends = {
        positive_context.context_id: LeanInteractBackend(
            BackendSettings(
                project_dir=positive_project_dir,
                context_fingerprint=positive_context.context_fingerprint,
                environment_schema_version=positive_context.environment_schema_version,
                raw_response_dir=raw_response_dir / "positive",
            )
        ),
        negative_context.context_id: LeanInteractBackend(
            BackendSettings(
                project_dir=negative_project_dir,
                context_fingerprint=negative_context.context_fingerprint,
                environment_schema_version=negative_context.environment_schema_version,
                raw_response_dir=raw_response_dir / "negative",
            )
        ),
    }

    source_theorems: list[TheoremRecord] = []
    source_representations: list[RepresentationRecord] = []
    attempts: list[TransformationAttempt] = []
    drafts: list[VariantDraft] = []
    candidate_theorems: list[TheoremRecord] = []
    candidate_representations: list[RepresentationRecord] = []
    audits: list[TransformationAudit] = []
    variants: list[VariantRecord] = []
    pairs: list[PairRecord] = []
    evidence: list[EvidenceRecord] = []
    labels: list[ResolvedLabel] = []
    failures: list[SmokeFailureRecord] = []
    family_results: list[SmokeCaseResult] = []
    source_by_case_role: dict[tuple[str, str], _ExtractedSource] = {}

    def extract_source(
        *,
        case_id: str,
        role: str,
        name: str,
        code: str,
        context: ContextRecord,
        imports: str,
        source_revision: str,
    ) -> _ExtractedSource:
        keyed_case_id = f"{case_id}_{role}"
        extracted = _extract_fixture_source(
            backends[context.context_id],
            case_id=keyed_case_id,
            source_name=name,
            source_code=code,
            source_revision=source_revision,
            context=context,
            imports=imports,
            created_at=config.record_timestamp,
        )
        source_theorems.append(extracted.theorem)
        source_representations.append(extracted.representation)
        source_by_case_role[(case_id, role)] = extracted
        return extracted

    try:
        for positive_case_config in positive_profile.config.cases:
            try:
                extract_source(
                    case_id=positive_case_config.case_id,
                    role="primary",
                    name=positive_case_config.source_name,
                    code=positive_case_config.source_code,
                    context=positive_context,
                    imports=positive_profile.config.imports,
                    source_revision=positive_profile.config_hash,
                )
            except Exception as exc:
                failures.append(
                    SmokeFailureRecord(
                        case_id=positive_case_config.case_id,
                        stage="source_extraction",
                        failure_type=type(exc).__name__,
                        detail=str(exc) or type(exc).__name__,
                        rule_id=positive_case_config.rule_id,
                    )
                )
        for negative_case_config in negative_profile.cases:
            try:
                extract_source(
                    case_id=negative_case_config.case_id,
                    role="primary",
                    name=negative_case_config.primary_name,
                    code=negative_case_config.primary_code,
                    context=negative_context,
                    imports=negative_profile.imports,
                    source_revision=negative_loaded.config_hash,
                )
                if negative_case_config.rule_id == "n10_nearby_theorem":
                    assert (
                        negative_case_config.donor_name is not None
                        and negative_case_config.donor_code is not None
                    )
                    extract_source(
                        case_id=negative_case_config.case_id,
                        role="donor",
                        name=negative_case_config.donor_name,
                        code=negative_case_config.donor_code,
                        context=negative_context,
                        imports=negative_profile.imports,
                        source_revision=negative_loaded.config_hash,
                    )
            except Exception as exc:
                failures.append(
                    SmokeFailureRecord(
                        case_id=negative_case_config.case_id,
                        stage="source_extraction",
                        failure_type=type(exc).__name__,
                        detail=str(exc) or type(exc).__name__,
                        rule_id=negative_case_config.rule_id,
                    )
                )
        for statement in config.inventory_only_statements:
            try:
                extract_source(
                    case_id=statement.case_id,
                    role="inventory",
                    name=statement.source_name,
                    code=statement.source_code,
                    context=positive_context,
                    imports=positive_profile.config.imports,
                    source_revision=loaded_config.config_hash,
                )
            except Exception as exc:
                failures.append(
                    SmokeFailureRecord(
                        case_id=statement.case_id,
                        stage="source_extraction",
                        failure_type=type(exc).__name__,
                        detail=str(exc) or type(exc).__name__,
                    )
                )
        for statement in config.expected_failure_statements:
            failures.append(
                _record_expected_failure(
                    backends[positive_context.context_id],
                    statement=statement,
                    context=positive_context,
                    imports=positive_profile.config.imports,
                )
            )

        def materialize_case(
            *,
            case_id: str,
            rule_id: str,
            seed: int,
            sources: Sequence[_ExtractedSource],
            primary: _ExtractedSource,
            context: ContextRecord,
            imports: str,
            polarity: Polarity,
            positive_case: PositiveFixtureCase | None = None,
        ) -> None:
            attempt: TransformationAttempt | None = None
            stage = "execute_transformation"
            try:
                if rule_id == "n10_nearby_theorem":
                    if len(sources) != 2:
                        raise RuntimeError("N10 smoke case requires exactly two sources")
                    pair_rule = negative_runtime.pair_rules[0]
                    execution = execute_pair_transformation(
                        loaded_registry,
                        pair_rule,
                        sources[0].theorem,
                        sources[0].representation,
                        sources[1].theorem,
                        sources[1].representation,
                        seed,
                    )
                else:
                    runtime = (
                        positive_runtime.runtime
                        if polarity == Polarity.POSITIVE
                        else negative_runtime.runtime
                    )
                    execution = runtime.execute(
                        rule_id,
                        primary.theorem,
                        primary.representation,
                        seed,
                    )
                attempt = execution.attempt
                attempts.append(attempt)
                if attempt.terminal_outcome != "generated" or len(execution.drafts) != 1:
                    raise RuntimeError("smoke family case must generate exactly one draft")
                draft = execution.drafts[0]
                if positive_case is not None:
                    if positive_case.expected_candidate_fragment not in draft.candidate_code:
                        raise RuntimeError("positive fixture candidate fragment missing")
                    operation = draft.transformation_trace[0].get("operation")
                    if operation != positive_case.expected_trace_operation:
                        raise RuntimeError("positive fixture trace operation drifted")
                drafts.append(draft)

                stage = "candidate_reelaboration"
                status, diagnostics = _candidate_status(
                    backends[context.context_id],
                    case_id=case_id,
                    context_id=context.context_id,
                    imports=imports,
                    code=draft.candidate_code,
                )
                stage = "candidate_materialization"
                candidate = build_derived_theorem_record(
                    draft=draft,
                    sources=[source.theorem for source in sources],
                    primary_source_id=primary.theorem.theorem_id,
                    elaboration_status=status,
                    elaboration_diagnostics=diagnostics,
                    metadata={"artifact_class": "smoke", "smoke_case_id": case_id},
                )
                candidate_theorems.append(candidate)
                stage = "candidate_representation"
                candidate_representation = _build_representation(
                    backends[context.context_id],
                    candidate,
                    imports=imports,
                    created_at=config.record_timestamp,
                )
                candidate_representations.append(candidate_representation)
                stage = "candidate_audit"
                if rule_id == "n10_nearby_theorem":
                    audit = audit_pair_transformation(
                        loaded_registry,
                        negative_runtime.pair_rules[0],
                        sources[0].theorem,
                        sources[0].representation,
                        sources[1].theorem,
                        sources[1].representation,
                        candidate,
                        candidate_representation,
                        draft,
                    )
                else:
                    runtime = (
                        positive_runtime.runtime
                        if polarity == Polarity.POSITIVE
                        else negative_runtime.runtime
                    )
                    audit = runtime.audit(
                        rule_id,
                        primary.theorem,
                        primary.representation,
                        candidate,
                        candidate_representation,
                        draft,
                    )
                audits.append(audit)
                stage = "variant_materialization"
                variant = build_deterministic_variant_record(
                    attempt=attempt,
                    draft=draft,
                    audit=audit,
                    candidate=candidate,
                    candidate_representation=candidate_representation,
                    polarity=polarity,
                    metadata={"artifact_class": "smoke", "smoke_case_id": case_id},
                )
                variants.append(variant)
                stage = "pair_materialization"
                pair = build_deterministic_pair_record(
                    source=primary.theorem,
                    candidate=candidate,
                    draft=draft,
                    audit=audit,
                    all_sources=[source.theorem for source in sources],
                    metadata={"artifact_class": "smoke", "smoke_case_id": case_id},
                )
                audit_evidence = _make_audit_evidence(
                    pair=pair,
                    audit=audit,
                    config_hash=loaded_config.config_hash,
                    created_at=config.record_timestamp,
                )
                evidence.append(audit_evidence)
                pair = _attach_evidence(pair, audit_evidence)
                label_id: str | None = None
                if rule_id == "p01_alpha":
                    pair, label = _make_p01_smoke_label(pair, audit_evidence)
                    labels.append(label)
                    label_id = label.label_id
                pairs.append(pair)
                family_results.append(
                    SmokeCaseResult(
                        case_id=case_id,
                        rule_id=rule_id,
                        source_theorem_ids=draft.source_theorem_ids,
                        source_representation_ids=draft.source_representation_ids,
                        attempt_id=attempt.attempt_id,
                        draft_id=draft.draft_id,
                        candidate_theorem_id=candidate.theorem_id,
                        candidate_representation_id=candidate_representation.representation_id,
                        audit_id=audit.audit_id,
                        variant_id=variant.variant_id,
                        pair_id=pair.pair_id,
                        evidence_id=audit_evidence.evidence_id,
                        resolved_label_id=label_id,
                    )
                )
            except Exception as exc:
                captured = getattr(exc, "execution", None)
                if captured is not None and (
                    attempt is None or captured.attempt.attempt_id != attempt.attempt_id
                ):
                    attempts.append(captured.attempt)
                    attempt = captured.attempt
                failures.append(
                    SmokeFailureRecord(
                        case_id=case_id,
                        rule_id=rule_id,
                        stage=getattr(exc, "stage", stage),
                        failure_type=type(exc).__name__,
                        detail=str(exc) or type(exc).__name__,
                        attempt_id=None if attempt is None else attempt.attempt_id,
                    )
                )

        for positive_case_config in positive_profile.config.cases:
            source = source_by_case_role.get((positive_case_config.case_id, "primary"))
            if source is not None:
                materialize_case(
                    case_id=positive_case_config.case_id,
                    rule_id=positive_case_config.rule_id,
                    seed=positive_case_config.seed,
                    sources=(source,),
                    primary=source,
                    context=positive_context,
                    imports=positive_profile.config.imports,
                    polarity=Polarity.POSITIVE,
                    positive_case=positive_case_config,
                )
        for case_index, negative_case_config in enumerate(negative_profile.cases):
            primary = source_by_case_role.get((negative_case_config.case_id, "primary"))
            if primary is None:
                continue
            case_sources = [primary]
            donor = source_by_case_role.get((negative_case_config.case_id, "donor"))
            if donor is not None:
                case_sources.append(donor)
            materialize_case(
                case_id=negative_case_config.case_id,
                rule_id=negative_case_config.rule_id,
                seed=negative_profile.seed + case_index,
                sources=case_sources,
                primary=primary,
                context=negative_context,
                imports=negative_profile.imports,
                polarity=Polarity.NEGATIVE,
            )
    finally:
        for backend in backends.values():
            backend.close()

    unexpected_failures = tuple(failure for failure in failures if not failure.expected)
    expected_failures = tuple(failure for failure in failures if failure.expected)
    split_manifest = _component_assignments(pairs, seed=config.split_seed)

    representation_by_theorem = {
        record.theorem_id: record
        for record in (*source_representations, *candidate_representations)
    }
    label_by_pair = {label.target_id: label for label in labels}
    p01_pairs = [pair for pair in pairs if pair.transformation_family == "p01_alpha"]
    if len(p01_pairs) != 1 or p01_pairs[0].pair_id not in label_by_pair:
        raise RuntimeError("LF-019 requires exactly one linked P01 smoke label")
    training_pair = p01_pairs[0]
    training_features = _pair_features(
        training_pair,
        representation_by_theorem=representation_by_theorem,
    )
    training_result = train_tiny_smoke_classifier(
        (
            SmokeTrainingExample(
                pair=training_pair,
                label=label_by_pair[training_pair.pair_id],
                features=training_features,
            ),
        ),
        config=TinySmokeTrainingConfig(seed=config.split_seed),
    )
    predictions = tuple(
        predict_tiny_smoke_classifier(
            training_result.model,
            pair_id=pair.pair_id,
            features=_pair_features(
                pair,
                representation_by_theorem=representation_by_theorem,
            ),
        )
        for pair in sorted(pairs, key=lambda value: value.pair_id)
    )
    model_metrics = SmokeMetrics(
        model_artifact_id=training_result.model.artifact_id,
        prediction_count=len(predictions),
        schema_valid_count=len(predictions),
        finite_score_count=sum(
            all(
                math.isfinite(score)
                for score in (
                    prediction.same_claim_probability,
                    prediction.ambiguity_probability,
                    *prediction.relation_scores.values(),
                )
            )
            for prediction in predictions
        ),
        prediction_hash=hash_canonical(
            [prediction.model_dump(mode="json") for prediction in predictions]
        ),
    )
    plumbing_metrics = SmokePlumbingMetrics(
        prediction_count=len(predictions),
        schema_valid_count=len(predictions),
        finite_score_count=model_metrics.finite_score_count,
        labeled_training_count=len(labels),
        component_count=split_manifest.component_count,
    )

    partition_records: dict[str, Sequence[StrictModel]] = {
        "contexts": contexts,
        "source_theorems": source_theorems,
        "source_representations": source_representations,
        "attempts": attempts,
        "drafts": drafts,
        "candidate_theorems": candidate_theorems,
        "candidate_representations": candidate_representations,
        "audits": audits,
        "variants": variants,
        "pairs": pairs,
        "failures": failures,
    }
    semantic_records: dict[str, Sequence[StrictModel]] = {
        **partition_records,
        "evidence": evidence,
        "labels": labels,
        "predictions": predictions,
        "model": (training_result.model,),
        "model_metrics": (model_metrics,),
        "plumbing_metrics": (plumbing_metrics,),
    }
    semantic_fingerprint = _semantic_fingerprint(
        contexts=contexts,
        partition_records=semantic_records,
        split_manifest=split_manifest,
    )
    replay_pass = (
        expected_semantic_fingerprint is not None
        and semantic_fingerprint == expected_semantic_fingerprint
    )

    partition_paths = _output_paths(resolved_output)
    partition_hashes = {
        str(partition_paths[name].relative_to(paths.root)): _write_jsonl(
            records,
            partition_paths[name],
        )
        for name, records in partition_records.items()
    }
    raw_hashes = {
        str(path.relative_to(paths.root)): hash_file(path)
        for path in sorted(raw_response_dir.rglob("*"))
        if path.is_file()
    }

    evidence_dir = paths.data / "evidence" / "lf019_smoke_v1" / run_id
    evidence_path = evidence_dir / "evidence.jsonl"
    evidence_hash = _write_jsonl(evidence, evidence_path)
    label_dir = paths.data / "labels" / "provisional" / "lf019_smoke_v1" / run_id
    label_path = label_dir / "labels.jsonl"
    label_hash = _write_jsonl(labels, label_path)
    split_path = paths.data / "split_manifests" / f"lf019_smoke_{run_id}.json"
    split_hash = _write_json(split_manifest, split_path)
    model_dir = paths.artifacts / "checkpoints" / "smoke" / "lf019" / run_id
    model_path = model_dir / "model.json"
    model_hash = _write_json(training_result.model, model_path)
    model_eligibility_path = model_dir / "eligibility.json"
    model_eligibility_hash = _write_json(
        SmokeArtifactBoundary(),
        model_eligibility_path,
    )
    prediction_dir = paths.artifacts / "predictions" / "smoke" / run_id
    prediction_path = prediction_dir / "predictions.jsonl"
    prediction_hash = _write_jsonl(predictions, prediction_path)
    model_metrics_path = prediction_dir / "model_metrics.json"
    model_metrics_hash = _write_json(model_metrics, model_metrics_path)
    plumbing_metrics_path = prediction_dir / "plumbing_metrics.json"
    plumbing_metrics_hash = _write_json(plumbing_metrics, plumbing_metrics_path)

    context_hash = hash_canonical([context.model_dump(mode="json") for context in contexts])
    common_inputs = {
        str(authorization_path.relative_to(paths.root)): authorization_hash,
        str(benchmark.manifest_path.relative_to(paths.root)): benchmark_hash,
    }
    bound_config_paths = (
        config_path,
        positive_profile.path,
        negative_loaded.path,
        loaded_registry.registry_path,
        loaded_registry.profile_path,
        loaded_registry.promotion_policy_path,
    )
    input_partition_checksums = {
        str(path.relative_to(paths.root)): hash_file(path) for path in bound_config_paths
    }
    if code_bundle_path is not None and code_bundle_hash is not None:
        key = (
            str(code_bundle_path.relative_to(paths.root))
            if code_bundle_path.is_relative_to(paths.root)
            else str(code_bundle_path)
        )
        input_partition_checksums[key] = code_bundle_hash

    main_output_manifest = _partition_manifest(
        artifact_class=ArtifactClass.SMOKE,
        run_id=run_id,
        source="lf019_smoke_fixture",
        source_revision=loaded_config.config_hash,
        config_hash=loaded_registry.registry_hash,
        row_count=len(pairs),
        file_checksums={**partition_hashes, **raw_hashes},
        input_manifest_hashes=common_inputs,
        input_partition_checksums=input_partition_checksums,
        output_partition_checksums=partition_hashes,
        failure_partition_checksums={
            str(partition_paths["failures"].relative_to(paths.root)): partition_hashes[
                str(partition_paths["failures"].relative_to(paths.root))
            ]
        },
        environment_hash=environment_lock_hash,
        context_hash=context_hash,
        code_state=code_state,
        created_at=created_at,
        notes="LF-019 smoke-only integrated deterministic generation; never scientific data.",
    )
    output_manifest_path = resolved_output / "manifest.json"
    output_manifest_hash = write_manifest(main_output_manifest, output_manifest_path)

    evidence_manifest = _partition_manifest(
        artifact_class=ArtifactClass.SMOKE,
        run_id=run_id,
        source="lf019_smoke_evidence",
        source_revision=loaded_config.config_hash,
        config_hash=loaded_registry.registry_hash,
        row_count=len(evidence),
        file_checksums={str(evidence_path.relative_to(paths.root)): evidence_hash},
        input_manifest_hashes={
            str(output_manifest_path.relative_to(paths.root)): output_manifest_hash
        },
        input_partition_checksums=input_partition_checksums,
        output_partition_checksums={str(evidence_path.relative_to(paths.root)): evidence_hash},
        failure_partition_checksums={},
        environment_hash=environment_lock_hash,
        context_hash=context_hash,
        code_state=code_state,
        created_at=created_at,
        notes="Pair-linked transformation audit evidence only; LF-020 has not begun.",
    )
    evidence_manifest_path = evidence_dir / "manifest.json"
    evidence_manifest_hash = write_manifest(evidence_manifest, evidence_manifest_path)

    label_manifest = _partition_manifest(
        artifact_class=ArtifactClass.SMOKE,
        run_id=run_id,
        source="lf019_smoke_labels",
        source_revision=loaded_config.config_hash,
        config_hash=loaded_registry.registry_hash,
        row_count=len(labels),
        file_checksums={str(label_path.relative_to(paths.root)): label_hash},
        input_manifest_hashes={
            str(output_manifest_path.relative_to(paths.root)): output_manifest_hash,
            str(evidence_manifest_path.relative_to(paths.root)): evidence_manifest_hash,
        },
        input_partition_checksums=input_partition_checksums,
        output_partition_checksums={str(label_path.relative_to(paths.root)): label_hash},
        failure_partition_checksums={},
        environment_hash=environment_lock_hash,
        context_hash=context_hash,
        code_state=code_state,
        created_at=created_at,
        notes="Exactly one provisional smoke_alpha_certificate label; evaluation-ineligible.",
    )
    label_manifest_path = label_dir / "manifest.json"
    label_manifest_hash = write_manifest(label_manifest, label_manifest_path)

    catalog_artifacts = {
        **partition_hashes,
        **raw_hashes,
        str(output_manifest_path.relative_to(paths.root)): output_manifest_hash,
        str(evidence_path.relative_to(paths.root)): evidence_hash,
        str(evidence_manifest_path.relative_to(paths.root)): evidence_manifest_hash,
        str(label_path.relative_to(paths.root)): label_hash,
        str(label_manifest_path.relative_to(paths.root)): label_manifest_hash,
        str(split_path.relative_to(paths.root)): split_hash,
        str(model_path.relative_to(paths.root)): model_hash,
        str(model_eligibility_path.relative_to(paths.root)): model_eligibility_hash,
        str(prediction_path.relative_to(paths.root)): prediction_hash,
        str(model_metrics_path.relative_to(paths.root)): model_metrics_hash,
        str(plumbing_metrics_path.relative_to(paths.root)): plumbing_metrics_hash,
    }
    catalog = SmokeArtifactCatalog(
        run_id=run_id,
        artifact_paths=tuple(sorted(catalog_artifacts)),
        artifact_hashes=dict(sorted(catalog_artifacts.items())),
    )
    catalog_path = resolved_output / "artifact_catalog.json"
    catalog_hash = _write_json(catalog, catalog_path)

    successful_rules = tuple(sorted(result.rule_id for result in family_results))
    p01_results = [result for result in family_results if result.rule_id == "p01_alpha"]
    non_p01_pairs = [pair for pair in pairs if pair.transformation_family != "p01_alpha"]
    n10_results = [result for result in family_results if result.rule_id == "n10_nearby_theorem"]
    n10_pair = next(
        (pair for pair in pairs if pair.transformation_family == "n10_nearby_theorem"),
        None,
    )
    all_representations = (*source_representations, *candidate_representations)
    protected_overlap = _protected_overlap(
        benchmark,
        (*source_theorems, *candidate_theorems),
        all_representations,
    )
    release_guard_pass = all(
        _guard_rejects_bound_smoke_catalog(paths=paths, catalog=catalog, use=use)
        for use in (
            ArtifactUse.RELEASE,
            ArtifactUse.CALIBRATION,
            ArtifactUse.SCIENTIFIC_TABLE,
        )
    )
    selection_guard_pass = _guard_rejects_bound_smoke_catalog(
        paths=paths,
        catalog=catalog,
        use=ArtifactUse.MODEL_SELECTION,
    )
    first_source = source_theorems[0] if source_theorems else None
    first_representation = source_representations[0] if source_representations else None
    disabled_rejected = (
        first_source is not None
        and first_representation is not None
        and _disabled_dispatch_rejected(
            loaded_registry,
            first_source,
            first_representation,
        )
    )
    family_ids = {
        family.family_id
        for family in loaded_registry.config.families
        if family.family_id in loaded_registry.profile.active_family_ids
    }
    lineage_complete = len(family_results) == 8 and all(
        result.attempt_id in {item.attempt_id for item in attempts}
        and result.draft_id in {item.draft_id for item in drafts}
        and result.candidate_theorem_id in {item.theorem_id for item in candidate_theorems}
        and result.candidate_representation_id
        in {item.representation_id for item in candidate_representations}
        and result.audit_id in {item.audit_id for item in audits}
        and result.variant_id in {item.variant_id for item in variants}
        and result.pair_id in {item.pair_id for item in pairs}
        and result.evidence_id in {item.evidence_id for item in evidence}
        for result in family_results
    )
    check_results = {
        "current_registry_snapshot_frozen": _registry_validation_is_current(
            paths, loaded_registry.registry_hash
        ),
        "active_family_inventory_exact": family_ids == set(_ACTIVE_RULES),
        "all_eight_active_families_executed": successful_rules == _ACTIVE_RULES,
        "disabled_family_dispatch_rejected": bool(disabled_rejected),
        "ten_or_more_fixture_sources_extracted": len(source_theorems) >= 10,
        "all_candidates_reelaborated": len(candidate_theorems) == 8
        and all(
            theorem.elaboration_status
            in {
                ValidationStatus.ELABORATES,
                ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            }
            for theorem in candidate_theorems
        ),
        "complete_attempt_draft_audit_variant_pair_lineage": lineage_complete,
        "all_candidates_have_validation_and_audit_status": len(audits) == 8
        and all(
            audit.recommended_validation_status
            in {
                ValidationStatus.ELABORATES,
                ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            }
            and not audit.violation_codes
            for audit in audits
        ),
        "n10_dual_ancestry_persisted": len(n10_results) == 1
        and n10_pair is not None
        and len(n10_results[0].source_theorem_ids) == 2
        and len(n10_pair.split_group_ids) == 2,
        "transformation_evidence_linked": len(evidence) == len(pairs) == 8
        and all(len(pair.evidence_ids) == 1 for pair in pairs),
        "p01_only_smoke_resolution": len(labels) == 1
        and len(p01_results) == 1
        and labels[0].target_id == p01_results[0].pair_id
        and labels[0].resolution_method == "smoke_alpha_certificate"
        and labels[0].quality_tier == QualityTier.PROVISIONAL
        and labels[0].same_claim is True,
        "non_p01_semantics_unresolved": len(non_p01_pairs) == 7
        and all(pair.resolved_label_id is None for pair in non_p01_pairs),
        "zero_intention_to_label_inference": all(
            pair.resolved_label_id is None
            for pair in pairs
            if pair.intended_relation is not None and pair.intended_relation.value != "equivalent"
        ),
        "zero_gold_labels": all(
            label.quality_tier
            not in {
                QualityTier.GOLD_HUMAN,
                QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
                QualityTier.GOLD_COUNTEREXAMPLE,
            }
            for label in labels
        ),
        "zero_promotions": all(
            variant.quality_tier == QualityTier.PROVISIONAL for variant in variants
        ),
        "zero_protected_benchmark_overlap": not protected_overlap,
        "zero_connected_split_leakage": split_manifest.group_overlap_count == 0,
        "batch_failure_isolation_passed": len(expected_failures)
        == len(config.expected_failure_statements)
        and len(source_theorems) >= 10
        and not unexpected_failures,
        "deterministic_semantic_replay_passed": replay_pass,
        "smoke_release_guard_passed": release_guard_pass,
        "smoke_selection_guard_passed": selection_guard_pass,
    }
    mechanical_pass = not unexpected_failures and all(
        passed
        for name, passed in check_results.items()
        if name != "deterministic_semantic_replay_passed"
    )
    clean_checkout = not code_state.git_dirty
    lf019_accepted = mechanical_pass and replay_pass and clean_checkout

    report = LF019SmokeReport(
        mechanical_pass=mechanical_pass,
        clean_checkout_pass=clean_checkout,
        lf019_accepted=lf019_accepted,
        gate_4g_closed=lf019_accepted,
        run_id=run_id,
        registry_hash=loaded_registry.registry_hash,
        config_hash=loaded_config.config_hash,
        bound_input_hashes={
            **input_partition_checksums,
            **common_inputs,
            "environment_lock": environment_lock_hash,
            "context_bundle": context_bundle_hash,
        },
        context_ids=tuple(context.context_id for context in contexts),
        configured_source_count=(
            len(positive_profile.config.cases)
            + len(negative_profile.cases)
            + 1  # N10 donor
            + len(config.inventory_only_statements)
        ),
        accepted_source_count=len(source_theorems),
        expected_failure_count=len(expected_failures),
        unexpected_failure_count=len(unexpected_failures),
        family_results=tuple(sorted(family_results, key=lambda value: value.rule_id)),
        generated_pair_count=len(pairs),
        evidence_count=len(evidence),
        smoke_label_count=len(labels),
        gold_label_count=sum(
            label.quality_tier
            in {
                QualityTier.GOLD_HUMAN,
                QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
                QualityTier.GOLD_COUNTEREXAMPLE,
            }
            for label in labels
        ),
        promoted_item_count=sum(
            variant.quality_tier != QualityTier.PROVISIONAL for variant in variants
        ),
        split_component_count=split_manifest.component_count,
        check_results=check_results,
        output_manifest_path=str(output_manifest_path.relative_to(paths.root)),
        output_manifest_sha256=output_manifest_hash,
        artifact_catalog_path=str(catalog_path.relative_to(paths.root)),
        artifact_catalog_sha256=catalog_hash,
        notes=tuple(
            note
            for note in (
                "All LF-019 artifacts are smoke-only and barred from scientific use.",
                (
                    "Clean-checkout acceptance remains open for this run."
                    if not clean_checkout
                    else ""
                ),
                (f"Protected overlaps: {protected_overlap}" if protected_overlap else ""),
            )
            if note
        ),
    )
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    report_hash = _write_json(report, resolved_report)
    run_manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.SMOKE,
        command="leanfaith generate-deterministic --run-smoke-vertical-slice",
        argv=(
            "leanfaith",
            "generate-deterministic",
            "--run-smoke-vertical-slice",
        ),
        code=code_state,
        environment_schema_version=environment_lock.environment_schema_version,
        config_hashes={
            str(path.relative_to(paths.root)): hash_file(path) for path in bound_config_paths
        },
        input_hashes={
            **common_inputs,
            **({"code_bundle": code_bundle_hash} if code_bundle_hash is not None else {}),
        },
        output_hashes={
            **catalog_artifacts,
            str(catalog_path.relative_to(paths.root)): catalog_hash,
            str(resolved_report.relative_to(paths.root)): report_hash,
        },
        seeds={"lf019_split": config.split_seed},
        execution={
            "artifact_class": "smoke",
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
            "semantic_fingerprint": semantic_fingerprint,
            "expected_semantic_fingerprint": expected_semantic_fingerprint,
            "clean_checkout": clean_checkout,
        },
        status_counts={
            "configured_sources": report.configured_source_count,
            "accepted_sources": len(source_theorems),
            "expected_failures": len(expected_failures),
            "unexpected_failures": len(unexpected_failures),
            "generated_pairs": len(pairs),
            "smoke_labels": len(labels),
            "gold_labels": 0,
            "promoted_items": 0,
        },
        created_at=created_at,
        notes=(
            "LF-019 smoke vertical slice only. Gate 4A/4B remain open; "
            "no artifact is release/model-selection/calibration/table eligible."
        ),
    )
    run_path = run_manifest_path(paths, run_id)
    write_manifest(run_manifest, run_path)
    artifacts = LF019SmokeArtifacts(
        output_dir=resolved_output,
        report_path=resolved_report,
        output_manifest_path=output_manifest_path,
        run_manifest_path=run_path,
        catalog_path=catalog_path,
        semantic_fingerprint=semantic_fingerprint,
        mechanical_pass=mechanical_pass,
        gate_4g_closed=lf019_accepted,
    )
    if not mechanical_pass:
        failed = tuple(name for name, passed in check_results.items() if not passed)
        raise LF019SmokeError(
            f"LF-019 mechanical checks failed: {failed}",
            artifacts=artifacts,
        )
    return artifacts


@dataclass(frozen=True, slots=True)
class LF019SmokeReplayArtifacts:
    run_a: LF019SmokeArtifacts
    run_b: LF019SmokeArtifacts


def run_lf019_smoke_replay(
    *,
    paths: RepoPaths,
    code_bundle_path: Path | None = None,
) -> LF019SmokeReplayArtifacts:
    """Run A then Run B; Run B binds the exact Run-A semantic fingerprint."""

    run_a = run_lf019_smoke_once(
        paths=paths,
        code_bundle_path=code_bundle_path,
    )
    run_b = run_lf019_smoke_once(
        paths=paths,
        expected_semantic_fingerprint=run_a.semantic_fingerprint,
        code_bundle_path=code_bundle_path,
    )
    if run_a.semantic_fingerprint != run_b.semantic_fingerprint:
        raise LF019SmokeError(
            "LF-019 semantic replay mismatch",
            artifacts=run_b,
        )
    return LF019SmokeReplayArtifacts(run_a=run_a, run_b=run_b)


__all__ = [
    "LF019SmokeArtifacts",
    "LF019SmokeConfig",
    "LF019SmokeError",
    "LF019SmokeReplayArtifacts",
    "LF019SmokeReport",
    "SmokeArtifactCatalog",
    "SmokeFailureRecord",
    "SmokePlumbingMetrics",
    "SmokeSplitManifest",
    "SmokeStatement",
    "run_lf019_smoke_once",
    "run_lf019_smoke_replay",
]

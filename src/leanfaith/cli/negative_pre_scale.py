"""LF-018 Lean-backed negative pre-scale audit slice.

This is the first persisted execution of every scoped negative family.  It is
small and fixture-backed, but it exercises the production registry, LeanInteract
representations, candidate re-elaboration, dual-source N10 lineage, deterministic
variant materialization, and unlabeled pair persistence end to end.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.cli.transformations import _validate_authorization
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets import load_active_benchmark_registry
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
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas import (
    ArtifactClass,
    ContextRecord,
    DataStage,
    OutputManifest,
    PairRecord,
    Polarity,
    QualityTier,
    RepresentationRecord,
    RunManifest,
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
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.negative_factory import build_negative_rule_runtime
from leanfaith.transforms.pair_runtime import (
    PairTransformationDispatchError,
    audit_pair_transformation,
    execute_pair_transformation,
)
from leanfaith.transforms.protocol import build_deterministic_variant_record
from leanfaith.transforms.registry import (
    TransformationExecutionFailed,
    load_transformation_registry,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_CONFIG = Path("configs/transformations/lf018_pre_scale_v1.yaml")
_DEFAULT_OUTPUT = Path("data/generated/deterministic/lf018_pre_scale_v1")
_DEFAULT_REPORT_DIR = Path("reports/transformation_audits/lf018_pre_scale")
_BOUND_TRANSFORMATION_INPUTS = (
    Path("configs/transformations/n01_operator.yaml"),
    Path("configs/transformations/n02_quantifier.yaml"),
    Path("configs/transformations/n03_drop_hypothesis.yaml"),
    Path("configs/transformations/n07_literal_bound.yaml"),
    Path("configs/transformations/n10_nearby_theorem.yaml"),
    Path("configs/transformations/replacement_table_v1.yaml"),
)
_EXPECTED_RULES = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
)
_REQUIRED_AUDIT_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "semantic_atoms",
    "operator_tree",
)
_MECHANICAL_CHECKS = (
    "all_scoped_negative_families_executed",
    "source_and_candidate_views_lean_backed",
    "candidate_statements_reelaborated",
    "attempt_draft_audit_variant_pair_lineage_complete",
    "n10_dual_source_ancestry_persisted",
    "all_outputs_provisional",
    "zero_resolved_semantic_labels",
    "zero_promotions",
)


class NegativePreScaleCase(StrictModel):
    """One immutable, known-applicable LF-018 fixture."""

    case_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    rule_id: Literal[
        "n01_operator",
        "n02_quantifier",
        "n03_drop_hypothesis",
        "n07_literal_bound",
        "n10_nearby_theorem",
    ]
    primary_name: str = Field(min_length=1)
    primary_code: str = Field(min_length=1)
    donor_name: str | None = None
    donor_code: str | None = None

    @model_validator(mode="after")
    def _pair_shape(self) -> NegativePreScaleCase:
        pair_rule = self.rule_id == "n10_nearby_theorem"
        if pair_rule != (self.donor_name is not None and self.donor_code is not None):
            raise ValueError("only N10 requires both donor_name and donor_code")
        return self


class NegativePreScaleConfig(StrictModel):
    """Versioned LF-018 fixture-slice configuration."""

    schema_version: Literal[1] = 1
    audit_profile_id: Literal["lf018_pre_scale_v1"] = "lf018_pre_scale_v1"
    audit_profile_version: Literal["1.0.0"] = "1.0.0"
    project_dir: Literal["tests/lean_fixtures"] = "tests/lean_fixtures"
    imports: str
    seed: int = Field(strict=True)
    record_timestamp_utc: str = Field(min_length=1)
    cases: tuple[NegativePreScaleCase, ...]

    @model_validator(mode="after")
    def _complete_inventory(self) -> NegativePreScaleConfig:
        rule_ids = tuple(case.rule_id for case in self.cases)
        case_ids = tuple(case.case_id for case in self.cases)
        if tuple(sorted(rule_ids)) != _EXPECTED_RULES:
            raise ValueError(f"cases must contain exactly {list(_EXPECTED_RULES)}")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique")
        parsed = datetime.datetime.fromisoformat(self.record_timestamp_utc)
        if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
            raise ValueError("record_timestamp_utc must be timezone-aware UTC")
        return self

    @property
    def record_timestamp(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.record_timestamp_utc)


class NegativePreScaleFailure(StrictModel):
    """Explicit terminal failure for one configured case."""

    schema_version: Literal[1] = 1
    case_id: str
    rule_id: str
    stage: str
    failure_type: str
    detail: str
    attempt_id: str | None = None


class NegativePreScaleCaseResult(StrictModel):
    """Compact index into the persisted records for one successful case."""

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
    root_ancestry_ids: tuple[str, ...]
    output_tier: Literal["provisional"] = "provisional"
    resolved_label_id: None = None


class NegativePreScaleReport(StrictModel):
    """Hash-bound end-to-end LF-018 mechanical audit report."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf018_negative_pre_scale_audit"] = "lf018_negative_pre_scale_audit"
    mechanical_pass: bool
    registry_hash: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    transformation_input_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    authorization_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    environment_lock_sha256: str = Field(pattern=_HEX64)
    context_record_sha256: str = Field(pattern=_HEX64)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    output_manifest_path: str = Field(min_length=1)
    output_manifest_sha256: str = Field(pattern=_HEX64)
    configured_case_count: int = Field(ge=1)
    successful_case_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    case_results: tuple[NegativePreScaleCaseResult, ...]
    failure_records: tuple[NegativePreScaleFailure, ...]
    generated_drafts: int = Field(ge=0)
    generated_pairs: int = Field(ge=0)
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    gate_4g_closed: Literal[False] = False
    check_results: dict[str, bool]
    checks: tuple[str, ...]

    @model_validator(mode="after")
    def _counts(self) -> NegativePreScaleReport:
        expected_inputs = {str(path) for path in _BOUND_TRANSFORMATION_INPUTS}
        if set(self.transformation_input_hashes) != expected_inputs:
            raise ValueError(
                "transformation_input_hashes must bind the five rule configs "
                "and replacement table exactly"
            )
        if set(self.check_results) != set(_MECHANICAL_CHECKS):
            raise ValueError("check_results must contain the exact LF-018 mechanical checks")
        expected_passed = tuple(name for name in _MECHANICAL_CHECKS if self.check_results[name])
        if self.checks != expected_passed:
            raise ValueError("checks must list exactly the mechanically passing checks")
        if self.successful_case_count != len(self.case_results):
            raise ValueError("successful_case_count does not match case_results")
        if self.failure_count != len(self.failure_records):
            raise ValueError("failure_count does not match failure_records")
        if self.successful_case_count + self.failure_count != self.configured_case_count:
            raise ValueError("every configured case must have one terminal report outcome")
        expected_pass = self.failure_count == 0 and all(self.check_results.values())
        if self.mechanical_pass != expected_pass:
            raise ValueError(
                "mechanical_pass must require zero failures and every mechanical check"
            )
        return self


@dataclass(frozen=True, slots=True)
class NegativePreScaleArtifacts:
    output_dir: Path
    output_manifest_path: Path
    output_manifest_sha256: str
    report_path: Path
    report_sha256: str
    run_manifest_path: Path
    run_manifest_sha256: str


class NegativePreScaleAuditError(RuntimeError):
    """The audit persisted its explicit failures and did not pass."""

    def __init__(self, detail: str, *, artifacts: NegativePreScaleArtifacts) -> None:
        super().__init__(detail)
        self.artifacts = artifacts


def _write_jsonl(records: Sequence[StrictModel], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )
    path.write_bytes(payload)
    return hash_file(path)


def _source_record(
    *,
    case: NegativePreScaleCase,
    role: Literal["primary", "donor"],
    name: str,
    code: str,
    context_id: str,
    config_hash: str,
) -> TheoremRecord:
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    theorem_id = make_id(
        "thm",
        {
            "schema": "lf018_pre_scale_source_v1",
            "case_id": case.case_id,
            "role": role,
            "context_id": context_id,
            "statement_content_hash": code_hash,
        },
    )
    ancestry_id = make_id(
        "anc",
        {
            "schema": "lf018_pre_scale_source_v1",
            "case_id": case.case_id,
            "role": role,
            "theorem_id": theorem_id,
        },
    )
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="lf018_pre_scale_fixture",
        source_revision=config_hash,
        source_record=f"{case.case_id}:{role}",
        context_id=context_id,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source=code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=code_hash,
        metadata={
            "audit_profile": "lf018_pre_scale_v1",
            "fixture_role": role,
            "transform_source_eligible": True,
        },
    )


def _build_representation(
    backend: LeanInteractBackend,
    theorem: TheoremRecord,
    *,
    imports: str,
    created_at: datetime.datetime,
) -> RepresentationRecord:
    name = theorem.declaration_full_name
    if name is None:
        raise ValueError("fixture theorem has no declaration_full_name")
    (record,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=theorem.theorem_id,
                full_name=name,
                proof_stripped=theorem.proof_stripped_declaration,
                context_id=theorem.context_id,
                inline_declaration=True,
                inline_source=theorem.inline_elaboration_source,
            )
        ],
        imports=imports,
        created_at=created_at,
    )
    failed = tuple(
        view for view in _REQUIRED_AUDIT_VIEWS if record.view_status[view] != ViewStatus.OK
    )
    if failed:
        raise RuntimeError(f"required representation views failed: {','.join(failed)}")
    if record.alpha_identity_fingerprint is None:
        raise RuntimeError("alpha identity fingerprint is missing")
    return record


def _candidate_status(
    backend: LeanInteractBackend,
    *,
    case_id: str,
    context_id: str,
    code: str,
) -> tuple[ValidationStatus, tuple[str, ...]]:
    result = backend.run(
        LeanRequest(
            request_id=f"lf018-pre-scale-{case_id}-candidate",
            context_id=context_id,
            code=code,
            declarations=True,
            allow_sorry=True,
        )
    )
    diagnostics = tuple(str(message.get("data", "")) for message in result.messages)
    if result.status == LeanStatus.VALID_WITH_SORRY:
        return ValidationStatus.ELABORATES_WITH_PLACEHOLDER, diagnostics
    if result.status == LeanStatus.VALID:
        return ValidationStatus.ELABORATES, diagnostics
    raise RuntimeError(f"candidate did not elaborate: {result.status.value}")


def _output_paths(output_dir: Path) -> dict[str, Path]:
    return {
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


def _fixture_project_revision(project_dir: Path) -> str:
    """Content-address the local fixture project used by the Lean context."""

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
    """Build the canonical project/toolchain-bound fixture context record."""

    projects = load_project_registry(paths)
    spec = projects.get("fixtures")
    if spec is None:
        raise ValueError("project registry has no fixtures entry")
    expected_dir = spec.local_directory(paths)
    if expected_dir is None or expected_dir.resolve() != project_dir:
        raise ValueError("pre-scale project_dir does not match the fixtures registry entry")
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


def _mechanical_check_results(
    *,
    case_results: Sequence[NegativePreScaleCaseResult],
    source_representations: Sequence[RepresentationRecord],
    attempts: Sequence[TransformationAttempt],
    drafts: Sequence[VariantDraft],
    candidate_theorems: Sequence[TheoremRecord],
    candidate_representations: Sequence[RepresentationRecord],
    audits: Sequence[TransformationAudit],
    variants: Sequence[VariantRecord],
    pairs: Sequence[PairRecord],
) -> dict[str, bool]:
    successful_rules = tuple(sorted(result.rule_id for result in case_results))
    all_representations = (*source_representations, *candidate_representations)
    views_ok = bool(all_representations) and all(
        record.alpha_identity_fingerprint is not None
        and all(record.view_status[view] == ViewStatus.OK for view in _REQUIRED_AUDIT_VIEWS)
        for record in all_representations
    )
    candidates_ok = len(candidate_theorems) == len(_EXPECTED_RULES) and all(
        theorem.is_proposition
        and theorem.elaboration_status
        in {
            ValidationStatus.ELABORATES,
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        }
        for theorem in candidate_theorems
    )
    complete_counts = all(
        len(records) == len(_EXPECTED_RULES)
        for records in (attempts, drafts, candidate_representations, audits, variants, pairs)
    )
    lineage_ids = {
        "attempt": {record.attempt_id for record in attempts},
        "draft": {record.draft_id for record in drafts},
        "candidate": {record.theorem_id for record in candidate_theorems},
        "candidate_representation": {
            record.representation_id for record in candidate_representations
        },
        "audit": {record.audit_id for record in audits},
        "variant": {record.variant_id for record in variants},
        "pair": {record.pair_id for record in pairs},
    }
    lineage_complete = complete_counts and all(
        result.attempt_id in lineage_ids["attempt"]
        and result.draft_id in lineage_ids["draft"]
        and result.candidate_theorem_id in lineage_ids["candidate"]
        and result.candidate_representation_id in lineage_ids["candidate_representation"]
        and result.audit_id in lineage_ids["audit"]
        and result.variant_id in lineage_ids["variant"]
        and result.pair_id in lineage_ids["pair"]
        for result in case_results
    )
    n10_results = tuple(result for result in case_results if result.rule_id == "n10_nearby_theorem")
    n10_pairs = tuple(pair for pair in pairs if pair.generator_id == "n10_nearby_theorem")
    n10_dual = (
        len(n10_results) == 1
        and len(n10_pairs) == 1
        and len(n10_results[0].source_theorem_ids) == 2
        and len(n10_results[0].source_representation_ids) == 2
        and len(n10_results[0].root_ancestry_ids) == 2
        and n10_pairs[0].split_group_ids == n10_results[0].root_ancestry_ids
    )
    provisional = (
        len(audits) == len(_EXPECTED_RULES)
        and len(variants) == len(_EXPECTED_RULES)
        and all(audit.recommended_quality_tier == QualityTier.PROVISIONAL for audit in audits)
        and all(variant.quality_tier == QualityTier.PROVISIONAL for variant in variants)
    )
    return {
        "all_scoped_negative_families_executed": (
            successful_rules == _EXPECTED_RULES and len(case_results) == len(_EXPECTED_RULES)
        ),
        "source_and_candidate_views_lean_backed": views_ok,
        "candidate_statements_reelaborated": candidates_ok,
        "attempt_draft_audit_variant_pair_lineage_complete": lineage_complete,
        "n10_dual_source_ancestry_persisted": n10_dual,
        "all_outputs_provisional": provisional,
        "zero_resolved_semantic_labels": all(pair.resolved_label_id is None for pair in pairs),
        "zero_promotions": True,
    }


def run_negative_pre_scale_audit(
    *,
    paths: RepoPaths,
    output_dir: Path | None = None,
    report_path: Path | None = None,
) -> NegativePreScaleArtifacts:
    """Execute all five scoped negative families and persist full lineage."""

    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    resolved_config = (paths.root / _DEFAULT_CONFIG).resolve()
    resolved_output = (output_dir or paths.root / _DEFAULT_OUTPUT / run_id).resolve()
    resolved_report = (report_path or paths.root / _DEFAULT_REPORT_DIR / f"{run_id}.json").resolve()
    for candidate_path in (resolved_config, resolved_output, resolved_report):
        if not candidate_path.is_relative_to(paths.root.resolve()):
            raise ValueError("LF-018 pre-scale paths must stay inside the repository")
    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise FileExistsError(f"LF-018 pre-scale output directory is not empty: {resolved_output}")
    if resolved_report.exists():
        raise FileExistsError(f"LF-018 pre-scale report already exists: {resolved_report}")

    loaded_config = load_config(resolved_config, NegativePreScaleConfig)
    config = loaded_config.config
    authorization_path, authorization_hash, _, _ = _validate_authorization(paths.root)
    benchmark = load_active_benchmark_registry(
        repo_root=paths.root,
        authorization_path=authorization_path,
    )
    benchmark_hash = hash_file(benchmark.manifest_path)
    loaded_registry = load_transformation_registry(paths.root)
    transformation_input_hashes = {
        str(relative_path): hash_file(paths.root / relative_path)
        for relative_path in _BOUND_TRANSFORMATION_INPUTS
    }
    registration = build_negative_rule_runtime(loaded_registry)
    if registration.registered_rule_ids != _EXPECTED_RULES[:4]:
        raise RuntimeError("unary negative runtime does not contain the exact LF-018 inventory")
    if tuple(rule.rule_id for rule in registration.pair_rules) != (_EXPECTED_RULES[4],):
        raise RuntimeError("pair negative runtime does not contain exactly N10")
    pair_rule = registration.pair_rules[0]

    project_dir = (paths.root / config.project_dir).resolve()
    context, environment_lock = _context_for_fixture(
        paths,
        project_dir=project_dir,
        header_text=config.imports,
    )
    environment_lock_path = paths.configs / "environment.lock.yaml"
    environment_lock_hash = hash_file(environment_lock_path)
    code_state = collect_code_state(paths.root)

    resolved_output.mkdir(parents=True, exist_ok=True)
    raw_response_dir = resolved_output / "raw_lean_responses"
    context_path = resolved_output / "context.json"
    context_hash = write_manifest(context, context_path)
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context.context_fingerprint,
            environment_schema_version=context.environment_schema_version,
            raw_response_dir=raw_response_dir,
        )
    )

    source_theorems: list[TheoremRecord] = []
    source_representations: list[RepresentationRecord] = []
    attempts: list[TransformationAttempt] = []
    drafts: list[VariantDraft] = []
    candidate_theorems: list[TheoremRecord] = []
    candidate_representations: list[RepresentationRecord] = []
    audits: list[TransformationAudit] = []
    variants: list[VariantRecord] = []
    pairs: list[PairRecord] = []
    failures: list[NegativePreScaleFailure] = []
    case_results: list[NegativePreScaleCaseResult] = []

    try:
        for case_index, case in enumerate(config.cases):
            attempt: TransformationAttempt | None = None
            stage = "build_primary_source"
            try:
                primary = _source_record(
                    case=case,
                    role="primary",
                    name=case.primary_name,
                    code=case.primary_code,
                    context_id=context.context_id,
                    config_hash=loaded_config.config_hash,
                )
                source_theorems.append(primary)
                case_sources = [primary]
                stage = "represent_primary_source"
                primary_representation = _build_representation(
                    backend,
                    primary,
                    imports=config.imports,
                    created_at=config.record_timestamp,
                )
                source_representations.append(primary_representation)
                donor: TheoremRecord | None = None
                donor_representation: RepresentationRecord | None = None
                seed = config.seed + case_index

                if case.rule_id == "n10_nearby_theorem":
                    assert case.donor_name is not None and case.donor_code is not None
                    stage = "build_donor_source"
                    donor = _source_record(
                        case=case,
                        role="donor",
                        name=case.donor_name,
                        code=case.donor_code,
                        context_id=context.context_id,
                        config_hash=loaded_config.config_hash,
                    )
                    source_theorems.append(donor)
                    case_sources.append(donor)
                    stage = "represent_donor_source"
                    donor_representation = _build_representation(
                        backend,
                        donor,
                        imports=config.imports,
                        created_at=config.record_timestamp,
                    )
                    source_representations.append(donor_representation)
                    stage = "execute_pair_transformation"
                    execution = execute_pair_transformation(
                        loaded_registry,
                        pair_rule,
                        primary,
                        primary_representation,
                        donor,
                        donor_representation,
                        seed,
                    )
                else:
                    stage = "execute_transformation"
                    execution = registration.runtime.execute(
                        case.rule_id,
                        primary,
                        primary_representation,
                        seed,
                    )
                attempt = execution.attempt
                attempts.append(attempt)
                if execution.attempt.terminal_outcome != "generated" or len(execution.drafts) != 1:
                    raise RuntimeError("pre-scale case must generate exactly one draft")
                draft = execution.drafts[0]
                drafts.append(draft)

                stage = "reelaborate_candidate"
                status, diagnostics = _candidate_status(
                    backend,
                    case_id=case.case_id,
                    context_id=context.context_id,
                    code=draft.candidate_code,
                )
                stage = "materialize_candidate_theorem"
                candidate = build_derived_theorem_record(
                    draft=draft,
                    sources=case_sources,
                    primary_source_id=primary.theorem_id,
                    elaboration_status=status,
                    elaboration_diagnostics=diagnostics,
                    metadata={"audit_case_id": case.case_id},
                )
                candidate_theorems.append(candidate)
                stage = "represent_candidate"
                candidate_representation = _build_representation(
                    backend,
                    candidate,
                    imports=config.imports,
                    created_at=config.record_timestamp,
                )
                candidate_representations.append(candidate_representation)
                stage = "audit_candidate"
                if case.rule_id == "n10_nearby_theorem":
                    assert donor is not None and donor_representation is not None
                    audit = audit_pair_transformation(
                        loaded_registry,
                        pair_rule,
                        primary,
                        primary_representation,
                        donor,
                        donor_representation,
                        candidate,
                        candidate_representation,
                        draft,
                    )
                else:
                    audit = registration.runtime.audit(
                        case.rule_id,
                        primary,
                        primary_representation,
                        candidate,
                        candidate_representation,
                        draft,
                    )
                audits.append(audit)
                stage = "materialize_variant"
                variant = build_deterministic_variant_record(
                    attempt=attempt,
                    draft=draft,
                    audit=audit,
                    candidate=candidate,
                    candidate_representation=candidate_representation,
                    polarity=Polarity.NEGATIVE,
                    metadata={"audit_case_id": case.case_id},
                )
                variants.append(variant)
                stage = "materialize_pair"
                pair = build_deterministic_pair_record(
                    source=primary,
                    candidate=candidate,
                    draft=draft,
                    audit=audit,
                    all_sources=case_sources,
                    metadata={"audit_case_id": case.case_id},
                )
                pairs.append(pair)
                stage = "verify_case_result"
                if (
                    audit.recommended_quality_tier != QualityTier.PROVISIONAL
                    or variant.quality_tier != QualityTier.PROVISIONAL
                    or pair.resolved_label_id is not None
                ):
                    raise RuntimeError(
                        "completed pre-scale output is not provisional and unlabeled"
                    )
                case_results.append(
                    NegativePreScaleCaseResult(
                        case_id=case.case_id,
                        rule_id=case.rule_id,
                        source_theorem_ids=draft.source_theorem_ids,
                        source_representation_ids=draft.source_representation_ids,
                        attempt_id=attempt.attempt_id,
                        draft_id=draft.draft_id,
                        candidate_theorem_id=candidate.theorem_id,
                        candidate_representation_id=candidate_representation.representation_id,
                        audit_id=audit.audit_id,
                        variant_id=variant.variant_id,
                        pair_id=pair.pair_id,
                        root_ancestry_ids=candidate.root_ancestry_ids,
                        output_tier="provisional",
                    )
                )
            except (TransformationExecutionFailed, PairTransformationDispatchError) as exc:
                captured = getattr(exc, "execution", None)
                if captured is not None and (
                    attempt is None or captured.attempt.attempt_id != attempt.attempt_id
                ):
                    attempts.append(captured.attempt)
                    attempt = captured.attempt
                failures.append(
                    NegativePreScaleFailure(
                        case_id=case.case_id,
                        rule_id=case.rule_id,
                        stage=getattr(exc, "stage", "dispatch"),
                        failure_type=type(exc).__name__,
                        detail=str(exc),
                        attempt_id=None if attempt is None else attempt.attempt_id,
                    )
                )
            except Exception as exc:
                failures.append(
                    NegativePreScaleFailure(
                        case_id=case.case_id,
                        rule_id=case.rule_id,
                        stage=stage,
                        failure_type=type(exc).__name__,
                        detail=str(exc) or type(exc).__name__,
                        attempt_id=None if attempt is None else attempt.attempt_id,
                    )
                )
    finally:
        backend.close()

    partition_records: dict[str, Sequence[StrictModel]] = {
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
    context_relative = str(context_path.relative_to(paths.root))
    check_results = _mechanical_check_results(
        case_results=case_results,
        source_representations=source_representations,
        attempts=attempts,
        drafts=drafts,
        candidate_theorems=candidate_theorems,
        candidate_representations=candidate_representations,
        audits=audits,
        variants=variants,
        pairs=pairs,
    )
    mechanical_pass = not failures and all(check_results.values())
    output_manifest = OutputManifest(
        stage=DataStage.GENERATED,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        run_id=run_id,
        source="lf018_pre_scale_fixture",
        source_revision=loaded_config.config_hash,
        config_hash=loaded_registry.registry_hash,
        record_schema_version=1,
        row_count=len(pairs),
        attempted_row_count=len(config.cases),
        declaration_count=len(candidate_theorems),
        terminal_outcome_counts={
            "success": len(case_results),
            "failure": len(failures),
        },
        file_checksums={
            context_relative: context_hash,
            **partition_hashes,
            **raw_hashes,
        },
        input_manifest_hashes={
            str(benchmark.manifest_path.relative_to(paths.root)): benchmark_hash,
            str(authorization_path.relative_to(paths.root)): authorization_hash,
            context_relative: context_hash,
        },
        input_partition_checksums={
            str(resolved_config.relative_to(paths.root)): hash_file(resolved_config),
            str(environment_lock_path.relative_to(paths.root)): environment_lock_hash,
            **transformation_input_hashes,
        },
        output_partition_checksums=partition_hashes,
        failure_partition_checksums={
            str(partition_paths["failures"].relative_to(paths.root)): partition_hashes[
                str(partition_paths["failures"].relative_to(paths.root))
            ]
        },
        environment_hash=environment_lock_hash,
        context_hash=context_hash,
        code_tree_hash=code_state.code_tree_hash,
        code=code_state,
        created_at=created_at,
        notes=(
            "Fixture-backed LF-018 pre-scale audit only; provisional intentions, "
            "no semantic labels or promotions."
        ),
    )
    output_manifest_path = resolved_output / "manifest.json"
    output_manifest_hash = write_manifest(output_manifest, output_manifest_path)

    report = NegativePreScaleReport(
        mechanical_pass=mechanical_pass,
        registry_hash=loaded_registry.registry_hash,
        config_hash=loaded_config.config_hash,
        transformation_input_hashes=transformation_input_hashes,
        authorization_sha256=authorization_hash,
        active_benchmark_manifest_sha256=benchmark_hash,
        environment_lock_sha256=environment_lock_hash,
        context_record_sha256=context_hash,
        context_id=context.context_id,
        output_manifest_path=str(output_manifest_path.relative_to(paths.root)),
        output_manifest_sha256=output_manifest_hash,
        configured_case_count=len(config.cases),
        successful_case_count=len(case_results),
        failure_count=len(failures),
        case_results=tuple(case_results),
        failure_records=tuple(failures),
        generated_drafts=len(drafts),
        generated_pairs=len(pairs),
        check_results=check_results,
        checks=tuple(name for name in _MECHANICAL_CHECKS if check_results[name]),
    )
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.write_bytes(canonical_json_bytes(report.model_dump(mode="json")) + b"\n")
    report_hash = hash_file(resolved_report)
    argv = ["leanfaith", "generate-deterministic", "--run-negative-pre-scale"]
    if output_dir is not None:
        argv.extend(("--output-dir", str(resolved_output)))
    if report_path is not None:
        argv.extend(("--report", str(resolved_report)))
    run_manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith generate-deterministic --run-negative-pre-scale",
        argv=tuple(argv),
        code=code_state,
        environment_schema_version=environment_lock.environment_schema_version,
        config_hashes={
            str(resolved_config.relative_to(paths.root)): loaded_config.config_hash,
            str(loaded_registry.registry_path.relative_to(paths.root)): (
                loaded_registry.registry_config_hash
            ),
            str(loaded_registry.profile_path.relative_to(paths.root)): (
                loaded_registry.profile_config_hash
            ),
            str(loaded_registry.promotion_policy_path.relative_to(paths.root)): (
                loaded_registry.promotion_policy_hash
            ),
            **transformation_input_hashes,
        },
        input_hashes={
            str(authorization_path.relative_to(paths.root)): authorization_hash,
            str(benchmark.manifest_path.relative_to(paths.root)): benchmark_hash,
            str(resolved_config.relative_to(paths.root)): hash_file(resolved_config),
            str(environment_lock_path.relative_to(paths.root)): environment_lock_hash,
            context_relative: context_hash,
        },
        output_hashes={
            context_relative: context_hash,
            **partition_hashes,
            **raw_hashes,
            str(output_manifest_path.relative_to(paths.root)): output_manifest_hash,
            str(resolved_report.relative_to(paths.root)): report_hash,
        },
        seeds={"lf018_pre_scale": config.seed},
        execution={
            "context_id": context.context_id,
            "project_dir": config.project_dir,
            "record_timestamp_utc": config.record_timestamp_utc,
            "resolved_output_dir": str(resolved_output.relative_to(paths.root)),
            "resolved_report_path": str(resolved_report.relative_to(paths.root)),
        },
        status_counts={
            "configured_cases": len(config.cases),
            "successful_cases": len(case_results),
            "failed_cases": len(failures),
            "generated_drafts": len(drafts),
            "generated_pairs": len(pairs),
            "resolved_semantic_labels": 0,
            "promoted_items": 0,
        },
        created_at=created_at,
        notes=(
            "LF-018 pre-scale audit. Gate 4G remains open until integrated Phase-4 "
            "generation acceptance is separately recorded."
        ),
    )
    manifest_path = run_manifest_path(paths, run_id)
    run_manifest_hash = write_manifest(run_manifest, manifest_path)
    artifacts = NegativePreScaleArtifacts(
        output_dir=resolved_output,
        output_manifest_path=output_manifest_path,
        output_manifest_sha256=output_manifest_hash,
        report_path=resolved_report,
        report_sha256=report_hash,
        run_manifest_path=manifest_path,
        run_manifest_sha256=run_manifest_hash,
    )
    if not mechanical_pass:
        failed_checks = sorted(name for name, passed in check_results.items() if not passed)
        raise NegativePreScaleAuditError(
            (
                f"{len(failures)} of {len(config.cases)} LF-018 cases failed; "
                f"failed_checks={failed_checks}"
            ),
            artifacts=artifacts,
        )
    return artifacts


__all__ = [
    "NegativePreScaleArtifacts",
    "NegativePreScaleAuditError",
    "NegativePreScaleCase",
    "NegativePreScaleCaseResult",
    "NegativePreScaleConfig",
    "NegativePreScaleFailure",
    "NegativePreScaleReport",
    "run_negative_pre_scale_audit",
]

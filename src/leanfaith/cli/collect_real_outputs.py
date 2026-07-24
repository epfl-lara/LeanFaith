"""LF-021 real-output collection entry-point foundations.

Revision 4.1 intentionally keeps every provider execution mode disabled until
the Phase-5 ADR pins sources, provider revisions, prompt/parser versions, and a
held-out family.  This module makes that boundary executable: the checked-in
configuration can be validated and hash-bound without making a provider call.
The actual collector is added behind the same command after an authorized
configuration exists.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
)
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.config import (
    GenerationFoundationConfigs,
    NearDuplicateConfig,
    ProblemPoolConfig,
    ProblemPoolOutputConfig,
    ProblemPoolSourceConfig,
    SourceAuthorizationConfig,
    load_generation_foundation_configs,
)
from leanfaith.generation.problem_pool import (
    ProblemPoolCandidate,
    ProblemPoolDenylistBinding,
    build_problem_pool,
)
from leanfaith.generation.prompts import (
    DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE,
    DIRECT_AUTOFORMALIZATION_TEMPLATE_ID,
    DIRECT_AUTOFORMALIZATION_TEMPLATE_VERSION,
    DirectOutputParseError,
    ParsedLeanDeclaration,
    PublicTrustedProblem,
    parse_direct_autoformalization_output,
    render_direct_autoformalization_prompt,
)
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ReplayProvider,
    bridge_provider_result_to_llm_lineage,
    create_provider_request_for_problem,
    persist_provider_request,
    verify_llm_call_artifacts,
)
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    CandidateScreeningStatus,
    RealOutputMaterializationResult,
    RealOutputOutcomeCode,
    admit_screened_real_output_candidate,
    candidate_benchmark_hits,
    materialize_real_output_candidate,
)
from leanfaith.lean.extraction import PLACEHOLDER
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.project_registry import (
    ContextPayload,
    build_context_record,
    check_project_revision,
    check_project_toolchain,
    load_environment_lock,
    load_project_registry,
)
from leanfaith.representations.pipeline import RepresentationFailure
from leanfaith.schemas.enums import (
    ArtifactClass,
    NLTrust,
    ParseStatus,
    ValidationStatus,
)
from leanfaith.schemas.ids import ANCESTRY_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    check_llm_call_attempt_lineage,
)
from leanfaith.schemas.manifest import (
    CodeState,
    RunManifest,
    collect_code_state,
    new_run_id,
    require_utc,
    run_manifest_path,
    write_manifest,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import (
    ContextRecord,
    TheoremRecord,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_OFFLINE_FIXTURE = Path("examples/lf021_offline_smoke_v1.json")
_DEFAULT_OFFLINE_HEADER = Path("examples/lf021_offline_smoke_header_v1.lean")
_DEFAULT_OFFLINE_ADR = Path("docs/adr/ADR-0005-lf021-offline-replay.md")
_DEFAULT_OFFLINE_OUTPUT = Path("data/real_outputs/smoke/lf021_offline_v1")
_OFFLINE_PROVIDER_SLOT = "offline_fixture"
_OFFLINE_MODEL_FAMILY = "leanfaith_offline_fixture"
_DIRECT_PARSER_VERSION: Literal["direct_autoformalization_v1"] = "direct_autoformalization_v1"


class LF021FoundationError(RuntimeError):
    """The checked-in LF-021 safety/configuration boundary is inconsistent."""


class LF021FoundationReport(StrictModel):
    """Machine-readable result of the no-provider LF-021 preflight."""

    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_generation_foundation"] = "lf021_generation_foundation"
    generated_at: datetime.datetime
    passed: Literal[True] = True
    execution_authorized: Literal[False] = False
    provider_calls_made: Literal[0] = 0
    semantic_labels_created: Literal[0] = 0
    problem_pool_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    real_outputs_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: dict[str, bool]
    unresolved_requirements: tuple[str, ...]

    @model_validator(mode="after")
    def _checks_pass(self) -> LF021FoundationReport:
        require_utc(self.generated_at)
        if not self.checks or not all(self.checks.values()):
            raise ValueError("LF-021 foundation report requires every safety check to pass")
        if not self.unresolved_requirements:
            raise ValueError(
                "disabled LF-021 foundation must name the requirements blocking execution"
            )
        return self


@dataclass(frozen=True, slots=True)
class LF021FoundationValidation:
    """Validated configs plus their fail-closed report."""

    configs: GenerationFoundationConfigs
    report: LF021FoundationReport


class LF021OfflineSmokeFixture(StrictModel):
    """Hand-authored public LF-021 smoke/qualification fixture.

    Schema v1 is the original deterministic offline fixture and remains
    byte-for-byte compatible with its sealed replay artifacts.  Schema v2 adds
    only the project/header/generated-name bindings needed to run the same
    public-fixture path against the pinned mathlib checkout.
    """

    schema_version: Literal[1, 2] = 1
    fixture_id: Literal[
        "lf021_offline_public_identity_v1",
        "lf021_kimina_mathlib_nat_comm_20260723_v1",
        "lf021_goedel_mathlib_nat_comm_20260723_v1",
        "lf021_stepfun_mathlib_nat_comm_20260723_v1",
    ]
    source: Literal[
        "lf021_offline_public_fixture",
        "lf021_kimina_mathlib_public_fixture",
        "lf021_goedel_mathlib_public_fixture",
        "lf021_stepfun_mathlib_public_fixture",
    ]
    source_revision: str = Field(min_length=1)
    source_split: Literal["smoke"]
    source_record_id: str = Field(min_length=1)
    source_license: str = Field(min_length=1)
    problem_id: str = Field(min_length=1)
    problem_group: str = Field(min_length=1)
    nl_statement: str = Field(min_length=1)
    nl_source_link: str = Field(min_length=1)
    imports: str = Field(min_length=1)
    reference_name: str = Field(min_length=1)
    reference_statement: str = Field(min_length=1)
    generated_response: str = Field(min_length=1)
    project_registry_key: Literal["fixtures", "mathlib"] | None = None
    import_header_artifact: str | None = None
    generated_declaration_name: str | None = None

    @model_validator(mode="after")
    def _fixture_contract(self) -> LF021OfflineSmokeFixture:
        if self.reference_name not in self.reference_statement:
            raise ValueError("reference_statement must declare reference_name")
        if PLACEHOLDER in self.reference_statement:
            raise ValueError("reference_statement must be proof-free")
        if not self.nl_source_link.startswith("repo://"):
            raise ValueError("offline public fixture source link must be repo://")
        parsed_generated = parse_direct_autoformalization_output(self.generated_response)
        if (
            self.generated_declaration_name is not None
            and parsed_generated.declaration_name != self.generated_declaration_name
        ):
            raise ValueError(
                "generated_response declaration differs from generated_declaration_name"
            )
        if self.schema_version == 1:
            if (
                self.fixture_id != "lf021_offline_public_identity_v1"
                or self.source != "lf021_offline_public_fixture"
                or self.project_registry_key is not None
                or self.import_header_artifact is not None
                or self.generated_declaration_name is not None
            ):
                raise ValueError("schema-v1 is reserved for the original offline fixture")
        else:
            supported_mathlib_fixtures = {
                (
                    "lf021_kimina_mathlib_nat_comm_20260723_v1",
                    "lf021_kimina_mathlib_public_fixture",
                ),
                (
                    "lf021_goedel_mathlib_nat_comm_20260723_v1",
                    "lf021_goedel_mathlib_public_fixture",
                ),
                (
                    "lf021_stepfun_mathlib_nat_comm_20260723_v1",
                    "lf021_stepfun_mathlib_public_fixture",
                ),
            }
            if (
                (self.fixture_id, self.source) not in supported_mathlib_fixtures
                or self.project_registry_key != "mathlib"
                or self.generated_declaration_name is None
                or self.import_header_artifact is None
            ):
                raise ValueError(
                    "schema-v2 mathlib fixture requires its exact project/header/name bindings"
                )
            header_path = PurePosixPath(self.import_header_artifact)
            if (
                header_path.is_absolute()
                or ".." in header_path.parts
                or not self.import_header_artifact.strip()
            ):
                raise ValueError("import_header_artifact must be repository-relative")
        return self

    @property
    def resolved_project_registry_key(self) -> Literal["fixtures", "mathlib"]:
        """Return the explicit v2 key or the sealed v1 fixture default."""

        return self.project_registry_key or "fixtures"

    @property
    def resolved_generated_declaration_name(self) -> str:
        """Return the exact expected generated declaration name."""

        if self.generated_declaration_name is not None:
            return self.generated_declaration_name
        return parse_direct_autoformalization_output(self.generated_response).declaration_name

    @property
    def resolved_import_header_artifact(self) -> str:
        """Return the exact header artifact without changing the v1 JSON."""

        return self.import_header_artifact or str(_DEFAULT_OFFLINE_HEADER)

    @property
    def authorization_adr(self) -> Literal["ADR-0005", "ADR-0006"]:
        """Bind each fixture generation path to its recorded authorization."""

        return "ADR-0005" if self.schema_version == 1 else "ADR-0006"


class LF021ParsedOutputRecord(StrictModel):
    """Persisted strict-parser result bound to one provider request."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    provider_request_hash: str = Field(pattern=_HEX64)
    call_id: str
    parser_version: Literal["direct_autoformalization_v1"] = _DIRECT_PARSER_VERSION
    declaration_kind: Literal["theorem", "lemma"]
    declaration_name: str
    statement: str
    statement_sha256: str = Field(pattern=_HEX64)


class LF021RepresentationFailureRecord(StrictModel):
    """JSONL-safe form of one representation failure dataclass."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    theorem_id: str
    view: str
    status: str
    detail: str


class LF021CollectionTerminalRecord(StrictModel):
    """One terminal operational outcome for one problem x family x seed."""

    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    terminal_id: str = Field(pattern=r"^collection_terminal:[0-9a-f]{64}$")
    problem_record_id: str
    provider_request_hash: str = Field(pattern=_HEX64)
    call_id: str
    attempt_id: str
    seed: int
    parse_status: ParseStatus
    terminal_status: Literal[
        "materialized_admitted",
        "materialized_smoke_only",
        "empty_response",
        "parse_failed",
        "validation_failed",
        "materialization_error",
    ]
    materializer_outcome_id: str | None = None
    candidate_theorem_id: str | None = None
    representation_id: str | None = None
    screening_id: str | None = None
    pair_ids: tuple[str, ...] = ()
    nl_lean_id: str | None = None
    semantic_pool_admitted: bool = False
    semantic_labels_created: Literal[0] = 0
    error_code: str | None = None
    error_detail: str | None = None

    @model_validator(mode="after")
    def _terminal_shape(self) -> LF021CollectionTerminalRecord:
        admitted = self.terminal_status == "materialized_admitted"
        smoke_only = self.terminal_status == "materialized_smoke_only"
        core_required = (
            self.materializer_outcome_id,
            self.candidate_theorem_id,
            self.representation_id,
            self.screening_id,
        )
        if admitted:
            if self.parse_status is not ParseStatus.PARSED or any(
                value is None for value in (*core_required, self.nl_lean_id)
            ):
                raise ValueError("materialized terminal record requires complete parsed lineage")
            if not self.pair_ids:
                raise ValueError("materialized terminal record requires admitted pair IDs")
            if not self.semantic_pool_admitted:
                raise ValueError("materialized terminal record must record semantic-pool admission")
            if self.error_code is not None or self.error_detail is not None:
                raise ValueError("materialized terminal record cannot carry an error")
        elif smoke_only:
            if self.parse_status is not ParseStatus.PARSED or any(
                value is None for value in core_required
            ):
                raise ValueError("smoke-only terminal record requires complete parsed lineage")
            if self.pair_ids or self.nl_lean_id is not None or self.semantic_pool_admitted:
                raise ValueError("smoke-only terminal record cannot enter semantic pools")
            if self.error_code is not None or self.error_detail is not None:
                raise ValueError("smoke-only terminal record cannot carry an error")
        else:
            if self.semantic_pool_admitted:
                raise ValueError("non-materialized terminal record cannot be admitted")
            if self.error_code is None or self.error_detail is None:
                raise ValueError("non-materialized terminal record requires error details")
        return self


class LF021OfflineSmokeReport(StrictModel):
    """Machine-checkable result of one fixture run plus byte replay."""

    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_offline_smoke_replay"] = "lf021_offline_smoke_replay"
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    network_calls_made: Literal[0] = 0
    semantic_labels_created: Literal[0] = 0
    run_id: str
    passed: bool
    problem_record_id: str
    provider_request_hash: str = Field(pattern=_HEX64)
    call_id: str
    attempt_id: str
    raw_response_sha256: str = Field(pattern=_HEX64)
    terminal: LF021CollectionTerminalRecord
    replay_checks: dict[str, bool]
    semantic_ids: dict[str, str]
    semantic_hashes: dict[str, str]
    artifact_hashes: dict[str, str]
    notes: tuple[str, ...]

    @model_validator(mode="after")
    def _report_consistency(self) -> LF021OfflineSmokeReport:
        if not self.replay_checks:
            raise ValueError("offline smoke report requires replay checks")
        expected = (
            all(self.replay_checks.values())
            and self.terminal.terminal_status == "materialized_smoke_only"
        )
        if self.passed != expected:
            raise ValueError("passed must match replay checks and terminal materialization")
        for mapping_name in ("semantic_hashes", "artifact_hashes"):
            mapping = getattr(self, mapping_name)
            if any(
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for value in mapping.values()
            ):
                raise ValueError(f"{mapping_name} values must be SHA-256 digests")
        return self


@dataclass(frozen=True, slots=True)
class LF021OfflineSmokeRun:
    """Paths and records returned by the ADR-0005 smoke runner."""

    output_dir: Path
    report_path: Path
    run_manifest_path: Path
    report: LF021OfflineSmokeReport
    materialization: RealOutputMaterializationResult | None
    screening: CandidateScreeningRecord | None


def _provider_slots(document: dict[str, object]) -> dict[str, dict[str, object]]:
    slots = document.get("slots")
    if not isinstance(slots, dict) or not slots:
        raise LF021FoundationError("provider registry must contain a nonempty slots mapping")
    validated: dict[str, dict[str, object]] = {}
    for name, value in slots.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise LF021FoundationError("provider slot names and values must be mappings")
        validated[name] = value
    return validated


def validate_lf021_foundation(
    paths: RepoPaths,
    *,
    generated_at: datetime.datetime | None = None,
) -> LF021FoundationValidation:
    """Validate that the checked-in LF-021 configuration fails closed.

    This function performs no writes and instantiates no provider runtime.
    """

    try:
        configs = load_generation_foundation_configs(paths)
    except ValueError as exc:
        raise LF021FoundationError(f"invalid LF-021 foundation config: {exc}") from exc
    provider_path = paths.configs / "generation" / "providers.yaml"
    provider_document = load_yaml_mapping(provider_path)
    slots = _provider_slots(provider_document)

    slot_statuses_disabled = all(
        str(slot.get("status", "")).startswith("disabled_") for slot in slots.values()
    )
    slot_pins_unresolved = all(
        slot.get("exact_model") is None and slot.get("revision") is None for slot in slots.values()
    )
    unresolved = provider_document.get("resolution_blockers")
    if (
        not isinstance(unresolved, list)
        or not unresolved
        or not all(isinstance(item, str) and item.strip() for item in unresolved)
    ):
        raise LF021FoundationError(
            "provider registry must retain explicit nonempty resolution_blockers"
        )

    problem = configs.problem_pool.config
    real = configs.real_outputs.config
    checks = {
        "problem_pool_disabled": problem.status == "disabled_until_phase_5_adr",
        "problem_pool_sources_disabled": not any(source.enabled for source in problem.sources),
        "private_source_external_transmission_false": (
            problem.private_source_external_transmission is False
            and real.safety.private_source_external_transmission is False
        ),
        "generation_disabled": (
            real.status == "disabled_until_phase_5_adr" and not real.generation_enabled
        ),
        "external_provider_calls_disabled": (not real.execution.external_provider_calls_enabled),
        "local_provider_calls_disabled": not real.execution.local_provider_calls_enabled,
        "replay_import_disabled": not real.execution.replay_import_enabled,
        "provider_slots_disabled": slot_statuses_disabled,
        "provider_pins_unresolved": slot_pins_unresolved,
        "semantic_labels_disabled": not real.safety.semantic_labels_created,
        "noncompiling_semantic_pool_disabled": (
            not real.safety.noncompiling_outputs_semantic_pool_eligible
        ),
        "four_family_full_track": (
            real.family_policy.full_track_successful_families >= 4
            and real.family_policy.supervision_eligible_families >= 3
            and real.family_policy.heldout_families >= 1
        ),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise LF021FoundationError(
            "checked-in LF-021 foundation is not fail-closed: " + ", ".join(failed)
        )

    report = LF021FoundationReport(
        generated_at=generated_at or datetime.datetime.now(tz=datetime.UTC),
        problem_pool_config_hash=configs.problem_pool.config_hash,
        real_outputs_config_hash=configs.real_outputs.config_hash,
        provider_registry_sha256=hash_file(provider_path),
        checks=checks,
        unresolved_requirements=tuple(str(item).strip() for item in unresolved),
    )
    return LF021FoundationValidation(configs=configs, report=report)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LF021FoundationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_offline_fixture(path: Path) -> LF021OfflineSmokeFixture:
    if path.is_symlink() or not path.is_file():
        raise LF021FoundationError(f"offline fixture is missing or not a regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        return LF021OfflineSmokeFixture.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF021FoundationError(f"invalid offline fixture {path}: {exc}") from exc


def _fixture_project_files(project_dir: Path) -> tuple[Path, ...]:
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
    files = tuple(sorted(path for path in candidates if path.is_file()))
    if not files:
        raise LF021FoundationError("fixture project has no context-defining files")
    return files


def _fixture_project_revision(project_dir: Path) -> str:
    entries = tuple(
        (str(path.relative_to(project_dir)), hash_file(path))
        for path in _fixture_project_files(project_dir)
    )
    return f"workspace:{hash_canonical(entries)}"


def _offline_context(
    paths: RepoPaths,
    *,
    project_dir: Path,
    imports_text: str,
    project_registry_key: str = "fixtures",
) -> ContextRecord:
    projects = load_project_registry(paths)
    project = projects.get(project_registry_key)
    if project is None:
        raise LF021FoundationError(f"project registry has no {project_registry_key!r} entry")
    if not project_dir.is_dir():
        raise LF021FoundationError(f"qualification project directory is missing: {project_dir}")
    expected = project.local_directory(paths)
    if expected is not None:
        if expected.resolve() != project_dir.resolve():
            raise LF021FoundationError(
                "offline smoke project differs from its local registry entry"
            )
        project_revision = _fixture_project_revision(project_dir)
    else:
        try:
            project_revision = check_project_revision(project, project_dir)
        except Exception as exc:
            raise LF021FoundationError(
                f"qualification project revision preflight failed: {exc}"
            ) from exc
    lock = load_environment_lock(paths)
    try:
        lean_version = check_project_toolchain(
            project,
            project_dir,
            lock.toolchain_lock,
        )
    except Exception as exc:
        raise LF021FoundationError(
            f"qualification project toolchain preflight failed: {exc}"
        ) from exc
    imports = tuple(
        module
        for line in imports_text.splitlines()
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
        project_uri=project.uri,
        project_revision=project_revision,
        imports=imports,
        header_text=imports_text,
    )
    return build_context_record(
        payload,
        project_kind=project.kind.value,
        project_registry_key=project.registry_key,
    )


def _qualification_fixture_header_path(
    fixture: LF021OfflineSmokeFixture,
    *,
    paths: RepoPaths,
) -> Path:
    """Resolve and verify the exact regular import-header artifact."""

    unresolved = paths.root / fixture.resolved_import_header_artifact
    if unresolved.is_symlink():
        raise LF021FoundationError(
            f"qualification fixture header must not be a symlink: {unresolved}"
        )
    path = unresolved.resolve()
    try:
        path.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise LF021FoundationError("qualification fixture header escapes the repository") from exc
    if not path.is_file():
        raise LF021FoundationError(
            f"qualification fixture header is missing or not regular: {path}"
        )
    try:
        observed = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LF021FoundationError(f"cannot read qualification fixture header: {path}") from exc
    if observed != fixture.imports:
        raise LF021FoundationError(
            "qualification fixture imports must exactly match its header artifact"
        )
    return path


def _offline_reference(
    *,
    fixture: LF021OfflineSmokeFixture,
    fixture_hash: str,
    context: ContextRecord,
) -> TheoremRecord:
    parsed = parse_direct_autoformalization_output(f"```lean4\n{fixture.reference_statement}\n```")
    if parsed.declaration_name != fixture.reference_name:
        raise LF021FoundationError("reference declaration name differs from fixture")
    ancestry_id = make_id(
        ANCESTRY_PREFIX,
        {
            "schema": "lf021_offline_reference_v1",
            "fixture_id": fixture.fixture_id,
            "statement_sha256": parsed.statement_sha256,
        },
    )
    theorem_id = make_id(
        THEOREM_PREFIX,
        {
            "schema": "lf021_offline_reference_v1",
            "fixture_id": fixture.fixture_id,
            "context_id": context.context_id,
            "statement_sha256": parsed.statement_sha256,
        },
    )
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source=fixture.source,
        source_revision=fixture.source_revision,
        source_split=fixture.source_split,
        source_record=fixture.source_record_id,
        context_id=context.context_id,
        declaration_kind=parsed.declaration_kind,
        declaration_name=parsed.declaration_name,
        declaration_full_name=parsed.declaration_name,
        proof_stripped_declaration=parsed.statement + PLACEHOLDER,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=parsed.statement_sha256,
        nl_source_link=fixture.nl_source_link,
        nl_trust=NLTrust.TRUSTED,
        metadata={
            "artifact_class": ArtifactClass.SMOKE.value,
            "fixture_hash": fixture_hash,
            "resolved_semantic_label": False,
        },
    )


def _offline_problem_config(
    *,
    fixture: LF021OfflineSmokeFixture,
    fixture_path: Path,
    paths: RepoPaths,
    denylist: ProblemPoolDenylistBinding,
) -> ProblemPoolConfig:
    source_config = str(fixture_path.resolve().relative_to(paths.root.resolve()))
    authorization = SourceAuthorizationConfig(
        source_revision=fixture.source_revision,
        license_id=fixture.source_license,
        private_source=False,
        external_transmission=True,
        release_eligible=False,
    )
    return ProblemPoolConfig(
        config_id="problem_pool_v1",
        status="ready",
        selection_seed=fixture.fixture_id,
        sources=(
            ProblemPoolSourceConfig(
                source=fixture.source,
                source_config=source_config,
                source_config_sha256=hash_file(fixture_path),
                authorization=authorization,
                enabled=True,
                private_source=False,
                external_provider_eligible=True,
                allowed_trust=(NLTrust.TRUSTED,),
                require_reference_theorem=True,
            ),
        ),
        active_benchmark_registry_manifest=(denylist.manifest_path),
        active_benchmark_registry_manifest_sha256=denylist.manifest_sha256,
        benchmark_preflight_required=True,
        normalized_nl_exact_dedup=True,
        near_duplicate=NearDuplicateConfig(
            status="frozen",
            method="supplied_group_ids",
            method_version="v1",
            threshold=1.0,
        ),
        private_source_external_transmission=False,
        public_replication_profile="configs/sources/public_replication.yaml",
        outputs=ProblemPoolOutputConfig(
            records="data/real_outputs/smoke/problem_pool.jsonl",
            failures="data/real_outputs/smoke/problem_pool_failures.jsonl",
            manifest="data/real_outputs/smoke/problem_pool_manifest.json",
            coverage_report="reports/generation/lf021_offline_smoke.md",
        ),
    )


def _offline_problem(
    *,
    fixture: LF021OfflineSmokeFixture,
    fixture_hash: str,
    fixture_path: Path,
    import_header_path: Path,
    paths: RepoPaths,
    context: ContextRecord,
    reference: TheoremRecord,
    denylist: ProblemPoolDenylistBinding,
) -> tuple[ProblemPoolRecord, PublicTrustedProblem, str]:
    config = _offline_problem_config(
        fixture=fixture,
        fixture_path=fixture_path,
        paths=paths,
        denylist=denylist,
    )
    candidate = ProblemPoolCandidate(
        problem_id=fixture.problem_id,
        problem_group=fixture.problem_group,
        source=fixture.source,
        source_revision=fixture.source_revision,
        source_split=fixture.source_split,
        source_record_id=fixture.source_record_id,
        source_record_content_hash=fixture_hash,
        nl_statement=fixture.nl_statement,
        nl_trust=NLTrust.TRUSTED,
        nl_source_link=fixture.nl_source_link,
        context_id=context.context_id,
        import_header_artifact=str(import_header_path.resolve().relative_to(paths.root.resolve())),
        import_header_hash=hash_file(import_header_path),
        reference_theorem_ids=(reference.theorem_id,),
        source_license=fixture.source_license,
        private_source_content=False,
        release_eligible=False,
        metadata={
            "artifact_class": ArtifactClass.SMOKE.value,
            "adr": fixture.authorization_adr,
            "fixture_id": fixture.fixture_id,
        },
    )
    built = build_problem_pool(
        config=config,
        denylist=denylist,
        candidates=(candidate,),
    )
    if len(built.records) != 1 or len(built.public_trusted_problems) != 1:
        reasons = built.records[0].exclusion_reasons if built.records else ()
        raise LF021FoundationError(
            f"{fixture.authorization_adr} fixture is not "
            "public/trusted/denylist-clean: " + ", ".join(reasons)
        )
    return (
        built.records[0],
        built.public_trusted_problems[0],
        hash_canonical(config.model_dump(mode="json")),
    )


def _write_new_jsonl(records: Sequence[StrictModel], path: Path) -> str:
    payload = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise FileExistsError(f"immutable smoke partition already exists: {path}") from exc
    return hash_file(path)


def _write_new_json(record: StrictModel, path: Path) -> str:
    return _write_new_jsonl((record,), path)


def _relative_artifact(path: Path, *, paths: RepoPaths) -> str:
    try:
        return str(path.resolve().relative_to(paths.root.resolve()))
    except ValueError as exc:
        raise LF021FoundationError(f"artifact must stay inside repository root: {path}") from exc


def _parse_provider_result(
    result: ProviderResult,
) -> tuple[ParsedLeanDeclaration | None, ParseStatus, str | None, str | None]:
    if result.response.status != "success":
        return (
            None,
            ParseStatus.EMPTY,
            result.response.error_type or "provider_error",
            result.response.error_detail or "provider returned an error",
        )
    output = result.response.output_text or ""
    try:
        return parse_direct_autoformalization_output(output), ParseStatus.PARSED, None, None
    except DirectOutputParseError as exc:
        status = ParseStatus.EMPTY if exc.code.value == "empty_output" else ParseStatus.PARSE_FAILED
        return None, status, exc.code.value, str(exc)


def _representation_failure_record(
    failure: RepresentationFailure,
) -> LF021RepresentationFailureRecord:
    return LF021RepresentationFailureRecord(
        theorem_id=failure.theorem_id,
        view=failure.view,
        status=failure.status,
        detail=failure.detail,
    )


def _materialization_payload(
    result: RealOutputMaterializationResult,
) -> dict[str, object]:
    return {
        "outcome": result.outcome.model_dump(mode="json"),
        "variant": result.variant.model_dump(mode="json"),
        "theorem": result.theorem.model_dump(mode="json") if result.theorem else None,
        "representation": (
            result.representation.model_dump(mode="json") if result.representation else None
        ),
        "representation_failures": [
            {
                "theorem_id": failure.theorem_id,
                "view": failure.view,
                "status": failure.status,
                "detail": failure.detail,
            }
            for failure in result.representation_failures
        ],
        "pairs": [pair.model_dump(mode="json") for pair in result.pairs],
        "nl_lean": result.nl_lean.model_dump(mode="json") if result.nl_lean else None,
    }


def _semantic_ids(result: RealOutputMaterializationResult) -> dict[str, str]:
    values = {
        "outcome_id": result.outcome.outcome_id,
        "variant_id": result.variant.variant_id,
    }
    if result.theorem is not None:
        values["candidate_theorem_id"] = result.theorem.theorem_id
    if result.representation is not None:
        values["representation_id"] = result.representation.representation_id
    for index, pair in enumerate(result.pairs):
        values[f"pair_id_{index}"] = pair.pair_id
    if result.nl_lean is not None:
        values["nl_lean_id"] = result.nl_lean.nl_lean_id
    return values


def _semantic_hashes(result: RealOutputMaterializationResult) -> dict[str, str]:
    hashes = {
        "materialization": hash_canonical(_materialization_payload(result)),
        "outcome": hash_canonical(result.outcome.model_dump(mode="json")),
        "variant": hash_canonical(result.variant.model_dump(mode="json")),
    }
    if result.theorem is not None:
        hashes["theorem_record"] = hash_canonical(result.theorem.model_dump(mode="json"))
        hashes["theorem_statement"] = result.theorem.statement_content_hash
    if result.representation is not None:
        hashes["representation_record"] = hash_canonical(
            result.representation.model_dump(mode="json")
        )
        hashes["representation_content"] = result.representation.content_hash
    hashes["pairs"] = hash_canonical([pair.model_dump(mode="json") for pair in result.pairs])
    hashes["nl_lean"] = hash_canonical(
        result.nl_lean.model_dump(mode="json") if result.nl_lean else None
    )
    return hashes


def _lock_smoke_materialization(
    result: RealOutputMaterializationResult,
) -> RealOutputMaterializationResult:
    """Make an operational smoke candidate unusable as scientific data.

    The offline smoke calls the real admission function as a dry run so the
    screen-before-admit contract is exercised.  It must not persist the
    admitted semantic-pool records, however.  Persist the pre-admission
    materialization with explicit smoke metadata and disabled transform-source
    eligibility instead.
    """

    variant = result.variant.model_copy(
        update={
            "metadata": {
                **result.variant.metadata,
                "artifact_class": ArtifactClass.SMOKE.value,
                "training_eligible": False,
            }
        }
    )
    theorem = (
        result.theorem.model_copy(
            update={
                "metadata": {
                    **result.theorem.metadata,
                    "artifact_class": ArtifactClass.SMOKE.value,
                    "transform_source_eligible": False,
                    "training_eligible": False,
                }
            }
        )
        if result.theorem is not None
        else None
    )
    return RealOutputMaterializationResult(
        outcome=result.outcome,
        variant=variant,
        theorem=theorem,
        representation=result.representation,
        representation_failures=result.representation_failures,
        pairs=(),
        nl_lean=None,
    )


def _terminal_record(
    *,
    problem: ProblemPoolRecord,
    request: ProviderRequest,
    lineage_call: LLMCallRecord,
    lineage_attempt: LLMAttemptRecord,
    parse_status: ParseStatus,
    materialization: RealOutputMaterializationResult | None,
    screening: CandidateScreeningRecord | None,
    error_code: str | None,
    error_detail: str | None,
    seed: int,
) -> LF021CollectionTerminalRecord:
    status: Literal[
        "materialized_admitted",
        "materialized_smoke_only",
        "empty_response",
        "parse_failed",
        "validation_failed",
        "materialization_error",
    ]
    if materialization is not None and (
        materialization.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED
    ):
        status = "materialized_admitted"
        error_code = None
        error_detail = None
    elif (
        materialization is not None
        and materialization.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
        and screening is not None
        and screening.status is CandidateScreeningStatus.CLEAN
    ):
        status = "materialized_smoke_only"
        error_code = None
        error_detail = None
    elif materialization is not None:
        status = "validation_failed"
        error_code = (
            materialization.outcome.failure_code.value
            if materialization.outcome.failure_code is not None
            else "validation_failed"
        )
        error_detail = materialization.outcome.failure_detail or "validation failed"
    elif parse_status is ParseStatus.EMPTY:
        status = "empty_response"
    elif parse_status is ParseStatus.PARSE_FAILED:
        status = "parse_failed"
    else:
        status = "materialization_error"
    terminal_id = "collection_terminal:" + hash_canonical(
        {
            "schema": "lf021_collection_terminal_v1",
            "problem_record_id": problem.problem_record_id,
            "provider_request_hash": request.request_hash,
            "call_id": lineage_call.call_id,
            "seed": seed,
        }
    )
    return LF021CollectionTerminalRecord(
        terminal_id=terminal_id,
        problem_record_id=problem.problem_record_id,
        provider_request_hash=request.request_hash,
        call_id=lineage_call.call_id,
        attempt_id=lineage_attempt.attempt_id,
        seed=seed,
        parse_status=parse_status,
        terminal_status=status,
        materializer_outcome_id=(
            materialization.outcome.outcome_id if materialization is not None else None
        ),
        candidate_theorem_id=(
            materialization.theorem.theorem_id
            if materialization is not None and materialization.theorem is not None
            else None
        ),
        representation_id=(
            materialization.representation.representation_id
            if materialization is not None and materialization.representation is not None
            else None
        ),
        screening_id=screening.screening_id if screening is not None else None,
        pair_ids=(
            tuple(pair.pair_id for pair in materialization.pairs)
            if materialization is not None
            else ()
        ),
        nl_lean_id=(
            materialization.nl_lean.nl_lean_id
            if materialization is not None and materialization.nl_lean is not None
            else None
        ),
        semantic_pool_admitted=bool(
            materialization is not None and materialization.outcome.semantic_pool_eligible
        ),
        error_code=error_code,
        error_detail=error_detail,
    )


def _consumed_input_hashes(
    *,
    paths: RepoPaths,
    fixture_path: Path,
    import_header_path: Path,
    template_path: Path,
    adr_path: Path,
    project_dir: Path,
    active_registry_paths: Sequence[Path],
) -> dict[str, str]:
    consumed = {
        fixture_path,
        import_header_path,
        template_path,
        adr_path,
        paths.configs / "environment.lock.yaml",
        paths.reports / "gates" / "lf_016_authorization.json",
        *tuple((paths.configs / "projects").glob("*.yaml")),
        *_fixture_project_files(project_dir),
        *active_registry_paths,
    }
    result: dict[str, str] = {}
    for path in sorted(consumed):
        if not path.is_file():
            raise LF021FoundationError(f"consumed smoke input is missing: {path}")
        try:
            key = _relative_artifact(path, paths=paths)
        except LF021FoundationError:
            # The frozen Gate-3 registry intentionally points to immutable,
            # content-addressed artifacts outside the checkout.  Preserve the
            # absolute locator and exact digest rather than pretending that
            # such an input is repository-local.
            key = f"external:{path.resolve()}"
        result[key] = hash_file(path)
    return result


def _materialize_verified_candidate(
    *,
    provider_result: ProviderResult,
    request: ProviderRequest,
    request_path: Path,
    artifact_root: Path,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    reference: TheoremRecord,
    imports: str,
    generation_config_hash: str,
    denylist: ProblemPoolDenylistBinding,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> tuple[
    ParsedLeanDeclaration | None,
    ParseStatus,
    LLMAttemptRecord,
    LLMCallRecord,
    RealOutputMaterializationResult | None,
    CandidateScreeningRecord | None,
    str | None,
    str | None,
]:
    parsed, parse_status, error_code, error_detail = _parse_provider_result(provider_result)
    lineage = bridge_provider_result_to_llm_lineage(
        request=request,
        result=provider_result,
        request_artifact_path=request_path,
        artifact_root=artifact_root,
        problem=problem,
        provider_slot=_OFFLINE_PROVIDER_SLOT,
        model_family=_OFFLINE_MODEL_FAMILY,
        prompt_template_id=DIRECT_AUTOFORMALIZATION_TEMPLATE_ID,
        prompt_template_version=DIRECT_AUTOFORMALIZATION_TEMPLATE_VERSION,
        execution_mode="replay",
        parse_status=parse_status,
        parsed_statement=parsed.statement if parsed is not None else None,
        started_at=created_at,
        completed_at=created_at,
        supervision_eligible=False,
        metadata={
            "artifact_class": ArtifactClass.SMOKE.value,
            "authorization_adr": "ADR-0005",
            "parser_version": _DIRECT_PARSER_VERSION,
            "network_calls_made": 0,
            "semantic_labels_created": 0,
        },
    )
    violations = check_llm_call_attempt_lineage(lineage.call, (lineage.attempt,))
    if violations:
        raise LF021FoundationError(
            "provider/LLM lineage validation failed: " + ", ".join(violations)
        )
    if parsed is None:
        return (
            None,
            parse_status,
            lineage.attempt,
            lineage.call,
            None,
            None,
            error_code,
            error_detail,
        )

    verified_response = verify_llm_call_artifacts(
        call=lineage.call,
        problem=problem,
        artifact_root=artifact_root,
    )
    reparsed = parse_direct_autoformalization_output(verified_response.output_text or "")
    if reparsed != parsed:
        raise LF021FoundationError(
            "strict parse of verified raw response differs from pre-lineage parse"
        )
    materialized = materialize_real_output_candidate(
        problem=problem,
        parsed=reparsed,
        call=lineage.call,
        raw_output_artifact=cast(str, lineage.call.raw_output_artifact),
        context=context,
        references=(reference,),
        imports=imports,
        backend=backend,
        generation_config_hash=generation_config_hash,
        created_at=created_at,
    )
    screening: CandidateScreeningRecord | None = None
    if materialized.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
        if materialized.theorem is None or materialized.representation is None:
            raise LF021FoundationError(
                "pending-screening materialization lacks theorem/representation"
            )
        screening = CandidateScreeningRecord.create(
            problem_record_id=problem.problem_record_id,
            call_id=lineage.call.call_id,
            theorem=materialized.theorem,
            representation=materialized.representation,
            frozen_registry_hash=denylist.registry_content_hash,
            benchmark_hits=candidate_benchmark_hits(
                denylist_index=denylist.index,
                theorem=materialized.theorem,
                representation=materialized.representation,
            ),
            duplicate_candidate_theorem_ids=(),
            created_at=created_at,
        )
        if screening.status is CandidateScreeningStatus.CLEAN:
            admitted_dry_run = admit_screened_real_output_candidate(
                materialized=materialized,
                screening=screening,
                problem=problem,
                references=(reference,),
                expected_frozen_registry_hash=denylist.registry_content_hash,
                created_at=created_at,
            )
            if (
                not admitted_dry_run.outcome.semantic_pool_eligible
                or not admitted_dry_run.pairs
                or admitted_dry_run.nl_lean is None
            ):
                raise LF021FoundationError(
                    "screen-before-admit dry run did not produce complete pool records"
                )
            materialized = _lock_smoke_materialization(materialized)
        else:
            error_code = "candidate_screening_rejected"
            error_detail = "candidate matched frozen benchmark signatures: " + ", ".join(
                screening.benchmark_hits
            )
    return (
        reparsed,
        parse_status,
        lineage.attempt,
        lineage.call,
        materialized,
        screening,
        error_code,
        error_detail,
    )


def run_lf021_offline_smoke(
    paths: RepoPaths,
    *,
    fixture_path: Path | None = None,
    template_path: Path | None = None,
    output_dir: Path | None = None,
    created_at: datetime.datetime | None = None,
    run_nonce: str | None = None,
    argv: tuple[str, ...] = (
        "leanfaith",
        "collect-real-outputs",
        "--run-offline-smoke",
    ),
    backend: LeanInteractBackend | None = None,
    code_state: CodeState | None = None,
) -> LF021OfflineSmokeRun:
    """Run the one-example ADR-0005 fixture, then replay it byte-for-byte."""

    created_at = require_utc(created_at or datetime.datetime.now(tz=datetime.UTC))
    run_id = new_run_id(created_at, nonce=run_nonce)
    fixture_path = (fixture_path or paths.root / _DEFAULT_OFFLINE_FIXTURE).resolve()
    import_header_path = (paths.root / _DEFAULT_OFFLINE_HEADER).resolve()
    template_path = (
        template_path or paths.root / DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE
    ).resolve()
    adr_path = (paths.root / _DEFAULT_OFFLINE_ADR).resolve()
    project_dir = (paths.root / "tests" / "lean_fixtures").resolve()
    destination = output_dir or paths.root / _DEFAULT_OFFLINE_OUTPUT / run_id
    if not destination.is_absolute():
        destination = paths.root / destination
    destination = destination.resolve()
    _relative_artifact(destination, paths=paths)
    if destination.exists():
        raise LF021FoundationError(
            f"offline smoke output is immutable and already exists: {destination}"
        )
    if run_manifest_path(paths, run_id).exists():
        raise LF021FoundationError(f"run manifest already exists for {run_id}")
    if not adr_path.is_file():
        raise LF021FoundationError("ADR-0005 authorization is missing")

    active = load_active_benchmark_registry(repo_root=paths.root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(
        active,
        repo_root=paths.root,
    )
    fixture = _load_offline_fixture(fixture_path)
    if (
        import_header_path.is_symlink()
        or not import_header_path.is_file()
        or import_header_path.read_text(encoding="utf-8") != fixture.imports
    ):
        raise LF021FoundationError(
            "offline fixture imports must exactly match its regular header artifact"
        )
    fixture_hash = hash_file(fixture_path)
    context = _offline_context(
        paths,
        project_dir=project_dir,
        imports_text=fixture.imports,
    )
    reference = _offline_reference(
        fixture=fixture,
        fixture_hash=fixture_hash,
        context=context,
    )
    problem, public_problem, problem_config_hash = _offline_problem(
        fixture=fixture,
        fixture_hash=fixture_hash,
        fixture_path=fixture_path,
        import_header_path=import_header_path,
        paths=paths,
        context=context,
        reference=reference,
        denylist=denylist,
    )
    prompt = render_direct_autoformalization_prompt(
        public_problem,
        template_path=template_path,
    )
    identity = ProviderIdentity(
        provider="leanfaith_fixture",
        model="lf021_offline_deterministic",
        revision=f"fixture-{fixture_hash}",
        transport="fixture",
    )
    decoding = {"temperature": 0.0, "seed": 0, "max_tokens": 256}
    request = create_provider_request_for_problem(
        identity=identity,
        problem=problem,
        prompt_template_hash=prompt.template_sha256,
        rendered_prompt=prompt.text,
        decoding=decoding,
    )
    generation_config_hash = hash_canonical(
        {
            "schema": "lf021_offline_generation_v1",
            "adr_sha256": hash_file(adr_path),
            "fixture_sha256": fixture_hash,
            "problem_pool_config_hash": problem_config_hash,
            "provider": identity.model_dump(mode="json"),
            "prompt_template_id": prompt.template_id,
            "prompt_template_version": prompt.template_version,
            "prompt_template_sha256": prompt.template_sha256,
            "prompt_render_sha256": prompt.render_sha256,
            "parser_version": _DIRECT_PARSER_VERSION,
            "decoding": decoding,
            "frozen_registry_hash": denylist.registry_content_hash,
        }
    )

    destination.mkdir(parents=True)
    request_path = destination / "requests" / "request.json"
    persist_provider_request(request, request_path)
    raw_root = destination / "raw" / "provider_responses"
    provider_result = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=raw_root,
        responses={request.request_hash: fixture.generated_response},
    ).generate(request)

    owned_backend = backend is None
    if backend is None:
        lock = load_environment_lock(paths)
        backend = LeanInteractBackend(
            BackendSettings(
                project_dir=project_dir,
                context_fingerprint=context.context_fingerprint,
                environment_schema_version=lock.environment_schema_version,
                raw_response_dir=destination / "raw" / "lean",
            )
        )
    assert backend is not None
    try:
        first = _materialize_verified_candidate(
            provider_result=provider_result,
            request=request,
            request_path=request_path,
            artifact_root=paths.root,
            problem=problem,
            context=context,
            reference=reference,
            imports=fixture.imports,
            generation_config_hash=generation_config_hash,
            denylist=denylist,
            backend=backend,
            created_at=created_at,
        )
        replay_result = ReplayProvider(
            identity=identity.model_copy(update={"transport": "replay"}),
            raw_response_root=raw_root,
        ).generate(request)
        replay = _materialize_verified_candidate(
            provider_result=replay_result,
            request=request,
            request_path=request_path,
            artifact_root=paths.root,
            problem=problem,
            context=context,
            reference=reference,
            imports=fixture.imports,
            generation_config_hash=generation_config_hash,
            denylist=denylist,
            backend=backend,
            created_at=created_at,
        )
    finally:
        if owned_backend:
            backend.close()

    (
        parsed,
        parse_status,
        attempt,
        call,
        materialization,
        screening,
        error_code,
        error_detail,
    ) = first
    (
        replay_parsed,
        replay_parse_status,
        replay_attempt,
        replay_call,
        replay_materialization,
        replay_screening,
        _replay_error_code,
        _replay_error_detail,
    ) = replay
    terminal = _terminal_record(
        problem=problem,
        request=request,
        lineage_call=call,
        lineage_attempt=attempt,
        parse_status=parse_status,
        materialization=materialization,
        screening=screening,
        error_code=error_code,
        error_detail=error_detail,
        seed=0,
    )

    replay_checks = {
        "provider_marked_replayed": replay_result.replayed,
        "raw_response_bytes": (
            provider_result.raw_response_sha256 == replay_result.raw_response_sha256
        ),
        "strict_parse": parsed == replay_parsed and parse_status == replay_parse_status,
        "attempt_record": attempt == replay_attempt,
        "call_record": call == replay_call,
        "screening_record": screening == replay_screening,
        "materialization_record": (
            _materialization_payload(materialization) if materialization is not None else None
        )
        == (
            _materialization_payload(replay_materialization)
            if replay_materialization is not None
            else None
        ),
        "semantic_ids": (_semantic_ids(materialization) if materialization is not None else {})
        == (_semantic_ids(replay_materialization) if replay_materialization is not None else {}),
        "semantic_hashes": (
            _semantic_hashes(materialization) if materialization is not None else {}
        )
        == (_semantic_hashes(replay_materialization) if replay_materialization is not None else {}),
    }

    artifact_paths: dict[str, Path] = {}

    def persist(name: str, records: Sequence[StrictModel]) -> None:
        path = destination / f"{name}.jsonl"
        _write_new_jsonl(records, path)
        artifact_paths[name] = path

    persist("contexts", (context,))
    persist("reference_theorems", (reference,))
    persist("problem_pool", (problem,))
    persist("llm_attempts", (attempt,))
    persist("llm_calls", (call,))
    persist(
        "parsed_outputs",
        (
            (
                LF021ParsedOutputRecord(
                    provider_request_hash=request.request_hash,
                    call_id=call.call_id,
                    parser_version=_DIRECT_PARSER_VERSION,
                    declaration_kind=parsed.declaration_kind,
                    declaration_name=parsed.declaration_name,
                    statement=parsed.statement,
                    statement_sha256=parsed.statement_sha256,
                ),
            )
            if parsed is not None
            else ()
        ),
    )
    persist("candidate_screenings", (screening,) if screening is not None else ())
    persist(
        "variants",
        (materialization.variant,) if materialization is not None else (),
    )
    persist(
        "theorems",
        (
            (materialization.theorem,)
            if materialization is not None and materialization.theorem is not None
            else ()
        ),
    )
    persist(
        "representations",
        (
            (materialization.representation,)
            if materialization is not None and materialization.representation is not None
            else ()
        ),
    )
    persist(
        "representation_failures",
        (
            tuple(
                _representation_failure_record(failure)
                for failure in materialization.representation_failures
            )
            if materialization is not None
            else ()
        ),
    )
    persist(
        "pairs",
        materialization.pairs if materialization is not None else (),
    )
    persist(
        "nl_lean",
        (
            (materialization.nl_lean,)
            if materialization is not None and materialization.nl_lean is not None
            else ()
        ),
    )
    persist(
        "materialization_outcomes",
        (materialization.outcome,) if materialization is not None else (),
    )
    persist("collection_terminals", (terminal,))
    artifact_paths["provider_request"] = request_path
    artifact_paths["provider_raw_response"] = provider_result.raw_response_path
    for index, lean_raw_path in enumerate(sorted((destination / "raw" / "lean").rglob("*.json"))):
        artifact_paths[f"lean_raw_response_{index}"] = lean_raw_path
    artifact_hashes = {
        _relative_artifact(path, paths=paths): hash_file(path) for path in artifact_paths.values()
    }
    semantic_ids = _semantic_ids(materialization) if materialization is not None else {}
    semantic_hashes = _semantic_hashes(materialization) if materialization is not None else {}
    if screening is not None:
        semantic_ids["screening_id"] = screening.screening_id
        semantic_hashes["screening_record"] = hash_canonical(screening.model_dump(mode="json"))
    report = LF021OfflineSmokeReport(
        run_id=run_id,
        passed=(
            all(replay_checks.values()) and terminal.terminal_status == "materialized_smoke_only"
        ),
        problem_record_id=problem.problem_record_id,
        provider_request_hash=request.request_hash,
        call_id=call.call_id,
        attempt_id=attempt.attempt_id,
        raw_response_sha256=provider_result.raw_response_sha256,
        terminal=terminal,
        replay_checks=replay_checks,
        semantic_ids=semantic_ids,
        semantic_hashes=semantic_hashes,
        artifact_hashes=artifact_hashes,
        notes=(
            "ADR-0005 one-example offline smoke; no network request was made.",
            "The strict direct_autoformalization_v1 proof-free single-fence parser "
            "was used; no local-model adapter was activated.",
            "Candidate screening used the active frozen benchmark registry; the "
            "admission path was dry-run and no semantic-pool record was persisted.",
            "Artifact is permanently ineligible for Gate 5, training, calibration, "
            "model selection, scientific tables, and release.",
        ),
    )
    report_path = destination / "report.json"
    report_sha256 = _write_new_json(report, report_path)

    active_registry_paths = (
        active.manifest_path,
        active.base_registry_path,
        active.active_registry_path,
        active.detailed_index_path,
        active.input_manifest_path,
        active.code_bundle_path,
    )
    input_hashes = _consumed_input_hashes(
        paths=paths,
        fixture_path=fixture_path,
        import_header_path=import_header_path,
        template_path=template_path,
        adr_path=adr_path,
        project_dir=project_dir,
        active_registry_paths=active_registry_paths,
    )
    output_hashes = dict(artifact_hashes)
    output_hashes[_relative_artifact(report_path, paths=paths)] = report_sha256
    code = code_state or collect_code_state(paths.root)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.SMOKE,
        command=" ".join(argv),
        argv=argv,
        code=code,
        environment_schema_version=context.environment_schema_version,
        environment={
            "context_id": context.context_id,
            "lean_version": context.lean_version,
            "lean_interact_version": context.lean_interact_version,
        },
        config_hashes={
            "offline_problem_pool": problem_config_hash,
            "offline_generation": generation_config_hash,
        },
        input_hashes=input_hashes,
        output_hashes=output_hashes,
        seeds={"generation": 0},
        execution={
            "mode": "offline_fixture_replay",
            "network_calls_made": 0,
            "provider_invocations": 2,
            "fixture_provider_invocations": 1,
            "replay_provider_invocations": 1,
            "semantic_labels_created": 0,
            "candidate_screening": True,
            "candidate_admission_dry_run": True,
            "replay_verified": all(replay_checks.values()),
        },
        revisions={
            "fixture": fixture_hash,
            "provider": identity.revision,
            "project": context.project_revision,
            "benchmark_registry": denylist.registry_content_hash,
        },
        status_counts={
            terminal.terminal_status: 1,
            "network_provider_calls_made": 0,
            "provider_invocations": 2,
            "fixture_provider_invocations": 1,
            "replay_provider_invocations": 1,
            "semantic_labels_created": 0,
            "candidate_screenings": int(screening is not None),
            "candidate_admission_dry_runs": 2,
            "semantic_pool_admissions": int(terminal.semantic_pool_admitted),
        },
        created_at=created_at,
        notes=(
            "ADR-0005 smoke-only fixture and byte replay. Gate 5/Gate 5G remain "
            "open; no artifact is eligible for scientific use."
        ),
    )
    manifest_path = run_manifest_path(paths, run_id)
    write_manifest(manifest, manifest_path)
    return LF021OfflineSmokeRun(
        output_dir=destination,
        report_path=report_path,
        run_manifest_path=manifest_path,
        report=report,
        materialization=materialization,
        screening=screening,
    )


__all__ = [
    "LF021FoundationError",
    "LF021FoundationReport",
    "LF021FoundationValidation",
    "LF021OfflineSmokeFixture",
    "LF021OfflineSmokeReport",
    "LF021OfflineSmokeRun",
    "run_lf021_offline_smoke",
    "validate_lf021_foundation",
]

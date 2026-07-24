"""Strict LF-021 problem-pool and real-output collection configurations.

These models intentionally contain no provider client. The checked-in v1
configs are fail-closed: generation, local execution, replay import, and every
external provider call remain disabled until a later frozen Phase-5 ADR.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import HEX64_PATTERN


def _check_repo_relative_path(value: str, *, field_name: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty repository-relative path")


class SourceAuthorizationConfig(StrictModel):
    """Source-owned authorization facts copied into the pool config.

    A ready pool is accepted only when this object is byte-hash bound to an
    identical ``lf021_authorization`` block in the referenced source config.
    """

    source_revision: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    private_source: bool
    external_transmission: bool
    release_eligible: bool

    @model_validator(mode="after")
    def _privacy_is_fail_closed(self) -> SourceAuthorizationConfig:
        for field_name in ("source_revision", "license_id"):
            value = getattr(self, field_name)
            if not value.strip() or "\x00" in value:
                raise ValueError(f"{field_name} must contain non-whitespace text without NUL")
        if self.private_source and (self.external_transmission or self.release_eligible):
            raise ValueError("private source authorization cannot permit transmission or release")
        return self


class ProblemPoolSourceConfig(StrictModel):
    source: str = Field(min_length=1)
    source_config: str = Field(min_length=1)
    source_config_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    authorization: SourceAuthorizationConfig | None = None
    enabled: bool
    private_source: bool
    external_provider_eligible: bool
    allowed_trust: tuple[NLTrust, ...] = ()
    require_reference_theorem: Literal[True] = True

    @model_validator(mode="after")
    def _checks(self) -> ProblemPoolSourceConfig:
        _check_repo_relative_path(self.source_config, field_name="source_config")
        if len(self.allowed_trust) != len(set(self.allowed_trust)):
            raise ValueError("allowed_trust must be unique")
        if self.enabled and not self.allowed_trust:
            raise ValueError("enabled problem-pool sources require allowed_trust")
        if self.enabled and (self.source_config_sha256 is None or self.authorization is None):
            raise ValueError("enabled problem-pool sources require a hash-bound authorization")
        if self.authorization is not None:
            if self.private_source != self.authorization.private_source:
                raise ValueError("source private_source must match its authorization record")
            if self.external_provider_eligible and not self.authorization.external_transmission:
                raise ValueError("external-provider eligibility requires source authorization")
        if self.private_source and self.external_provider_eligible:
            raise ValueError("private problem-pool sources cannot be external-provider eligible")
        return self


class NearDuplicateConfig(StrictModel):
    status: Literal["disabled_until_method_frozen", "frozen"]
    method: Literal["supplied_group_ids"] | None = None
    method_version: str | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _method_is_complete(self) -> NearDuplicateConfig:
        values = (self.method, self.method_version, self.threshold)
        if self.status == "frozen" and any(value is None for value in values):
            raise ValueError("frozen near-duplicate policy requires method/version/threshold")
        if self.status == "frozen" and (
            self.method != "supplied_group_ids"
            or self.method_version != "v1"
            or self.threshold != 1.0
        ):
            raise ValueError(
                "problem_pool_v1 implements only supplied_group_ids/v1 with threshold 1.0"
            )
        if self.status != "frozen" and any(value is not None for value in values):
            raise ValueError("disabled near-duplicate policy cannot carry a partial method")
        return self


class ProblemPoolOutputConfig(StrictModel):
    records: str
    failures: str
    manifest: str
    coverage_report: str

    @model_validator(mode="after")
    def _relative_paths(self) -> ProblemPoolOutputConfig:
        for field_name in ("records", "failures", "manifest", "coverage_report"):
            _check_repo_relative_path(getattr(self, field_name), field_name=field_name)
        return self


class ProblemPoolConfig(StrictModel):
    schema_version: Literal[1] = 1
    config_id: Literal["problem_pool_v1"]
    status: Literal["disabled_until_phase_5_adr", "ready"]
    selection_seed: str = Field(min_length=1)
    sources: tuple[ProblemPoolSourceConfig, ...] = Field(min_length=1)
    active_benchmark_registry_manifest: str
    active_benchmark_registry_manifest_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    benchmark_preflight_required: Literal[True] = True
    normalized_nl_exact_dedup: Literal[True] = True
    near_duplicate: NearDuplicateConfig
    private_source_external_transmission: Literal[False] = False
    public_replication_profile: str
    outputs: ProblemPoolOutputConfig

    @model_validator(mode="after")
    def _checks(self) -> ProblemPoolConfig:
        for field_name in (
            "active_benchmark_registry_manifest",
            "public_replication_profile",
        ):
            _check_repo_relative_path(getattr(self, field_name), field_name=field_name)
        source_names = [source.source for source in self.sources]
        if len(source_names) != len(set(source_names)):
            raise ValueError("problem-pool source names must be unique")
        if self.status == "ready":
            if not any(source.enabled for source in self.sources):
                raise ValueError("ready problem-pool config requires an enabled source")
            if self.active_benchmark_registry_manifest_sha256 is None:
                raise ValueError(
                    "ready problem-pool config requires a pinned benchmark manifest hash"
                )
            if self.near_duplicate.status != "frozen":
                raise ValueError(
                    "ready problem-pool config requires a frozen near-duplicate method"
                )
        elif any(source.enabled for source in self.sources):
            raise ValueError("disabled problem-pool config cannot enable sources")
        return self


class ProviderExecutionConfig(StrictModel):
    external_provider_calls_enabled: bool
    local_provider_calls_enabled: bool
    replay_import_enabled: bool
    allowed_provider_slots: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _slots_are_unique(self) -> ProviderExecutionConfig:
        if len(self.allowed_provider_slots) != len(set(self.allowed_provider_slots)):
            raise ValueError("allowed_provider_slots must be unique")
        if self.allowed_provider_slots and not (
            self.external_provider_calls_enabled
            or self.local_provider_calls_enabled
            or self.replay_import_enabled
        ):
            raise ValueError("allowed provider slots require an enabled execution mode")
        return self


ProviderTransport = Literal["external", "local", "replay"]
ProviderSlotKind = Literal["generator", "judge"]


class ProviderSlotConfig(StrictModel):
    """Typed authorization-relevant projection of one provider slot."""

    role: str = Field(min_length=1)
    family_constraint: str = Field(min_length=1)
    exact_model: str | None = Field(default=None, min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    api_key_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    status: Literal["disabled_until_phase_5_adr", "enabled"]
    family: str | None = Field(default=None, min_length=1)
    transport: ProviderTransport | None = None
    slot_kind: ProviderSlotKind | None = None
    allowed_sources: tuple[str, ...] = ()
    unresolved: str | None = None
    candidate_model: str | None = None
    fallback_model: str | None = None
    license: str | None = None
    base_family: str | None = None
    decision: str | None = None
    overlap_rule: str | None = None

    @model_validator(mode="after")
    def _enabled_slot_is_fully_pinned(self) -> ProviderSlotConfig:
        if list(self.allowed_sources) != sorted(set(self.allowed_sources)):
            raise ValueError("provider allowed_sources must be sorted and unique")
        if any(not source.strip() or "\x00" in source for source in self.allowed_sources):
            raise ValueError("provider allowed_sources must contain nonempty source names")
        if self.status == "enabled":
            required = {
                "exact_model": self.exact_model,
                "revision": self.revision,
                "family": self.family,
                "transport": self.transport,
                "slot_kind": self.slot_kind,
            }
            missing = sorted(name for name, value in required.items() if value is None)
            if missing:
                raise ValueError("enabled provider slots require: " + ", ".join(missing))
            if self.slot_kind == "generator" and not self.allowed_sources:
                raise ValueError("enabled generator slots require an explicit allowed_sources list")
            if self.unresolved is not None:
                raise ValueError("enabled provider slots cannot retain unresolved status")
            if self.transport == "external" and self.api_key_env is None:
                raise ValueError("enabled external provider slots require api_key_env")
        return self


class ProviderRegistryConfig(StrictModel):
    """Strict checked-in provider registry used by ready-config preflight."""

    config_version: Literal["providers_v1"]
    plan_sections: tuple[str, ...]
    status: Literal[
        "external_slots_disabled_until_phase_5_adr",
        "ready",
    ]
    rules: dict[str, str]
    api_key_env_convention: str = Field(min_length=1)
    slots: dict[str, ProviderSlotConfig]
    resolution_blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _registry_shape(self) -> ProviderRegistryConfig:
        if not self.slots:
            raise ValueError("provider registry requires at least one slot")
        if any(not name.strip() for name in self.slots):
            raise ValueError("provider slot names must be nonempty")
        if self.status == "external_slots_disabled_until_phase_5_adr":
            if any(slot.status == "enabled" for slot in self.slots.values()):
                raise ValueError("disabled provider registry cannot enable slots")
            if not self.resolution_blockers:
                raise ValueError("disabled provider registry requires resolution_blockers")
        else:
            if not any(slot.status == "enabled" for slot in self.slots.values()):
                raise ValueError("ready provider registry requires an enabled slot")
            if self.resolution_blockers:
                raise ValueError("ready provider registry cannot retain resolution blockers")
        return self


class RealOutputPromptConfig(StrictModel):
    status: Literal["disabled", "frozen"]
    template_artifact: str | None = Field(default=None, min_length=1)
    template_version: Literal["v1"] | None = None
    template_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    parser_version: Literal["direct_autoformalization_v1"] | None = None
    strict_machine_parse: Literal[True] = True

    @model_validator(mode="after")
    def _checks(self) -> RealOutputPromptConfig:
        values = (
            self.template_artifact,
            self.template_version,
            self.template_sha256,
            self.parser_version,
        )
        if self.status == "frozen":
            if any(value is None for value in values):
                raise ValueError("frozen prompt config requires template/version/parser")
            assert self.template_artifact is not None
            _check_repo_relative_path(
                self.template_artifact,
                field_name="template_artifact",
            )
        elif any(value is not None for value in values):
            raise ValueError("disabled prompt config cannot carry partial prompt settings")
        return self


class RealOutputRetryConfig(StrictModel):
    max_attempts: int = Field(ge=1, strict=True)
    retry_statuses: tuple[
        Literal["empty_response", "timeout", "provider_error", "infrastructure_error"],
        ...,
    ]
    append_only_attempt_artifacts: Literal[True] = True

    @model_validator(mode="after")
    def _unique_statuses(self) -> RealOutputRetryConfig:
        if len(self.retry_statuses) != len(set(self.retry_statuses)):
            raise ValueError("retry_statuses must be unique")
        return self


class GeneratorFamilyPolicy(StrictModel):
    full_track_successful_families: int = Field(ge=1, strict=True)
    supervision_eligible_families: int = Field(ge=1, strict=True)
    heldout_families: int = Field(ge=0, strict=True)
    heldout_family: str | None = None

    @model_validator(mode="after")
    def _family_arithmetic(self) -> GeneratorFamilyPolicy:
        required = self.supervision_eligible_families + self.heldout_families
        if self.full_track_successful_families < required:
            raise ValueError("full-track family count must cover supervision and held-out families")
        if self.heldout_family is not None and self.heldout_families == 0:
            raise ValueError("heldout_family requires heldout_families > 0")
        return self


class RealOutputPaths(StrictModel):
    raw: str
    parsed: str
    validated: str
    manifest: str

    @model_validator(mode="after")
    def _relative_paths(self) -> RealOutputPaths:
        for field_name in ("raw", "parsed", "validated", "manifest"):
            _check_repo_relative_path(getattr(self, field_name), field_name=field_name)
        return self


class RealOutputSafetyConfig(StrictModel):
    private_source_external_transmission: Literal[False] = False
    failed_attempts_retained: Literal[True] = True
    noncompiling_outputs_semantic_pool_eligible: Literal[False] = False
    semantic_labels_created: Literal[False] = False


class RealOutputsConfig(StrictModel):
    schema_version: Literal[1] = 1
    config_id: Literal["real_outputs_v1"]
    status: Literal["disabled_until_phase_5_adr", "ready"]
    problem_pool_config: str
    provider_registry: str
    generation_enabled: bool
    execution: ProviderExecutionConfig
    prompt: RealOutputPromptConfig
    retry: RealOutputRetryConfig
    family_policy: GeneratorFamilyPolicy
    safety: RealOutputSafetyConfig
    outputs: RealOutputPaths

    @model_validator(mode="after")
    def _checks(self) -> RealOutputsConfig:
        _check_repo_relative_path(self.problem_pool_config, field_name="problem_pool_config")
        _check_repo_relative_path(self.provider_registry, field_name="provider_registry")
        any_mode = (
            self.execution.external_provider_calls_enabled
            or self.execution.local_provider_calls_enabled
            or self.execution.replay_import_enabled
        )
        if self.status == "disabled_until_phase_5_adr":
            if self.generation_enabled or any_mode or self.execution.allowed_provider_slots:
                raise ValueError("disabled real-output config must fail closed")
            if self.prompt.status != "disabled":
                raise ValueError("disabled real-output config cannot freeze an active prompt")
        else:
            if not self.generation_enabled or not any_mode:
                raise ValueError("ready real-output config requires an enabled execution mode")
            if not self.execution.allowed_provider_slots:
                raise ValueError("ready real-output config requires allowed provider slots")
            if self.prompt.status != "frozen":
                raise ValueError("ready real-output config requires a frozen prompt")
            if (
                self.family_policy.heldout_families > 0
                and self.family_policy.heldout_family is None
            ):
                raise ValueError("ready real-output config must bind its held-out family")
        return self


@dataclass(frozen=True, slots=True)
class GenerationFoundationConfigs:
    problem_pool: LoadedConfig[ProblemPoolConfig]
    real_outputs: LoadedConfig[RealOutputsConfig]
    provider_registry: LoadedConfig[ProviderRegistryConfig]


def load_problem_pool_config(path: Path) -> LoadedConfig[ProblemPoolConfig]:
    return load_config(path, ProblemPoolConfig)


def load_real_outputs_config(path: Path) -> LoadedConfig[RealOutputsConfig]:
    return load_config(path, RealOutputsConfig)


def load_provider_registry_config(
    path: Path,
) -> LoadedConfig[ProviderRegistryConfig]:
    return load_config(path, ProviderRegistryConfig)


def _validate_ready_source_authorizations(
    paths: RepoPaths,
    problem: ProblemPoolConfig,
) -> None:
    for source in problem.sources:
        source_path = paths.root / source.source_config
        if not source_path.is_file():
            raise ValueError(f"problem-pool source config does not exist: {source_path}")
        if not source.enabled:
            continue
        assert source.source_config_sha256 is not None
        assert source.authorization is not None
        observed_hash = hash_file(source_path)
        if observed_hash != source.source_config_sha256:
            raise ValueError(
                f"source config SHA-256 mismatch for {source.source!r}: "
                f"expected {source.source_config_sha256}, got {observed_hash}"
            )
        raw = load_yaml_mapping(source_path)
        if raw.get("source") != source.source:
            raise ValueError(f"source config identity mismatch for {source.source!r}")
        raw_authorization = raw.get("lf021_authorization")
        if not isinstance(raw_authorization, dict):
            raise ValueError(
                f"enabled source {source.source!r} requires a typed "
                "lf021_authorization block in its source config"
            )
        observed_authorization = SourceAuthorizationConfig.model_validate(raw_authorization)
        if observed_authorization != source.authorization:
            raise ValueError(f"source authorization mismatch for {source.source!r}")
        probe = raw.get("probe")
        if not isinstance(probe, dict) or (
            probe.get("resolved_revision") != source.authorization.source_revision
        ):
            raise ValueError(f"source revision mismatch for {source.source!r}")


def _validate_ready_provider_bindings(
    problem: ProblemPoolConfig,
    real: RealOutputsConfig,
    registry: ProviderRegistryConfig,
) -> None:
    if registry.status != "ready":
        raise ValueError("ready real-output config requires provider registry status=ready")
    enabled_sources = {source.source: source for source in problem.sources if source.enabled}
    allowed_slots = set(real.execution.allowed_provider_slots)
    missing_slots = sorted(allowed_slots - set(registry.slots))
    if missing_slots:
        raise ValueError(
            "real-output config names unknown provider slots: " + ", ".join(missing_slots)
        )

    generators: list[ProviderSlotConfig] = []
    for name in sorted(allowed_slots):
        slot = registry.slots[name]
        if slot.status != "enabled":
            raise ValueError(f"allowed provider slot {name!r} is not enabled")
        if slot.transport == "external":
            mode_enabled = real.execution.external_provider_calls_enabled
        elif slot.transport == "local":
            mode_enabled = real.execution.local_provider_calls_enabled
        else:
            mode_enabled = real.execution.replay_import_enabled
        if not mode_enabled:
            raise ValueError(f"provider slot {name!r} uses disabled transport {slot.transport!r}")
        unknown_sources = sorted(set(slot.allowed_sources) - set(enabled_sources))
        if unknown_sources:
            raise ValueError(
                f"provider slot {name!r} authorizes disabled/unknown sources: "
                + ", ".join(unknown_sources)
            )
        for source_name in slot.allowed_sources:
            source = enabled_sources[source_name]
            assert source.authorization is not None
            if slot.transport == "external" and not source.authorization.external_transmission:
                raise ValueError(f"external slot {name!r} cannot receive source {source_name!r}")
        if slot.slot_kind == "generator":
            generators.append(slot)

    if not generators:
        raise ValueError("ready real-output config requires an enabled generator slot")
    uncovered_sources = sorted(
        source_name
        for source_name in enabled_sources
        if not any(source_name in slot.allowed_sources for slot in generators)
    )
    if uncovered_sources:
        raise ValueError(
            "enabled problem sources have no authorized generator: " + ", ".join(uncovered_sources)
        )
    heldout_family = real.family_policy.heldout_family
    if heldout_family is not None and not any(slot.family == heldout_family for slot in generators):
        raise ValueError(f"held-out family {heldout_family!r} is not bound to an allowed generator")


def load_generation_foundation_configs(
    root: Path | RepoPaths,
) -> GenerationFoundationConfigs:
    """Load and cross-check the checked-in fail-closed LF-021 configs."""

    paths = root if isinstance(root, RepoPaths) else RepoPaths(root=root)
    problem_path = paths.configs / "generation" / "problem_pool_v1.yaml"
    real_path = paths.configs / "generation" / "real_outputs_v1.yaml"
    problem = load_problem_pool_config(problem_path)
    real_outputs = load_real_outputs_config(real_path)
    expected_problem_path = str(problem_path.relative_to(paths.root))
    if real_outputs.config.problem_pool_config != expected_problem_path:
        raise ValueError(
            "real_outputs_v1 problem_pool_config does not reference the loaded "
            "problem_pool_v1 config"
        )
    provider_path = paths.root / real_outputs.config.provider_registry
    if not provider_path.is_file():
        raise ValueError(f"provider registry does not exist: {provider_path}")
    provider_registry = load_provider_registry_config(provider_path)
    active_registry = paths.root / problem.config.active_benchmark_registry_manifest
    if not active_registry.is_file():
        raise ValueError(f"active benchmark registry manifest does not exist: {active_registry}")
    if problem.config.active_benchmark_registry_manifest_sha256 is not None:
        observed_manifest_hash = hash_file(active_registry)
        if observed_manifest_hash != problem.config.active_benchmark_registry_manifest_sha256:
            raise ValueError(
                "active benchmark registry manifest SHA-256 mismatch: "
                f"expected {problem.config.active_benchmark_registry_manifest_sha256}, "
                f"got {observed_manifest_hash}"
            )
    public_profile = paths.root / problem.config.public_replication_profile
    if not public_profile.is_file():
        raise ValueError(f"public replication profile does not exist: {public_profile}")
    for source in problem.config.sources:
        source_config = paths.root / source.source_config
        if not source_config.is_file():
            raise ValueError(f"problem-pool source config does not exist: {source_config}")
    if problem.config.status == "ready":
        _validate_ready_source_authorizations(paths, problem.config)
    if real_outputs.config.status == "ready":
        if problem.config.status != "ready":
            raise ValueError("ready real-output config requires problem-pool status=ready")
        assert real_outputs.config.prompt.template_artifact is not None
        assert real_outputs.config.prompt.template_sha256 is not None
        template_path = paths.root / real_outputs.config.prompt.template_artifact
        if not template_path.is_file():
            raise ValueError(f"frozen prompt artifact does not exist: {template_path}")
        observed_prompt_hash = hash_file(template_path)
        if observed_prompt_hash != real_outputs.config.prompt.template_sha256:
            raise ValueError(
                "frozen prompt SHA-256 mismatch: "
                f"expected {real_outputs.config.prompt.template_sha256}, "
                f"got {observed_prompt_hash}"
            )
        _validate_ready_provider_bindings(
            problem.config,
            real_outputs.config,
            provider_registry.config,
        )
    return GenerationFoundationConfigs(
        problem_pool=problem,
        real_outputs=real_outputs,
        provider_registry=provider_registry,
    )

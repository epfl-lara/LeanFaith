"""Strict SFT2A configuration and persisted schemas."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from leanfaith.config.models import StrictModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmpty = Annotated[str, Field(min_length=1, strict=True)]
Polarity = Literal["preserving", "breaking"]
Verdict = Literal["equivalent", "non_equivalent", "unknown"]

_FORBIDDEN_SIGNATURE_WORD = re.compile(
    r"(?<![\w'])\b(?:theorem|lemma|def|example|axiom|opaque|by|sorry|admit)\b(?![\w'])"
)
_PROOF_DELIMITER = re.compile(r":=\s*(?:by\b|sorry\b|admit\b)")


class ArtifactBinding(StrictModel):
    path: NonEmpty
    sha256: Sha256


class ReprFreeze(StrictModel):
    freeze_commit: GitCommit
    implementation_commit: GitCommit
    spec_hash: Sha256
    renderer_semantic_hash: Sha256
    implementation_set_hash: Sha256
    lean_renderer: ArtifactBinding
    injected_helper_sha256: Sha256
    python_renderer: ArtifactBinding
    frozen_config: ArtifactBinding
    universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    universe_profile_hash: Sha256
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Sha256


class PromptBindings(StrictModel):
    codex_proposer: ArtifactBinding
    blinded_claude_judge: ArtifactBinding


class SchemaBindings(StrictModel):
    codex_proposer_output: ArtifactBinding
    blinded_judge_output: ArtifactBinding


class ProviderPin(StrictModel):
    provider_id: NonEmpty
    cli: Literal["codex", "claude", "lemex"]
    cli_version: NonEmpty
    binary_path: NonEmpty
    binary_sha256: Sha256
    model: NonEmpty
    effort: Literal["medium", "high", "xhigh", "max"]
    server_revision_status: Literal["unavailable_floating_provider_alias"]
    timeout_seconds: int = Field(ge=1, le=7200, strict=True)
    termination_grace_seconds: int = Field(ge=1, le=60, strict=True)
    public_sources_only: Literal[True]
    tools_disabled: Literal[True]


class CompileContextConfig(StrictModel):
    project_id: NonEmpty
    project_revision: GitCommit
    lean_version: NonEmpty
    project_dir: NonEmpty
    import_header: NonEmpty
    command_preamble: str = ""
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    environment_schema_version: int = Field(ge=1, strict=True)
    leaninteract_version: NonEmpty
    repl_revision: NonEmpty
    memory_hard_limit_mb: Literal[24576]
    synchronous_elaboration: Literal[True]
    workers: Literal[1]


class OneRootConfig(StrictModel):
    root_id: NonEmpty
    source: Literal["mathlib", "physlib", "cslib", "compiler_data"]
    source_revision: GitCommit
    source_license: Literal["Apache-2.0", "MIT"]
    external_transmission: Literal[True]
    policy_version: Literal["source_use_v2"]
    declaration_name: NonEmpty
    reference_signature: NonEmpty
    expected_reference_goal_v1: NonEmpty
    compile_context: CompileContextConfig


class SlotConfig(StrictModel):
    slot_id: Literal["preserve_0", "preserve_1", "break_0", "break_1"]
    requested_polarity: Polarity
    preferred_mechanism: NonEmpty
    max_attempts: Literal[3]

    @model_validator(mode="after")
    def _slot_polarity(self) -> Self:
        expected = "preserving" if self.slot_id.startswith("preserve_") else "breaking"
        if self.requested_polarity != expected:
            raise ValueError("slot ID and requested polarity differ")
        return self


class LegacyRecipe(StrictModel):
    decision: Literal["accepted_separate_legacy_single_judge"]
    source_root: NonEmpty
    immutable_tree_sha256: Sha256
    trainer_records_path: NonEmpty
    trainer_records_sha256: Sha256
    judgments_path: NonEmpty
    judgments_sha256: Sha256
    pair_plan_path: NonEmpty
    pair_plan_sha256: Sha256
    gross_rows: Literal[13373]
    resolved_rows_before_dedup: Literal[13367]
    positive_rows_before_dedup: Literal[307]
    negative_rows_before_dedup: Literal[13060]
    unknown_sidecar_rows: Literal[6]
    directed_duplicate_excess_rows: Literal[7]
    rejected_anonymous_rows: Literal[0]
    rejected_ellipsis_rows_before_dedup: Literal[144]
    rejected_ellipsis_rows_after_dedup: Literal[144]
    admitted_rows_after_dedup_and_placeholder_screen: Literal[13216]
    admitted_positive_rows: Literal[297]
    admitted_negative_rows: Literal[12919]
    dedup_key: Literal["raw_reference_headless+raw_candidate_headless"]
    keep_rule: Literal["lexicographically_smallest_record_id"]
    placeholder_policy: Literal["reject_to_legacy_placeholder_audit"]
    output_configuration: Literal["legacy_single_judge"]
    label_basis: Literal["qwen_or_kimi_proposer+single_codex_judge"]


class AuditPolicy(StrictModel):
    provider: Literal["lemex"]
    blinded_prompt: Literal[True]
    fraction: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    stratify_by: tuple[Literal["requested_polarity", "claude_verdict"], ...]
    disagreement_action: Literal["unknown_review_exclude_core"]
    requires_reproducible_one_root: Literal[True]

    @field_validator("fraction")
    @classmethod
    def _exact_ten_percent(cls, value: float) -> float:
        if value != 0.1:
            raise ValueError("SFT2A audit fraction is frozen at 0.1")
        return value


class GoldScreenPolicy(StrictModel):
    path: NonEmpty
    sha256: Sha256
    canonical_rows: Literal[5111]
    group_key_count: Literal[591]
    near_dup_hash_count: Literal[5779]
    signature_key: Literal["signature_near_dup_hash_v1"]
    match_action: Literal["exclude_to_contamination_configuration"]


class VersionedRunLayout(StrictModel):
    """Keep reusable execution caches outside immutable, judge-scoped outputs."""

    shared_cache_root: NonEmpty
    run_output_subdir: NonEmpty
    historical_fable_run_subdir: Literal["one_root_v1"]
    audit_output_subdir: NonEmpty
    comparison_output_subdir: NonEmpty
    pilot_output_subdir: NonEmpty
    legacy_rejudge_output_subdir: NonEmpty
    post_audit_output_subdir: NonEmpty


class ExecutionCeilings(StrictModel):
    maximum_roots: int = Field(ge=1, strict=True)
    maximum_provider_calls: int = Field(ge=1, strict=True)
    maximum_proposer_calls: int = Field(ge=0, strict=True)
    maximum_opus_calls: int = Field(ge=0, strict=True)
    maximum_lemex_calls: int = Field(ge=0, strict=True)
    maximum_attempts_per_slot: Literal[3]
    maximum_reported_opus_spend_usd: float = Field(gt=0.0, strict=True)
    codex_cost_status: Literal["unavailable"]
    lemex_cost_status: Literal["unavailable"]

    @model_validator(mode="after")
    def _provider_subcaps(self) -> Self:
        subtotal = self.maximum_proposer_calls + self.maximum_opus_calls + self.maximum_lemex_calls
        if subtotal > self.maximum_provider_calls:
            raise ValueError("provider-specific ceilings exceed the total provider-call ceiling")
        return self


class PilotSourceAllocation(StrictModel):
    source: Literal["mathlib", "physlib", "cslib", "compiler_data"]
    roots: int = Field(ge=1, strict=True)


class PilotPlan(StrictModel):
    authorized: bool = Field(strict=True)
    sampler_version: Literal["sft2a_deterministic_stratified_roots_v1"]
    salt: NonEmpty
    catalog_path: NonEmpty
    catalog_sha256: Sha256
    allocations: tuple[PilotSourceAllocation, ...]
    group_by: Literal["project_id+project_revision+compile_context_id"]
    persistent_lean_environment_per_group: Literal[True]
    ceilings: ExecutionCeilings

    @model_validator(mode="after")
    def _sources_once(self) -> Self:
        expected = ("mathlib", "physlib", "cslib", "compiler_data")
        observed = tuple(item.source for item in self.allocations)
        if observed != expected:
            raise ValueError("pilot allocations require the ordered four frozen sources")
        if sum(item.roots for item in self.allocations) != self.ceilings.maximum_roots:
            raise ValueError("pilot source allocation does not equal the root ceiling")
        return self


class LegacyRejudgePolicy(StrictModel):
    authorized: bool = Field(strict=True)
    input_configuration: Literal["legacy_single_judge"]
    output_configuration: Literal["legacy_double_judge"]
    all_admitted_positives: Literal[233]
    minimum_stratified_negatives: int = Field(ge=2000, strict=True)
    include_all_renderable_unresolved: Literal[True]
    exclude_placeholders: Literal[True]
    exclude_repr_invalid: Literal[True]
    stratify_by: tuple[Literal["family"], ...]
    salt: NonEmpty
    ceilings: ExecutionCeilings


class LegacyRejudgeV2Policy(StrictModel):
    authorized: bool = Field(strict=True)
    input_configuration: Literal["legacy_single_judge"]
    output_configuration: Literal["legacy_double_judge"]
    output_subdir: NonEmpty
    all_admitted_positives: Literal[233]
    minimum_stratified_negatives: int = Field(ge=2000, strict=True)
    renderable_unresolved_action: Literal["single_judge_needs_second_judge_auxiliary_no_opus_call"]
    exclude_placeholders: Literal[True]
    exclude_repr_invalid: Literal[True]
    stratify_by: tuple[Literal["family"], ...]
    salt: NonEmpty
    ceilings: ExecutionCeilings


class DetachedLaunchPolicy(StrictModel):
    session_name: NonEmpty
    resource_task: NonEmpty
    lean_workers: Literal[1]
    lean_rss_gib: Annotated[float, Field(strict=True)]
    run_lock_relative_path: NonEmpty
    combined_log_relative_path: NonEmpty
    journal_relative_path: NonEmpty
    terminal_status_relative_path: NonEmpty
    launch_receipt_relative_path: NonEmpty
    stdin_closed: Literal[True]
    exclusive_run_lock: Literal[True]
    duplicate_restart_forbidden: Literal[True]

    @field_validator("lean_rss_gib")
    @classmethod
    def _exact_lean_rss(cls, value: float) -> float:
        if value != 20.0:
            raise ValueError("SFT2A detached pilot requires the measured 20 GiB claim")
        return value


class FailedPilotRecoverySource(StrictModel):
    failed_output_subdir: NonEmpty
    source_sample_sha256: Sha256
    source_sample_manifest_sha256: Sha256
    terminal_status_sha256: Sha256
    provider_budget_journal_sha256: Sha256
    required_terminal_status: Literal["failed"]


class PilotReadinessConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal["leanfaith_sft2a_diverse_root_opus5_pilot_v2"]
    status: Literal["ready_not_authorized", "authorized_pilot"]
    task_id: Literal["SFT2A"]
    base_opus_smoke_config: ArtifactBinding
    base_opus_smoke_config_hash: Sha256
    catalog: ArtifactBinding
    expected_sample_sha256: Sha256
    sample_output_subdir: NonEmpty
    allocations: tuple[PilotSourceAllocation, ...]
    group_by: Literal["project_id+project_revision+compile_context_id"]
    persistent_lean_environment_per_group: Literal[True]
    ceilings: ExecutionCeilings
    authorization_receipt: ArtifactBinding
    historical_fable_seal: ArtifactBinding
    legacy_rejudge: LegacyRejudgeV2Policy

    @model_validator(mode="after")
    def _pilot_v2_sources(self) -> Self:
        expected = ("mathlib", "physlib", "cslib", "compiler_data")
        observed = tuple(item.source for item in self.allocations)
        if observed != expected:
            raise ValueError("pilot v2 allocations require the ordered four frozen sources")
        if sum(item.roots for item in self.allocations) != self.ceilings.maximum_roots:
            raise ValueError("pilot v2 source allocation does not equal the root ceiling")
        if self.status == "ready_not_authorized" and self.legacy_rejudge.authorized:
            raise ValueError("readiness config cannot authorize legacy rejudging")
        return self


class ProductionPilotReadinessConfig(PilotReadinessConfig):
    config_id: Literal[  # type: ignore[assignment]
        "leanfaith_sft2a_production_defaults_pilot_v1"
    ]
    labeling_defaults_policy: ArtifactBinding
    exact_settings_smoke_receipt: ArtifactBinding
    detached_launch: DetachedLaunchPolicy


class RecoveryProductionPilotReadinessConfig(ProductionPilotReadinessConfig):
    config_id: Literal[  # type: ignore[assignment]
        "leanfaith_sft2a_production_defaults_pilot_recovery_v3"
    ]
    status: Literal["ready_not_authorized"]
    catalog_corrections: ArtifactBinding
    failed_pilot_recovery_source: FailedPilotRecoverySource


class AuthorizedProductionPilotReadinessConfig(ProductionPilotReadinessConfig):
    config_id: Literal[  # type: ignore[assignment]
        "leanfaith_sft2a_production_defaults_pilot_v2"
    ]
    status: Literal["authorized_pilot"]
    activation_plan: ArtifactBinding
    source_readiness_config: ArtifactBinding
    source_readiness_config_hash: Sha256
    source_authorization_receipt: ArtifactBinding


class AuthorizedRecoveryProductionPilotReadinessConfig(RecoveryProductionPilotReadinessConfig):
    config_id: Literal[  # type: ignore[assignment]
        "leanfaith_sft2a_production_defaults_pilot_recovery_v4"
    ]
    status: Literal["authorized_pilot"]  # type: ignore[assignment]
    activation_plan: ArtifactBinding
    source_readiness_config: ArtifactBinding
    source_readiness_config_hash: Sha256
    source_authorization_receipt: ArtifactBinding


class PilotActivationPlan(StrictModel):
    schema_version: Literal[1]
    activation_id: Literal[
        "leanfaith_sft2a_production_pilot_activation_v2",
        "leanfaith_sft2a_production_pilot_recovery_activation_v4",
    ]
    status: Literal["awaiting_exact_authorization"]
    task_id: Literal["SFT2A"]
    production_config: ArtifactBinding
    production_config_hash: Sha256
    source_readiness_config: ArtifactBinding
    source_readiness_config_hash: Sha256
    source_authorization_receipt: ArtifactBinding
    labeling_defaults_policy: ArtifactBinding
    exact_settings_smoke_receipt: ArtifactBinding
    catalog: ArtifactBinding
    expected_sample_sha256: Sha256
    source_staged_sample_manifest_sha256: Sha256
    source_sample_implementation_commit: GitCommit
    source_sample_implementation_tree: GitCommit
    ceilings: ExecutionCeilings
    ceilings_sha256: Sha256
    target_config_id: Literal[
        "leanfaith_sft2a_production_defaults_pilot_v2",
        "leanfaith_sft2a_production_defaults_pilot_recovery_v4",
    ]
    target_authorization_receipt_path: NonEmpty
    target_readiness_config_path: NonEmpty
    fresh_sample_output_subdir: NonEmpty
    authorized_detached_launch: DetachedLaunchPolicy
    authorization_sentence_sha256: Sha256
    requires_exact_authorization_sentence: Literal[True]
    pilot_launch_currently_authorized: Literal[False]
    legacy_rejudge_authorized: Literal[False]
    publication_authorized: Literal[False]
    scale_50k_authorized: Literal[False]


class SFT2AConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal["leanfaith_sft2a_one_root_v1"]
    status: Literal["frozen_one_root_only"]
    task_id: Literal["SFT2A"]
    maximum_roots: Literal[1]
    maximum_candidate_slots: Literal[4]
    publication_allowed: Literal[False]
    scale_50k_allowed: Literal[False]
    staging_root: NonEmpty
    prompts: PromptBindings
    schemas: SchemaBindings
    repr: ReprFreeze
    proposer: ProviderPin
    claude_judge: ProviderPin
    lemex_auditor: ProviderPin
    root: OneRootConfig
    slots: tuple[SlotConfig, ...]
    legacy: LegacyRecipe
    audit: AuditPolicy
    gold_screen: GoldScreenPolicy

    @model_validator(mode="after")
    def _four_slots(self) -> Self:
        expected = ("preserve_0", "preserve_1", "break_0", "break_1")
        if tuple(slot.slot_id for slot in self.slots) != expected:
            raise ValueError("SFT2A one-root config requires the exact ordered four slots")
        return self


class SFT2AOpusConfig(SFT2AConfig):
    """Additive Opus smoke and executable-but-not-authorized pilot contract."""

    config_id: Literal["leanfaith_sft2a_one_root_opus5_v1"]  # type: ignore[assignment]
    status: Literal["frozen_opus_one_root_only"]  # type: ignore[assignment]
    run_layout: VersionedRunLayout
    smoke_ceilings: ExecutionCeilings
    pilot: PilotPlan
    legacy_rejudge: LegacyRejudgePolicy


class SFT2AProductionConfig(SFT2AOpusConfig):
    """Additive active-default smoke and production-pilot contract."""

    config_id: Literal["leanfaith_sft2a_production_pilot_v1"]  # type: ignore[assignment]
    status: Literal["production_defaults_smoke_only"]  # type: ignore[assignment]
    labeling_defaults_policy: ArtifactBinding


class MechanismRotationPolicy(StrictModel):
    version: Literal["sft2a_applicability_aware_rotation_v5"]
    salt: NonEmpty
    maximum_family_fraction_per_polarity: Annotated[float, Field(strict=True, gt=0.0, le=0.25)]
    maximum_shortcut_aggregate_fraction_per_polarity: Annotated[
        float, Field(strict=True, ge=0.0, le=0.25)
    ]
    minimum_families_per_polarity: int = Field(ge=8, strict=True)
    exact_copy_action: Literal["reject_before_judge"]
    closed_expr_or_goal_identity_action: Literal["reject_before_judge"]
    vacuous_padding_action: Literal["reject_before_lean"]


class SourceCensusPolicy(StrictModel):
    version: Literal["sft2a_zero_lean_source_census_v5"]
    output_subdir: NonEmpty
    compiler_data_path: NonEmpty
    compiler_data_sha256: Sha256
    library_source_subdirs: dict[Literal["mathlib", "physlib", "cslib"], NonEmpty]
    no_lean_requests: Literal[True]
    no_provider_calls: Literal[True]


class RehearsalPassCriteria(StrictModel):
    zero_self_pairs: Literal[True]
    zero_duplicates: Literal[True]
    zero_contamination: Literal[True]
    zero_confirmed_label_errors: Literal[True]
    minimum_mechanism_families_per_polarity: int = Field(ge=8, strict=True)
    maximum_dominant_family_fraction: Annotated[float, Field(strict=True, le=0.25)]
    maximum_cosmetic_or_tautological_fraction: Annotated[float, Field(strict=True, gt=0.0, le=0.05)]
    minimum_audit_agreement_after_malformed_retries: Annotated[
        float, Field(strict=True, ge=0.95, le=1.0)
    ]
    require_fresh_per_context_throughput: Literal[True]
    require_interrupted_resume_verification: Literal[True]
    require_zero_call_replay: Literal[True]


class RehearsalPlan(StrictModel):
    authorized: Literal[False]
    sampler_version: Literal["sft2a_source_domain_shape_stratified_v5"]
    salt: NonEmpty
    output_subdir: NonEmpty
    roots_per_shard: int = Field(ge=1, le=25, strict=True)
    allocations: tuple[PilotSourceAllocation, ...]
    minimum_kimi_audits: int = Field(ge=40, strict=True)
    maximum_kimi_audits: int = Field(ge=40, strict=True)
    malformed_audit_retries: Literal[1]
    ceilings: ExecutionCeilings
    pass_criteria: RehearsalPassCriteria
    detached_launch: DetachedLaunchPolicy

    @model_validator(mode="after")
    def _rehearsal_contract(self) -> Self:
        expected = ("mathlib", "physlib", "cslib", "compiler_data")
        if tuple(item.source for item in self.allocations) != expected:
            raise ValueError("v5 rehearsal allocations require the ordered four sources")
        if sum(item.roots for item in self.allocations) != 100:
            raise ValueError("v5 rehearsal requires exactly 100 roots")
        if self.ceilings.maximum_roots != 100:
            raise ValueError("v5 rehearsal root ceiling must be exactly 100")
        if self.maximum_kimi_audits > self.ceilings.maximum_lemex_calls:
            raise ValueError("v5 Kimi audit cap exceeds the provider ceiling")
        return self


class SFT2AV5Config(SFT2AProductionConfig):
    """Additive closure-aware v5 smoke and unauthorized rehearsal contract."""

    config_id: Literal["leanfaith_sft2a_closure_aware_v5"]  # type: ignore[assignment]
    status: Literal["v5_one_root_smoke_only"]  # type: ignore[assignment]
    recovery_v4_seal: ArtifactBinding
    closure_canaries: ArtifactBinding
    mechanism_rotation: MechanismRotationPolicy
    source_census: SourceCensusPolicy
    rehearsal: RehearsalPlan
    legacy_rejudge: LegacyRejudgeV2Policy  # type: ignore[assignment]


class ReferenceCertificationPolicy(StrictModel):
    version: Literal["sft2a_reference_certification_v5_2"]
    authorized: Literal[False]
    output_subdir: NonEmpty
    cache_subdir: NonEmpty
    pool_salt: NonEmpty
    initial_allocations: tuple[PilotSourceAllocation, ...]
    extension_allocations: tuple[PilotSourceAllocation, ...]
    final_allocations: tuple[PilotSourceAllocation, ...]
    initial_pool_size: Literal[300]
    maximum_certification_attempts: Literal[600]
    extension_rule: Literal[
        "only_underfilled_source_one_fixed_quota_sized_block_after_initial_pool"
    ]
    provider_calls_allowed: Literal[0]
    lean_workers_initial: Literal[1]
    lean_workers_maximum: Literal[2]
    measured_rss_gib_maximum: Annotated[float, Field(strict=True)]
    timeout_seconds_per_reference: int = Field(ge=30, le=600, strict=True)
    constant_lookup_sources: tuple[Literal["mathlib", "physlib", "cslib"], ...]
    term_elaboration_sources: tuple[Literal["compiler_data"], ...]
    positive_canary_declaration: Literal["Cslib.LTS.mem_saturate_image_τ"]
    detached_launch: DetachedLaunchPolicy

    @model_validator(mode="after")
    def _certification_contract(self) -> Self:
        sources = ("mathlib", "physlib", "cslib", "compiler_data")
        if tuple(item.source for item in self.initial_allocations) != sources:
            raise ValueError("v5.2 initial certification pool requires the ordered four sources")
        if tuple(item.source for item in self.extension_allocations) != sources:
            raise ValueError("v5.2 certification extensions require the ordered four sources")
        if tuple(item.source for item in self.final_allocations) != sources:
            raise ValueError("v5.2 final sample requires the ordered four sources")
        initial = tuple(item.roots for item in self.initial_allocations)
        extension = tuple(item.roots for item in self.extension_allocations)
        final = tuple(item.roots for item in self.final_allocations)
        if initial != (126, 75, 51, 48) or extension != initial:
            raise ValueError("v5.2 certification pool quotas differ from 126/75/51/48")
        if final != (42, 25, 17, 16):
            raise ValueError("v5.2 final quotas differ from 42/25/17/16")
        if sum(initial) != self.initial_pool_size:
            raise ValueError("v5.2 initial certification allocations do not total 300")
        if sum(initial) + sum(extension) != self.maximum_certification_attempts:
            raise ValueError("v5.2 certification extension contract does not cap at 600")
        if self.measured_rss_gib_maximum != 40.0:
            raise ValueError("v5.2 reference certification RSS cap must be exactly 40 GiB")
        if self.constant_lookup_sources != ("mathlib", "physlib", "cslib"):
            raise ValueError("v5.2 imported-library reference routes differ")
        return self


class ParallelRehearsalPolicy(StrictModel):
    version: Literal["sft2a_bounded_parallel_rehearsal_v5_2"]
    execution_authorized: Literal[False]
    maximum_root_workers: Literal[2]
    lean_workers_per_root: Literal[1]
    maximum_total_lean_workers: Literal[2]
    maximum_measured_rss_gib: Annotated[float, Field(strict=True)]
    provider_budget_protocol: Literal["atomic_reservation_then_finalization_v1"]
    lean_isolation: Literal["one_persistent_project_session_per_root_worker"]
    cross_worker_deduplication: Literal[True]
    mid_root_resume_required: Literal[True]
    between_root_resume_required: Literal[True]
    duplicate_launch_refusal: Literal[True]
    deterministic_compaction: Literal[True]
    zero_call_replay: Literal[True]
    report_planned_and_accepted_mechanisms_separately: Literal[True]

    @field_validator("maximum_measured_rss_gib")
    @classmethod
    def _parallel_rss_cap(cls, value: float) -> float:
        if value != 40.0:
            raise ValueError("v5.2 parallel rehearsal RSS cap must be exactly 40 GiB")
        return value


class SFT2AV52Config(SFT2AV5Config):
    """Reference-certified v5.2 track; provider-backed rehearsal remains disabled."""

    config_id: Literal["leanfaith_sft2a_closure_aware_v5_2"]  # type: ignore[assignment]
    status: Literal["v5_2_reference_certification_only"]  # type: ignore[assignment]
    failed_v5_1_seal: ArtifactBinding
    reference_certification: ReferenceCertificationPolicy
    parallel_rehearsal: ParallelRehearsalPolicy


class SFT2AV52RecoveryConfig(SFT2AV52Config):
    """Fresh-output recovery after the preserved context-fingerprint failure."""

    config_id: Literal["leanfaith_sft2a_closure_aware_v5_2_recovery_v2"]  # type: ignore[assignment]
    failed_v5_2_seal: ArtifactBinding


class ProposerOutput(StrictModel):
    schema_version: Literal[1]
    requested_polarity: Polarity
    mechanism: Literal[
        "premise_restructure",
        "quantifier_restructure",
        "logical_restatement",
        "equation_orientation",
        "type_or_domain",
        "premise_strength",
        "conclusion_strength",
        "guard_or_boundary",
        "converse_or_negation",
        "existence_or_uniqueness",
        "witness_dependency",
        "other",
    ]
    candidate_signature: str = Field(min_length=1, max_length=12000)
    change_summary: str = Field(min_length=1, max_length=800)
    judge_trap: str = Field(min_length=1, max_length=800)
    informative: Literal[True]
    proof_free: Literal[True]

    @field_validator("candidate_signature")
    @classmethod
    def _proof_free_signature(cls, value: str) -> str:
        candidate = value.strip()
        lowered = candidate.casefold()
        if "[anonymous]" in lowered:
            raise ValueError("candidate signature contains forbidden [anonymous] placeholder")
        if "⋯" in candidate or "..." in candidate:
            raise ValueError("candidate signature contains an ellipsis placeholder")
        if "```" in candidate:
            raise ValueError("candidate signature contains a markdown fence")
        if "\x00" in candidate or "\r" in candidate:
            raise ValueError("candidate signature contains a forbidden control character")
        if _FORBIDDEN_SIGNATURE_WORD.search(candidate) or _PROOF_DELIMITER.search(candidate):
            raise ValueError("candidate signature contains a declaration, axiom, or proof token")
        return candidate


class ProposerOutputV5(StrictModel):
    schema_version: Literal[5]
    requested_polarity: Polarity
    mechanism: NonEmpty
    applicability_reason: str = Field(min_length=1, max_length=1000)
    candidate_signature: str = Field(min_length=1, max_length=12000)
    change_summary: str = Field(min_length=1, max_length=800)
    judge_trap: str = Field(min_length=1, max_length=800)
    informative: Literal[True]
    substantive_change: Literal[True]
    proof_free: Literal[True]

    @field_validator("mechanism")
    @classmethod
    def _known_mechanism(cls, value: str) -> str:
        from leanfaith.sft2a.mechanisms import ALL_MECHANISM_FAMILIES

        if value not in ALL_MECHANISM_FAMILIES:
            raise ValueError("v5 proposer mechanism is not in the frozen rotation")
        return value

    @field_validator("candidate_signature")
    @classmethod
    def _proof_free_signature(cls, value: str) -> str:
        return ProposerOutput(
            schema_version=1,
            requested_polarity="preserving",
            mechanism="other",
            candidate_signature=value,
            change_summary="v5 signature preflight",
            judge_trap="not used",
            informative=True,
            proof_free=True,
        ).candidate_signature


class JudgeOutput(StrictModel):
    schema_version: Literal[1]
    verdict: Verdict
    confidence: Literal["high", "medium", "low"]
    relation_class: Literal[
        "representation_only",
        "logical_restatement",
        "quantifier_scope",
        "type_or_domain",
        "premise_strength",
        "conclusion_strength",
        "converse",
        "negation_scope",
        "existence_uniqueness",
        "witness_dependency",
        "boundary_case",
        "unrelated",
        "ambiguous_or_missing_context",
        "other",
    ]
    error_type: Literal[
        "none",
        "ambiguous",
        "missing_context",
        "insufficient_confidence",
    ]
    rationale: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def _unknown_contract(self) -> Self:
        if (self.verdict == "unknown") == (self.error_type == "none"):
            raise ValueError(
                "unknown requires an error type; binary verdicts require error_type=none"
            )
        return self


ClosureCheck = Literal[
    "supports_equivalence",
    "supports_non_equivalence",
    "checked_no_effect",
    "not_applicable",
]


class ClosureChecks(StrictModel):
    entire_universally_closed_proposition: Literal[True]
    argument_swapping: ClosureCheck
    symmetry: ClosureCheck
    antisymmetry: ClosureCheck
    extensionality: ClosureCheck
    recoverable_boundary_cases: ClosureCheck


class JudgeOutputV5(JudgeOutput):
    schema_version: Literal[5]  # type: ignore[assignment]
    closure_checks: ClosureChecks


class CoreRow(StrictModel):
    reference: NonEmpty
    candidate: NonEmpty
    label: bool


class LegacyCoreRow(CoreRow):
    pass

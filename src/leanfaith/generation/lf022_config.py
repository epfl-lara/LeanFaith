"""Typed, fail-closed LF-022 foundation configuration and replay validation.

The checked-in LF-022 configs describe a *non-admitted* foundation.  Loading
them never enables a provider transport.  The replay helper accepts only an
already persisted, hash-bound provider request/response pair and runs the
strict proposer or blinded-judge parser without materializing variants,
semantic labels, or silver records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.llm_variants import parse_variant_proposer_output
from leanfaith.generation.providers import (
    PrivateContentTransmissionError,
    ProviderIdentity,
    ReplayArtifactError,
    ReplayProvider,
    load_provider_request,
)
from leanfaith.generation.weak_supervision import parse_blinded_judge_output
from leanfaith.schemas.ids import HEX64_PATTERN

ReplayKind = Literal["proposer", "judge"]


def _repo_relative(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a nonempty repository-relative path")


class LF022PromptConfig(StrictModel):
    template_id: Literal["lean_variant", "lean_pair_blinded"]
    template_version: Literal["v1"]
    path: str
    sha256: str = Field(pattern=HEX64_PATTERN)
    parser_id: Literal[
        "strict_llm_variant_json_v1",
        "strict_blinded_judgment_json_v1",
    ]
    strict_json_only: Literal[True]
    proof_free_declarations_only: Literal[True] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _repo_relative(self.path, field="prompt.path")
        if self.template_id == "lean_variant":
            if (
                self.parser_id != "strict_llm_variant_json_v1"
                or self.proof_free_declarations_only is not True
            ):
                raise ValueError("lean_variant requires its strict parser and proof-free policy")
        elif (
            self.parser_id != "strict_blinded_judgment_json_v1"
            or self.proof_free_declarations_only is not None
        ):
            raise ValueError("lean_pair_blinded requires its strict parser only")
        return self


class LF022SourcePolicyConfig(StrictModel):
    public_sources_only: Literal[True]
    private_sft_classic_transmission_forbidden: Literal[True]
    external_transmission_requires_explicit_source_permission: Literal[True]
    benchmark_denylist_clear_required: Literal[True]


class LF022GenerationDistributionConfig(StrictModel):
    sci_conditioned: bool
    store_requested_and_validated_sci_separately: Literal[True] | None = None
    open_ended_adversarial: Literal[True] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.sci_conditioned:
            if (
                self.store_requested_and_validated_sci_separately is not True
                or self.open_ended_adversarial is not None
            ):
                raise ValueError("SCI-conditioned generation must retain separate SCI metadata")
        elif (
            self.open_ended_adversarial is not True
            or self.store_requested_and_validated_sci_separately is not None
        ):
            raise ValueError("open generation must be explicitly adversarial and non-SCI")
        return self


class LF022GenerationDistributionsConfig(StrictModel):
    G_sci: LF022GenerationDistributionConfig
    G_open: LF022GenerationDistributionConfig

    @model_validator(mode="after")
    def _roles(self) -> Self:
        if not self.G_sci.sci_conditioned or self.G_open.sci_conditioned:
            raise ValueError("G_sci must be SCI-conditioned and G_open must not be")
        return self


class LF022VariantValidationConfig(StrictModel):
    persist_raw_before_parse: Literal[True]
    retain_parse_failures: Literal[True]
    reject_proof_bearing_candidates: Literal[True]
    reject_duplicate_normalized_candidates: Literal[True]
    elaboration_required_for_candidate_pool: Literal[True]
    failed_proof_search_is_negative: Literal[False]
    intention_is_label: Literal[False]


class LF022VariantOutputsConfig(StrictModel):
    provisional_variants: str
    raw_calls: str
    failures: str
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]

    @model_validator(mode="after")
    def _paths(self) -> Self:
        for field in ("provisional_variants", "raw_calls", "failures"):
            _repo_relative(getattr(self, field), field=f"outputs.{field}")
        return self


class LF022ProposerFamilyControlsConfig(StrictModel):
    minimum_proposer_families_for_confirmatory_D4_D5: int = Field(ge=3, strict=True)
    maximum_one_family_fraction_of_G_sci_plus_G_open: float = Field(
        gt=0.0,
        le=0.40,
    )
    proposer_must_differ_from_sci_validator: Literal[True]
    unavailable_family_behavior: Literal["reduced_data_ablation"]


class LF022VariantAdmissionConfig(StrictModel):
    live_calls_authorized: Literal[False]
    required_before_live_calls: tuple[
        Literal[
            "separately_versioned_provider_admission",
            "exact_model_and_revision_pin",
            "public_source_transmission_check",
            "frozen_prompt_and_decoding",
            "immutable_raw_artifact_directory",
        ],
        ...,
    ] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if len(set(self.required_before_live_calls)) != 5:
            raise ValueError("variant admission prerequisites must be unique and complete")
        return self


class LF022VariantFoundationConfig(StrictModel):
    protocol_version: Literal["llm_variants_v1"]
    status: Literal["foundation_ready_generation_not_admitted"]
    plan_sections: tuple[str, ...] = Field(min_length=1)
    source_policy: LF022SourcePolicyConfig
    prompt: LF022PromptConfig
    generation_distributions: LF022GenerationDistributionsConfig
    requested_strata: tuple[
        Literal[
            "equivalent",
            "A_stronger",
            "B_stronger",
            "near_miss",
            "semantic_erasure",
            "minimal_edit",
        ],
        ...,
    ] = Field(min_length=1)
    validation: LF022VariantValidationConfig
    outputs: LF022VariantOutputsConfig
    family_controls: LF022ProposerFamilyControlsConfig
    admission: LF022VariantAdmissionConfig

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.requested_strata)) != len(self.requested_strata):
            raise ValueError("requested_strata must be unique")
        return self


class LF022JudgeBlindingConfig(StrictModel):
    hide_proposer_family: Literal[True]
    hide_generation_intention: Literal[True]
    hide_gold_and_silver_labels: Literal[True]
    hide_symbolic_evidence_by_default: Literal[True]
    hide_other_judge_votes: Literal[True]
    require_forward_and_swapped_copy: Literal[True]
    randomize_dispatch_order: Literal[True]


class LF022JudgeFamilyControlsConfig(StrictModel):
    require_two_distinct_weak_judge_families: Literal[True]
    judge_must_differ_from_item_proposer: Literal[True]
    primary_eval_judge_supervision_excluded: Literal[True]
    primary_eval_judge_family: str | None
    judge_A_family: str | None
    judge_B_family: str | None

    @model_validator(mode="after")
    def _foundation_unassigned(self) -> Self:
        if any(
            value is not None
            for value in (
                self.primary_eval_judge_family,
                self.judge_A_family,
                self.judge_B_family,
            )
        ):
            raise ValueError("non-admitted judge foundation cannot assign provider families")
        return self


class LF022AggregationConfig(StrictModel):
    retain_disagreement: Literal[True]
    disagreement_route: Literal["human_adjudication"]
    uncertain_route: Literal["human_adjudication"]
    ambiguous_route: Literal["human_adjudication"]
    exact_swapped_semantic_agreement_required: Literal[True]
    exact_cross_family_semantic_agreement_required: Literal[True]
    output_record: Literal["WeakConsensusCandidateRecordV1"]
    output_quality_tier: Literal["provisional"]
    automatic_silver_promotion: Literal[False]


class LF022PromotionConfig(StrictModel):
    gate_6_generation_only: Literal[True]
    human_pilot_required_before_promotion: Literal[True]
    capped_audit_required: Literal[True]
    swapped_agreement_minimum: float = Field(ge=0.90, le=1.0)
    final_tier_after_policy_only: Literal["silver_consensus"]


class LF022JudgeOutputsConfig(StrictModel):
    raw_calls: str
    judgment_evidence: str
    weak_consensus_candidates: str
    promoted_silver: str
    promoted_silver_write_enabled: Literal[False]

    @model_validator(mode="after")
    def _paths(self) -> Self:
        for field in (
            "raw_calls",
            "judgment_evidence",
            "weak_consensus_candidates",
            "promoted_silver",
        ):
            _repo_relative(getattr(self, field), field=f"outputs.{field}")
        return self


class LF022JudgeAdmissionConfig(StrictModel):
    live_calls_authorized: Literal[False]
    required_before_live_calls: tuple[
        Literal[
            "separately_versioned_judge_admission",
            "exact_model_and_revision_pins",
            "frozen_judge_x_supervision_matrix",
            "primary_eval_family_exclusion_check",
            "immutable_raw_artifact_directory",
        ],
        ...,
    ] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if len(set(self.required_before_live_calls)) != 5:
            raise ValueError("judge admission prerequisites must be unique and complete")
        return self


class LF022JudgeFoundationConfig(StrictModel):
    protocol_version: Literal["weak_supervision_v1"]
    status: Literal["foundation_ready_judging_not_admitted"]
    plan_sections: tuple[str, ...] = Field(min_length=1)
    prompt: LF022PromptConfig
    blinding: LF022JudgeBlindingConfig
    family_controls: LF022JudgeFamilyControlsConfig
    aggregation: LF022AggregationConfig
    promotion: LF022PromotionConfig
    outputs: LF022JudgeOutputsConfig
    admission: LF022JudgeAdmissionConfig


@dataclass(frozen=True, slots=True)
class LF022FoundationConfigs:
    variants: LoadedConfig[LF022VariantFoundationConfig]
    judges: LoadedConfig[LF022JudgeFoundationConfig]
    proposer_prompt_sha256: str
    judge_prompt_sha256: str


class LF022ReplayValidation(StrictModel):
    replay_kind: ReplayKind
    request_hash: str = Field(pattern=HEX64_PATTERN)
    raw_response_sha256: str = Field(pattern=HEX64_PATTERN)
    parsed_item_count: int = Field(ge=1, strict=True)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False


class LF022FoundationValidationReport(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["foundation_validated_no_live_calls"]
    variants_config_sha256: str = Field(pattern=HEX64_PATTERN)
    judges_config_sha256: str = Field(pattern=HEX64_PATTERN)
    proposer_prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    live_provider_calls_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    replay: LF022ReplayValidation | None = None


def _resolve_prompt(paths: RepoPaths, config: LF022PromptConfig) -> tuple[Path, str]:
    candidate = paths.root / config.path
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"prompt is missing, symlinked, or not a regular file: {config.path}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValueError(f"prompt escapes repository root: {config.path}") from exc
    observed = hash_file(resolved)
    if observed != config.sha256:
        raise ValueError(
            f"prompt hash mismatch for {config.path}: expected {config.sha256}, got {observed}"
        )
    return resolved, observed


def load_lf022_foundation_configs(
    *,
    paths: RepoPaths,
    variants_path: Path,
    judges_path: Path,
) -> LF022FoundationConfigs:
    """Load both fail-closed configs and validate their prompt assets."""

    variants = load_config(variants_path, LF022VariantFoundationConfig)
    judges = load_config(judges_path, LF022JudgeFoundationConfig)
    if variants.config.prompt.template_id != "lean_variant":
        raise ValueError("variant config must use the lean_variant prompt")
    if judges.config.prompt.template_id != "lean_pair_blinded":
        raise ValueError("judge config must use the lean_pair_blinded prompt")
    _, proposer_hash = _resolve_prompt(paths, variants.config.prompt)
    _, judge_hash = _resolve_prompt(paths, judges.config.prompt)
    return LF022FoundationConfigs(
        variants=variants,
        judges=judges,
        proposer_prompt_sha256=proposer_hash,
        judge_prompt_sha256=judge_hash,
    )


def replay_lf022_response(
    *,
    configs: LF022FoundationConfigs,
    replay_kind: ReplayKind,
    request_path: Path,
    raw_response_root: Path,
) -> LF022ReplayValidation:
    """Replay one immutable provider response and run only its strict parser."""

    request = load_provider_request(request_path)
    if request.private_source_content:
        raise PrivateContentTransmissionError(
            "LF-022 replay foundation rejects private-source provider requests"
        )
    expected_template_hash = (
        configs.proposer_prompt_sha256 if replay_kind == "proposer" else configs.judge_prompt_sha256
    )
    if request.prompt_template_hash != expected_template_hash:
        raise ReplayArtifactError(
            "provider request prompt template hash differs from the selected LF-022 config"
        )
    result = ReplayProvider(
        identity=ProviderIdentity(
            provider=request.provider,
            model=request.model,
            revision=request.revision,
            transport="replay",
        ),
        raw_response_root=raw_response_root,
    ).generate(request)
    if result.response.status != "success" or result.response.output_text is None:
        raise ReplayArtifactError("LF-022 replay requires a successful response with output text")
    if replay_kind == "proposer":
        parsed_count = len(parse_variant_proposer_output(result.response.output_text).variants)
    else:
        parse_blinded_judge_output(result.response.output_text)
        parsed_count = 1
    return LF022ReplayValidation(
        replay_kind=replay_kind,
        request_hash=request.request_hash,
        raw_response_sha256=result.raw_response_sha256,
        parsed_item_count=parsed_count,
    )


def validate_lf022_foundation(
    *,
    paths: RepoPaths,
    variants_path: Path,
    judges_path: Path,
    replay_kind: ReplayKind | None = None,
    request_path: Path | None = None,
    raw_response_root: Path | None = None,
) -> LF022FoundationValidationReport:
    """Validate checked-in foundations and optionally replay one response."""

    replay_args = (replay_kind, request_path, raw_response_root)
    if any(value is not None for value in replay_args) and any(
        value is None for value in replay_args
    ):
        raise ValueError(
            "replay_kind, request_path, and raw_response_root must be supplied together"
        )
    configs = load_lf022_foundation_configs(
        paths=paths,
        variants_path=variants_path,
        judges_path=judges_path,
    )
    replay = None
    if replay_kind is not None:
        assert request_path is not None
        assert raw_response_root is not None
        replay = replay_lf022_response(
            configs=configs,
            replay_kind=replay_kind,
            request_path=request_path,
            raw_response_root=raw_response_root,
        )
    return LF022FoundationValidationReport(
        status="foundation_validated_no_live_calls",
        variants_config_sha256=configs.variants.config_hash,
        judges_config_sha256=configs.judges.config_hash,
        proposer_prompt_sha256=configs.proposer_prompt_sha256,
        judge_prompt_sha256=configs.judge_prompt_sha256,
        replay=replay,
    )


__all__ = [
    "LF022FoundationConfigs",
    "LF022FoundationValidationReport",
    "LF022JudgeFoundationConfig",
    "LF022ReplayValidation",
    "LF022VariantFoundationConfig",
    "ReplayKind",
    "load_lf022_foundation_configs",
    "replay_lf022_response",
    "validate_lf022_foundation",
]

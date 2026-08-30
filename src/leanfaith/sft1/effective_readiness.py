"""Lean-free effective SFT1 readiness state for additive revision 0.3.3.

The revision 0.3.2 admission, census, and N31 documents are immutable
snapshots.  This module composes those snapshots with a small additive
effective-state overlay.  It does not execute an operation, inspect a Lean
environment, emit a row, or turn a proposed operation into an admission.

Loading fails closed on unknown fields, dependency drift, registry drift,
authority conflation, an independently sampled N-PROOF lane, or a census
dependency stronger than the approved staged gate contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictFloat, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.admission_readiness import (
    LoadedWave1GateAdmission,
    load_wave1_gate_admission,
)
from leanfaith.sft1.composition_policy import OperationSpec
from leanfaith.sft1.n31_guard_policy import load_n31_guard_bank
from leanfaith.sft1.source_census import LoadedWave1SourceCensus, load_wave1_source_census

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
SymbolicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", strict=True)]
IsoDate = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", strict=True)]

DEFAULT_EFFECTIVE_READINESS_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_effective_readiness_v0_3_3.yaml"
)

EXPECTED_OVERLAY_ID = "sft1_wave1_effective_readiness_v0_3_3"
EXPECTED_OVERLAY_VERSION = "0.3.3"
EXPECTED_CHECKPOINT_COMMIT = "dae99b3bd04d765a7a2011e10129589951dcb3c2"
EXPECTED_REVIEW_URL = "https://chatgpt.com/share/6a9450ec-3ae4-83eb-a6a8-0283a07124a2"
EXPECTED_REVIEW_ATTACHMENT_ID = "323373ba-a0c6-42b5-b3be-e19a76600684/pasted-text.txt"
EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256 = (
    "6eacfa333ab0f3507189584e497539b2b4053acc4e8fc4a91bb74b94d9597a75"
)
EXPECTED_USER_AUTHORIZATION_SHA256 = (
    "fc0c951ebaf1c43c47c9582e0f6c8ca0769b40c1d6af0613d59556278d111e56"
)

EXPECTED_WAVE1_OPERATION_IDS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
)
EXPECTED_WAVE1_MECHANISM_IDS: tuple[str, ...] = (
    "p01_alpha_rename_single_v1",
    "p15_swap_iff_sides_v1",
    "p18_symmetrize_equality_v1",
    "p21_beta_reduce_v1",
    "n31_required_guard_mutation",
)
EXPECTED_OPERATION_TO_MECHANISM: tuple[tuple[str, str], ...] = (
    (EXPECTED_WAVE1_OPERATION_IDS[0], EXPECTED_WAVE1_MECHANISM_IDS[0]),
    (EXPECTED_WAVE1_OPERATION_IDS[1], EXPECTED_WAVE1_MECHANISM_IDS[1]),
    (EXPECTED_WAVE1_OPERATION_IDS[2], EXPECTED_WAVE1_MECHANISM_IDS[2]),
    (EXPECTED_WAVE1_OPERATION_IDS[3], EXPECTED_WAVE1_MECHANISM_IDS[3]),
    (EXPECTED_WAVE1_OPERATION_IDS[4], EXPECTED_WAVE1_MECHANISM_IDS[4]),
    (EXPECTED_WAVE1_OPERATION_IDS[5], EXPECTED_WAVE1_MECHANISM_IDS[4]),
)
EXPECTED_PROJECT_IDS: tuple[str, ...] = ("compiler_data", "cslib", "mathlib", "physlib")

EXPECTED_WAVE2_OPERATION_IDS: tuple[str, ...] = (
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P16_REASSOC_AND_LEFT_V1",
    "P22_ETA_REDUCE_EXPLICIT_FUN_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
    "P28_DECOMPOSE_IFF_V1",
    "P32_ADD_COMM_LOCAL_V1",
    "P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1",
    "P35_SET_INTER_MEMBERSHIP_V1",
    "P40_EXISTS_UNIQUE_EXPAND_V1",
    "P42_RING_POLYNOMIAL_LOCAL_V1",
    "N25_TOGGLE_EQ_NE_RUBRIC_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N26_INCREMENT_BOUND_RUBRIC_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
    "N32_SWAP_ROLE_ORDER_RUBRIC_V1",
)
EXPECTED_WAVE2_N_PROOF_IDS: tuple[str, ...] = (
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
)
EXPECTED_WAVE2_DIMENSION_IDS: tuple[str, ...] = (
    "n25_negation_mistakes_natural_v1",
    "n26_edge_cases_natural_v1",
    "n30_existence_uniqueness_natural_v1",
    "n32_converse_mistakes_natural_v1",
)

EXPECTED_BASE_POLICY_PATH = (
    "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml"
)
EXPECTED_BASE_POLICY_FILE_SHA256 = (
    "a052ecec4cc8f61db7438dd5acbc39373a624b155f8c0305bb75b7ae15d7195d"
)
EXPECTED_BASE_POLICY_SEMANTIC_HASH = (
    "08a6d1b2ea03f3674d06cdac44478377084af24ba5cd4af7cab57303f4e7a917"
)
EXPECTED_OPERATION_REGISTRY_HASH = (
    "d56fca674f7b58d92dca09f0b76a702c54d1df2e5b68dcbe94225cad7e5cd95f"
)
EXPECTED_ADMISSION_PATH = (
    "configs/transformations/sft1_value_first_v1/wave1_gate_admission_v0_3_2.yaml"
)
EXPECTED_ADMISSION_FILE_SHA256 = "c1cf07713bfca91e6b5fbedf75a5b5f6e0f841886df7a71e7f4f6c9d82c862b3"
EXPECTED_ADMISSION_SEMANTIC_HASH = (
    "8f50f38231e3cee7a6d5ab0d66cb708ce1f949789e91cc90b74516ef1406d409"
)
EXPECTED_CLEAN_RECEIPT_PATH = (
    "configs/transformations/sft1_value_first_v1/clean_checkout_receipt_v0_3_2.json"
)
EXPECTED_CLEAN_RECEIPT_FILE_SHA256 = (
    "4133c2df44b81b388d3cc39e499feb65d1cd410909b6843591ec6b1295ea3331"
)
EXPECTED_CLEAN_RECEIPT_SEMANTIC_HASH = (
    "90ca160b90e294170a1d88918a6aaf5cf900b8a1c89e8c7f77fcd2c8ba5b89c5"
)
EXPECTED_CENSUS_PATH = "configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml"
EXPECTED_CENSUS_FILE_SHA256 = "a8c6c3616a543ff9e1f5d4700a3b5a86da2442f70475737caf23bd264ebd2aaa"
EXPECTED_CENSUS_SEMANTIC_HASH = "daf4b26b782d096f77b9677e0a7cef5670103771942c415dc3420b3031eda44e"
EXPECTED_N31_BANK_PATH = (
    "configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml"
)
EXPECTED_N31_BANK_FILE_SHA256 = "c2a5aa63158ffbc561bc61f2e3acaa2598aff54a926fd774014e62e6c1cd8cd8"
EXPECTED_N31_BANK_SEMANTIC_HASH = "82bca9b16861412ebaf296591944338932e51f6aaaf8372baa4fd4c1f097f9e1"
EXPECTED_SOURCE_USE_PATH = "policies/source_use_v2.yaml"
EXPECTED_SOURCE_USE_FILE_SHA256 = "62a4daca09ca669aef0133cd4d0b0913e1d7795558560f3aac4b289efc75e95c"
EXPECTED_SOURCE_USE_SEMANTIC_HASH = (
    "ac9356772746459a1fed9277e127ce0575cbe4063d334a6914ed5c9ac186784d"
)
EXPECTED_REPR_GATE_PATH = (
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml"
)
EXPECTED_REPR_GATE_FILE_SHA256 = "5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91"
EXPECTED_REPR_GATE_SEMANTIC_HASH = (
    "7404e31935ab35b9c3270bf46654936121944a7e8f55fb91da4f1e047f59c0ad"
)
EXPECTED_REPR_RECEIPT_PATH = (
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json"
)
EXPECTED_REPR_RECEIPT_FILE_SHA256 = (
    "ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8"
)
EXPECTED_REPR_RECEIPT_SEMANTIC_HASH = (
    "f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78"
)

EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256 = (
    "5673d2ee2e3d9b088bcc42ccec4d4d851096b6c0fc8cc5349b1c3b231f2b1474"
)
EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH = (
    "1b323508b3c3edcc62582d637c88af693e81507c3b7f1bd178dc7f3b8af2412e"
)
# Compatibility spelling used by the other SFT1 typed loaders and tests.
EXPECTED_EFFECTIVE_CONFIG_HASH = EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH


class EffectiveReadinessError(ValueError):
    """Raised when the additive overlay or one of its frozen inputs drifts."""


class ArtifactBinding(StrictModel):
    path: NonEmptyStr
    file_sha256: Sha256
    semantic_hash: Sha256


class BasePolicyBinding(ArtifactBinding):
    operation_registry_hash: Sha256
    operation_count: Literal[46]


class ReviewBinding(StrictModel):
    review_url: NonEmptyStr
    attachment_id: NonEmptyStr
    attachment_raw_sha256: Sha256
    reviewed_checkpoint_commit: GitCommit
    review_is_policy_input_not_authorization: Literal[True]
    wave2_recommendation_is_not_admission: Literal[True]


class UserAuthorization(StrictModel):
    exact_user_text: NonEmptyStr
    exact_user_text_sha256: Sha256
    recorded_date: IsoDate
    interpretation: Literal["additive_policy_and_loader_only_no_execution"]


class FrozenDependencies(StrictModel):
    checkpoint_commit: GitCommit
    base_policy: BasePolicyBinding
    admission_v0_3_2: ArtifactBinding
    clean_checkout_receipt_v0_3_2: ArtifactBinding
    source_census_v0_3_2: ArtifactBinding
    n31_guard_bank_v0_3_2: ArtifactBinding
    source_use_v2: ArtifactBinding
    repr_gate_v0_3_1: ArtifactBinding
    repr_receipt_v0_3_1: ArtifactBinding


class AllowedNow(StrictModel):
    additive_policy_file_changes: Literal[True]
    effective_state_loader_changes: Literal[True]
    lean_free_loader_tests: Literal[True]
    lean_free_invariant_tests: Literal[True]
    formatting_checks: Literal[True]
    plan_tests: Literal[True]


class PriorWave1Authority(StrictModel):
    gate_admission_recorded: Literal[True]
    task_owned_bounded_implementation_scope_authorized: Literal[True]
    implementation_readiness: Literal[False]
    gate_execution_may_start: Literal[False]


class CurrentRevisionScope(StrictModel):
    transform_implementation_changes_authorized: Literal[False]
    wave2_implementation_changes_authorized: Literal[False]
    execution_binding_resolution_authorized: Literal[False]


class CoordinatorRequest(StrictModel):
    contract_path: Literal["plans/00_shared_contracts.md"]
    status: Literal["open_untouched"]
    task_may_edit_contract: Literal[False]


class ProductionState(StrictModel):
    production_admitted_operation_count: Literal[0]
    production_admitted_negative_count: Literal[0]
    row_emission_authorized: Literal[False]


class AuthorizationBoundaries(StrictModel):
    allowed_now: AllowedNow
    prior_wave1_authority_preserved: PriorWave1Authority
    current_revision_scope: CurrentRevisionScope
    coordinator_request: CoordinatorRequest
    production_state: ProductionState


class OperationMechanismBinding(StrictModel):
    operation_id: OperationId
    mechanism_id: SymbolicId


class DynamicGateAccounting(StrictModel):
    registered_project_ids: tuple[ProjectId, ...]
    registered_project_count: Literal[4]
    primary_operation_ids: tuple[OperationId, ...]
    optional_proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    primary_operation_project_cell_count: Literal[20]
    proof_eligible_project_ids: tuple[ProjectId, ...]
    current_optional_proof_operation_project_cell_count: Literal[0]
    current_total_operation_project_cell_count: Literal[20]
    minimum_total_operation_project_cell_count: Literal[20]
    maximum_total_operation_project_cell_count: Literal[24]
    operation_project_cell_formula: Literal[
        "primary_operation_project_cell_count_plus_proof_eligible_project_count"
    ]
    fixtures_per_operation_project_cell: Literal[2]
    current_fixture_count: Literal[40]
    minimum_fixture_count: Literal[40]
    maximum_fixture_count: Literal[48]
    fixture_count_formula: Literal["two_times_total_operation_project_cell_count"]
    approximate_roots_per_semantic_mechanism: Literal[100]
    approximate_independent_root_pool_target: Literal[500]
    proof_additional_independent_root_pool_count: Literal[0]
    counts_are_gate_accounting_not_row_commitments: Literal[True]

    @model_validator(mode="after")
    def _exact_accounting(self) -> DynamicGateAccounting:
        if self.registered_project_ids != EXPECTED_PROJECT_IDS:
            raise ValueError("dynamic gate project inventory drift")
        if self.primary_operation_ids != EXPECTED_WAVE1_OPERATION_IDS[:5]:
            raise ValueError("dynamic gate primary operation inventory drift")
        if self.proof_eligible_project_ids:
            raise ValueError("unknown proof routes must create zero optional proof cells")
        return self


class EffectiveWave1(StrictModel):
    operation_ids: tuple[OperationId, ...]
    effective_mechanism_ids: tuple[SymbolicId, ...]
    operation_to_mechanism: tuple[OperationMechanismBinding, ...]
    exact_operation_count: Literal[6]
    semantic_mechanism_count: Literal[5]
    gate_admission_recorded: Literal[True]
    implementation_ready: Literal[False]
    smoke_ready: Literal[False]
    conformance_ready: Literal[False]
    approximately_100_roots_ready: Literal[False]
    production_admitted_operation_count: Literal[0]
    production_negative_count: Literal[0]
    row_emission_authorized: Literal[False]
    smoke_blocker_ids: tuple[SymbolicId, ...] = Field(min_length=1)
    smoke_non_blocker_ids: tuple[SymbolicId, ...] = Field(min_length=1)
    dynamic_gate_accounting: DynamicGateAccounting

    @model_validator(mode="after")
    def _exact_wave(self) -> EffectiveWave1:
        if self.operation_ids != EXPECTED_WAVE1_OPERATION_IDS:
            raise ValueError("effective Wave 1 operation IDs/order drift")
        if self.effective_mechanism_ids != EXPECTED_WAVE1_MECHANISM_IDS:
            raise ValueError("effective Wave 1 mechanism IDs/order drift")
        observed = tuple(
            (binding.operation_id, binding.mechanism_id) for binding in self.operation_to_mechanism
        )
        if observed != EXPECTED_OPERATION_TO_MECHANISM:
            raise ValueError("Wave 1 operation-to-mechanism mapping drift")
        expected_blockers = (
            "coordinator_shared_label_contract_update",
            "five_primary_wave1_implementation_dispatch_checker_anchor_bank_fixture_bindings",
            "n31_closed_rubric_checker_and_target_head_bank",
            "positive_smoke_root_specific_micro_census",
            "n31_rubric_smoke_root_specific_micro_census",
        )
        expected_non_blockers = (
            "n31_source_proof_availability",
            "n31_optional_proof_execution_binding",
            "selected_wave_sampling_frame_census",
            "complete_cross_source_census",
            "wave2_proposal",
        )
        if self.smoke_blocker_ids != expected_blockers:
            raise ValueError("exact Wave 1 smoke blocker inventory drift")
        if self.smoke_non_blocker_ids != expected_non_blockers:
            raise ValueError("exact Wave 1 smoke non-blocker inventory drift")
        return self


class SmokeMicroCensus(StrictModel):
    tier_id: SymbolicId
    status: Literal["required_not_completed"]
    required_before: tuple[SymbolicId, ...] = Field(min_length=1)
    complete: Literal[False]
    blocks_two_row_smoke: Literal[True]
    blocks_approximately_100_root_gate: Literal[False]
    may_invoke_lean: Literal[False]
    receipt_sha256: None
    receipt_scope: Literal["exact_selected_smoke_root"]
    receipt_hash_bound: Literal[True]
    selection_pool_hash_bound: Literal[True]
    selection_rule: Literal["minimum_stable_eligible_root_hash_v1"]
    requirements: tuple[SymbolicId, ...] = Field(min_length=1)


class SelectedWaveSamplingFrameCensus(StrictModel):
    tier_id: SymbolicId
    status: Literal["required_not_completed"]
    required_before: tuple[SymbolicId, ...] = Field(min_length=1)
    complete: Literal[False]
    blocks_two_row_smoke: Literal[False]
    blocks_approximately_100_root_gate: Literal[True]
    may_invoke_lean: Literal[False]
    receipt_sha256: None
    requirements: tuple[SymbolicId, ...] = Field(min_length=1)


class CompleteCrossSourceCensus(StrictModel):
    tier_id: SymbolicId
    status: Literal["required_not_completed"]
    required_before: tuple[SymbolicId, ...] = Field(min_length=1)
    complete: Literal[False]
    blocks_two_row_smoke: Literal[False]
    blocks_approximately_100_root_gate: Literal[False]
    may_invoke_lean: Literal[False]
    receipt_sha256: None
    requirements: tuple[SymbolicId, ...] = Field(min_length=1)


class CensusTiers(StrictModel):
    smoke_micro_census: SmokeMicroCensus
    selected_wave_sampling_frame_census: SelectedWaveSamplingFrameCensus
    complete_cross_source_census: CompleteCrossSourceCensus

    @model_validator(mode="after")
    def _exact_tiers(self) -> CensusTiers:
        smoke = self.smoke_micro_census
        selected = self.selected_wave_sampling_frame_census
        complete = self.complete_cross_source_census
        expected_smoke_requirements = (
            "pinned_source_identity_and_revision",
            "internal_gate_source_policy_eligibility",
            "exact_root_identity",
            "reproducible_project_toolchain_import_options_context",
            "closed_expr_construction_route",
            "root_level_blocklist_screen",
            "exact_duplicate_screen_over_hash_bound_smoke_pool",
            "operation_specific_typed_applicability",
            "smoke_pool_construction_rule_and_candidate_set_hash",
            "minimum_stable_eligible_root_hash_over_smoke_pool",
            "selected_root_is_minimum_over_bound_eligible_pool",
        )
        if (
            smoke.tier_id != "root_specific_smoke_micro_census_v1"
            or smoke.required_before != ("one_positive_one_negative_end_to_end_smoke",)
            or not smoke.blocks_two_row_smoke
            or smoke.requirements != expected_smoke_requirements
        ):
            raise ValueError("root-specific smoke micro-census contract drift")
        expected_selected_requirements = (
            "operation_project_candidate_pools",
            "deterministic_prefix_has_enough_eligible_roots",
            "exact_and_near_duplicate_clusters_over_candidate_pools",
            "source_domain_signature_strata",
            "operation_specific_proof_availability",
            "measured_applicability_denominators",
        )
        if (
            selected.tier_id != "selected_wave_sampling_frame_census_v1"
            or selected.required_before != ("approximately_100_roots_per_semantic_mechanism",)
            or selected.blocks_two_row_smoke
            or selected.requirements != expected_selected_requirements
        ):
            raise ValueError("selected-wave sampling-frame census contract drift")
        expected_complete = (
            "ten_k_pilot_decision",
            "production_row_count_decision",
            "multi_million_feasibility_claim",
            "scale_decision",
            "publication_decision",
        )
        expected_complete_requirements = (
            "raw_theorem_or_lemma_counts",
            "pinned_source_revision_and_release_state",
            "complete_cross_source_duplicate_unions",
            "exact_and_near_duplicate_clusters",
            "complete_source_strata",
            "source_domain_signature_strata",
            "exact_import_context_availability",
            "closed_expr_route_availability",
            "license_and_release_state",
            "realistic_root_and_yield_estimates",
        )
        if (
            complete.tier_id != "complete_cross_source_census_v1"
            or complete.required_before != expected_complete
            or complete.blocks_two_row_smoke
            or complete.requirements != expected_complete_requirements
        ):
            raise ValueError("complete cross-source census contract drift")
        return self


class SourceAuthorizationEntry(StrictModel):
    source_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    source_kind: Literal[
        "user_owned_huggingface_dataset",
        "pinned_apache_2_0_git_library",
    ]
    repository: NonEmptyStr
    revision: GitCommit
    source_artifact_sha256: Sha256 | None
    spdx_id: Literal["Apache-2.0"] | None
    authorization_basis: Literal[
        "owner_authorization_source_use_v2_and_pinned_revision",
        "pinned_revision_spdx_apache_2_0_and_hash_bound_license",
    ]
    authorization_evidence_sha256: Sha256
    exact_revision_pinned: Literal[True]
    internal_gate_eligible: Literal[True]
    pilot_eligible: Literal[True]
    redistribution_review_complete: Literal[False]
    publication_eligible: Literal[False]


class SourceAuthorization(StrictModel):
    policy_path: Literal["policies/source_use_v2.yaml"]
    policy_file_sha256: Sha256
    policy_semantic_hash: Sha256
    sources: tuple[SourceAuthorizationEntry, ...]
    internal_eligibility_does_not_authorize_gate: Literal[True]
    pilot_eligibility_does_not_authorize_ten_k: Literal[True]
    publication_requires_review: Literal[True]

    @model_validator(mode="after")
    def _exact_sources(self) -> SourceAuthorization:
        if (
            self.policy_file_sha256 != EXPECTED_SOURCE_USE_FILE_SHA256
            or self.policy_semantic_hash != EXPECTED_SOURCE_USE_SEMANTIC_HASH
        ):
            raise ValueError("source_use_v2 policy identity drift")
        if tuple(source.source_id for source in self.sources) != EXPECTED_PROJECT_IDS:
            raise ValueError("source authorization entries/order drift")
        expected_bases = (
            "owner_authorization_source_use_v2_and_pinned_revision",
            "pinned_revision_spdx_apache_2_0_and_hash_bound_license",
            "pinned_revision_spdx_apache_2_0_and_hash_bound_license",
            "pinned_revision_spdx_apache_2_0_and_hash_bound_license",
        )
        if tuple(source.authorization_basis for source in self.sources) != expected_bases:
            raise ValueError("source authorization basis drift")
        expected_identities = (
            (
                "compiler_data",
                "user_owned_huggingface_dataset",
                "formalmathatepfl/compiler_data",
                "ca37d4701b11022f183e72b7b96ff543a8a615d3",
                "c45145de5b681efd5aa265fdc90b9dc68d542321dd931d139cdead693360a81a",
                None,
                EXPECTED_SOURCE_USE_FILE_SHA256,
            ),
            (
                "cslib",
                "pinned_apache_2_0_git_library",
                "https://github.com/leanprover/cslib",
                "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
                None,
                "Apache-2.0",
                "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
            ),
            (
                "mathlib",
                "pinned_apache_2_0_git_library",
                "https://github.com/leanprover-community/mathlib4",
                "d568c8c09630de097a046763c17b9ea99f95f950",
                None,
                "Apache-2.0",
                "b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1",
            ),
            (
                "physlib",
                "pinned_apache_2_0_git_library",
                "https://github.com/leanprover-community/physlib",
                "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
                None,
                "Apache-2.0",
                "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
            ),
        )
        observed_identities = tuple(
            (
                source.source_id,
                source.source_kind,
                source.repository,
                source.revision,
                source.source_artifact_sha256,
                source.spdx_id,
                source.authorization_evidence_sha256,
            )
            for source in self.sources
        )
        if observed_identities != expected_identities:
            raise ValueError("source revision/license/authorization evidence drift")
        return self


class ProofAvailability(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    status: Literal["unknown"]
    scope: Literal["not_in_scope_for_n_proof"]
    proof_eligible: Literal[False]


class DuplicateInvariants(StrictModel):
    proof_is_sidecar_evidence_upgrade: Literal[True]
    proof_may_emit_additional_core_pair: Literal[False]
    model_facing_pair_multiplicity_maximum: Literal[1]
    canonical_unordered_pair_duplicate_screen_before_caps: Literal[True]
    same_label_duplicate_keep_minimum_stable_row_hash: Literal[True]
    conflicting_label_canonical_pair_rejected_globally: Literal[True]


class N31LaneContract(StrictModel):
    rubric_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    shared_semantic_mechanism_id: Literal["n31_required_guard_mutation"]
    rubric_projects: tuple[ProjectId, ...]
    proof_eligible_projects: tuple[ProjectId, ...]
    proof_eligibility_formula: tuple[
        Literal[
            "parent_n_rubric_applicable",
            "exact_source_proof_available",
            "exact_candidate_refutation_replays",
        ],
        ...,
    ]
    proof_eligibility_is_project_and_root_scoped: Literal[True]
    proof_availability_by_project: tuple[ProofAvailability, ...]
    proof_unavailability_outcome: Literal["not_in_scope_for_n_proof"]
    proof_unavailability_is_operation_failure: Literal[False]
    proof_unavailability_blocks_rubric: Literal[False]
    proof_unavailability_blocks_smoke: Literal[False]
    proof_unavailability_blocks_wave: Literal[False]
    proof_unavailability_blocks_conformance: Literal[False]
    proof_unavailability_blocks_approximately_100_root_gate: Literal[False]
    proof_unavailability_blocks_other_operations: Literal[False]
    same_parent_root_pool: Literal[True]
    independent_root_sampling_forbidden: Literal[True]
    proof_counts_against_own_cap: Literal[True]
    proof_counts_against_parent_semantic_cap: Literal[True]
    proof_counts_as_distinct_semantic_mechanism: Literal[False]
    parent_maximum_retained_share: StrictFloat
    proof_maximum_retained_share: StrictFloat
    natural_cap_denominator: Literal[
        "natural_model_facing_retained_semantic_pair_population_after_duplicate_conflict_screen_before_orientation"
    ]
    parent_semantic_union_key: Literal[
        "unique_parent_mutation_pair_id_including_proof_upgraded_subset"
    ]
    synthetic_denominator_separate: Literal[True]
    parent_production_admission_required: Literal[True]
    shared_family_id: Literal["N31"]
    shared_mechanism_superclass: Literal["required_guard_mutation"]
    shared_correlation_group_id: Literal["corr_n31_guard_drop"]
    shared_effective_diversity_group_id: Literal["corr_n31_guard_drop"]
    shared_heldout_group_id: Literal["corr_n31_guard_drop"]
    duplicate_invariants: DuplicateInvariants

    @model_validator(mode="after")
    def _proof_is_nested_and_optional(self) -> N31LaneContract:
        if self.rubric_projects != EXPECTED_PROJECT_IDS:
            raise ValueError("N31 rubric project scope drift")
        if self.proof_eligible_projects:
            raise ValueError("the frozen census supports zero proof-eligible projects")
        if tuple(item.project_id for item in self.proof_availability_by_project) != (
            EXPECTED_PROJECT_IDS
        ):
            raise ValueError("N31 proof-availability project inventory drift")
        if self.proof_eligibility_formula != (
            "parent_n_rubric_applicable",
            "exact_source_proof_available",
            "exact_candidate_refutation_replays",
        ):
            raise ValueError("N-PROOF project/root eligibility equation drift")
        if self.parent_maximum_retained_share != 0.01:
            raise ValueError("N31 parent semantic cap drift")
        if self.proof_maximum_retained_share != 0.005:
            raise ValueError("N31 proof subtype cap drift")
        if self.proof_maximum_retained_share > self.parent_maximum_retained_share:
            raise ValueError("N-PROOF cap cannot exceed its parent semantic cap")
        return self


class Wave2Proposal(StrictModel):
    status: Literal["proposed_not_admitted"]
    exact_operation_count: Literal[17]
    semantic_mechanism_count: Literal[15]
    operation_ids: tuple[OperationId, ...]
    n_proof_operation_ids: tuple[OperationId, ...]
    n_proof_ids_count_as_distinct_mechanisms: Literal[False]
    family_dimension_ids: tuple[SymbolicId, ...]
    implementation_authorized: Literal[False]
    implementation_started: Literal[False]
    gate_admitted: Literal[False]
    dimension_gate_admitted: Literal[False]
    execution_authorized: Literal[False]
    production_admitted: Literal[False]
    row_emission_authorized: Literal[False]
    ten_k_authorized: Literal[False]
    scale_authorized: Literal[False]
    publication_authorized: Literal[False]

    @model_validator(mode="after")
    def _exact_proposal(self) -> Wave2Proposal:
        if self.operation_ids != EXPECTED_WAVE2_OPERATION_IDS:
            raise ValueError("Wave 2 proposal operation IDs/order drift")
        if self.n_proof_operation_ids != EXPECTED_WAVE2_N_PROOF_IDS:
            raise ValueError("Wave 2 proposed proof-lane inventory drift")
        if self.family_dimension_ids != EXPECTED_WAVE2_DIMENSION_IDS:
            raise ValueError("Wave 2 proposed family/dimension inventory drift")
        return self


class Prohibitions(StrictModel):
    lean_execution_authorized: Literal[False]
    transform_execution_authorized: Literal[False]
    census_scale_processing_authorized: Literal[False]
    gate_execution_authorized: Literal[False]
    row_generation_authorized: Literal[False]
    model_facing_training_row_emission_authorized: Literal[False]
    wave2_implementation_authorized: Literal[False]
    production_admission_authorized: Literal[False]
    ten_k_pilot_authorized: Literal[False]
    bulk_generation_authorized: Literal[False]
    scale_authorized: Literal[False]
    training_authorized: Literal[False]
    publication_authorized: Literal[False]
    row_count_commitment_authorized: Literal[False]
    source_root_count_commitment_authorized: Literal[False]
    registry_rewrite_authorized: Literal[False]
    frozen_artifact_mutation_authorized: Literal[False]
    shared_contract_edit_authorized: Literal[False]


class EffectiveWaveReadinessOverlay(StrictModel):
    schema_version: Literal[1]
    overlay_id: SymbolicId
    overlay_version: Literal["0.3.3"]
    status: Literal["policy_overlay_authorized_execution_prohibited"]
    review_binding: ReviewBinding
    user_authorization: UserAuthorization
    frozen_dependencies: FrozenDependencies
    authorization_boundaries: AuthorizationBoundaries
    effective_wave1: EffectiveWave1
    census_tiers: CensusTiers
    source_authorization: SourceAuthorization
    n31_lane_contract: N31LaneContract
    wave2_proposal: Wave2Proposal
    prohibitions: Prohibitions

    @model_validator(mode="after")
    def _authority_separation(self) -> EffectiveWaveReadinessOverlay:
        if self.overlay_id != EXPECTED_OVERLAY_ID:
            raise ValueError("effective-readiness overlay identity drift")
        if self.user_authorization.exact_user_text_sha256 != sha256_hex(
            self.user_authorization.exact_user_text.encode("utf-8")
        ):
            raise ValueError("embedded user authorization text/hash mismatch")
        if self.user_authorization.exact_user_text_sha256 != EXPECTED_USER_AUTHORIZATION_SHA256:
            raise ValueError("user authorization differs from the approved revision 0.3.3 text")
        prohibited = self.prohibitions.model_dump(mode="python")
        if any(prohibited.values()):
            raise ValueError("the additive overlay cannot grant execution or release authority")
        return self


@dataclass(frozen=True, slots=True)
class LoadedEffectiveWaveState:
    loaded_overlay: LoadedConfig[EffectiveWaveReadinessOverlay]
    loaded_admission: LoadedWave1GateAdmission
    loaded_census: LoadedWave1SourceCensus
    config_file_sha256: str

    @property
    def config(self) -> EffectiveWaveReadinessOverlay:
        return self.loaded_overlay.config

    @property
    def path(self) -> Path:
        return self.loaded_overlay.path

    @property
    def config_hash(self) -> str:
        return self.loaded_overlay.config_hash


def _operation_map(loaded_admission: LoadedWave1GateAdmission) -> dict[str, OperationSpec]:
    policy = loaded_admission.loaded_base_policy.config
    return {
        operation.operation_id: operation
        for operation in (*policy.operations, *policy.synthetic_track.operations)
    }


def _expected_artifact_bindings() -> dict[str, tuple[str, str, str]]:
    return {
        "base_policy": (
            EXPECTED_BASE_POLICY_PATH,
            EXPECTED_BASE_POLICY_FILE_SHA256,
            EXPECTED_BASE_POLICY_SEMANTIC_HASH,
        ),
        "admission_v0_3_2": (
            EXPECTED_ADMISSION_PATH,
            EXPECTED_ADMISSION_FILE_SHA256,
            EXPECTED_ADMISSION_SEMANTIC_HASH,
        ),
        "clean_checkout_receipt_v0_3_2": (
            EXPECTED_CLEAN_RECEIPT_PATH,
            EXPECTED_CLEAN_RECEIPT_FILE_SHA256,
            EXPECTED_CLEAN_RECEIPT_SEMANTIC_HASH,
        ),
        "source_census_v0_3_2": (
            EXPECTED_CENSUS_PATH,
            EXPECTED_CENSUS_FILE_SHA256,
            EXPECTED_CENSUS_SEMANTIC_HASH,
        ),
        "n31_guard_bank_v0_3_2": (
            EXPECTED_N31_BANK_PATH,
            EXPECTED_N31_BANK_FILE_SHA256,
            EXPECTED_N31_BANK_SEMANTIC_HASH,
        ),
        "source_use_v2": (
            EXPECTED_SOURCE_USE_PATH,
            EXPECTED_SOURCE_USE_FILE_SHA256,
            EXPECTED_SOURCE_USE_SEMANTIC_HASH,
        ),
        "repr_gate_v0_3_1": (
            EXPECTED_REPR_GATE_PATH,
            EXPECTED_REPR_GATE_FILE_SHA256,
            EXPECTED_REPR_GATE_SEMANTIC_HASH,
        ),
        "repr_receipt_v0_3_1": (
            EXPECTED_REPR_RECEIPT_PATH,
            EXPECTED_REPR_RECEIPT_FILE_SHA256,
            EXPECTED_REPR_RECEIPT_SEMANTIC_HASH,
        ),
    }


def _validate_dependency_bindings(config: EffectiveWaveReadinessOverlay) -> None:
    dependencies = config.frozen_dependencies
    if dependencies.checkpoint_commit != EXPECTED_CHECKPOINT_COMMIT:
        raise EffectiveReadinessError("frozen admission-readiness checkpoint drift")
    for name, expected in _expected_artifact_bindings().items():
        binding = getattr(dependencies, name)
        if (binding.path, binding.file_sha256, binding.semantic_hash) != expected:
            raise EffectiveReadinessError(f"frozen dependency binding drift: {name}")
    base = dependencies.base_policy
    if (
        base.operation_count != 46
        or base.operation_registry_hash != EXPECTED_OPERATION_REGISTRY_HASH
    ):
        raise EffectiveReadinessError("46-operation registry identity drift")


def _validate_registry_projection(
    config: EffectiveWaveReadinessOverlay,
    loaded_admission: LoadedWave1GateAdmission,
) -> None:
    operations = _operation_map(loaded_admission)
    if len(operations) != 46:
        raise EffectiveReadinessError("the frozen registry must retain exactly 46 operations")
    for operation_id in (*EXPECTED_WAVE1_OPERATION_IDS, *EXPECTED_WAVE2_OPERATION_IDS):
        operation = operations.get(operation_id)
        if operation is None or operation.track.value != "natural":
            raise EffectiveReadinessError(
                f"overlay operation is absent/non-natural: {operation_id}"
            )

    admission_ids = tuple(
        operation.operation_id for operation in loaded_admission.config.approved_operations
    )
    if admission_ids != config.effective_wave1.operation_ids:
        raise EffectiveReadinessError("effective Wave 1 differs from the frozen admission receipt")

    rubric = operations[config.n31_lane_contract.rubric_operation_id]
    proof = operations[config.n31_lane_contract.proof_operation_id]
    if (
        proof.n_proof_subtype_of != rubric.operation_id
        or rubric.family_id != proof.family_id
        or rubric.mechanism_superclass != proof.mechanism_superclass
        or rubric.cap.maximum_retained_share
        != config.n31_lane_contract.parent_maximum_retained_share
        or proof.cap.maximum_retained_share != config.n31_lane_contract.proof_maximum_retained_share
    ):
        raise EffectiveReadinessError("N31 proof lane no longer matches its parent registry entry")

    accounting = loaded_admission.loaded_base_policy.config.operation_accounting_contract
    for group_name in ("correlation_groups", "effective_diversity_groups"):
        groups = getattr(accounting, group_name)
        matches = [group for group in groups if rubric.operation_id in group.operation_ids]
        if len(matches) != 1 or tuple(matches[0].operation_ids) != (
            rubric.operation_id,
            proof.operation_id,
        ):
            raise EffectiveReadinessError(f"N31 parent/proof {group_name} nesting drift")


def validate_effective_wave_state(
    config: EffectiveWaveReadinessOverlay,
    loaded_admission: LoadedWave1GateAdmission,
) -> None:
    """Validate the effective projection against every frozen input snapshot."""

    review = config.review_binding
    if (
        review.reviewed_checkpoint_commit != EXPECTED_CHECKPOINT_COMMIT
        or review.review_url != EXPECTED_REVIEW_URL
        or review.attachment_id != EXPECTED_REVIEW_ATTACHMENT_ID
        or review.attachment_raw_sha256 != EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256
    ):
        raise EffectiveReadinessError("GPT Pro review binding drift")
    _validate_dependency_bindings(config)
    _validate_registry_projection(config, loaded_admission)

    if not loaded_admission.config.authorization.gate_admission_recorded:
        raise EffectiveReadinessError("frozen Wave 1 gate admission is no longer recorded")
    if loaded_admission.config.authorization.bounded_gate_execution_may_start_now:
        raise EffectiveReadinessError("frozen admission unexpectedly permits gate execution")


def _repo_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise EffectiveReadinessError(f"effective-readiness dependency escapes repo: {relative}")
    return path


def _verify_repo_artifacts(root: Path, config: EffectiveWaveReadinessOverlay) -> None:
    for name, (_path, expected_file_hash, _semantic_hash) in _expected_artifact_bindings().items():
        binding = getattr(config.frozen_dependencies, name)
        observed = hash_file(_repo_path(root, binding.path))
        if observed != expected_file_hash:
            raise EffectiveReadinessError(f"frozen dependency raw-file drift: {name}")

    source_use_path = _repo_path(root, EXPECTED_SOURCE_USE_PATH)
    source_use = load_yaml_mapping(source_use_path)
    source_use_semantic_hash = hash_canonical(source_use)
    if source_use_semantic_hash != EXPECTED_SOURCE_USE_SEMANTIC_HASH:
        raise EffectiveReadinessError("source_use_v2 semantic hash drift")
    scope = source_use.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("namespace") != "formalmathatepfl/*"
        or source_use.get("access_basis") != "owner_confirmed_ownership"
        or source_use.get("research_use") is not True
        or source_use.get("external_model_processing") is not True
    ):
        raise EffectiveReadinessError("source_use_v2 owner/research authorization drift")


def _verify_frozen_snapshot_loaders(
    root: Path,
    config: EffectiveWaveReadinessOverlay,
) -> tuple[LoadedWave1GateAdmission, LoadedWave1SourceCensus]:
    admission = load_wave1_gate_admission(root)
    census = load_wave1_source_census(root)
    n31 = load_n31_guard_bank(root)
    if (
        admission.config_hash != config.frozen_dependencies.admission_v0_3_2.semantic_hash
        or census.config_hash != config.frozen_dependencies.source_census_v0_3_2.semantic_hash
        or n31.config_hash != config.frozen_dependencies.n31_guard_bank_v0_3_2.semantic_hash
    ):
        raise EffectiveReadinessError("typed frozen snapshot replay drift")
    if any(source.n31_source_proof.status != "unknown" for source in census.config.sources):
        raise EffectiveReadinessError(
            "v0.3.2 proof availability is no longer the frozen unknown state"
        )
    if tuple(source.source_id for source in census.config.sources) != EXPECTED_PROJECT_IDS:
        raise EffectiveReadinessError("source-census project inventory drift")
    overlay_sources = config.source_authorization.sources
    for overlay, source in zip(overlay_sources, census.config.sources, strict=True):
        if (
            overlay.source_id != source.source_id
            or overlay.repository != source.identity.repository
            or overlay.revision != source.identity.revision
            or overlay.source_artifact_sha256 != source.identity.source_artifact_sha256
        ):
            raise EffectiveReadinessError("source authorization/census identity drift")
        if source.source_id == "compiler_data":
            if (
                source.identity.repository_kind != "huggingface_dataset"
                or source.identity.repository != "formalmathatepfl/compiler_data"
                or overlay.authorization_evidence_sha256 != EXPECTED_SOURCE_USE_FILE_SHA256
                or overlay.authorization_basis
                != "owner_authorization_source_use_v2_and_pinned_revision"
            ):
                raise EffectiveReadinessError("compiler_data owner/revision binding drift")
        elif (
            source.identity.repository_kind != "git"
            or source.license.status != "identified_unreviewed"
            or source.license.spdx_id != "Apache-2.0"
            or source.license.evidence_sha256 is None
            or overlay.spdx_id != source.license.spdx_id
            or overlay.authorization_evidence_sha256 != source.license.evidence_sha256
            or overlay.authorization_basis
            != "pinned_revision_spdx_apache_2_0_and_hash_bound_license"
        ):
            raise EffectiveReadinessError(
                f"source authorization projection identity drift: {source.source_id} Apache binding"
            )
    return admission, census


def load_effective_wave_state(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedEffectiveWaveState:
    """Load revision 0.3.3 without starting Lean, transforms, or generation."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    config_path = (path or root / DEFAULT_EFFECTIVE_READINESS_PATH).resolve()
    expected_path = (root / DEFAULT_EFFECTIVE_READINESS_PATH).resolve()
    if not config_path.is_relative_to(resolved_root) or config_path != expected_path:
        raise EffectiveReadinessError("effective-readiness config path differs from frozen path")
    observed_file_hash = hash_file(config_path)
    if observed_file_hash != EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256:
        raise EffectiveReadinessError("effective-readiness YAML raw-file hash drift")
    loaded = load_config(config_path, EffectiveWaveReadinessOverlay)
    if loaded.config_hash != EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH:
        raise EffectiveReadinessError("effective-readiness YAML canonical hash drift")

    _verify_repo_artifacts(root, loaded.config)
    loaded_admission, loaded_census = _verify_frozen_snapshot_loaders(root, loaded.config)
    validate_effective_wave_state(loaded.config, loaded_admission)
    return LoadedEffectiveWaveState(
        loaded_overlay=loaded,
        loaded_admission=loaded_admission,
        loaded_census=loaded_census,
        config_file_sha256=observed_file_hash,
    )


__all__ = [
    "DEFAULT_EFFECTIVE_READINESS_PATH",
    "EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256",
    "EXPECTED_EFFECTIVE_CONFIG_HASH",
    "EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH",
    "EXPECTED_OPERATION_TO_MECHANISM",
    "EXPECTED_PROJECT_IDS",
    "EXPECTED_WAVE1_MECHANISM_IDS",
    "EXPECTED_WAVE1_OPERATION_IDS",
    "EXPECTED_WAVE2_OPERATION_IDS",
    "EffectiveReadinessError",
    "EffectiveWaveReadinessOverlay",
    "LoadedEffectiveWaveState",
    "load_effective_wave_state",
    "validate_effective_wave_state",
]

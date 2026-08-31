"""Strict policy and request construction for bounded Wave 1 live readiness.

Loading this module performs no Lean work.  It binds the accepted policy,
five frozen primary bundles, task-owned runtime sources, corrected fixture
contexts, resource ceiling, persistence contract, and all authorization
boundaries.  Gate execution and row emission are deliberately absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft1.wave1_readiness import (
    EXPECTED_PRIMARY_OPERATION_IDS,
    Wave1CacheKey,
    load_wave1_implementation_readiness,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]

DEFAULT_RUNTIME_CONFIG_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"
)
DEFAULT_RUNTIME_FIXTURE_PATH = Path("tests/fixtures/sft1/wave1_v0_3_6.yaml")
DEFAULT_POSITIVE_CHECKPOINT_RECEIPT_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_positive_live_checkpoint_v0_3_6.json"
)
DEFAULT_N31_PROPOSAL_RECEIPT_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_n31_resolution_proposal_v0_3_6.json"
)

# Filled after the additive artifacts are finalized.  They are deliberately
# nonzero and are asserted by the loader, so drift always fails closed.
EXPECTED_RUNTIME_CONFIG_FILE_SHA256 = (
    "c1df4530a515fadbfcddb3a728b5114475c850f561bf6cddc20d33f5cd43ad3c"
)
EXPECTED_RUNTIME_CONFIG_HASH = "69a6af5b0030af5adf6d3a4412f785b7428d234c7b85d92499dc85d723d3b8ba"
EXPECTED_RUNTIME_FIXTURE_FILE_SHA256 = (
    "67e5012f7c7e3bb5d4b2cb55d103f335aa6fb082d29baae434ace91010d035c1"
)
EXPECTED_RUNTIME_FIXTURE_HASH = "cac6cd12ace0945daf3a93b5357671a14d13d4d43008b00bbca7a61b94d34685"

EXPECTED_PROJECT_IDS = ("compiler_data", "cslib", "mathlib", "physlib")
EXPECTED_OPERATION_IDS = EXPECTED_PRIMARY_OPERATION_IDS
EXPECTED_OPERATION_MAP = {
    "P01_ALPHA_RENAME_SINGLE_V1": (
        "presentation_alpha",
        "P01_ALPHA_RENAME",
        "36b680c6c3407d0761de8af1b0c5e685ce0bc89fefed720bc46255c9bc218844",
        "ca485f300ecc818057f10877f0eec5c6b4b963fec2e0a574a6895f9d83357095",
        "da08e62d1dd1839da683bcdb8d4a73e40fd627498ffed78a20fed146e1088c39",
        "8fad8342d8925bf3018778aa8ae800c98303a6cecec7ae1f7610ed053d436454",
    ),
    "P15_SWAP_IFF_SIDES_V1": (
        "logical_symmetry",
        "P15_IFF_SWAP",
        "cefb0b1f138d40cc48df102d834e6f98735697213f4ab0d2688fe64943c7ed97",
        "ecad6c6ea110dff281b051045eb37846b194b54fa1a879bce11f5c04e5ed6bd4",
        "ca645071138232854736d49515efb37f4c602122b55e06ac75c973313a3d64cb",
        "0ba5ebb5669dcc3d9a14f64504d9f13fd1efe97f26e66dfa6aec7b1923a1b247",
    ),
    "P18_SYMMETRIZE_EQUALITY_V1": (
        "relation_symmetry",
        "P18_EQUALITY_SYMMETRY",
        "84d9a3615a30aca49e44705fc341f147b74b2885f4831244bb31ea7f97364472",
        "a79b0b92d5fca0360c38a9edaead1edec038df63bc2167da580bcf9d2c335b8d",
        "007d09f2a6b2037c22fd220fdc8b10371e0a7f4caceb37f101d5b296065fb205",
        "3ffb6d02097c003cc2bad10c41248d4c752c27f06b3f918cd3fa4b3344308a10",
    ),
    "P21_BETA_REDUCE_V1": (
        "definitional_beta",
        "P21_BETA_INTRO_REDUCE",
        "5a7ff8ce195cafd42b971bb5a07e23ec7e46b7d0376b7ab34c8dfa42459261f3",
        "dd364fd25afd801ae97c2f47ea59ca9f46cae0fff393da5a57b5842acb306d4c",
        "655c3d8a28820d66b6f0227cd8c26aa40f77e7217e5f2ca5366b3ac358df281b",
        "f49d9aa1ab1e6f5fee5394d542c09c38e122012843ac026da451f7e7a6b443f8",
    ),
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1": (
        "required_guard_mutation",
        "N31_DROP_REQUIRED_GUARD",
        "e10cae5e82a8b17bcfe9a5bd4d5811eea6b75c78e77b6c67a3c082a2c2feae5f",
        "03789414409dca332f807a6b53ed1fe4416fbfd13c377362119a910858fbf00d",
        "cb9dbe3695f197ecbacc65ae0f2c58ff78919bf727c64876a838da47ad7a38f8",
        "1bfc134f3c33481afddfe869865539536cd4f5c0b785cbbdbbf0e13b347b71dc",
    ),
}
EXPECTED_OPERATION_CONSTRUCTORS = {
    "P01_ALPHA_RENAME_SINGLE_V1": (
        "LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle",
        "LeanFaith.SFT1.Wave1.Selector.outerBinder",
    ),
    "P15_SWAP_IFF_SIDES_V1": (
        "LeanFaith.SFT1.Wave1.PrimaryOperation.p15SwapIffSides",
        "LeanFaith.SFT1.Wave1.Selector.outerTarget",
    ),
    "P18_SYMMETRIZE_EQUALITY_V1": (
        "LeanFaith.SFT1.Wave1.PrimaryOperation.p18SymmetrizeEquality",
        "LeanFaith.SFT1.Wave1.Selector.outerTarget",
    ),
    "P21_BETA_REDUCE_V1": (
        "LeanFaith.SFT1.Wave1.PrimaryOperation.p21BetaReduce",
        "LeanFaith.SFT1.Wave1.Selector.subexpr",
    ),
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1": (
        "LeanFaith.SFT1.Wave1.PrimaryOperation.n31DropRequiredGuardRubric",
        "LeanFaith.SFT1.Wave1.Selector.requiredGuard",
    ),
}
EXPECTED_SOURCE_BINDING_PATHS = {
    "fixed_reference_elaboration": "LeanFaith/Meta/SFT1/RepresentationGate.lean",
    "frozen_wave1_engine": "LeanFaith/Meta/SFT1/Wave1.lean",
    "p01_policy": "configs/transformations/sft1_value_first_v1/p01_identity_policy_v0_3_5.yaml",
    "composition_runtime": "src/leanfaith/sft1/wave1_runtime.py",
    "meta_cache_adapter": "src/leanfaith/sft1/wave1_meta_adapter.py",
    "live_readiness_runner": "src/leanfaith/sft1/wave1_live_runner.py",
    "lean_runtime_helper": "LeanFaith/Meta/SFT1/Wave1Runtime.lean",
    "runtime_fixtures": "tests/fixtures/sft1/wave1_v0_3_6.yaml",
}
EXPECTED_SOURCE_BINDING_ROLES = tuple(EXPECTED_SOURCE_BINDING_PATHS)
EXPECTED_POSITIVE_BANK_HASH_FIELDS = (
    "operation_id",
    "project_id",
    "toolchain_revision",
    "frozen_wave1_source_sha256",
    "runtime_helper_sha256",
    "operation_constructor",
    "dispatch_symbol",
    "checker_symbol",
    "anchor_hash",
    "symbol_resolution_receipt_hash",
)
EXPECTED_N31_ADMISSION_FIELDS = (
    "project_id",
    "bank_id",
    "resolved_lean_hash",
    "resolution_receipt_hash",
)
EXPECTED_FIXTURE_KIND_ORDER = ("success", "adversarial_rejection")
EXPECTED_TEMPLATE_IDS = (
    "p01_success_v0_3_6",
    "p01_reject_v0_3_6",
    "p15_success_v0_3_6",
    "p15_reject_v0_3_6",
    "p18_success_v0_3_6",
    "p18_reject_v0_3_6",
    "p21_success_v0_3_6",
    "p21_reject_v0_3_6",
    "n31_rubric_success_proposal_v0_3_6",
    "n31_rubric_reject_proposal_v0_3_6",
)
EXPECTED_TEMPLATE_CONTENT = (
    (
        "(x : Nat) → x + 1 = Nat.succ x",
        "applicable",
        None,
        "exact_single_binder_name_delta",
    ),
    (
        "True",
        "typedNotApplicable",
        "operationNotApplicable",
        "no_explicit_named_forall_at_selected_site",
    ),
    (
        "((0 : Nat) = 1) ↔ ((1 : Nat) = 0)",
        "applicable",
        None,
        "exact_distinct_iff_side_swap",
    ),
    (
        "((0 : Nat) = 0) ↔ ((0 : Nat) = 0)",
        "typedNotApplicable",
        "operationNotApplicable",
        "definitionally_equal_iff_sides",
    ),
    (
        "(0 : Nat) = 1",
        "applicable",
        None,
        "exact_distinct_equality_operand_swap",
    ),
    (
        "(0 : Nat) = 0",
        "typedNotApplicable",
        "operationNotApplicable",
        "definitionally_equal_equality_operands",
    ),
    (
        "(fun p : Prop => p ∧ (0 : Nat) = 0) ((0 : Nat) = 1)",
        "applicable",
        None,
        "exact_single_closed_argument_beta_reduction",
    ),
    (
        "(fun _p : Prop => (0 : Nat) = 0) True",
        "typedNotApplicable",
        "operationNotApplicable",
        "beta_argument_erases_complete_claim_input",
    ),
    (
        "(x : Nat) → (hx : x ≠ 0) → x / x = 1",
        "proposed_not_admitted",
        None,
        "exact_required_ne_zero_guard_delta_proposal",
    ),
    (
        "(x : Nat) → (hx : x ≠ 0) → (hpos : 0 < x) → x / x = 1",
        "proposal_rejected",
        "n31CompetingGuard",
        "retained_positive_guard_implies_removed_ne_zero_guard",
    ),
)
EXPECTED_PROJECT_CONTEXTS = {
    "compiler_data": (
        "ca37d4701b11022f183e72b7b96ff543a8a615d3",
        "mathlib",
        "d568c8c09630de097a046763c17b9ea99f95f950",
        "v4.31.0-rc1",
        "import Mathlib",
    ),
    "cslib": (
        "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
        "cslib",
        "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
        "v4.31.0-rc1",
        "import Cslib",
    ),
    "mathlib": (
        "d568c8c09630de097a046763c17b9ea99f95f950",
        "mathlib",
        "d568c8c09630de097a046763c17b9ea99f95f950",
        "v4.31.0-rc1",
        "import Mathlib",
    ),
    "physlib": (
        "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
        "physlib",
        "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
        "v4.30.0",
        "import Physlib",
    ),
}
EXPECTED_BACKEND_PROJECTS = (
    (
        "mathlib",
        "/storage/milikic/leanfaith/mathlib4",
        "d568c8c09630de097a046763c17b9ea99f95f950",
        "v4.31.0-rc1",
        ("compiler_data", "mathlib"),
    ),
    (
        "cslib",
        "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/checkouts/cslib",
        "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
        "v4.31.0-rc1",
        ("cslib",),
    ),
    (
        "physlib",
        "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/checkouts/physlib",
        "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
        "v4.30.0",
        ("physlib",),
    ),
)


class Wave1LiveReadinessError(ValueError):
    """Raised when additive live-readiness policy or fixtures drift."""


class SourceBinding(StrictModel):
    path: NonEmptyStr
    file_sha256: Sha256
    role: Literal[
        "fixed_reference_elaboration",
        "frozen_wave1_engine",
        "p01_policy",
        "composition_runtime",
        "meta_cache_adapter",
        "live_readiness_runner",
        "lean_runtime_helper",
        "runtime_fixtures",
    ]


class ResourceContract(StrictModel):
    task_id: Literal["SFT1"]
    shared_resource_claim_required_before_lean: Literal[True]
    initial_persistent_lean_workers: Literal[1]
    maximum_concurrent_lean_workers: Literal[2]
    maximum_combined_measured_rss_gib: Literal[40]
    increase_only_after_measurement: Literal[True]
    elab_async: Literal[False]
    per_row_process_spawn_allowed: Literal[False]
    compile_corpus_allowed: Literal[False]
    memory_hard_limit_mb_per_worker: Literal[24576]


class PersistenceContract(StrictModel):
    root: Literal[
        "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave1_readiness_v0_3_6"
    ]
    one_append_only_journal_per_project: Literal[True]
    durable_logs: Literal[True]
    central_cache_adapter: Literal[True]
    resume_command_recorded_before_detached_run: Literal[True]
    terminal_marker_required: Literal[True]
    detached_tmux_required_if_run_may_outlive_turn: Literal[True]
    model_facing_staging_area: Literal[False]


class BackendProjectBinding(StrictModel):
    backend_id: Literal["mathlib", "cslib", "physlib"]
    project_dir: NonEmptyStr
    project_revision: GitCommit
    lean_version: NonEmptyStr
    source_project_ids: tuple[Literal["compiler_data", "cslib", "mathlib", "physlib"], ...]


class RepresentationContract(StrictModel):
    route: Literal["closed_expr_in_session"]
    python_entrypoint: Literal["render_closed_expr_in_session"]
    endpoint_emitter: Literal["LeanFaith.GoalV1.emitClosedProp"]
    one_run_meta_per_pair: Literal[True]
    explicitly_unrolled_endpoint_count: Literal[2]
    same_persistent_request: Literal[True]
    complete_sidecars_persisted: Literal[True]
    model_facing_projection: Literal["sidecar.core_text()"]
    candidate_declaration_allowed: Literal[False]
    candidate_proof_allowed: Literal[False]
    copied_renderer_allowed: Literal[False]
    text_reelaboration_allowed: Literal[False]
    goal_v1_compilation_allowed: Literal[False]
    forbidden_rendered_substrings: tuple[Literal["[anonymous]", "⋯"], ...]

    @model_validator(mode="after")
    def _exact_render_contract(self) -> RepresentationContract:
        if self.forbidden_rendered_substrings != ("[anonymous]", "⋯"):
            raise ValueError("forbidden rendered-substring inventory/order drift")
        return self


class PositiveBankHashConvention(StrictModel):
    convention_id: Literal["positive_live_resolved_runtime_anchor_bundle_v1"]
    canonical_hash_fields: tuple[
        Literal[
            "operation_id",
            "project_id",
            "toolchain_revision",
            "frozen_wave1_source_sha256",
            "runtime_helper_sha256",
            "operation_constructor",
            "dispatch_symbol",
            "checker_symbol",
            "anchor_hash",
            "symbol_resolution_receipt_hash",
        ],
        ...,
    ]
    zero_or_authored_yaml_substitution_allowed: Literal[False]
    required_for_bank_resolved_lean_hash_of_positive_operations: Literal[True]

    @model_validator(mode="after")
    def _exact_fields(self) -> PositiveBankHashConvention:
        if self.canonical_hash_fields != EXPECTED_POSITIVE_BANK_HASH_FIELDS:
            raise ValueError("positive resolved-anchor field order drift")
        return self


class LeanEnvironmentContract(StrictModel):
    environment_lock_path: Literal["configs/environment.lock.yaml"]
    environment_lock_file_sha256: Sha256
    environment_schema_version: Literal[1]
    lean_interact_version: Literal["0.11.4"]
    repl_revision: Literal["augustepoiroux/repl@lean-interact-0.11.4"]
    server_mode: Literal["stable"]
    timeout_seconds_per_request: Literal[300]
    infrastructure_retry_max_attempts: Literal[2]
    retried_statuses: tuple[Literal["crash", "internal_error"], ...]
    semantic_failures_retried: Literal[False]
    heartbeat_seconds: Literal[30]

    @model_validator(mode="after")
    def _exact_retry_inventory(self) -> LeanEnvironmentContract:
        if self.retried_statuses != ("crash", "internal_error"):
            raise ValueError("live readiness retry-status inventory/order drift")
        return self


class PreambleContract(StrictModel):
    assembler_version: Literal["sft1_wave1_import_stripped_concat_v1"]
    ordered_source_roles: tuple[
        Literal[
            "fixed_reference_elaboration",
            "frozen_wave1_engine",
            "lean_runtime_helper",
        ],
        ...,
    ]
    import_line_policy: Literal["remove_lines_starting_exactly_import_space_v1"]
    expected_removed_import_line_count: Literal[3]
    assembled_preamble_sha256: Sha256
    hash_reviewed_before_lean: Literal[True]
    endpoint_declarations_allowed: Literal[False]
    proof_or_sorry_allowed: Literal[False]

    @model_validator(mode="after")
    def _exact_preamble_sources(self) -> PreambleContract:
        if self.ordered_source_roles != (
            "fixed_reference_elaboration",
            "frozen_wave1_engine",
            "lean_runtime_helper",
        ):
            raise ValueError("preamble source order drift")
        return self


class CacheContract(StrictModel):
    complete_wave1_cache_key_field_count: Literal[30]
    central_evidence_cache_adapter_required: Literal[True]
    cache_hit_requires_evidence_replay: Literal[True]
    immutable_no_overwrite: Literal[True]
    positive_bank_hash_convention: PositiveBankHashConvention
    n31_bank_hash_is_exact_resolved_typed_bank_hash: Literal[True]
    imports_hash_convention: Literal["sha256_utf8_exact_import_header_v1"]
    options_hash_convention: Literal["sha256_canonical_sorted_compile_options_v1"]
    synthesized_instance_hash_convention: Literal[
        "sha256_canonical_typed_instance_expr_inventory_entry_v1"
    ]
    empty_instance_inventory_requires_typed_receipt: Literal[True]
    environment_hash_convention: Literal["compile_context_fingerprint_v1"]


class RuntimeOperationBinding(StrictModel):
    operation_id: OperationId
    mechanism_superclass: NonEmptyStr
    inverse_token: NonEmptyStr
    registry_entry_hash: Sha256
    anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    fixture_aggregate_hash: Sha256
    runtime_fixture_bundle_hash: Sha256
    operation_constructor: NonEmptyStr
    selector_constructor: NonEmptyStr
    dispatch_symbol: Literal["LeanFaith.SFT1.Wave1.dispatchAt"]
    checker_symbol: Literal["LeanFaith.SFT1.Wave1.replayCertificate"]
    transparency: Literal["none", "reducible"]
    allowed_axiom_profile: Literal["constructive_kernel", "classical_recorded"]
    runtime_status: Literal[
        "positive_implementation_authorized_pending_live_receipt",
        "n31_resolution_proposal_only_not_admitted",
    ]


class N31Contract(StrictModel):
    parent_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    optional_proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    frozen_admitted_identity_count: Literal[0]
    proposal_resolution_authorized: Literal[True]
    activation_authorized: Literal[False]
    proposal_result_status: Literal["proposed_not_admitted"]
    n_proof_optional_per_project_and_root: Literal[True]
    n_proof_unavailability_blocks_n_rubric: Literal[False]
    n_proof_independent_root_pool_allowed: Literal[False]
    exact_user_admission_fields: tuple[
        Literal["project_id", "bank_id", "resolved_lean_hash", "resolution_receipt_hash"], ...
    ]
    stop_after_proposal_receipt: Literal[True]

    @model_validator(mode="after")
    def _exact_admission_fields(self) -> N31Contract:
        if self.exact_user_admission_fields != EXPECTED_N31_ADMISSION_FIELDS:
            raise ValueError("N31 exact admission field order drift")
        return self


class N31ProposalBankEntry(StrictModel):
    entry_id: Literal["n31_ne_zero_hdiv_nat_v0_3_6", "n31_positive_hdiv_nat_v0_3_6"]
    guard_shape_id: Literal["ne_zero_guard_v1", "positive_guard_v1"]
    guard_head_name: Literal["Ne", "LT.lt"]
    guard_argument_count: Literal[3, 4]
    guard_role_argument_indices: tuple[int, ...]
    guard_instance_or_type_argument_indices: tuple[int, ...]
    guard_exact_zero_argument_index: Literal[2]
    target_head_name: Literal["HDiv.hDiv"]
    target_argument_count: Literal[6]
    target_role_argument_indices: tuple[int, ...]
    target_instance_or_type_argument_indices: tuple[int, ...]


class N31RetainedProposalPattern(StrictModel):
    shape_id: Literal["eq_zero_retained_v1"]
    head_name: Literal["Eq"]
    argument_count: Literal[3]
    role_paths: tuple[tuple[int, ...], ...]
    instance_or_type_paths: tuple[tuple[int, ...], ...]
    exact_zero_paths: tuple[tuple[int, ...], ...]


class N31ProposalImplication(StrictModel):
    premise_shape_id: Literal["positive_guard_v1"]
    conclusion_shape_id: Literal["ne_zero_guard_v1"]


class N31ProposalContradiction(StrictModel):
    retained_shape_id: Literal["eq_zero_retained_v1"]
    removed_shape_id: Literal["ne_zero_guard_v1"]


class N31ProposalBank(StrictModel):
    template_id: Literal["sft1_n31_nat_ne_zero_hdiv_proposal_template_v0_3_6"]
    bank_id: Literal["sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6"]
    status: Literal["proposed_not_admitted"]
    project_ids: tuple[Literal["compiler_data", "cslib", "mathlib", "physlib"], ...]
    zero_term: Literal["(0 : Nat)"]
    retained_witness_term: Literal["(1 : Nat) = 0"]
    entries: tuple[N31ProposalBankEntry, ...]
    retained_contradiction_patterns: tuple[N31RetainedProposalPattern, ...]
    implications: tuple[N31ProposalImplication, ...]
    contradictions: tuple[N31ProposalContradiction, ...]
    phase_two_source_template_id: Literal["n31_rubric_success_proposal_v0_3_6"]
    phase_two_selector_guard_ordinal: Literal[1]
    phase_two_selector_target_path: Literal["/"]
    phase_two_selector_bank_entry_id: Literal["n31_ne_zero_hdiv_nat_v0_3_6"]
    reachability_mode_id: Literal["explicit_telescope_witness_and_retained_hypothesis_proofs"]
    reachability_assignment_terms: tuple[Literal["(1 : Nat)", "Nat.one_ne_zero"], ...]
    phase_one_identity_hash_fields_empty: Literal[True]
    semantic_success_conformance_performed: Literal[False]
    semantic_adversarial_conformance_performed: Literal[False]
    activation_exposed: Literal[False]

    @model_validator(mode="after")
    def _exact_narrow_proposal(self) -> N31ProposalBank:
        if self.project_ids != EXPECTED_PROJECT_IDS:
            raise ValueError("N31 proposal project inventory/order drift")
        observed_entries = tuple(
            (
                item.entry_id,
                item.guard_shape_id,
                item.guard_head_name,
                item.guard_argument_count,
                item.guard_role_argument_indices,
                item.guard_instance_or_type_argument_indices,
                item.target_role_argument_indices,
                item.target_instance_or_type_argument_indices,
            )
            for item in self.entries
        )
        expected_entries = (
            (
                "n31_ne_zero_hdiv_nat_v0_3_6",
                "ne_zero_guard_v1",
                "Ne",
                3,
                (1,),
                (0,),
                (5,),
                (0,),
            ),
            (
                "n31_positive_hdiv_nat_v0_3_6",
                "positive_guard_v1",
                "LT.lt",
                4,
                (3,),
                (0,),
                (5,),
                (0,),
            ),
        )
        if observed_entries != expected_entries:
            raise ValueError("N31 proposal entry inventory drift")
        retained = self.retained_contradiction_patterns
        if len(retained) != 1 or (
            retained[0].role_paths,
            retained[0].instance_or_type_paths,
            retained[0].exact_zero_paths,
        ) != (((1,),), ((0,),), ((2,),)):
            raise ValueError("N31 proposal retained-pattern path drift")
        if len(self.implications) != 1 or len(self.contradictions) != 1:
            raise ValueError("N31 proposal rule inventory drift")
        if self.reachability_assignment_terms != ("(1 : Nat)", "Nat.one_ne_zero"):
            raise ValueError("N31 proposal reachability assignment drift")
        return self


class Authorization(StrictModel):
    zero_lean_censuses: Literal[True]
    task_owned_wave1_implementation: Literal[True]
    bounded_live_compile_and_fixture_replay: Literal[True]
    wave1_gate_execution: Literal[False]
    model_facing_rows: Literal[False]
    wave2_implementation_or_execution: Literal[False]
    production_admission: Literal[False]
    ten_k: Literal[False]
    scale: Literal[False]
    training: Literal[False]
    publication: Literal[False]
    shared_contract_edit_by_sft1: Literal[False]


class ReadinessState(StrictModel):
    implementation_ready: Literal[False]
    p01_runtime_ready: Literal[False]
    all_five_live_bundles_ready: Literal[False]
    persistent_meta_adapter_ready: Literal[False]
    central_cache_replay_ready: Literal[False]
    n31_active: Literal[False]
    positive_checkpoint_receipt_path: None
    positive_checkpoint_receipt_hash: None
    n31_proposal_receipt_path: None
    n31_proposal_receipt_hash: None
    combined_five_mechanism_receipt_path: None
    combined_five_mechanism_receipt_hash: None
    gate_execution_enabled: Literal[False]


class PositiveResolvedAnchorInput(StrictModel):
    """Exact live identity used for bankless positive cache-key fields."""

    operation_id: Literal[
        "P01_ALPHA_RENAME_SINGLE_V1",
        "P15_SWAP_IFF_SIDES_V1",
        "P18_SYMMETRIZE_EQUALITY_V1",
        "P21_BETA_REDUCE_V1",
    ]
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    toolchain_revision: NonEmptyStr
    frozen_wave1_source_sha256: Literal[
        "7d4c27e1fd631cc1ba2f8de7cacec1eca618280c12c8ac351d9544a06e94ba4d"
    ]
    runtime_helper_sha256: Sha256
    operation_constructor: NonEmptyStr
    dispatch_symbol: Literal["LeanFaith.SFT1.Wave1.dispatchAt"]
    checker_symbol: Literal["LeanFaith.SFT1.Wave1.replayCertificate"]
    anchor_hash: Sha256
    symbol_resolution_receipt_hash: Sha256


def compute_positive_resolved_anchor_hash(value: PositiveResolvedAnchorInput) -> str:
    """Compute the sole permitted positive ``bank_resolved_lean_hash``."""

    expected = EXPECTED_OPERATION_MAP[value.operation_id]
    if value.anchor_hash != expected[3]:
        raise Wave1LiveReadinessError("positive resolved-anchor input has the wrong anchor hash")
    return hash_canonical(value.model_dump(mode="json"))


_N31_EXTERNAL_HASH_CONTRACT = {
    "algorithm": "sha256",
    "canonicalization": "python_canonical_json_utf8_v1",
    "resolved_lean_hash_preimage_field": "bank_fingerprint_payload",
    "resolution_receipt_hash_preimage_field": "resolution_receipt_hash_preimage_payload",
    "identity_equality_rechecked_in_second_meta_request": True,
    "payload_digest_verification_owned_by_strict_runner": True,
}

_N31_PHASE_ONE_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "receipt_id",
        "source_version",
        "proposal",
        "bank_fingerprint_payload",
        "resolution_receipt_hash_preimage_payload",
        "external_hash_installation_contract",
        "candidate_constructed",
        "candidate_exposed",
        "semantic_conformance_performed",
        "row_or_gate_emitted",
    }
)
_N31_PHASE_TWO_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "receipt_id",
        "source_version",
        "operation_id",
        "identity",
        "expected_resolved_lean_hash",
        "expected_resolution_receipt_hash",
        "expected_hashes_nonempty",
        "expected_hashes_are_lower_hex_sha256",
        "identity_matches_expected_hashes",
        "external_hash_computation_performed_in_lean",
        "external_strict_runner_hash_verification_required",
        "source_expr_hash",
        "selector",
        "reachability",
        "proposal",
        "bank_fingerprint_payload",
        "resolution_receipt_hash_preimage_payload",
        "external_hash_installation_contract",
        "proposal_resolution_passed",
        "frozen_admission_is_empty",
        "identity_absent_from_frozen_admission",
        "frozen_dispatch_rejected_as_unadmitted_bank",
        "rejection_reason",
        "private_semantic_checker_available",
        "semantic_conformance_performed",
        "candidate_constructed",
        "candidate_exposed",
        "activation_exposed",
        "row_or_gate_emitted",
    }
)
_N31_PROPOSAL_KEYS = frozenset(
    {
        "operation_id",
        "identity",
        "identity_project_and_bank_nonempty",
        "resolved_lean_hash_populated",
        "resolution_receipt_hash_populated",
        "entry_ids_unique",
        "retained_shape_ids_unique",
        "entries",
        "retained_patterns",
        "selectable_guard_definitions_coherent",
        "retained_shapes_disjoint_from_selectable",
        "implication_references_resolve",
        "contradiction_references_resolve",
        "all_entry_resolutions_passed",
        "all_retained_pattern_resolutions_passed",
        "all_names_resolved",
        "all_arities_resolved",
        "all_type_and_instance_constraints_resolved",
        "frozen_admission_is_empty",
        "proposed_identity_already_admitted",
        "private_semantic_checker_available",
        "semantic_success_conformance_performed",
        "semantic_adversarial_conformance_performed",
        "activation_exposed",
        "candidate_exposed",
        "proposal_resolution_passed",
    }
)
_N31_BANK_FINGERPRINT_KEYS = frozenset(
    {
        "basis_id",
        "sha256_input_contract",
        "structural_expr_fingerprint_id",
        "identity",
        "entries",
        "retained_contradiction_patterns",
        "implications",
        "contradictions",
        "resolved_entries",
        "resolved_retained_patterns",
    }
)
_N31_RECEIPT_PREIMAGE_KEYS = frozenset(
    {
        "basis_id",
        "sha256_input_contract",
        "bank_fingerprint_payload",
        "operation_id",
        "identity_project_and_bank_nonempty",
        "entry_ids_unique",
        "retained_shape_ids_unique",
        "selectable_guard_definitions_coherent",
        "retained_shapes_disjoint_from_selectable",
        "implication_references_resolve",
        "contradiction_references_resolve",
        "all_entry_resolutions_passed",
        "all_retained_pattern_resolutions_passed",
        "all_names_resolved",
        "all_arities_resolved",
        "all_type_and_instance_constraints_resolved",
        "frozen_admission_is_empty",
        "proposed_identity_already_admitted",
        "private_semantic_checker_available",
        "semantic_success_conformance_performed",
        "semantic_adversarial_conformance_performed",
        "activation_exposed",
        "proposal_resolution_passed",
    }
)


def n31_phase_receipt_id(
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"],
    phase: Literal["phase_one", "phase_two"],
) -> str:
    """Return the sole receipt ID admitted for one N31 resolution phase."""

    return f"sft1_wave1_n31_resolution_proposal_v0_3_6:{project_id}:{phase}"


def compute_n31_proposal_bank_template_hash(bank: N31ProposalBank) -> str:
    """Hash the authored, still-unadmitted N31 proposal template."""

    return hash_canonical(bank.model_dump(mode="json"))


def _require_exact_json_keys(
    value: dict[str, object], expected: frozenset[str], label: str
) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{label} field inventory drift")


def _require_decimal_u64(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
        or int(value) >= 2**64
    ):
        raise ValueError(f"{label} is not a canonical decimal UInt64")
    return value


def _require_n31_resolved_head(
    value: object, *, expected_name: str, expected_argument_count: int
) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 resolved head is not an object")
    _require_exact_json_keys(
        value,
        frozenset(
            {
                "name",
                "expected_argument_count",
                "observed_argument_count",
                "declaration_type_hash",
                "argument_binder_info_tags",
                "declaration_found",
                "declaration_type_closed",
                "arity_matches",
            }
        ),
        "N31 resolved head",
    )
    tags = value["argument_binder_info_tags"]
    if (
        value["name"] != expected_name
        or value["expected_argument_count"] != expected_argument_count
        or value["observed_argument_count"] != expected_argument_count
        or not isinstance(tags, list)
        or len(tags) != expected_argument_count
        or any(tag not in {"default", "implicit", "strictImplicit", "instImplicit"} for tag in tags)
        or value["declaration_found"] is not True
        or value["declaration_type_closed"] is not True
        or value["arity_matches"] is not True
    ):
        raise ValueError("N31 resolved head evidence failed")
    _require_decimal_u64(value["declaration_type_hash"], "N31 declaration type hash")


def _require_n31_entry_resolution(
    value: object,
    *,
    entry_id: str,
    guard_shape_id: str,
    guard_head_name: str,
    guard_argument_count: int,
) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 entry resolution is not an object")
    _require_exact_json_keys(
        value,
        frozenset(
            {
                "entry_id",
                "guard_shape_id",
                "guard_head",
                "target_head",
                "guard_fixed_heads",
                "target_fixed_heads",
                "guard_nested_heads",
                "target_nested_heads",
                "guard_role_indices_in_range",
                "target_role_indices_in_range",
                "guard_instance_or_type_indices_in_range",
                "target_instance_or_type_indices_in_range",
                "guard_role_binder_infos_match",
                "target_role_binder_infos_match",
                "guard_instance_or_type_binder_infos_match",
                "target_instance_or_type_binder_infos_match",
                "guard_role_observed_binder_info_tags",
                "target_role_observed_binder_info_tags",
                "guard_instance_or_type_observed_binder_info_tags",
                "target_instance_or_type_observed_binder_info_tags",
                "structural_shape_resolved",
                "exact_constraint_terms_closed_and_typed",
                "passed",
            }
        ),
        "N31 entry resolution",
    )
    _require_n31_resolved_head(
        value["guard_head"],
        expected_name=guard_head_name,
        expected_argument_count=guard_argument_count,
    )
    _require_n31_resolved_head(
        value["target_head"], expected_name="HDiv.hDiv", expected_argument_count=6
    )
    empty_resolution_fields = (
        "guard_fixed_heads",
        "target_fixed_heads",
        "guard_nested_heads",
        "target_nested_heads",
    )
    required_true = (
        "guard_role_indices_in_range",
        "target_role_indices_in_range",
        "guard_instance_or_type_indices_in_range",
        "target_instance_or_type_indices_in_range",
        "guard_role_binder_infos_match",
        "target_role_binder_infos_match",
        "guard_instance_or_type_binder_infos_match",
        "target_instance_or_type_binder_infos_match",
        "structural_shape_resolved",
        "exact_constraint_terms_closed_and_typed",
        "passed",
    )
    expected_tags = {
        "guard_role_observed_binder_info_tags": ["default"],
        "target_role_observed_binder_info_tags": ["default"],
    }
    instance_tag_fields = (
        "guard_instance_or_type_observed_binder_info_tags",
        "target_instance_or_type_observed_binder_info_tags",
    )
    if (
        value["entry_id"] != entry_id
        or value["guard_shape_id"] != guard_shape_id
        or any(value[field] != [] for field in empty_resolution_fields)
        or any(value[field] is not True for field in required_true)
        or any(value[field] != tags for field, tags in expected_tags.items())
        or any(
            not isinstance(value[field], list)
            or len(value[field]) != 1
            or value[field][0] not in {"implicit", "strictImplicit", "instImplicit"}
            for field in instance_tag_fields
        )
    ):
        raise ValueError("N31 entry resolution detail failed")


def _require_n31_application_path(value: object, expected: list[int]) -> None:
    if not isinstance(value, dict) or value != {"argument_indices": expected}:
        raise ValueError("N31 application path drift")


def _require_n31_path_resolution(
    value: object, *, expected_path: list[int], expected_role_explicit: bool
) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 path resolution is not an object")
    _require_exact_json_keys(
        value,
        frozenset(
            {
                "path",
                "expected_role_explicit",
                "steps",
                "selected_expr_hash",
                "selected_type_hash",
                "selected_binder_info",
                "selected_binder_info_tag",
                "path_resolved",
                "binder_info_class_matches",
                "passed",
            }
        ),
        "N31 path resolution",
    )
    _require_n31_application_path(value["path"], expected_path)
    steps = value["steps"]
    if (
        value["expected_role_explicit"] is not expected_role_explicit
        or not isinstance(steps, list)
        or len(steps) != len(expected_path)
        or value["path_resolved"] is not True
        or value["binder_info_class_matches"] is not True
        or value["passed"] is not True
        or value["selected_binder_info"]
        not in {"default", "implicit", "strictImplicit", "instImplicit"}
        or value["selected_binder_info_tag"] != value["selected_binder_info"]
    ):
        raise ValueError("N31 path resolution evidence failed")
    _require_decimal_u64(value["selected_expr_hash"], "N31 selected expression hash")
    _require_decimal_u64(value["selected_type_hash"], "N31 selected type hash")
    for ordinal, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError("N31 path step is not an object")
        _require_exact_json_keys(
            step,
            frozenset(
                {
                    "head_name",
                    "observed_argument_count",
                    "selected_argument_index",
                    "selected_argument_binder_info",
                    "selected_argument_binder_info_tag",
                    "declaration_type_hash",
                    "selected_expr_hash",
                    "selected_type_hash",
                    "passed",
                }
            ),
            "N31 path step",
        )
        if (
            step["selected_argument_index"] != expected_path[ordinal]
            or step["selected_argument_binder_info"]
            not in {"default", "implicit", "strictImplicit", "instImplicit"}
            or step["selected_argument_binder_info_tag"] != step["selected_argument_binder_info"]
            or step["passed"] is not True
            or not isinstance(step["head_name"], str)
            or not isinstance(step["observed_argument_count"], int)
        ):
            raise ValueError("N31 path-step evidence failed")
        for field in ("declaration_type_hash", "selected_expr_hash", "selected_type_hash"):
            _require_decimal_u64(step[field], f"N31 path-step {field}")


def _require_n31_retained_resolution(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 retained-pattern resolution is not an object")
    _require_exact_json_keys(
        value,
        frozenset(
            {
                "shape_id",
                "head",
                "nested_heads",
                "witness_found_uniquely",
                "witness_expr_hash",
                "witness_type_hash",
                "witness_is_typed_prop",
                "root_head_matches",
                "root_arity_matches",
                "role_path_resolutions",
                "instance_or_type_path_resolutions",
                "nested_head_witness_resolutions",
                "literal_witness_resolutions",
                "exact_expr_witness_resolutions",
                "paths_nonempty",
                "constraint_witness_replays_passed",
                "structural_shape_resolved",
                "exact_constraint_terms_closed_and_typed",
                "passed",
            }
        ),
        "N31 retained-pattern resolution",
    )
    _require_n31_resolved_head(value["head"], expected_name="Eq", expected_argument_count=3)
    required_true = (
        "witness_found_uniquely",
        "witness_is_typed_prop",
        "root_head_matches",
        "root_arity_matches",
        "paths_nonempty",
        "constraint_witness_replays_passed",
        "structural_shape_resolved",
        "exact_constraint_terms_closed_and_typed",
        "passed",
    )
    roles = value["role_path_resolutions"]
    instances = value["instance_or_type_path_resolutions"]
    exact = value["exact_expr_witness_resolutions"]
    if (
        value["shape_id"] != "eq_zero_retained_v1"
        or value["nested_heads"] != []
        or any(value[field] is not True for field in required_true)
        or value["nested_head_witness_resolutions"] != []
        or value["literal_witness_resolutions"] != []
        or not isinstance(roles, list)
        or len(roles) != 1
        or not isinstance(instances, list)
        or len(instances) != 1
        or not isinstance(exact, list)
        or len(exact) != 1
    ):
        raise ValueError("N31 retained-pattern resolution evidence failed")
    _require_decimal_u64(value["witness_expr_hash"], "N31 retained witness expression hash")
    _require_decimal_u64(value["witness_type_hash"], "N31 retained witness type hash")
    _require_n31_path_resolution(roles[0], expected_path=[1], expected_role_explicit=True)
    _require_n31_path_resolution(instances[0], expected_path=[0], expected_role_explicit=False)
    exact_resolution = exact[0]
    if not isinstance(exact_resolution, dict):
        raise ValueError("N31 exact-expression witness resolution is not an object")
    _require_exact_json_keys(
        exact_resolution,
        frozenset(
            {
                "path",
                "expected_expr_hash",
                "selected_expr_hash",
                "path_resolved",
                "exact_expr_matches",
                "passed",
            }
        ),
        "N31 exact-expression witness resolution",
    )
    _require_n31_application_path(exact_resolution["path"], [2])
    if (
        exact_resolution["path_resolved"] is not True
        or exact_resolution["exact_expr_matches"] is not True
        or exact_resolution["passed"] is not True
    ):
        raise ValueError("N31 exact-expression witness replay failed")
    expected_hash = _require_decimal_u64(
        exact_resolution["expected_expr_hash"], "N31 retained expected expression hash"
    )
    selected_hash = _require_decimal_u64(
        exact_resolution["selected_expr_hash"], "N31 retained selected expression hash"
    )
    if selected_hash != expected_hash:
        raise ValueError("N31 retained exact-expression hashes differ")


def _require_n31_bank_fingerprint(
    value: object, *, project_id: str, bank_id: str, proposal: dict[str, object]
) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 bank fingerprint payload is not an object")
    _require_exact_json_keys(value, _N31_BANK_FINGERPRINT_KEYS, "N31 bank fingerprint")
    identity = value["identity"]
    if (
        value["basis_id"] != "sft1_n31_structural_bank_fingerprint_payload_v0_3_6"
        or value["sha256_input_contract"] != "python_canonical_json_utf8_v1"
        or value["structural_expr_fingerprint_id"] != "lean_hashable_expr_uint64_decimal_v1"
        or identity
        != {
            "project_id": project_id,
            "bank_id": bank_id,
            "resolved_lean_hash": "",
            "resolution_receipt_hash": "",
        }
    ):
        raise ValueError("N31 bank fingerprint identity drift")
    entries = value["entries"]
    expected_entries = (
        ("n31_ne_zero_hdiv_nat_v0_3_6", "ne_zero_guard_v1", "Ne", 3, [1], [0]),
        ("n31_positive_hdiv_nat_v0_3_6", "positive_guard_v1", "LT.lt", 4, [3], [0]),
    )
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("N31 bank entry inventory drift")
    zero_hashes: list[str] = []
    bank_entry_keys = frozenset(
        {
            "entry_id",
            "guard_shape_id",
            "guard_head_name",
            "guard_argument_count",
            "guard_role_argument_indices",
            "guard_instance_or_type_argument_indices",
            "guard_fixed_heads",
            "guard_nested_heads",
            "guard_literal_constraints",
            "guard_exact_expr_constraints",
            "target_head_name",
            "target_argument_count",
            "target_role_argument_indices",
            "target_instance_or_type_argument_indices",
            "target_fixed_heads",
            "target_nested_heads",
            "target_literal_constraints",
            "target_exact_expr_constraints",
        }
    )
    for entry, expected in zip(entries, expected_entries, strict=True):
        if not isinstance(entry, dict):
            raise ValueError("N31 bank entry is not an object")
        _require_exact_json_keys(entry, bank_entry_keys, "N31 bank entry")
        entry_id, shape_id, head_name, arity, role_indices, instance_indices = expected
        exact_constraints = entry["guard_exact_expr_constraints"]
        if (
            entry["entry_id"] != entry_id
            or entry["guard_shape_id"] != shape_id
            or entry["guard_head_name"] != head_name
            or entry["guard_argument_count"] != arity
            or entry["guard_role_argument_indices"] != role_indices
            or entry["guard_instance_or_type_argument_indices"] != instance_indices
            or any(
                entry[field] != []
                for field in (
                    "guard_fixed_heads",
                    "guard_nested_heads",
                    "guard_literal_constraints",
                    "target_fixed_heads",
                    "target_nested_heads",
                    "target_literal_constraints",
                    "target_exact_expr_constraints",
                )
            )
            or entry["target_head_name"] != "HDiv.hDiv"
            or entry["target_argument_count"] != 6
            or entry["target_role_argument_indices"] != [5]
            or entry["target_instance_or_type_argument_indices"] != [0]
            or not isinstance(exact_constraints, list)
            or len(exact_constraints) != 1
            or not isinstance(exact_constraints[0], dict)
            or frozenset(exact_constraints[0]) != {"path", "expected_expr_hash"}
        ):
            raise ValueError("N31 bank entry structure drift")
        _require_n31_application_path(exact_constraints[0]["path"], [2])
        zero_hashes.append(
            _require_decimal_u64(
                exact_constraints[0]["expected_expr_hash"], "N31 bank zero expression hash"
            )
        )
    retained = value["retained_contradiction_patterns"]
    if not isinstance(retained, list) or len(retained) != 1 or not isinstance(retained[0], dict):
        raise ValueError("N31 retained bank-pattern inventory drift")
    pattern = retained[0]
    _require_exact_json_keys(
        pattern,
        frozenset(
            {
                "shape_id",
                "head_name",
                "argument_count",
                "role_paths",
                "instance_or_type_paths",
                "nested_heads",
                "literal_constraints",
                "exact_expr_constraints",
            }
        ),
        "N31 retained bank pattern",
    )
    exact_constraints = pattern["exact_expr_constraints"]
    if (
        pattern["shape_id"] != "eq_zero_retained_v1"
        or pattern["head_name"] != "Eq"
        or pattern["argument_count"] != 3
        or pattern["role_paths"] != [{"argument_indices": [1]}]
        or pattern["instance_or_type_paths"] != [{"argument_indices": [0]}]
        or pattern["nested_heads"] != []
        or pattern["literal_constraints"] != []
        or not isinstance(exact_constraints, list)
        or len(exact_constraints) != 1
        or not isinstance(exact_constraints[0], dict)
        or frozenset(exact_constraints[0]) != {"path", "expected_expr_hash"}
    ):
        raise ValueError("N31 retained bank-pattern structure drift")
    _require_n31_application_path(exact_constraints[0]["path"], [2])
    zero_hashes.append(
        _require_decimal_u64(
            exact_constraints[0]["expected_expr_hash"], "N31 retained zero expression hash"
        )
    )
    if len(set(zero_hashes)) != 1:
        raise ValueError("N31 exact zero Expr hash differs across bank constraints")
    if value["implications"] != [
        {"premise_shape_id": "positive_guard_v1", "conclusion_shape_id": "ne_zero_guard_v1"}
    ] or value["contradictions"] != [
        {"retained_shape_id": "eq_zero_retained_v1", "removed_shape_id": "ne_zero_guard_v1"}
    ]:
        raise ValueError("N31 proposal rule inventory drift")
    if value["resolved_entries"] != proposal.get("entries") or value[
        "resolved_retained_patterns"
    ] != proposal.get("retained_patterns"):
        raise ValueError("N31 fingerprint and proposal resolutions differ")
    resolved_retained = value["resolved_retained_patterns"]
    if (
        not isinstance(resolved_retained, list)
        or len(resolved_retained) != 1
        or not isinstance(resolved_retained[0], dict)
    ):
        raise ValueError("N31 resolved retained-pattern inventory drift")
    exact_resolutions = resolved_retained[0].get("exact_expr_witness_resolutions")
    if (
        not isinstance(exact_resolutions, list)
        or len(exact_resolutions) != 1
        or exact_resolutions[0].get("expected_expr_hash") != zero_hashes[0]
    ):
        raise ValueError("N31 bank and retained replay zero Expr hashes differ")


def _require_n31_receipt_preimage(
    value: object, *, proposal: dict[str, object], bank_payload: dict[str, object]
) -> None:
    if not isinstance(value, dict):
        raise ValueError("N31 resolution receipt preimage is not an object")
    _require_exact_json_keys(value, _N31_RECEIPT_PREIMAGE_KEYS, "N31 receipt preimage")
    expected = {
        "basis_id": "sft1_n31_resolution_receipt_hash_preimage_v0_3_6",
        "sha256_input_contract": "python_canonical_json_utf8_v1",
        "bank_fingerprint_payload": bank_payload,
        "operation_id": proposal["operation_id"],
        "identity_project_and_bank_nonempty": proposal["identity_project_and_bank_nonempty"],
        "entry_ids_unique": proposal["entry_ids_unique"],
        "retained_shape_ids_unique": proposal["retained_shape_ids_unique"],
        "selectable_guard_definitions_coherent": proposal["selectable_guard_definitions_coherent"],
        "retained_shapes_disjoint_from_selectable": proposal[
            "retained_shapes_disjoint_from_selectable"
        ],
        "implication_references_resolve": proposal["implication_references_resolve"],
        "contradiction_references_resolve": proposal["contradiction_references_resolve"],
        "all_entry_resolutions_passed": proposal["all_entry_resolutions_passed"],
        "all_retained_pattern_resolutions_passed": proposal[
            "all_retained_pattern_resolutions_passed"
        ],
        "all_names_resolved": proposal["all_names_resolved"],
        "all_arities_resolved": proposal["all_arities_resolved"],
        "all_type_and_instance_constraints_resolved": proposal[
            "all_type_and_instance_constraints_resolved"
        ],
        "frozen_admission_is_empty": proposal["frozen_admission_is_empty"],
        "proposed_identity_already_admitted": proposal["proposed_identity_already_admitted"],
        "private_semantic_checker_available": proposal["private_semantic_checker_available"],
        "semantic_success_conformance_performed": proposal[
            "semantic_success_conformance_performed"
        ],
        "semantic_adversarial_conformance_performed": proposal[
            "semantic_adversarial_conformance_performed"
        ],
        "activation_exposed": proposal["activation_exposed"],
        "proposal_resolution_passed": proposal["proposal_resolution_passed"],
    }
    if value != expected:
        raise ValueError("N31 resolution receipt preimage replay failed")


def _require_n31_proposal_core(
    proposal: object,
    *,
    project_id: str,
    bank_id: str,
    resolved_lean_hash: str,
    resolution_receipt_hash: str,
    phase: Literal["one", "two"],
) -> None:
    if not isinstance(proposal, dict):
        raise ValueError("N31 proposal payload is not an object")
    _require_exact_json_keys(proposal, _N31_PROPOSAL_KEYS, "N31 proposal")
    identity = proposal.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("N31 proposal identity is absent")
    expected_identity = {
        "project_id": project_id,
        "bank_id": bank_id,
        "resolved_lean_hash": "" if phase == "one" else resolved_lean_hash,
        "resolution_receipt_hash": "" if phase == "one" else resolution_receipt_hash,
    }
    required_true = (
        "identity_project_and_bank_nonempty",
        "entry_ids_unique",
        "retained_shape_ids_unique",
        "selectable_guard_definitions_coherent",
        "retained_shapes_disjoint_from_selectable",
        "implication_references_resolve",
        "contradiction_references_resolve",
        "all_entry_resolutions_passed",
        "all_retained_pattern_resolutions_passed",
        "all_names_resolved",
        "all_arities_resolved",
        "all_type_and_instance_constraints_resolved",
        "frozen_admission_is_empty",
        "proposal_resolution_passed",
    )
    required_false = (
        "proposed_identity_already_admitted",
        "private_semantic_checker_available",
        "semantic_success_conformance_performed",
        "semantic_adversarial_conformance_performed",
        "activation_exposed",
        "candidate_exposed",
    )
    if (
        identity != expected_identity
        or proposal.get("operation_id") != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
        or any(proposal.get(key) is not True for key in required_true)
        or any(proposal.get(key) is not False for key in required_false)
        or proposal.get("resolved_lean_hash_populated") is not (phase == "two")
        or proposal.get("resolution_receipt_hash_populated") is not (phase == "two")
    ):
        raise ValueError("N31 proposal resolution core failed")
    entries = proposal.get("entries")
    retained = proposal.get("retained_patterns")
    if (
        not isinstance(entries, list)
        or [item.get("entry_id") for item in entries if isinstance(item, dict)]
        != ["n31_ne_zero_hdiv_nat_v0_3_6", "n31_positive_hdiv_nat_v0_3_6"]
        or any(not isinstance(item, dict) or item.get("passed") is not True for item in entries)
        or not isinstance(retained, list)
        or [item.get("shape_id") for item in retained if isinstance(item, dict)]
        != ["eq_zero_retained_v1"]
        or any(
            not isinstance(item, dict)
            or item.get("passed") is not True
            or item.get("constraint_witness_replays_passed") is not True
            for item in retained
        )
    ):
        raise ValueError("N31 proposal exact entry/pattern resolution failed")
    _require_n31_entry_resolution(
        entries[0],
        entry_id="n31_ne_zero_hdiv_nat_v0_3_6",
        guard_shape_id="ne_zero_guard_v1",
        guard_head_name="Ne",
        guard_argument_count=3,
    )
    _require_n31_entry_resolution(
        entries[1],
        entry_id="n31_positive_hdiv_nat_v0_3_6",
        guard_shape_id="positive_guard_v1",
        guard_head_name="LT.lt",
        guard_argument_count=4,
    )
    _require_n31_retained_resolution(retained[0])


class N31ResolutionProjectProposal(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    compile_context_id: Annotated[str, Field(pattern=r"^ctx:[0-9a-f]{64}$", strict=True)]
    compile_context_fingerprint: Sha256
    bank_id: Literal["sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6"]
    bank_template_hash: Sha256
    resolved_lean_hash: Sha256
    resolution_receipt_hash: Sha256
    phase_one_request_hash: Sha256
    phase_two_request_hash: Sha256
    phase_one_raw_response_sha256: Sha256
    phase_two_raw_response_sha256: Sha256
    phase_one_task_receipt: dict[str, object]
    phase_two_task_receipt: dict[str, object]
    phase_one_task_receipt_hash: Sha256
    phase_two_task_receipt_hash: Sha256
    exact_name_arity_type_instance_resolution_passed: Literal[True]
    frozen_nonactivation_replayed: Literal[True]
    runtime_activated: Literal[False]
    semantic_success_conformance_performed: Literal[False]
    semantic_adversarial_conformance_performed: Literal[False]
    candidate_constructed: Literal[False]
    row_or_gate_emitted: Literal[False]
    elapsed_ms: int = Field(gt=0, strict=True)
    measured_peak_rss_bytes: int = Field(gt=0, le=40 * 1024**3, strict=True)
    project_receipt_hash: Sha256

    @model_validator(mode="after")
    def _proposal_is_independently_replayable(self) -> N31ResolutionProjectProposal:
        if self.compile_context_id != f"ctx:{self.compile_context_fingerprint}":
            raise ValueError("N31 proposal compile-context identity mismatch")
        if (
            self.phase_one_request_hash == self.phase_two_request_hash
            or self.phase_one_raw_response_sha256 == self.phase_two_raw_response_sha256
            or self.phase_one_task_receipt_hash == self.phase_two_task_receipt_hash
        ):
            raise ValueError("N31 phase-one and phase-two artifacts are not distinct")
        if self.phase_one_task_receipt_hash != hash_canonical(self.phase_one_task_receipt):
            raise ValueError("N31 phase-one task receipt hash mismatch")
        if self.phase_two_task_receipt_hash != hash_canonical(self.phase_two_task_receipt):
            raise ValueError("N31 phase-two task receipt hash mismatch")
        phase_one = self.phase_one_task_receipt
        phase_two = self.phase_two_task_receipt
        _require_exact_json_keys(phase_one, _N31_PHASE_ONE_KEYS, "N31 phase-one receipt")
        _require_exact_json_keys(phase_two, _N31_PHASE_TWO_KEYS, "N31 phase-two receipt")
        if (
            phase_one.get("schema_version") != 1
            or isinstance(phase_one.get("schema_version"), bool)
            or phase_two.get("schema_version") != 1
            or isinstance(phase_two.get("schema_version"), bool)
            or phase_one.get("receipt_kind") != "n31_proposal_resolution"
            or phase_two.get("receipt_kind") != "n31_frozen_nonactivation"
            or phase_one.get("receipt_id") != n31_phase_receipt_id(self.project_id, "phase_one")
            or phase_two.get("receipt_id") != n31_phase_receipt_id(self.project_id, "phase_two")
            or phase_one.get("source_version") != "sft1_wave1_runtime_readiness_v0_3_6"
            or phase_two.get("source_version") != "sft1_wave1_runtime_readiness_v0_3_6"
            or phase_one.get("external_hash_installation_contract") != _N31_EXTERNAL_HASH_CONTRACT
            or phase_two.get("external_hash_installation_contract") != _N31_EXTERNAL_HASH_CONTRACT
        ):
            raise ValueError("N31 two-phase receipt identity drift")
        for payload in (phase_one, phase_two):
            if (
                payload.get("candidate_constructed") is not False
                or payload.get("candidate_exposed") is not False
                or payload.get("row_or_gate_emitted") is not False
            ):
                raise ValueError("N31 proposal receipt exposed a candidate, row, or gate")
        if phase_one.get("semantic_conformance_performed") is not False:
            raise ValueError("N31 phase one claimed semantic conformance")
        bank_payload = phase_one.get("bank_fingerprint_payload")
        receipt_preimage = phase_one.get("resolution_receipt_hash_preimage_payload")
        phase_one_proposal = phase_one.get("proposal")
        phase_two_proposal = phase_two.get("proposal")
        if (
            not isinstance(bank_payload, dict)
            or not isinstance(receipt_preimage, dict)
            or not isinstance(phase_one_proposal, dict)
            or not isinstance(phase_two_proposal, dict)
            or self.resolved_lean_hash != hash_canonical(bank_payload)
            or self.resolution_receipt_hash != hash_canonical(receipt_preimage)
            or phase_two.get("bank_fingerprint_payload") != bank_payload
            or phase_two.get("resolution_receipt_hash_preimage_payload") != receipt_preimage
        ):
            raise ValueError("N31 external hash replay failed")
        _require_n31_bank_fingerprint(
            bank_payload,
            project_id=self.project_id,
            bank_id=self.bank_id,
            proposal=phase_one_proposal,
        )
        _require_n31_bank_fingerprint(
            bank_payload,
            project_id=self.project_id,
            bank_id=self.bank_id,
            proposal=phase_two_proposal,
        )
        _require_n31_receipt_preimage(
            receipt_preimage, proposal=phase_one_proposal, bank_payload=bank_payload
        )
        _require_n31_receipt_preimage(
            phase_two["resolution_receipt_hash_preimage_payload"],
            proposal=phase_two_proposal,
            bank_payload=bank_payload,
        )
        _require_n31_proposal_core(
            phase_one_proposal,
            project_id=self.project_id,
            bank_id=self.bank_id,
            resolved_lean_hash=self.resolved_lean_hash,
            resolution_receipt_hash=self.resolution_receipt_hash,
            phase="one",
        )
        _require_n31_proposal_core(
            phase_two_proposal,
            project_id=self.project_id,
            bank_id=self.bank_id,
            resolved_lean_hash=self.resolved_lean_hash,
            resolution_receipt_hash=self.resolution_receipt_hash,
            phase="two",
        )
        expected_identity = {
            "project_id": self.project_id,
            "bank_id": self.bank_id,
            "resolved_lean_hash": self.resolved_lean_hash,
            "resolution_receipt_hash": self.resolution_receipt_hash,
        }
        reachability = phase_two.get("reachability")
        selector = phase_two.get("selector")
        if (
            selector
            != {
                "kind": "requiredGuard",
                "guard_ordinal": 1,
                "target_position": "/",
                "target_position_nat": "1",
                "bank_entry_id": "n31_ne_zero_hdiv_nat_v0_3_6",
            }
            or not isinstance(reachability, dict)
            or frozenset(reachability)
            != {
                "mode_id",
                "guard_ordinal",
                "assignment_expr_hashes",
            }
            or reachability.get("mode_id")
            != "explicit_telescope_witness_and_retained_hypothesis_proofs"
            or reachability.get("guard_ordinal") != 1
            or not isinstance(reachability.get("assignment_expr_hashes"), list)
            or len(reachability["assignment_expr_hashes"]) != 2
        ):
            raise ValueError("N31 phase-two selector/reachability drift")
        for value in reachability["assignment_expr_hashes"]:
            _require_decimal_u64(value, "N31 reachability assignment expression hash")
        _require_decimal_u64(phase_two.get("source_expr_hash"), "N31 phase-two source Expr hash")
        if (
            phase_two.get("operation_id") != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
            or phase_two.get("identity") != expected_identity
            or phase_two.get("expected_resolved_lean_hash") != self.resolved_lean_hash
            or phase_two.get("expected_resolution_receipt_hash") != self.resolution_receipt_hash
            or phase_two.get("expected_hashes_nonempty") is not True
            or phase_two.get("expected_hashes_are_lower_hex_sha256") is not True
            or phase_two.get("identity_matches_expected_hashes") is not True
            or phase_two.get("external_hash_computation_performed_in_lean") is not False
            or phase_two.get("external_strict_runner_hash_verification_required") is not True
            or phase_two.get("proposal_resolution_passed") is not True
            or phase_two.get("frozen_admission_is_empty") is not True
            or phase_two.get("identity_absent_from_frozen_admission") is not True
            or phase_two.get("frozen_dispatch_rejected_as_unadmitted_bank") is not True
            or phase_two.get("rejection_reason") != "n31BankInvalid"
            or phase_two.get("private_semantic_checker_available") is not False
            or phase_two.get("semantic_conformance_performed") is not False
            or phase_two.get("activation_exposed") is not False
        ):
            raise ValueError("N31 frozen nonactivation replay failed")
        core = self.model_dump(mode="json")
        observed = core.pop("project_receipt_hash")
        if observed != hash_canonical(core):
            raise ValueError("N31 project proposal receipt hash mismatch")
        return self


class LiveCheckpointGitIdentity(StrictModel):
    schema_version: Literal[1]
    worktree: NonEmptyStr
    implementation_commit: GitCommit
    implementation_tree: GitCommit
    status_porcelain_sha256: Sha256
    worktree_clean: Literal[True]
    verified_before_resource_claim: Literal[True]
    verification_hash: Sha256

    @model_validator(mode="after")
    def _identity_hash_replays(self) -> LiveCheckpointGitIdentity:
        core = self.model_dump(mode="json")
        observed = core.pop("verification_hash")
        if observed != hash_canonical(core):
            raise ValueError("live checkpoint Git identity hash mismatch")
        return self


class LiveCheckpointResourceSnapshot(StrictModel):
    task: Literal["SFT1"]
    lean_workers: Literal[1]
    lean_rss_gib: Literal[24.0]
    gpu: Literal[False]
    pid: int = Field(gt=0, strict=True)
    owner_session: NonEmptyStr
    hostname: NonEmptyStr
    worktree: NonEmptyStr
    created_at: NonEmptyStr


class N31ProjectCompletionBinding(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    path: NonEmptyStr
    file_sha256: Sha256
    completion_hash: Sha256


class N31ProposalProjectJournal(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    path: NonEmptyStr
    file_sha256: Sha256
    final_chain_hash: Sha256


class N31ResolutionProposalBundle(StrictModel):
    schema_version: Literal[1]
    receipt_id: Literal["sft1_wave1_n31_resolution_proposal_v0_3_6"]
    run_spec_hash: Sha256
    run_spec_path: NonEmptyStr
    run_spec_file_sha256: Sha256
    positive_checkpoint_receipt_hash: Sha256
    positive_checkpoint_receipt_path: NonEmptyStr
    positive_checkpoint_receipt_file_sha256: Sha256
    runtime_config_file_sha256: Sha256
    runtime_config_hash: Sha256
    runtime_fixture_file_sha256: Sha256
    runtime_fixture_hash: Sha256
    runtime_loader_file_sha256: Sha256
    live_runner_file_sha256: Sha256
    implementation_commit: GitCommit
    implementation_tree: GitCommit
    implementation_identity_receipt: LiveCheckpointGitIdentity
    implementation_identity_receipt_hash: Sha256
    assembled_preamble_sha256: Sha256
    resource_claim_id: NonEmptyStr
    resource_claim_snapshot: LiveCheckpointResourceSnapshot
    resource_claim_snapshot_hash: Sha256
    resource_released: Literal[True]
    persistent_worker_count: Literal[1]
    measured_combined_peak_rss_bytes: int = Field(gt=0, le=40 * 1024**3, strict=True)
    measured_total_lean_seconds: float = Field(gt=0, strict=True)
    elab_async: Literal[False]
    per_row_process_spawned: Literal[False]
    corpus_compiled: Literal[False]
    proposals: tuple[N31ResolutionProjectProposal, ...]
    project_completions: tuple[N31ProjectCompletionBinding, ...] = Field(
        min_length=4, max_length=4
    )
    project_journals: tuple[N31ProposalProjectJournal, ...] = Field(min_length=4, max_length=4)
    journal_is_durable_log: Literal[True]
    heartbeat_path: NonEmptyStr
    heartbeat_file_sha256: Sha256
    n31_activation_performed: Literal[False]
    semantic_success_conformance_performed: Literal[False]
    semantic_adversarial_conformance_performed: Literal[False]
    wave1_gate_executed: Literal[False]
    model_facing_rows_emitted: Literal[False]
    terminal_status: Literal["stopped_for_exact_n31_user_admission"]
    exact_user_admission_fields: tuple[
        Literal["project_id", "bank_id", "resolved_lean_hash", "resolution_receipt_hash"], ...
    ]
    terminal_marker_path: NonEmptyStr
    terminal_marker_preimage_hash: Sha256
    receipt_hash: Sha256

    @model_validator(mode="after")
    def _exact_four_project_stop(self) -> N31ResolutionProposalBundle:
        if tuple(item.project_id for item in self.proposals) != EXPECTED_PROJECT_IDS:
            raise ValueError("N31 proposal bundle project inventory/order drift")
        if tuple(item.project_id for item in self.project_completions) != EXPECTED_PROJECT_IDS:
            raise ValueError("N31 proposal completion inventory/order drift")
        if tuple(item.project_id for item in self.project_journals) != EXPECTED_PROJECT_IDS:
            raise ValueError("N31 proposal journal inventory/order drift")
        if self.exact_user_admission_fields != EXPECTED_N31_ADMISSION_FIELDS:
            raise ValueError("N31 proposal exact admission field order drift")
        if (
            self.implementation_identity_receipt_hash
            != self.implementation_identity_receipt.verification_hash
            or self.implementation_commit
            != self.implementation_identity_receipt.implementation_commit
            or self.implementation_tree
            != self.implementation_identity_receipt.implementation_tree
            or self.resource_claim_snapshot.worktree
            != self.implementation_identity_receipt.worktree
        ):
            raise ValueError("N31 proposal implementation identity mismatch")
        snapshot_bytes = (
            json.dumps(
                self.resource_claim_snapshot.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if self.resource_claim_snapshot_hash != sha256_hex(snapshot_bytes):
            raise ValueError("N31 proposal resource snapshot hash mismatch")
        core = self.model_dump(mode="json")
        observed = core.pop("receipt_hash")
        if observed != hash_canonical(core):
            raise ValueError("N31 proposal bundle receipt hash mismatch")
        return self


PositiveCheckpointGitIdentity = LiveCheckpointGitIdentity
PositiveCheckpointResourceSnapshot = LiveCheckpointResourceSnapshot


class PositiveCheckpointCaseReceipt(StrictModel):
    schema_version: Literal[1]
    run_spec_hash: Sha256
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    operation_id: Literal[
        "P01_ALPHA_RENAME_SINGLE_V1",
        "P15_SWAP_IFF_SIDES_V1",
        "P18_SYMMETRIZE_EQUALITY_V1",
        "P21_BETA_REDUCE_V1",
    ]
    fixture_kind: Literal["success", "adversarial_rejection"]
    case_id: NonEmptyStr
    request_hashes: tuple[Sha256, ...] = Field(min_length=1, max_length=2)
    task_receipt: dict[NonEmptyStr, object]
    task_receipt_hash: Sha256
    artifact_hashes: dict[NonEmptyStr, Sha256]
    symbol_resolution_receipt_hash: Sha256
    symbol_resolution_receipt_path: NonEmptyStr
    symbol_resolution_receipt_file_sha256: Sha256
    symbol_resolution_raw_response_path: NonEmptyStr
    symbol_resolution_raw_response_sha256: Sha256
    symbol_resolution_request_hash: Sha256
    typed_replay_performed: bool = Field(strict=True)
    cache_write_and_readback_replayed: bool = Field(strict=True)
    runtime_chain: dict[NonEmptyStr, object] | None
    runtime_chain_hash: Sha256 | None
    typed_replay_path: NonEmptyStr | None
    typed_replay_file_sha256: Sha256 | None
    raw_response_path: NonEmptyStr | None
    raw_response_file_sha256: Sha256 | None
    wave1_cache_key: Wave1CacheKey | None
    wave1_cache_key_hash: Sha256 | None
    central_cache_key_hash: Sha256 | None
    central_cache_entry_path: NonEmptyStr | None
    central_cache_entry_file_sha256: Sha256 | None
    elapsed_ms: int = Field(gt=0, strict=True)
    measured_lean_seconds: float = Field(gt=0, strict=True)
    reference_complete_sidecar_path: NonEmptyStr | None
    reference_complete_sidecar_bytes: int | None = Field(default=None, gt=0, strict=True)
    reference_complete_sidecar_sha256: Sha256 | None
    candidate_complete_sidecar_path: NonEmptyStr | None
    candidate_complete_sidecar_bytes: int | None = Field(default=None, gt=0, strict=True)
    candidate_complete_sidecar_sha256: Sha256 | None
    p01_runtime_replay_path: NonEmptyStr | None
    p01_runtime_replay_file_sha256: Sha256 | None
    p01_runtime_replay_receipt_hash: Sha256 | None
    n31_activation_performed: Literal[False]
    wave1_gate_executed: Literal[False]
    model_facing_rows_emitted: Literal[False]
    completion_hash: Sha256

    @model_validator(mode="after")
    def _case_hash_and_evidence_shape_replay(self) -> PositiveCheckpointCaseReceipt:
        core = self.model_dump(mode="json")
        observed = core.pop("completion_hash")
        if observed != hash_canonical(core):
            raise ValueError("positive checkpoint case hash mismatch")
        if self.case_id != f"{self.project_id}.{self.operation_id}.{self.fixture_kind}":
            raise ValueError("positive checkpoint case identity mismatch")
        if self.task_receipt_hash != hash_canonical(self.task_receipt):
            raise ValueError("positive checkpoint task receipt hash mismatch")
        if self.measured_lean_seconds != self.elapsed_ms / 1000:
            raise ValueError("positive checkpoint case timing mismatch")
        sidecar_values = (
            self.reference_complete_sidecar_path,
            self.reference_complete_sidecar_bytes,
            self.reference_complete_sidecar_sha256,
            self.candidate_complete_sidecar_path,
            self.candidate_complete_sidecar_bytes,
            self.candidate_complete_sidecar_sha256,
        )
        success_replay_values = (
            self.runtime_chain,
            self.runtime_chain_hash,
            self.typed_replay_path,
            self.typed_replay_file_sha256,
            self.raw_response_path,
            self.raw_response_file_sha256,
            self.wave1_cache_key,
            self.wave1_cache_key_hash,
            self.central_cache_key_hash,
            self.central_cache_entry_path,
            self.central_cache_entry_file_sha256,
        )
        p01_values = (
            self.p01_runtime_replay_path,
            self.p01_runtime_replay_file_sha256,
            self.p01_runtime_replay_receipt_hash,
        )
        if self.fixture_kind == "success":
            if (
                not self.typed_replay_performed
                or not self.cache_write_and_readback_replayed
                or any(value is None for value in sidecar_values)
                or any(value is None for value in success_replay_values)
            ):
                raise ValueError("positive success lost typed/runtime/cache/sidecar evidence")
            if self.operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
                if any(value is None for value in p01_values):
                    raise ValueError("P01 success lost runtime replay evidence")
            elif any(value is not None for value in p01_values):
                raise ValueError("non-P01 success invented P01 runtime replay evidence")
        elif (
            self.typed_replay_performed
            or self.cache_write_and_readback_replayed
            or any(
                value is not None
                for value in (*sidecar_values, *success_replay_values, *p01_values)
            )
        ):
            raise ValueError("positive rejection invented candidate/replay evidence")
        return self


class PositiveCheckpointProjectJournal(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    path: NonEmptyStr
    file_sha256: Sha256
    final_chain_hash: Sha256


class PositiveLiveCheckpointReceipt(StrictModel):
    """Typed outer contract for the separate 32-case positive checkpoint."""

    schema_version: Literal[1]
    receipt_id: Literal["sft1_wave1_positive_live_checkpoint_v0_3_6"]
    run_spec_hash: Sha256
    run_spec_path: NonEmptyStr
    run_spec_file_sha256: Sha256
    runtime_config_file_sha256: Sha256
    runtime_config_hash: Sha256
    runtime_fixture_file_sha256: Sha256
    runtime_fixture_hash: Sha256
    runtime_loader_file_sha256: Sha256
    live_runner_file_sha256: Sha256
    implementation_commit: GitCommit
    implementation_tree: GitCommit
    implementation_identity_receipt: PositiveCheckpointGitIdentity
    implementation_identity_receipt_hash: Sha256
    assembled_preamble_sha256: Sha256
    resource_claim_id: NonEmptyStr
    resource_claim_snapshot: PositiveCheckpointResourceSnapshot
    resource_claim_snapshot_hash: Sha256
    resource_released: Literal[True]
    persistent_worker_count: Literal[1]
    measured_combined_peak_rss_bytes: int = Field(gt=0, le=40 * 1024**3, strict=True)
    measured_case_lean_milliseconds: int = Field(gt=0, strict=True)
    measured_symbol_resolution_lean_milliseconds: int = Field(gt=0, strict=True)
    measured_total_lean_milliseconds: int = Field(gt=0, strict=True)
    measured_total_lean_seconds: float = Field(gt=0, strict=True)
    measured_total_complete_sidecar_bytes: int = Field(gt=0, strict=True)
    p01_runtime_policy_semantic_hash: Literal[
        "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"
    ]
    p01_runtime_source_sha256: Sha256
    p01_live_acceptance_project_count: Literal[4]
    p01_runtime_replay_receipt_hashes: tuple[Sha256, ...] = Field(min_length=4, max_length=4)
    p01_complete_scope_cap_execution_performed: Literal[True]
    elab_async: Literal[False]
    per_row_process_spawned: Literal[False]
    corpus_compiled: Literal[False]
    positive_case_count: Literal[32]
    cases: tuple[PositiveCheckpointCaseReceipt, ...] = Field(min_length=32, max_length=32)
    project_journals: tuple[PositiveCheckpointProjectJournal, ...] = Field(
        min_length=4, max_length=4
    )
    journal_is_durable_log: Literal[True]
    heartbeat_path: NonEmptyStr
    heartbeat_file_sha256: Sha256
    all_positive_cases_completed: Literal[True]
    all_success_sidecars_typed_replay_and_cache_readback_bound: Literal[True]
    all_rejections_candidate_free: Literal[True]
    n31_resolution_started: Literal[False]
    n31_activation_performed: Literal[False]
    wave1_gate_executed: Literal[False]
    model_facing_rows_emitted: Literal[False]
    terminal_marker_path: NonEmptyStr
    terminal_marker_preimage_hash: Sha256
    terminal_status: Literal["positive_checkpoint_complete_n31_not_started"]
    receipt_hash: Sha256

    @model_validator(mode="after")
    def _checkpoint_hash_and_inventory_replay(self) -> PositiveLiveCheckpointReceipt:
        core = self.model_dump(mode="json")
        observed = core.pop("receipt_hash")
        if observed != hash_canonical(core):
            raise ValueError("positive checkpoint receipt hash mismatch")
        if (
            self.implementation_identity_receipt_hash
            != self.implementation_identity_receipt.verification_hash
            or self.implementation_commit
            != self.implementation_identity_receipt.implementation_commit
            or self.implementation_tree != self.implementation_identity_receipt.implementation_tree
            or self.resource_claim_snapshot.worktree
            != self.implementation_identity_receipt.worktree
        ):
            raise ValueError("positive checkpoint implementation identity mismatch")
        snapshot_bytes = (
            json.dumps(
                self.resource_claim_snapshot.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if self.resource_claim_snapshot_hash != sha256_hex(snapshot_bytes):
            raise ValueError("positive checkpoint resource snapshot hash mismatch")
        expected_cases = tuple(
            (project_id, operation_id, fixture_kind)
            for project_id in EXPECTED_PROJECT_IDS
            for operation_id in EXPECTED_OPERATION_IDS[:4]
            for fixture_kind in EXPECTED_FIXTURE_KIND_ORDER
        )
        observed_cases = tuple(
            (item.project_id, item.operation_id, item.fixture_kind) for item in self.cases
        )
        if observed_cases != expected_cases:
            raise ValueError("positive checkpoint is not the exact ordered 32-case matrix")
        if tuple(item.project_id for item in self.project_journals) != EXPECTED_PROJECT_IDS:
            raise ValueError("positive checkpoint project journal inventory drift")
        case_ms = sum(item.elapsed_ms for item in self.cases)
        sidecar_bytes = sum(
            (item.reference_complete_sidecar_bytes or 0)
            + (item.candidate_complete_sidecar_bytes or 0)
            for item in self.cases
        )
        if (
            self.measured_case_lean_milliseconds != case_ms
            or self.measured_total_lean_milliseconds
            != case_ms + self.measured_symbol_resolution_lean_milliseconds
            or self.measured_total_lean_seconds != self.measured_total_lean_milliseconds / 1000
            or self.measured_total_complete_sidecar_bytes != sidecar_bytes
        ):
            raise ValueError("positive checkpoint aggregate measurement mismatch")
        observed_p01 = tuple(
            item.p01_runtime_replay_receipt_hash
            for item in self.cases
            if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1" and item.fixture_kind == "success"
        )
        if observed_p01 != self.p01_runtime_replay_receipt_hashes:
            raise ValueError("positive checkpoint P01 receipt inventory mismatch")
        return self


class Wave1RuntimeConfig(StrictModel):
    schema_version: Literal[1]
    policy_version: Literal["0.3.6"]
    status: Literal["implementation_authorized_live_receipts_pending"]
    accepted_corrective_commit: Literal["fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48"]
    accepted_parent_branch_push_completed: Literal[True]
    current_revision_commit: None
    current_revision_push_completed: Literal[False]
    frozen_readiness_file_sha256: Literal[
        "87197cef05d4e755a0d92745b2b3846787b5e1159edac29dfdd967ba81aed614"
    ]
    p01_required_policy_semantic_hash: Literal[
        "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"
    ]
    p01_corrected_envelope_semantic_hash: Literal[
        "dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4"
    ]
    source_bindings: tuple[SourceBinding, ...]
    resource_contract: ResourceContract
    persistence_contract: PersistenceContract
    backend_projects: tuple[BackendProjectBinding, ...]
    lean_environment_contract: LeanEnvironmentContract
    representation_contract: RepresentationContract
    preamble_contract: PreambleContract
    cache_contract: CacheContract
    operations: tuple[RuntimeOperationBinding, ...]
    n31_contract: N31Contract
    n31_proposal_bank: N31ProposalBank
    authorization: Authorization
    readiness_state: ReadinessState
    shared_label_contract_status: Literal["coordinator_request_open"]

    @model_validator(mode="after")
    def _exact_runtime_scope(self) -> Wave1RuntimeConfig:
        if tuple(item.operation_id for item in self.operations) != EXPECTED_OPERATION_IDS:
            raise ValueError("Wave 1 runtime operation order/inventory drift")
        for item in self.operations:
            observed = (
                item.mechanism_superclass,
                item.inverse_token,
                item.registry_entry_hash,
                item.anchor_hash,
                item.operation_bank_entry_hash,
                item.fixture_aggregate_hash,
            )
            if observed != EXPECTED_OPERATION_MAP[item.operation_id]:
                raise ValueError(f"Wave 1 runtime bundle drift: {item.operation_id}")
        if any(
            item.runtime_status != "positive_implementation_authorized_pending_live_receipt"
            for item in self.operations[:4]
        ):
            raise ValueError("four positive bundles must remain pending live receipts")
        if self.operations[4].runtime_status != "n31_resolution_proposal_only_not_admitted":
            raise ValueError("N31 runtime must remain proposal-only")
        roles = tuple(item.role for item in self.source_bindings)
        if roles != EXPECTED_SOURCE_BINDING_ROLES:
            raise ValueError("runtime source binding order/inventory drift")
        for source in self.source_bindings:
            if source.path != EXPECTED_SOURCE_BINDING_PATHS[source.role]:
                raise ValueError(f"runtime source path drift: {source.role}")
        for item in self.operations:
            expected_constructor, expected_selector = EXPECTED_OPERATION_CONSTRUCTORS[
                item.operation_id
            ]
            if (
                item.operation_constructor != expected_constructor
                or item.selector_constructor != expected_selector
            ):
                raise ValueError(f"runtime constructor drift: {item.operation_id}")
        observed_backends = tuple(
            (
                item.backend_id,
                item.project_dir,
                item.project_revision,
                item.lean_version,
                item.source_project_ids,
            )
            for item in self.backend_projects
        )
        if observed_backends != EXPECTED_BACKEND_PROJECTS:
            raise ValueError("runtime backend project binding drift")
        return self


class FixtureProjectContext(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    source_revision: NonEmptyStr
    compile_project_id: Literal["mathlib", "cslib", "physlib"]
    compile_project_revision: NonEmptyStr
    lean_version: NonEmptyStr
    import_header: NonEmptyStr
    namespace_context: tuple[str, ...]
    open_context: tuple[str, ...]
    scoped_context: tuple[str, ...]
    options: dict[str, bool]
    source_faithful_context_required_for_real_roots: Literal[True]

    @model_validator(mode="after")
    def _safe_context(self) -> FixtureProjectContext:
        if self.options != {"Elab.async": False, "autoImplicit": False}:
            raise ValueError("Wave 1 fixture context must disable async and autoImplicit")
        observed = (
            self.source_revision,
            self.compile_project_id,
            self.compile_project_revision,
            self.lean_version,
            self.import_header,
        )
        if observed != EXPECTED_PROJECT_CONTEXTS[self.project_id]:
            if self.project_id == "physlib" and self.import_header != "import Physlib":
                raise ValueError("additive Physlib fixture must correct the frozen PhysLean typo")
            raise ValueError(f"fixture project context drift: {self.project_id}")
        if self.namespace_context or self.open_context or self.scoped_context:
            raise ValueError("readiness fixtures use the exact empty namespace/notation scope")
        return self


class FixtureSelector(StrictModel):
    kind: Literal["outer_binder", "outer_target", "exact_expr_site", "required_guard_proposal"]
    binder_index: int | None = Field(default=None, ge=0, strict=True)
    site_path: str | None = None
    guard_binder_index: int | None = Field(default=None, ge=0, strict=True)
    guard_shape_id: str | None = None
    target_head_name: str | None = None
    reachability_assignments: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _exact_selector_shape(self) -> FixtureSelector:
        populated = {
            "binder_index": self.binder_index,
            "site_path": self.site_path,
            "guard_binder_index": self.guard_binder_index,
            "guard_shape_id": self.guard_shape_id,
            "target_head_name": self.target_head_name,
            "reachability_assignments": self.reachability_assignments,
        }
        required = {
            "outer_binder": {"binder_index"},
            "outer_target": set(),
            "exact_expr_site": {"site_path"},
            "required_guard_proposal": {
                "guard_binder_index",
                "guard_shape_id",
                "target_head_name",
                "reachability_assignments",
            },
        }[self.kind]
        observed = {field for field, value in populated.items() if value is not None}
        if observed != required:
            raise ValueError(f"fixture selector {self.kind} has fields {sorted(observed)}")
        return self


class FixtureTemplate(StrictModel):
    template_id: NonEmptyStr
    operation_id: OperationId
    fixture_kind: Literal["success", "adversarial_rejection"]
    reference_term: NonEmptyStr
    selector: FixtureSelector
    expected_engine_terminal: Literal[
        "applicable", "typedNotApplicable", "proposed_not_admitted", "proposal_rejected"
    ]
    expected_engine_reason: str | None = None
    expected_reason_class: NonEmptyStr


class FixtureMatrixContract(StrictModel):
    project_order: tuple[Literal["compiler_data", "cslib", "mathlib", "physlib"], ...]
    template_operation_order: tuple[OperationId, ...]
    positive_checkpoint_operation_order: tuple[
        Literal[
            "P01_ALPHA_RENAME_SINGLE_V1",
            "P15_SWAP_IFF_SIDES_V1",
            "P18_SYMMETRIZE_EQUALITY_V1",
            "P21_BETA_REDUCE_V1",
        ],
        ...,
    ]
    n31_proposal_operation_order: tuple[Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"], ...]
    fixture_kind_order: tuple[Literal["success", "adversarial_rejection"], ...]
    positive_checkpoint_expansion: Literal[
        "exact_cartesian_project_positive_operation_fixture_kind_v1"
    ]
    exact_positive_operation_project_count: Literal[16]
    exact_positive_fixture_count: Literal[32]
    exact_n31_project_count: Literal[4]
    exact_n31_phase_request_count: Literal[8]
    combined_40_case_live_checkpoint_allowed: Literal[False]
    every_positive_success_requires_same_request_two_endpoint_repr: Literal[True]
    every_rejection_requires_persistent_meta_outcome_receipt: Literal[True]
    rejections_must_not_invent_candidate_endpoints: Literal[True]
    n31_success_is_proposal_not_activation: Literal[True]
    matrix_is_gate_evidence: Literal[False]


class Wave1RuntimeFixtures(StrictModel):
    schema_version: Literal[1]
    fixture_set_id: Literal["sft1_wave1_readiness_fixtures_v0_3_6"]
    fixture_set_version: Literal["0.3.6"]
    status: Literal["authorized_for_bounded_readiness_not_a_gate"]
    parent_fixture_path: Literal["tests/fixtures/sft1/wave1_v0_3_4.yaml"]
    parent_fixture_file_sha256: Literal[
        "0856c6cfa1536bd935d4606ec2d09a34c72ef5c2ddf92d2f15b67867ac6dd6ea"
    ]
    fixture_text_is_not_a_source_root: Literal[True]
    fixture_text_is_not_model_facing: Literal[True]
    fixture_execution_is_wave1_gate_execution: Literal[False]
    optional_n31_proof_fixture_count: Literal[0]
    project_contexts: tuple[FixtureProjectContext, ...]
    templates: tuple[FixtureTemplate, ...]
    matrix_contract: FixtureMatrixContract

    @model_validator(mode="after")
    def _exact_fixture_matrix(self) -> Wave1RuntimeFixtures:
        if tuple(item.project_id for item in self.project_contexts) != EXPECTED_PROJECT_IDS:
            raise ValueError("Wave 1 fixture project order/inventory drift")
        if self.matrix_contract.project_order != EXPECTED_PROJECT_IDS:
            raise ValueError("Wave 1 fixture matrix project order drift")
        if self.matrix_contract.template_operation_order != EXPECTED_OPERATION_IDS:
            raise ValueError("Wave 1 fixture template operation order drift")
        if self.matrix_contract.positive_checkpoint_operation_order != EXPECTED_OPERATION_IDS[:4]:
            raise ValueError("Wave 1 positive checkpoint operation order drift")
        if self.matrix_contract.n31_proposal_operation_order != EXPECTED_OPERATION_IDS[4:]:
            raise ValueError("Wave 1 N31 proposal operation order drift")
        if self.matrix_contract.fixture_kind_order != EXPECTED_FIXTURE_KIND_ORDER:
            raise ValueError("Wave 1 fixture kind order drift")
        expected_templates = tuple(
            (operation_id, kind)
            for operation_id in EXPECTED_OPERATION_IDS
            for kind in ("success", "adversarial_rejection")
        )
        if tuple((item.operation_id, item.fixture_kind) for item in self.templates) != (
            expected_templates
        ):
            raise ValueError("Wave 1 fixture template order/inventory drift")
        if tuple(item.template_id for item in self.templates) != EXPECTED_TEMPLATE_IDS:
            raise ValueError("Wave 1 fixture template identity drift")
        observed_content = tuple(
            (
                item.reference_term,
                item.expected_engine_terminal,
                item.expected_engine_reason,
                item.expected_reason_class,
            )
            for item in self.templates
        )
        if observed_content != EXPECTED_TEMPLATE_CONTENT:
            raise ValueError("Wave 1 fixture content/terminal contract drift")
        if any(
            item.expected_engine_terminal != "applicable" for item in self.templates[:8:2]
        ) or any(
            item.expected_engine_terminal != "typedNotApplicable" for item in self.templates[1:8:2]
        ):
            raise ValueError("positive fixture terminal contract drift")
        if (
            self.templates[8].expected_engine_terminal != "proposed_not_admitted"
            or self.templates[9].expected_engine_terminal != "proposal_rejected"
        ):
            raise ValueError("N31 fixtures must remain proposal-only")
        expected_selector_payloads = (
            {"kind": "outer_binder", "binder_index": 0},
            {"kind": "outer_binder", "binder_index": 0},
            {"kind": "outer_target"},
            {"kind": "outer_target"},
            {"kind": "outer_target"},
            {"kind": "outer_target"},
            {"kind": "exact_expr_site", "site_path": "/"},
            {"kind": "exact_expr_site", "site_path": "/"},
            {
                "kind": "required_guard_proposal",
                "guard_binder_index": 1,
                "guard_shape_id": "ne_zero_guard_v1",
                "target_head_name": "HDiv.hDiv",
                "reachability_assignments": ["(1 : Nat)", "Nat.one_ne_zero"],
            },
            {
                "kind": "required_guard_proposal",
                "guard_binder_index": 1,
                "guard_shape_id": "ne_zero_guard_v1",
                "target_head_name": "HDiv.hDiv",
                "reachability_assignments": [
                    "(1 : Nat)",
                    "Nat.one_ne_zero",
                    "Nat.zero_lt_succ 0",
                ],
            },
        )
        for template, expected_selector in zip(
            self.templates, expected_selector_payloads, strict=True
        ):
            selector = template.selector.model_dump(mode="json", exclude_none=True)
            if selector != expected_selector:
                raise ValueError(f"fixture selector drift: {template.template_id}")
        return self


def compute_runtime_fixture_bundle_hash(fixtures: Wave1RuntimeFixtures, operation_id: str) -> str:
    """Hash one operation's exact readiness templates in every project context."""

    if operation_id not in EXPECTED_OPERATION_IDS:
        raise Wave1LiveReadinessError(f"unknown Wave 1 operation: {operation_id}")
    templates = [
        item.model_dump(mode="json")
        for item in fixtures.templates
        if item.operation_id == operation_id
    ]
    if len(templates) != 2:
        raise Wave1LiveReadinessError(f"incomplete runtime fixture bundle: {operation_id}")
    return hash_canonical(
        {
            "bundle_version": "sft1_wave1_runtime_fixture_bundle_v0_3_6",
            "operation_id": operation_id,
            "templates_success_then_adversarial_rejection": templates,
            "project_contexts_in_registered_order": [
                item.model_dump(mode="json") for item in fixtures.project_contexts
            ],
            "project_order": list(fixtures.matrix_contract.project_order),
            "fixture_kind_order": list(fixtures.matrix_contract.fixture_kind_order),
        }
    )


@dataclass(frozen=True, slots=True)
class LoadedWave1LiveReadiness:
    config: Wave1RuntimeConfig
    config_path: Path
    config_hash: str
    config_file_sha256: str
    fixtures: Wave1RuntimeFixtures
    fixture_path: Path
    fixture_hash: str
    fixture_file_sha256: str


def _repo_path(root: Path, relative: str | Path) -> Path:
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise Wave1LiveReadinessError(f"runtime path escapes repository: {relative}")
    return resolved


@dataclass(frozen=True, slots=True)
class AssembledPreamble:
    text: str
    sha256: str
    removed_import_line_count: int


def assemble_runtime_preamble(
    root: Path, source_bindings: tuple[SourceBinding, ...]
) -> AssembledPreamble:
    """Assemble only the reviewed task sources under the frozen strip rule."""

    by_role = {item.role: item for item in source_bindings}
    ordered_roles: tuple[
        Literal[
            "fixed_reference_elaboration",
            "frozen_wave1_engine",
            "lean_runtime_helper",
        ],
        ...,
    ] = (
        "fixed_reference_elaboration",
        "frozen_wave1_engine",
        "lean_runtime_helper",
    )
    chunks: list[str] = []
    removed = 0
    for role in ordered_roles:
        binding = by_role.get(role)
        if binding is None:
            raise Wave1LiveReadinessError(f"missing preamble source binding: {role}")
        source = _repo_path(root, binding.path).read_text(encoding="utf-8")
        retained_lines: list[str] = []
        for line in source.splitlines():
            if line.startswith("import "):
                removed += 1
            else:
                retained_lines.append(line)
        chunks.append("\n".join(retained_lines).rstrip())
    text = "\n\n".join(chunks) + "\n"
    return AssembledPreamble(
        text=text,
        sha256=sha256_hex(text.encode("utf-8")),
        removed_import_line_count=removed,
    )


def build_fixture_compile_context(
    project: FixtureProjectContext, *, assembled_preamble: str
) -> CompileContext:
    """Build the exact REPR context used by every readiness case in a project."""

    return CompileContext(
        project_id=project.project_id,
        project_revision=project.compile_project_revision,
        lean_version=project.lean_version,
        import_header=project.import_header,
        command_preamble=assembled_preamble,
        namespace_context=project.namespace_context,
        open_context=project.open_context,
        scoped_context=project.scoped_context,
        options=project.options,
    )


def load_wave1_live_readiness(
    repo_root: Path | None = None,
    *,
    config_path: Path | None = None,
    fixture_path: Path | None = None,
) -> LoadedWave1LiveReadiness:
    """Load/hash-check the additive contract without importing a Lean backend."""

    root = find_repo_root(repo_root)
    expected_config = _repo_path(root, DEFAULT_RUNTIME_CONFIG_PATH)
    expected_fixture = _repo_path(root, DEFAULT_RUNTIME_FIXTURE_PATH)
    resolved_config = (config_path or expected_config).resolve()
    resolved_fixture = (fixture_path or expected_fixture).resolve()
    if resolved_config != expected_config or resolved_fixture != expected_fixture:
        raise Wave1LiveReadinessError("runtime config/fixture path differs from additive freeze")

    config_file_sha256 = hash_file(resolved_config)
    fixture_file_sha256 = hash_file(resolved_fixture)
    if config_file_sha256 != EXPECTED_RUNTIME_CONFIG_FILE_SHA256:
        raise Wave1LiveReadinessError("runtime config raw-file hash drift")
    if fixture_file_sha256 != EXPECTED_RUNTIME_FIXTURE_FILE_SHA256:
        raise Wave1LiveReadinessError("runtime fixture raw-file hash drift")
    loaded_config: LoadedConfig[Wave1RuntimeConfig] = load_config(
        resolved_config, Wave1RuntimeConfig
    )
    loaded_fixtures: LoadedConfig[Wave1RuntimeFixtures] = load_config(
        resolved_fixture, Wave1RuntimeFixtures
    )
    if loaded_config.config_hash != EXPECTED_RUNTIME_CONFIG_HASH:
        raise Wave1LiveReadinessError("runtime config semantic hash drift")
    if loaded_fixtures.config_hash != EXPECTED_RUNTIME_FIXTURE_HASH:
        raise Wave1LiveReadinessError("runtime fixture semantic hash drift")

    for binding in loaded_config.config.source_bindings:
        if hash_file(_repo_path(root, binding.path)) != binding.file_sha256:
            raise Wave1LiveReadinessError(f"runtime source hash drift: {binding.role}")
    environment = loaded_config.config.lean_environment_contract
    if hash_file(_repo_path(root, environment.environment_lock_path)) != (
        environment.environment_lock_file_sha256
    ):
        raise Wave1LiveReadinessError("runtime environment-lock hash drift")
    preamble = assemble_runtime_preamble(root, loaded_config.config.source_bindings)
    if (
        preamble.sha256 != loaded_config.config.preamble_contract.assembled_preamble_sha256
        or preamble.removed_import_line_count
        != loaded_config.config.preamble_contract.expected_removed_import_line_count
    ):
        raise Wave1LiveReadinessError("runtime preamble hash/import-strip contract drift")
    readiness = load_wave1_implementation_readiness(root)
    if readiness.file_sha256 != loaded_config.config.frozen_readiness_file_sha256:
        raise Wave1LiveReadinessError("frozen readiness dependency drift")
    base_policy = readiness.parent.loaded_admission.loaded_base_policy.config
    registry = {
        operation.operation_id: operation
        for operation in (*base_policy.operations, *base_policy.synthetic_track.operations)
    }
    for runtime, frozen in zip(
        loaded_config.config.operations, readiness.config.primary_bundles, strict=True
    ):
        registered = registry[runtime.operation_id]
        if (
            runtime.operation_id != frozen.operation_id
            or runtime.registry_entry_hash != frozen.registry_entry_hash
            or runtime.anchor_hash != frozen.anchor_hash
            or runtime.operation_bank_entry_hash != frozen.operation_bank_entry_hash
            or runtime.fixture_aggregate_hash != frozen.fixture_aggregate_hash
            or runtime.operation_constructor != frozen.operation_constructor
            or runtime.transparency != registered.transparency
            or runtime.allowed_axiom_profile != registered.allowed_axiom_profile
        ):
            raise Wave1LiveReadinessError(f"runtime/frozen bundle drift: {runtime.operation_id}")
        if runtime.runtime_fixture_bundle_hash != compute_runtime_fixture_bundle_hash(
            loaded_fixtures.config, runtime.operation_id
        ):
            raise Wave1LiveReadinessError(
                f"runtime readiness-fixture bundle drift: {runtime.operation_id}"
            )
    return LoadedWave1LiveReadiness(
        config=loaded_config.config,
        config_path=loaded_config.path,
        config_hash=loaded_config.config_hash,
        config_file_sha256=config_file_sha256,
        fixtures=loaded_fixtures.config,
        fixture_path=loaded_fixtures.path,
        fixture_hash=loaded_fixtures.config_hash,
        fixture_file_sha256=fixture_file_sha256,
    )


def validate_n31_resolution_proposal_bundle(
    receipt: N31ResolutionProposalBundle,
    *,
    repo_root: Path | None = None,
) -> None:
    """Replay N31 against a freshly loaded canonical policy closure."""

    root = find_repo_root(repo_root)
    loaded = load_wave1_live_readiness(root)
    receipt = N31ResolutionProposalBundle.model_validate(receipt.model_dump(mode="json"))
    if (
        receipt.runtime_config_file_sha256 != loaded.config_file_sha256
        or receipt.runtime_config_hash != loaded.config_hash
        or receipt.runtime_fixture_file_sha256 != loaded.fixture_file_sha256
        or receipt.runtime_fixture_hash != loaded.fixture_hash
    ):
        raise Wave1LiveReadinessError("N31 proposal config/fixture binding drift")
    if receipt.runtime_loader_file_sha256 != hash_file(Path(__file__).resolve()):
        raise Wave1LiveReadinessError("N31 proposal does not bind the current strict loader")
    preamble = assemble_runtime_preamble(root, loaded.config.source_bindings)
    if receipt.assembled_preamble_sha256 != preamble.sha256:
        raise Wave1LiveReadinessError("N31 proposal preamble binding drift")
    n31_operation = loaded.config.operations[-1]
    if (
        n31_operation.operation_id != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
        or n31_operation.runtime_status != "n31_resolution_proposal_only_not_admitted"
        or loaded.config.n31_contract.frozen_admitted_identity_count != 0
        or loaded.config.n31_contract.activation_authorized
        or not loaded.config.n31_contract.stop_after_proposal_receipt
        or loaded.config.readiness_state.n31_active
    ):
        raise Wave1LiveReadinessError("N31 frozen proposal-only state drift")

    expected_bank_template_hash = compute_n31_proposal_bank_template_hash(
        loaded.config.n31_proposal_bank
    )
    projects = {item.project_id: item for item in loaded.fixtures.project_contexts}
    request_hashes: set[str] = set()
    raw_response_hashes: set[str] = set()
    task_receipt_hashes: set[str] = set()
    project_receipt_hashes: set[str] = set()
    resolved_hashes: set[str] = set()
    resolution_receipt_hashes: set[str] = set()
    for proposal in receipt.proposals:
        context = build_fixture_compile_context(
            projects[proposal.project_id], assembled_preamble=preamble.text
        )
        if (
            proposal.compile_context_id != context.compile_context_id
            or proposal.compile_context_fingerprint != context.fingerprint
        ):
            raise Wave1LiveReadinessError(
                f"N31 proposal compile-context drift: {proposal.project_id}"
            )
        if proposal.bank_template_hash != expected_bank_template_hash:
            raise Wave1LiveReadinessError(
                f"N31 proposal bank-template drift: {proposal.project_id}"
            )
        for value, inventory, label in (
            (proposal.phase_one_request_hash, request_hashes, "request"),
            (proposal.phase_two_request_hash, request_hashes, "request"),
            (proposal.phase_one_raw_response_sha256, raw_response_hashes, "raw response"),
            (proposal.phase_two_raw_response_sha256, raw_response_hashes, "raw response"),
            (proposal.phase_one_task_receipt_hash, task_receipt_hashes, "task receipt"),
            (proposal.phase_two_task_receipt_hash, task_receipt_hashes, "task receipt"),
            (proposal.project_receipt_hash, project_receipt_hashes, "project receipt"),
            (proposal.resolved_lean_hash, resolved_hashes, "resolved Lean identity"),
            (
                proposal.resolution_receipt_hash,
                resolution_receipt_hashes,
                "resolution receipt identity",
            ),
        ):
            if value in inventory:
                raise Wave1LiveReadinessError(f"duplicate N31 {label} hash")
            inventory.add(value)
    # Import lazily because the bounded executor imports this policy module.
    from leanfaith.sft1.wave1_live_runner import validate_n31_proposal_checkpoint

    validate_n31_proposal_checkpoint(loaded, receipt.model_dump(mode="json"))


def load_n31_resolution_proposal_bundle(
    repo_root: Path | None = None,
    *,
    receipt_path: Path | None = None,
) -> N31ResolutionProposalBundle:
    """Load the exact four-project stop receipt; absence remains fail-closed."""

    root = find_repo_root(repo_root)
    expected = _repo_path(root, DEFAULT_N31_PROPOSAL_RECEIPT_PATH)
    resolved = (receipt_path or expected).resolve()
    if resolved != expected:
        raise Wave1LiveReadinessError(
            "N31 proposal receipt path differs from the additive contract"
        )
    receipt = load_config(resolved, N31ResolutionProposalBundle).config
    validate_n31_resolution_proposal_bundle(receipt, repo_root=root)
    return receipt


def validate_positive_live_checkpoint_receipt(
    receipt: PositiveLiveCheckpointReceipt,
    *,
    repo_root: Path | None = None,
) -> None:
    """Replay a typed checkpoint against a freshly loaded canonical closure."""

    root = find_repo_root(repo_root)
    loaded = load_wave1_live_readiness(root)
    typed = PositiveLiveCheckpointReceipt.model_validate(receipt.model_dump(mode="json"))
    # Import lazily because the executor imports this policy module.
    from leanfaith.sft1.wave1_live_runner import validate_positive_checkpoint_receipt

    validate_positive_checkpoint_receipt(loaded, typed.model_dump(mode="json"))


def load_positive_live_checkpoint_receipt(
    repo_root: Path | None = None,
    *,
    receipt_path: Path | None = None,
) -> PositiveLiveCheckpointReceipt:
    """Load only the separate positive checkpoint; absence remains fail-closed."""

    root = find_repo_root(repo_root)
    expected = _repo_path(root, DEFAULT_POSITIVE_CHECKPOINT_RECEIPT_PATH)
    resolved = (receipt_path or expected).resolve()
    if resolved != expected:
        raise Wave1LiveReadinessError(
            "positive checkpoint receipt path differs from the additive contract"
        )
    receipt = load_config(resolved, PositiveLiveCheckpointReceipt).config
    validate_positive_live_checkpoint_receipt(receipt, repo_root=root)
    return receipt


__all__ = [
    "DEFAULT_N31_PROPOSAL_RECEIPT_PATH",
    "DEFAULT_POSITIVE_CHECKPOINT_RECEIPT_PATH",
    "DEFAULT_RUNTIME_CONFIG_PATH",
    "DEFAULT_RUNTIME_FIXTURE_PATH",
    "EXPECTED_RUNTIME_CONFIG_FILE_SHA256",
    "EXPECTED_RUNTIME_CONFIG_HASH",
    "EXPECTED_RUNTIME_FIXTURE_FILE_SHA256",
    "EXPECTED_RUNTIME_FIXTURE_HASH",
    "AssembledPreamble",
    "LoadedWave1LiveReadiness",
    "N31ProposalBank",
    "N31ResolutionProjectProposal",
    "N31ResolutionProposalBundle",
    "PositiveCheckpointCaseReceipt",
    "PositiveCheckpointGitIdentity",
    "PositiveCheckpointProjectJournal",
    "PositiveCheckpointResourceSnapshot",
    "PositiveLiveCheckpointReceipt",
    "PositiveResolvedAnchorInput",
    "Wave1LiveReadinessError",
    "Wave1RuntimeConfig",
    "Wave1RuntimeFixtures",
    "assemble_runtime_preamble",
    "build_fixture_compile_context",
    "compute_n31_proposal_bank_template_hash",
    "compute_positive_resolved_anchor_hash",
    "compute_runtime_fixture_bundle_hash",
    "load_n31_resolution_proposal_bundle",
    "load_positive_live_checkpoint_receipt",
    "load_wave1_live_readiness",
    "n31_phase_receipt_id",
    "validate_n31_resolution_proposal_bundle",
    "validate_positive_live_checkpoint_receipt",
]

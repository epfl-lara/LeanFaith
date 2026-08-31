"""Strict, Lean-free SFT1 Wave 1 implementation-readiness contracts.

Revision 0.3.4 is additive over the immutable revision-0.3.3 effective
readiness state.  This module may validate authored source/configuration
bytes and compute pure canonical hashes.  It deliberately exposes no Lean,
transform, gate, row-generation, or publication entrypoint.

An authored source file and a statically bound symbol are not evidence that
Lean accepted the file or that a live success/rejection fixture passed.  The
checked-in revision therefore keeps compile, live verification,
implementation readiness, and execution authorization false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.composition_policy import (
    EXPECTED_CACHE_KEY_FIELDS,
    OperationSpec,
)
from leanfaith.sft1.effective_readiness import (
    LoadedEffectiveWaveState,
    load_effective_wave_state,
)
from leanfaith.sft1.n31_guard_policy import load_n31_guard_bank

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
SymbolicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", strict=True)]
IsoDate = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", strict=True)]
LeanName = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$",
        strict=True,
    ),
]

DEFAULT_OPERATION_BANK_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_operation_banks_v0_3_4.yaml"
)
DEFAULT_FIXTURE_PATH = Path("tests/fixtures/sft1/wave1_v0_3_4.yaml")
DEFAULT_IMPLEMENTATION_READINESS_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_implementation_readiness_v0_3_4.yaml"
)

EXPECTED_PARENT_COMMIT = "18618ca6ff8383c5254bfacbfed2f4747daebbb7"
EXPECTED_PARENT_TREE = "2c3521f71fd6e54c47333c8ca759ae7dbdc80366"
EXPECTED_PARENT_EFFECTIVE_FILE_SHA256 = (
    "5673d2ee2e3d9b088bcc42ccec4d4d851096b6c0fc8cc5349b1c3b231f2b1474"
)
EXPECTED_PARENT_EFFECTIVE_SEMANTIC_HASH = (
    "1b323508b3c3edcc62582d637c88af693e81507c3b7f1bd178dc7f3b8af2412e"
)
EXPECTED_PARENT_LOADER_FILE_SHA256 = (
    "3e1ccb7f1b0507960bdf7632278ed994a60f22cab0efcc40999ed0d5504cb146"
)

EXPECTED_USER_AUTHORIZATION_TEXT = (
    "Keep commit `18618ca6ff8383c5254bfacbfed2f4747daebbb7` frozen and authorize only "
    "task-owned Wave 1 implementation-readiness work for the five primary mechanisms, with N31 "
    "N-PROOF remaining optional; do not start Lean or any gate, generate rows, implement Wave 2, "
    "authorize production or 10K, scale, or publish."
)
EXPECTED_USER_AUTHORIZATION_SHA256 = (
    "fcda45c53522ec46966e0b8ab1db7fbb4a12b7c6a0f9a720fb1da0ba40d74c6d"
)

EXPECTED_PRIMARY_OPERATION_IDS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
)
EXPECTED_OPTIONAL_PROOF_OPERATION_ID = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
EXPECTED_PROJECT_IDS: tuple[str, ...] = ("compiler_data", "cslib", "mathlib", "physlib")
EXPECTED_MECHANISM_IDS: tuple[str, ...] = (
    "p01_alpha_rename_single_v1",
    "p15_swap_iff_sides_v1",
    "p18_symmetrize_equality_v1",
    "p21_beta_reduce_v1",
    "n31_required_guard_mutation",
)
EXPECTED_EVIDENCE_CLASSES: tuple[str, ...] = (
    "P-DEF",
    "P-SCHEMA",
    "P-SCHEMA",
    "P-DEF",
    "N-RUBRIC",
)
EXPECTED_REGISTRY_ENTRY_HASHES: tuple[str, ...] = (
    "36b680c6c3407d0761de8af1b0c5e685ce0bc89fefed720bc46255c9bc218844",
    "cefb0b1f138d40cc48df102d834e6f98735697213f4ab0d2688fe64943c7ed97",
    "84d9a3615a30aca49e44705fc341f147b74b2885f4831244bb31ea7f97364472",
    "5a7ff8ce195cafd42b971bb5a07e23ec7e46b7d0376b7ab34c8dfa42459261f3",
    "e10cae5e82a8b17bcfe9a5bd4d5811eea6b75c78e77b6c67a3c082a2c2feae5f",
)
EXPECTED_OPTIONAL_PROOF_REGISTRY_ENTRY_HASH = (
    "b3ba4acd04c3adb7eff83e8a7b242a7c953f5d2b87ec659e9bbc9abafe81199a"
)
EXPECTED_ANCHORS: tuple[tuple[str, str], ...] = (
    (
        "sft1.meta.p01_alpha_rename_single.v1",
        "ca485f300ecc818057f10877f0eec5c6b4b963fec2e0a574a6895f9d83357095",
    ),
    (
        "sft1.schema.p15_swap_iff_sides.v1",
        "ecad6c6ea110dff281b051045eb37846b194b54fa1a879bce11f5c04e5ed6bd4",
    ),
    (
        "sft1.schema.p18_equality_symmetry.v1",
        "a79b0b92d5fca0360c38a9edaead1edec038df63bc2167da580bcf9d2c335b8d",
    ),
    (
        "sft1.meta.p21_beta_reduce.v1",
        "dd364fd25afd801ae97c2f47ea59ca9f46cae0fff393da5a57b5842acb306d4c",
    ),
    (
        "shared_consistency_rubric.required_domain_guard",
        "03789414409dca332f807a6b53ed1fe4416fbfd13c377362119a910858fbf00d",
    ),
)

# These two constants are intentionally isolated so the coordinator can fill
# the final checked-in identities after the independently authored YAML stops
# changing.  A zero digest never passes the checked-in loader.
EXPECTED_OPERATION_BANK_FILE_SHA256 = (
    "282836a539d055e227ccfba12dd612522654f036d6705aec07a551a217c82a34"
)
EXPECTED_OPERATION_BANK_SEMANTIC_HASH = (
    "99440883e2ae37b7ee95ca3332273107665277cb9597586c6de427b36e9ce8da"
)
EXPECTED_FIXTURE_FILE_SHA256 = "0856c6cfa1536bd935d4606ec2d09a34c72ef5c2ddf92d2f15b67867ac6dd6ea"
EXPECTED_FIXTURE_SEMANTIC_HASH = "6d8dbc0d7da271f880223ec519afabbe030dd0d480ba7d0731707d5b66f46e51"
EXPECTED_IMPLEMENTATION_READINESS_FILE_SHA256 = (
    "87197cef05d4e755a0d92745b2b3846787b5e1159edac29dfdd967ba81aed614"
)
EXPECTED_IMPLEMENTATION_READINESS_SEMANTIC_HASH = (
    "cdf5ad5572c3887213017fe6d7c17987fedbe0eadecb47e87b41a8111911e25a"
)

EXPECTED_CACHE_CONTRACT_HASH = "1ffb866d02376fbce8161fdfeaf75fd5d13770f36e7a2526e6d107f2b7091d43"
EXPECTED_EXECUTION_CONTRACT_HASH = (
    "ab98caf36f359d2a2599e166bb0e13cd07c2fe03b1392200b8348c78912412fa"
)


class Wave1ReadinessError(ValueError):
    """Raised when an additive Wave 1 readiness binding fails closed."""


class OperationDiscoveryOrders(StrictModel):
    p01_alpha_rename_single: Literal["outer_binder_ordinal_ascending_v1"]
    p15_swap_iff_sides: Literal["singleton_outer_target_v1"]
    p18_symmetrize_equality: Literal["singleton_outer_target_v1"]
    p21_beta_reduce: Literal["structural_preorder_term_site_v1"]
    n31_drop_required_guard_rubric: Literal[
        "structural_preorder_target_site_then_guard_binder_ordinal_then_bank_entry_order_v1"
    ]


class SelectionContract(StrictModel):
    expression_site_path_format: Literal["Lean.SubExpr.Pos.toString_v1"]
    expression_site_must_be_rediscovered_in_current_typed_expr: Literal[True]
    stale_or_ambiguous_site_disposition: Literal["typed_not_applicable"]
    n31_guard_binder_index_scope: Literal["opened_outer_telescope_v1"]
    n31_target_path_scope: Literal["opened_outer_telescope_body_v1"]
    deterministic_candidate_order: Literal[
        "operation_then_structural_preorder_site_then_binder_index_then_bank_entry_order_v1"
    ]
    operation_discovery_orders: OperationDiscoveryOrders
    surface_text_selection_forbidden: Literal[True]


class N31RuntimeAdmissionContract(StrictModel):
    source_identity_type_symbol: Literal["LeanFaith.SFT1.Wave1.N31BankIdentity"]
    source_admission_symbol: Literal["LeanFaith.SFT1.Wave1.admittedN31BankIdentitiesV0_3_4"]
    admitted_resolved_bank_identity_count: Literal[0]
    admitted_resolved_bank_identities: tuple[()]
    current_runtime_activation_authorized: Literal[False]
    empty_admission_blocks_dispatch_and_discovery: Literal[True]
    absent_bank_failure_reason: Literal["n31BankMissing"]
    unadmitted_bank_failure_reason: Literal["n31BankInvalid"]
    future_activation_requires_separate_user_authorization: Literal[True]
    future_identity_pin_field_order: tuple[
        Literal["projectId"],
        Literal["bankId"],
        Literal["resolvedLeanHash"],
        Literal["resolutionReceiptHash"],
    ]
    exact_project_bank_resolved_lean_hash_resolution_receipt_pin_required: Literal[True]
    in_process_exact_full_typed_bank_hash_verification_required: Literal[True]
    in_process_exact_full_typed_bank_hash_verifier_bound: Literal[False]
    identity_membership_is_not_a_substitute_for_bank_hash_verification: Literal[True]
    verification_must_precede_dispatch_discovery_and_replay: Literal[True]


class N31CertificateBindingContract(StrictModel):
    source_certificate_symbol: Literal["LeanFaith.SFT1.Wave1.N31RubricCertificate"]
    source_bank_symbol: Literal["LeanFaith.SFT1.Wave1.N31TargetBank"]
    source_reachability_symbol: Literal["LeanFaith.SFT1.Wave1.N31ReachabilityEvidence"]
    full_typed_bank_embedded_in_certificate: Literal[True]
    full_typed_bank_scope: Literal[
        "identity_entries_retained_patterns_implications_contradictions_v1"
    ]
    full_reachability_evidence_embedded_in_certificate: Literal[True]
    full_reachability_scope: Literal["mode_guard_ordinal_ordered_assignment_exprs_v1"]
    reachability_assignment_equality: Literal["ordered_Expr.equal_v1"]
    replay_requires_supplied_bank_and_reachability_exact_equal: Literal[True]
    mismatch_failure_reason: Literal["replayContextMismatch"]


class N31RuntimeStructuralConstraints(StrictModel):
    selectable_guard_role_and_instance_indices_disjoint: Literal[True]
    selectable_target_role_and_instance_indices_disjoint: Literal[True]
    selectable_guard_instance_or_type_indices_nonempty: Literal[True]
    selectable_target_instance_or_type_indices_nonempty: Literal[True]
    retained_contradiction_role_and_instance_paths_disjoint: Literal[True]
    retained_contradiction_instance_or_type_paths_nonempty: Literal[True]
    exact_expr_constraints_must_be_clean_and_closed: Literal[True]
    unresolved_symbolic_candidate_bank_is_runtime_inadmissible: Literal[True]
    resolved_runtime_bank_must_satisfy_all_constraints: Literal[True]


class OperationBankEntry(StrictModel):
    operation_id: OperationId
    mechanism_id: SymbolicId
    evidence_class: Literal["P-DEF", "P-SCHEMA", "N-RUBRIC"]
    registry_entry_hash: Sha256
    anchor_ref: NonEmptyStr
    anchor_hash: Sha256
    selector_kind: SymbolicId
    matcher_id: SymbolicId
    typed_applicability_checks: tuple[NonEmptyStr, ...] = Field(min_length=1)
    exact_delta_fields: tuple[NonEmptyStr, ...] = Field(min_length=1)
    static_source_complete: Literal[True]
    live_lean_verified: Literal[False]
    executable: Literal[False]


class HeadRolePattern(StrictModel):
    head_name: LeanName
    role_offsets_from_end: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _offsets_are_unique_and_nonnegative(self) -> HeadRolePattern:
        if any(offset < 0 for offset in self.role_offsets_from_end):
            raise ValueError("head-role offsets must be nonnegative")
        if len(set(self.role_offsets_from_end)) != len(self.role_offsets_from_end):
            raise ValueError("head-role offsets must be unique")
        return self


class GuardTargetShapeEntry(StrictModel):
    shape_id: Literal[
        "ne_zero_guard_v1",
        "positive_guard_v1",
        "nonnegative_guard_v1",
        "membership_guard_v1",
        "index_lt_guard_v1",
    ]
    guard_patterns: tuple[HeadRolePattern, ...] = Field(min_length=1)
    target_candidates: tuple[HeadRolePattern, ...] = Field(min_length=1)
    blocks_removal_of: tuple[SymbolicId, ...]
    contradiction_shape_ids: tuple[SymbolicId, ...] = Field(min_length=1)
    live_resolved: Literal[False]


class TargetMatchingSemantics(StrictModel):
    head_name_match: Literal["exact_Name.toString"]
    symbolic_path_basis: Literal["recursive_offset_from_end_of_stripMData_getAppArgs_v1"]
    live_resolved_path_basis: Literal["recursive_zero_based_index_into_stripMData_getAppArgs_v1"]
    role_expr_match: Literal["exact_binder_aware_structure"]
    metadata_transparent: Literal[True]


class SymbolicArgumentPath(StrictModel):
    offsets_from_end: tuple[int, ...] = Field(min_length=1)
    live_resolution_verified: Literal[False]

    @model_validator(mode="after")
    def _nonnegative_offsets(self) -> SymbolicArgumentPath:
        if any(offset < 0 for offset in self.offsets_from_end):
            raise ValueError("symbolic application-path offsets must be nonnegative")
        return self


class SymbolicNestedHeadConstraint(StrictModel):
    path: SymbolicArgumentPath
    head_name: LeanName
    argument_count: None
    live_resolution_verified: Literal[False]


class SymbolicLiteralConstraint(StrictModel):
    path: SymbolicArgumentPath
    literal_kind: Literal["nat"]
    nat_value: Literal[0]
    live_resolution_verified: Literal[False]


class RetainedContradictionPatternEntry(StrictModel):
    shape_id: Literal[
        "eq_zero_retained_v1",
        "nonpositive_retained_v1",
        "negative_retained_v1",
        "not_membership_retained_v1",
        "bound_le_index_retained_v1",
    ]
    contradicts_guard_shape_id: Literal[
        "ne_zero_guard_v1",
        "positive_guard_v1",
        "nonnegative_guard_v1",
        "membership_guard_v1",
        "index_lt_guard_v1",
    ]
    encoding_id: Literal[
        "ofnat_application_with_nat_literal_zero_v1",
        "not_membership_nested_application_v1",
        "reversed_bound_index_roles_v1",
    ]
    head_name: LeanName
    argument_count: None
    root_head_and_arity_live_resolution_verified: Literal[False]
    role_paths: tuple[SymbolicArgumentPath, ...] = Field(min_length=1)
    instance_paths: tuple[SymbolicArgumentPath, ...]
    instance_path_resolution_required: Literal[True]
    nested_head_constraints: tuple[SymbolicNestedHeadConstraint, ...]
    literal_constraints: tuple[SymbolicLiteralConstraint, ...]
    exact_expr_constraints: tuple[()]
    live_resolved: Literal[False]


class N31GuardTargetBank(StrictModel):
    bank_id: Literal["sft1_n31_guard_target_candidates_v0_3_4"]
    parent_design_path: Literal[
        "configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml"
    ]
    parent_design_file_sha256: Literal[
        "c2a5aa63158ffbc561bc61f2e3acaa2598aff54a926fd774014e62e6c1cd8cd8"
    ]
    status: Literal["authored_candidates_require_live_resolution"]
    all_selectable_entries_live_resolved: Literal[False]
    all_retained_contradiction_patterns_live_resolved: Literal[False]
    usable_for_gate_execution: Literal[False]
    matching_semantics: TargetMatchingSemantics
    retained_contradiction_lean_structure: Literal[
        "LeanFaith.SFT1.Wave1.N31RetainedContradictionPattern"
    ]
    retained_contradiction_patterns_are_not_selectable_or_dispatchable: Literal[True]
    retained_prop_match_cardinality: Literal[
        "exactly_one_across_selectable_and_contradiction_patterns_v1"
    ]
    unknown_or_ambiguous_retained_prop_disposition: Literal["n31RetainedContextUnknownOrAmbiguous"]
    unknown_or_ambiguous_retained_prop_is_typed_not_applicable: Literal[True]
    shape_entries: tuple[GuardTargetShapeEntry, ...]
    retained_contradiction_patterns: tuple[RetainedContradictionPatternEntry, ...]
    live_resolution_requirements: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _exact_shape_inventory(self) -> N31GuardTargetBank:
        expected = (
            "ne_zero_guard_v1",
            "positive_guard_v1",
            "nonnegative_guard_v1",
            "membership_guard_v1",
            "index_lt_guard_v1",
        )
        if tuple(entry.shape_id for entry in self.shape_entries) != expected:
            raise ValueError("N31 guard-target shape inventory/order drift")
        expected_contradictions = (
            (
                "eq_zero_retained_v1",
                "ne_zero_guard_v1",
                "ofnat_application_with_nat_literal_zero_v1",
                "Eq",
                ((1,),),
                ((0, 1),),
            ),
            (
                "nonpositive_retained_v1",
                "positive_guard_v1",
                "ofnat_application_with_nat_literal_zero_v1",
                "LE.le",
                ((1,),),
                ((0, 1),),
            ),
            (
                "negative_retained_v1",
                "nonnegative_guard_v1",
                "ofnat_application_with_nat_literal_zero_v1",
                "LT.lt",
                ((1,),),
                ((0, 1),),
            ),
            (
                "not_membership_retained_v1",
                "membership_guard_v1",
                "not_membership_nested_application_v1",
                "Not",
                ((0, 1), (0, 0)),
                (),
            ),
            (
                "bound_le_index_retained_v1",
                "index_lt_guard_v1",
                "reversed_bound_index_roles_v1",
                "LE.le",
                ((0,), (1,)),
                (),
            ),
        )
        observed_contradictions = tuple(
            (
                entry.shape_id,
                entry.contradicts_guard_shape_id,
                entry.encoding_id,
                entry.head_name,
                tuple(path.offsets_from_end for path in entry.role_paths),
                tuple(constraint.path.offsets_from_end for constraint in entry.literal_constraints),
            )
            for entry in self.retained_contradiction_patterns
        )
        if observed_contradictions != expected_contradictions:
            raise ValueError("N31 retained-contradiction pattern inventory/order drift")
        membership = self.retained_contradiction_patterns[3]
        if tuple(
            (constraint.path.offsets_from_end, constraint.head_name)
            for constraint in membership.nested_head_constraints
        ) != (((0,), "Membership.mem"),):
            raise ValueError("N31 not-membership nested-head candidate drift")
        for entry in self.retained_contradiction_patterns[:3]:
            if tuple(
                (constraint.path.offsets_from_end, constraint.head_name)
                for constraint in entry.nested_head_constraints
            ) != (((0,), "OfNat.ofNat"),):
                raise ValueError("N31 zero nested-head candidate drift")
            if tuple(
                (constraint.literal_kind, constraint.nat_value)
                for constraint in entry.literal_constraints
            ) != (("nat", 0),):
                raise ValueError("N31 exact zero literal candidate drift")
        if any(entry.live_resolved for entry in self.retained_contradiction_patterns):
            raise ValueError("N31 retained-contradiction candidates cannot claim live resolution")
        return self


class P01IdentityBlocker(StrictModel):
    blocker_id: Literal["p01_alpha_closed_expr_hash_collision"]
    status: Literal["open_fail_closed"]
    frozen_evidence_path: Literal[
        "configs/transformations/sft1_value_first_v1/"
        "repr_six_goal_evidence_v0_3_1/01_mathlib_add_pow.json"
    ]
    frozen_evidence_file_sha256: Literal[
        "c999fe73078a754f174f7f465d3c829f1944f3b637a31904f2c16dc0723cf4a5"
    ]
    observed_reference_closed_expr_hash: Sha256
    observed_candidate_closed_expr_hash: Sha256
    observed_render_hashes_are_distinct: Literal[True]
    frozen_composition_rejects_repeated_closed_expr_hashes: Literal[True]
    binder_aware_delta_fingerprint_is_additional_evidence_only: Literal[True]
    binder_aware_delta_may_override_frozen_duplicate_rule: Literal[False]
    blocks_p01_implementation_readiness: Literal[True]
    resolution_requires_additive_coordinator_policy_decision: Literal[True]

    @model_validator(mode="after")
    def _collision_is_exact(self) -> P01IdentityBlocker:
        expected = "c792a901d406878bca34269e580b34cb068515694fb6c80e5bebefb9b18f9c83"
        if (
            self.observed_reference_closed_expr_hash != expected
            or self.observed_candidate_closed_expr_hash != expected
        ):
            raise ValueError("P01 frozen closed-Expr collision evidence drift")
        return self


class OptionalProofAdapterBank(StrictModel):
    operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    registry_entry_hash: Literal["b3ba4acd04c3adb7eff83e8a7b242a7c953f5d2b87ec659e9bbc9abafe81199a"]
    parent_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    status: Literal["optional_not_implemented"]
    independent_mutation_implementation_forbidden: Literal[True]
    parent_candidate_reuse_required: Literal[True]
    parent_root_pool_only: Literal[True]
    blocks_parent_or_wave_readiness: Literal[False]
    exact_source_proof_required_if_attempted: Literal[True]
    exact_candidate_refutation_required_if_attempted: Literal[True]
    failed_search_is_evidence: Literal[False]
    candidate_truth_when_absent: Literal["unknown"]
    candidate_truth_when_complete_proof_receipt_passes: Literal["refuted"]
    counts_against_own_and_parent_caps: Literal[True]
    may_emit_additional_model_facing_pair: Literal[False]


class Wave1OperationBanks(StrictModel):
    schema_version: Literal[1]
    bank_id: Literal["sft1_wave1_operation_banks_v0_3_4"]
    bank_version: Literal["0.3.4"]
    status: Literal["static_authored_hash_bound_uncompiled"]
    parent_checkpoint_commit: Literal["18618ca6ff8383c5254bfacbfed2f4747daebbb7"]
    primary_operation_ids: tuple[OperationId, ...]
    optional_proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    bank_may_authorize_execution: Literal[False]
    bank_may_create_labels_or_rows: Literal[False]
    selection_contract: SelectionContract
    n31_runtime_admission_contract: N31RuntimeAdmissionContract
    n31_certificate_binding_contract: N31CertificateBindingContract
    n31_runtime_structural_constraints: N31RuntimeStructuralConstraints
    operation_banks: tuple[OperationBankEntry, ...]
    n31_guard_target_bank: N31GuardTargetBank
    p01_identity_blocker: P01IdentityBlocker
    optional_n31_proof_adapter: OptionalProofAdapterBank

    @model_validator(mode="after")
    def _exact_static_bank_projection(self) -> Wave1OperationBanks:
        if self.primary_operation_ids != EXPECTED_PRIMARY_OPERATION_IDS:
            raise ValueError("Wave 1 primary operation bank inventory/order drift")
        observed = tuple(entry.operation_id for entry in self.operation_banks)
        if observed != EXPECTED_PRIMARY_OPERATION_IDS:
            raise ValueError("Wave 1 operation bank entries/order drift")
        expected_rows = tuple(
            zip(
                EXPECTED_PRIMARY_OPERATION_IDS,
                EXPECTED_MECHANISM_IDS,
                EXPECTED_EVIDENCE_CLASSES,
                EXPECTED_REGISTRY_ENTRY_HASHES,
                EXPECTED_ANCHORS,
                strict=True,
            )
        )
        actual_rows = tuple(
            (
                entry.operation_id,
                entry.mechanism_id,
                entry.evidence_class,
                entry.registry_entry_hash,
                (entry.anchor_ref, entry.anchor_hash),
            )
            for entry in self.operation_banks
        )
        if actual_rows != expected_rows:
            raise ValueError("Wave 1 operation bank registry/anchor projection drift")
        return self


class FixtureProjectContext(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    source_revision: GitCommit
    compile_project_id: Literal["cslib", "mathlib", "physlib"]
    compile_project_revision: GitCommit
    import_header: NonEmptyStr
    namespace_context: tuple[NonEmptyStr, ...]
    open_context: tuple[NonEmptyStr, ...]
    scoped_context: tuple[NonEmptyStr, ...]
    options: dict[str, bool]
    live_context_verified_for_these_fixtures: Literal[False]

    @model_validator(mode="after")
    def _exact_options(self) -> FixtureProjectContext:
        if self.options != {"Elab.async": False, "autoImplicit": False}:
            raise ValueError("fixture project options must remain exact and synchronous")
        return self


class OuterBinderSelector(StrictModel):
    kind: Literal["outer_binder"]
    binder_index: Literal[0]


class ExactSiteSelector(StrictModel):
    kind: Literal["outer_target", "exact_expr_site"]
    site_path: Literal["/"]


class FutureTelescopeAssignment(StrictModel):
    binder_index: Annotated[int, Field(ge=0, strict=True)]
    binder_name: Literal["x", "hx", "hpos"]
    candidate_term: Literal["(1 : Nat)", "Nat.one_ne_zero", "Nat.zero_lt_succ 0"]
    live_elaboration_verified: Literal[False]


class GuardAndTargetSelector(StrictModel):
    kind: Literal["exact_outer_guard_and_unique_body_target"]
    guard_binder_index: Literal[1]
    guard_shape_id: Literal["ne_zero_guard_v1"]
    target_head_name: Literal["HDiv.hDiv"]
    target_path: None
    reachability_mode_id: Literal["explicit_telescope_witness_and_retained_hypothesis_proofs"]
    future_telescope_assignments: tuple[FutureTelescopeAssignment, ...]


FixtureSelector = Annotated[
    OuterBinderSelector | ExactSiteSelector | GuardAndTargetSelector,
    Field(discriminator="kind"),
]


class FixtureTemplate(StrictModel):
    template_id: SymbolicId
    operation_id: OperationId
    fixture_kind: Literal["success", "adversarial_rejection"]
    reference_term: NonEmptyStr
    selector: FixtureSelector
    expected_future_terminal: Literal["candidate", "typed_not_applicable"]
    expected_reason_class: SymbolicId

    @model_validator(mode="after")
    def _kind_matches_terminal(self) -> FixtureTemplate:
        if self.fixture_kind == "success":
            if self.expected_future_terminal != "candidate":
                raise ValueError("success fixture templates must expect a candidate")
        elif self.expected_future_terminal != "typed_not_applicable":
            raise ValueError("adversarial fixture templates must reject fail closed")
        if isinstance(self.selector, GuardAndTargetSelector):
            expected = (
                ((0, "x", "(1 : Nat)"), (1, "hx", "Nat.one_ne_zero"))
                if self.fixture_kind == "success"
                else (
                    (0, "x", "(1 : Nat)"),
                    (1, "hx", "Nat.one_ne_zero"),
                    (2, "hpos", "Nat.zero_lt_succ 0"),
                )
            )
            observed = tuple(
                (
                    assignment.binder_index,
                    assignment.binder_name,
                    assignment.candidate_term,
                )
                for assignment in self.selector.future_telescope_assignments
            )
            if observed != expected:
                raise ValueError("N31 future telescope assignment inventory/order drift")
        return self


class FixtureMatrixEntry(StrictModel):
    fixture_id: SymbolicId
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    operation_id: OperationId
    fixture_kind: Literal["success", "adversarial_rejection"]
    template_id: SymbolicId
    expected_future_terminal: Literal["candidate", "typed_not_applicable"]
    expected_reason_class: SymbolicId
    live_verified: Literal[False]
    receipt_sha256: None


class FixtureMatrixContract(StrictModel):
    primary_operation_count: Literal[5]
    registered_project_count: Literal[4]
    fixture_kinds_per_cell: Literal[2]
    exact_fixture_count: Literal[40]
    one_success_and_one_adversarial_rejection_per_operation_project: Literal[True]
    all_live_verified: Literal[False]
    any_receipt_present: Literal[False]
    matrix_is_gate_evidence: Literal[False]
    n31_reachability_candidate_terms_bound_by_fixture_hash: Literal[True]


class Wave1FixtureSet(StrictModel):
    schema_version: Literal[1]
    fixture_set_id: Literal["sft1_wave1_static_fixtures_v0_3_4"]
    fixture_set_version: Literal["0.3.4"]
    status: Literal["static_specification_uncompiled_unexecuted"]
    parent_checkpoint_commit: Literal["18618ca6ff8383c5254bfacbfed2f4747daebbb7"]
    primary_operation_ids: tuple[OperationId, ...]
    optional_n31_proof_fixture_count: Literal[0]
    fixture_text_is_not_a_source_root: Literal[True]
    fixture_text_is_not_a_model_facing_row: Literal[True]
    lean_compilation_or_execution_performed: Literal[False]
    project_contexts: tuple[FixtureProjectContext, ...]
    templates: tuple[FixtureTemplate, ...]
    fixture_matrix: tuple[FixtureMatrixEntry, ...]
    matrix_contract: FixtureMatrixContract

    @model_validator(mode="after")
    def _exact_fixture_matrix(self) -> Wave1FixtureSet:
        if self.primary_operation_ids != EXPECTED_PRIMARY_OPERATION_IDS:
            raise ValueError("fixture primary operation inventory/order drift")
        if tuple(item.project_id for item in self.project_contexts) != EXPECTED_PROJECT_IDS:
            raise ValueError("fixture project context inventory/order drift")
        expected_contexts = (
            (
                "compiler_data",
                "ca37d4701b11022f183e72b7b96ff543a8a615d3",
                "mathlib",
                "d568c8c09630de097a046763c17b9ea99f95f950",
                "import Mathlib",
            ),
            (
                "cslib",
                "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
                "cslib",
                "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
                "import Cslib",
            ),
            (
                "mathlib",
                "d568c8c09630de097a046763c17b9ea99f95f950",
                "mathlib",
                "d568c8c09630de097a046763c17b9ea99f95f950",
                "import Mathlib",
            ),
            (
                "physlib",
                "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
                "physlib",
                "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
                "import PhysLean",
            ),
        )
        observed_contexts = tuple(
            (
                item.project_id,
                item.source_revision,
                item.compile_project_id,
                item.compile_project_revision,
                item.import_header,
            )
            for item in self.project_contexts
        )
        if observed_contexts != expected_contexts:
            raise ValueError("fixture project revision/import context drift")
        if any(
            item.namespace_context or item.open_context or item.scoped_context
            for item in self.project_contexts
        ):
            raise ValueError("fixture contexts must remain minimal and scope-free")

        expected_templates = (
            ("p01_success_v1", EXPECTED_PRIMARY_OPERATION_IDS[0], "success"),
            (
                "p01_reject_v1",
                EXPECTED_PRIMARY_OPERATION_IDS[0],
                "adversarial_rejection",
            ),
            ("p15_success_v1", EXPECTED_PRIMARY_OPERATION_IDS[1], "success"),
            (
                "p15_reject_v1",
                EXPECTED_PRIMARY_OPERATION_IDS[1],
                "adversarial_rejection",
            ),
            ("p18_success_v1", EXPECTED_PRIMARY_OPERATION_IDS[2], "success"),
            (
                "p18_reject_v1",
                EXPECTED_PRIMARY_OPERATION_IDS[2],
                "adversarial_rejection",
            ),
            ("p21_success_v1", EXPECTED_PRIMARY_OPERATION_IDS[3], "success"),
            (
                "p21_reject_v1",
                EXPECTED_PRIMARY_OPERATION_IDS[3],
                "adversarial_rejection",
            ),
            ("n31_rubric_success_v1", EXPECTED_PRIMARY_OPERATION_IDS[4], "success"),
            (
                "n31_rubric_reject_v1",
                EXPECTED_PRIMARY_OPERATION_IDS[4],
                "adversarial_rejection",
            ),
        )
        observed_templates = tuple(
            (item.template_id, item.operation_id, item.fixture_kind) for item in self.templates
        )
        if observed_templates != expected_templates:
            raise ValueError("fixture template inventory/order drift")
        templates = {item.template_id: item for item in self.templates}

        expected_matrix: list[tuple[str, str, str]] = []
        for operation_id in EXPECTED_PRIMARY_OPERATION_IDS:
            for project_id in EXPECTED_PROJECT_IDS:
                expected_matrix.append((operation_id, project_id, "success"))
                expected_matrix.append((operation_id, project_id, "adversarial_rejection"))
        observed_matrix = [
            (item.operation_id, item.project_id, item.fixture_kind) for item in self.fixture_matrix
        ]
        if observed_matrix != expected_matrix or len(self.fixture_matrix) != 40:
            raise ValueError("fixture matrix must be the exact ordered 5x4x2 product")
        if len({item.fixture_id for item in self.fixture_matrix}) != 40:
            raise ValueError("fixture IDs must be unique")
        for item in self.fixture_matrix:
            template = templates.get(item.template_id)
            if template is None:
                raise ValueError(f"fixture references unknown template: {item.fixture_id}")
            if (
                template.operation_id != item.operation_id
                or template.fixture_kind != item.fixture_kind
                or template.expected_future_terminal != item.expected_future_terminal
                or template.expected_reason_class != item.expected_reason_class
            ):
                raise ValueError(f"fixture/template contract drift: {item.fixture_id}")
        return self


def fixture_operation_bundle_hash(fixtures: Wave1FixtureSet, operation_id: str) -> str:
    """Hash the complete, typed, independently reviewable fixture bundle."""

    if operation_id not in EXPECTED_PRIMARY_OPERATION_IDS:
        raise Wave1ReadinessError(f"not a primary Wave 1 operation: {operation_id}")
    indexed: dict[tuple[str, str], FixtureMatrixEntry] = {
        (item.project_id, item.fixture_kind): item
        for item in fixtures.fixture_matrix
        if item.operation_id == operation_id
    }
    entries = [
        indexed[(project_id, fixture_kind)].model_dump(mode="json")
        for project_id in EXPECTED_PROJECT_IDS
        for fixture_kind in ("success", "adversarial_rejection")
        if (project_id, fixture_kind) in indexed
    ]
    if len(entries) != 8:
        raise Wave1ReadinessError(
            f"operation fixture bundle must contain exactly eight entries: {operation_id}"
        )
    template_index = {(item.operation_id, item.fixture_kind): item for item in fixtures.templates}
    templates = [
        template_index[(operation_id, fixture_kind)].model_dump(mode="json")
        for fixture_kind in ("success", "adversarial_rejection")
        if (operation_id, fixture_kind) in template_index
    ]
    if len(templates) != 2:
        raise Wave1ReadinessError(
            f"operation fixture bundle must contain exactly two templates: {operation_id}"
        )
    project_contexts = [context.model_dump(mode="json") for context in fixtures.project_contexts]
    if len(project_contexts) != 4:
        raise Wave1ReadinessError("fixture bundle must contain four project contexts")
    return hash_canonical(
        {
            "operation_id": operation_id,
            "referenced_templates_success_then_adversarial_rejection": templates,
            "project_contexts_in_registered_order": project_contexts,
            "fixtures_sorted_by_project_then_kind": entries,
        }
    )


def operation_bank_entry_hash(banks: Wave1OperationBanks, operation_id: str) -> str:
    """Hash one exact typed operation-bank entry under the frozen field basis."""

    matches = [entry for entry in banks.operation_banks if entry.operation_id == operation_id]
    if len(matches) != 1:
        raise Wave1ReadinessError(f"operation bank must contain exactly one entry: {operation_id}")
    return hash_canonical({"operation_bank_entry": matches[0].model_dump(mode="json")})


class ParentFreezeBinding(StrictModel):
    commit: Literal["18618ca6ff8383c5254bfacbfed2f4747daebbb7"]
    tree: Literal["2c3521f71fd6e54c47333c8ca759ae7dbdc80366"]
    effective_overlay_path: Literal[
        "configs/transformations/sft1_value_first_v1/wave1_effective_readiness_v0_3_3.yaml"
    ]
    effective_overlay_file_sha256: Literal[
        "5673d2ee2e3d9b088bcc42ccec4d4d851096b6c0fc8cc5349b1c3b231f2b1474"
    ]
    effective_overlay_semantic_hash: Literal[
        "1b323508b3c3edcc62582d637c88af693e81507c3b7f1bd178dc7f3b8af2412e"
    ]
    effective_loader_path: Literal["src/leanfaith/sft1/effective_readiness.py"]
    effective_loader_file_sha256: Literal[
        "3e1ccb7f1b0507960bdf7632278ed994a60f22cab0efcc40999ed0d5504cb146"
    ]
    preserves_complete_46_operation_registry: Literal[True]


class ImplementationAuthorizationBinding(StrictModel):
    exact_user_text: NonEmptyStr
    exact_user_text_sha256: Sha256
    recorded_date: IsoDate
    interpretation: Literal["task_owned_static_wave1_implementation_readiness_only"]

    @model_validator(mode="after")
    def _exact_text_and_hash(self) -> ImplementationAuthorizationBinding:
        if self.exact_user_text != EXPECTED_USER_AUTHORIZATION_TEXT:
            raise ValueError("Wave 1 implementation authorization text drift")
        if authorization_text_hash(self.exact_user_text) != self.exact_user_text_sha256:
            raise ValueError("Wave 1 implementation authorization text/hash mismatch")
        if self.exact_user_text_sha256 != EXPECTED_USER_AUTHORIZATION_SHA256:
            raise ValueError("Wave 1 implementation authorization hash drift")
        return self


class ImplementationAuthorizationScope(StrictModel):
    primary_operation_ids: tuple[OperationId, ...]
    semantic_mechanism_count: Literal[5]
    optional_proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    task_owned_source_authoring_allowed: Literal[True]
    policy_bank_fixture_loader_test_work_allowed: Literal[True]
    lean_compilation_or_execution_allowed: Literal[False]
    transform_or_gate_execution_allowed: Literal[False]
    row_generation_or_emission_allowed: Literal[False]
    wave2_implementation_allowed: Literal[False]
    production_admission_allowed: Literal[False]
    ten_k_allowed: Literal[False]
    scale_allowed: Literal[False]
    publication_allowed: Literal[False]

    @model_validator(mode="after")
    def _exact_primary_scope(self) -> ImplementationAuthorizationScope:
        if self.primary_operation_ids != EXPECTED_PRIMARY_OPERATION_IDS:
            raise ValueError("implementation authorization primary scope drift")
        return self


class LeanSourceArtifact(StrictModel):
    path: Literal["LeanFaith/Meta/SFT1/Wave1.lean"]
    file_sha256: Sha256
    import_strip_policy: Literal["remove_lines_whose_first_token_is_import_v1"]
    import_stripped_preamble_sha256: Sha256
    source_version_symbol: Literal["LeanFaith.SFT1.Wave1.sourceVersion"]
    source_version_value: Literal["sft1_wave1_expr_engine_v0_3_4"]
    public_discover_symbol: Literal["LeanFaith.SFT1.Wave1.discover"]
    public_dispatch_symbol: Literal["LeanFaith.SFT1.Wave1.dispatchAt"]
    public_checker_symbol: Literal["LeanFaith.SFT1.Wave1.replayCertificate"]
    n31_bank_admission_symbol: Literal["LeanFaith.SFT1.Wave1.admittedN31BankIdentitiesV0_3_4"]
    imports_shared_transform_engine: Literal[False]
    owns_renderer_or_universe_canonicalizer: Literal[False]


class BoundYamlArtifact(StrictModel):
    path: NonEmptyStr
    file_sha256: Sha256
    semantic_hash: Sha256


class AuthoredArtifacts(StrictModel):
    lean_source: LeanSourceArtifact
    operation_bank: BoundYamlArtifact
    fixture_spec: BoundYamlArtifact


class ReadinessHashContracts(StrictModel):
    hash_algorithm: Literal["sha256_canonical_json_v1"]
    operation_bank_entry_hash_fields: tuple[Literal["operation_bank_entry"], ...]
    fixture_aggregate_hash_fields: tuple[
        Literal[
            "operation_id",
            "referenced_templates_success_then_adversarial_rejection",
            "project_contexts_in_registered_order",
            "fixtures_sorted_by_project_then_kind",
        ],
        ...,
    ]
    dispatch_binding_hash_fields: tuple[
        Literal[
            "operation_id",
            "lean_source_file_sha256",
            "operation_constructor",
            "dispatch_symbol",
            "bank_semantic_hash",
        ],
        ...,
    ]
    checker_binding_hash_fields: tuple[
        Literal[
            "operation_id",
            "lean_source_file_sha256",
            "checker_symbol",
            "registry_entry_hash",
            "anchor_hash",
            "operation_bank_entry_hash",
        ],
        ...,
    ]
    anchor_binding_hash_fields: tuple[
        Literal["operation_id", "anchor_ref", "anchor_hash", "registry_entry_hash"],
        ...,
    ]
    authored_bundle_hash_fields: tuple[
        Literal[
            "operation_id",
            "mechanism_id",
            "evidence_class",
            "registry_entry_hash",
            "lean_source_file_sha256",
            "dispatch_binding_hash",
            "checker_binding_hash",
            "anchor_binding_hash",
            "operation_bank_entry_hash",
            "fixture_aggregate_hash",
            "cache_contract_semantic_hash",
            "execution_contract_semantic_hash",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def _exact_field_bases(self) -> ReadinessHashContracts:
        exact = {
            "operation_bank_entry_hash_fields": ("operation_bank_entry",),
            "fixture_aggregate_hash_fields": (
                "operation_id",
                "referenced_templates_success_then_adversarial_rejection",
                "project_contexts_in_registered_order",
                "fixtures_sorted_by_project_then_kind",
            ),
            "dispatch_binding_hash_fields": (
                "operation_id",
                "lean_source_file_sha256",
                "operation_constructor",
                "dispatch_symbol",
                "bank_semantic_hash",
            ),
            "checker_binding_hash_fields": (
                "operation_id",
                "lean_source_file_sha256",
                "checker_symbol",
                "registry_entry_hash",
                "anchor_hash",
                "operation_bank_entry_hash",
            ),
            "anchor_binding_hash_fields": (
                "operation_id",
                "anchor_ref",
                "anchor_hash",
                "registry_entry_hash",
            ),
            "authored_bundle_hash_fields": (
                "operation_id",
                "mechanism_id",
                "evidence_class",
                "registry_entry_hash",
                "lean_source_file_sha256",
                "dispatch_binding_hash",
                "checker_binding_hash",
                "anchor_binding_hash",
                "operation_bank_entry_hash",
                "fixture_aggregate_hash",
                "cache_contract_semantic_hash",
                "execution_contract_semantic_hash",
            ),
        }
        for field_name, expected in exact.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"readiness hash field basis drift: {field_name}")
        return self


class PrimaryImplementationBundle(StrictModel):
    operation_id: OperationId
    mechanism_id: SymbolicId
    evidence_class: Literal["P-DEF", "P-SCHEMA", "N-RUBRIC"]
    registry_entry_hash: Sha256
    operation_constructor: LeanName
    dispatch_symbol: LeanName
    checker_symbol: LeanName
    anchor_ref: NonEmptyStr
    anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    fixture_aggregate_hash: Sha256
    dispatch_binding_hash: Sha256
    checker_binding_hash: Sha256
    anchor_binding_hash: Sha256
    authored_bundle_hash: Sha256
    static_source_authored: Literal[True]
    lean_compile_verified: Literal[False]
    live_success_verified: Literal[False]
    live_adversarial_rejection_verified: Literal[False]
    implementation_ready: Literal[False]
    gate_execution_may_start: Literal[False]
    production_admitted: Literal[False]
    row_emission_authorized: Literal[False]


class NotRunVerification(StrictModel):
    status: Literal["not_run_unauthorized"]
    verified: Literal[False]
    receipt_sha256: None


class ImplementationVerificationState(StrictModel):
    static_primary_bundle_count: Literal[5]
    static_primary_bundles_hash_bound: Literal[True]
    lean_compile: NotRunVerification
    live_success_fixtures: NotRunVerification
    live_adversarial_rejections: NotRunVerification
    certificate_replay: NotRunVerification
    implementation_ready: Literal[False]
    gate_execution_may_start: Literal[False]
    remaining_implementation_blockers: tuple[SymbolicId, ...]
    remaining_pre_smoke_nonimplementation_blockers: tuple[SymbolicId, ...]

    @model_validator(mode="after")
    def _exact_blockers(self) -> ImplementationVerificationState:
        if self.remaining_implementation_blockers != (
            "p01_alpha_closed_expr_hash_collision",
            "n31_guard_target_and_contradiction_bank_live_resolution",
            "n31_runtime_bank_identity_admission_requires_separate_user_authorization",
            "n31_in_process_exact_full_typed_bank_hash_verifier_binding",
            "lean_source_compile_and_symbol_resolution",
            "live_success_and_adversarial_fixture_replay",
            "certificate_checker_live_replay",
            "persistent_meta_request_and_same_request_repr_adapter",
            "central_persistent_cache_adapter_binding_and_replay",
        ):
            raise ValueError("remaining implementation blocker inventory drift")
        if self.remaining_pre_smoke_nonimplementation_blockers != (
            "coordinator_shared_label_contract_update",
            "positive_smoke_root_specific_micro_census",
            "n31_rubric_smoke_root_specific_micro_census",
        ):
            raise ValueError("remaining nonimplementation blocker inventory drift")
        return self


class ReadinessP01IdentityBlocker(StrictModel):
    blocker_id: Literal["p01_alpha_closed_expr_hash_collision"]
    operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    status: Literal["open_fail_closed"]
    source: Literal["frozen_attempt_009"]
    reference_and_candidate_closed_expr_hash_equal: Literal[True]
    reference_and_candidate_render_hash_distinct: Literal[True]
    frozen_repeated_closed_expr_hash_rejection_still_applies: Literal[True]
    additive_binder_aware_fingerprint_authored: Literal[True]
    additive_fingerprint_overrides_frozen_rule: Literal[False]
    blocks_operation_implementation_readiness: Literal[True]


class N31TargetBankState(StrictModel):
    operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    bank_id: Literal["sft1_n31_guard_target_candidates_v0_3_4"]
    bank_semantic_hash: Sha256
    operation_bank_artifact_semantic_hash: Sha256
    selectable_guard_target_candidate_entries_authored: Literal[True]
    retained_contradiction_candidate_entries_authored: Literal[True]
    bank_semantic_hash_binds_both_inventories: Literal[True]
    operation_bank_semantic_hash_binds_n31_runtime_contracts: Literal[True]
    source_admission_symbol: Literal["LeanFaith.SFT1.Wave1.admittedN31BankIdentitiesV0_3_4"]
    runtime_admitted_resolved_bank_identity_count: Literal[0]
    runtime_admitted_resolved_bank_identities: tuple[()]
    empty_admission_blocks_dispatch_and_discovery: Literal[True]
    current_runtime_activation_authorized: Literal[False]
    future_activation_requires_separate_user_authorization: Literal[True]
    future_identity_pin_field_order: tuple[
        Literal["projectId"],
        Literal["bankId"],
        Literal["resolvedLeanHash"],
        Literal["resolutionReceiptHash"],
    ]
    in_process_exact_full_typed_bank_hash_verification_required: Literal[True]
    in_process_exact_full_typed_bank_hash_verifier_bound: Literal[False]
    identity_membership_is_not_a_substitute_for_bank_hash_verification: Literal[True]
    certificate_binds_full_typed_bank: Literal[True]
    certificate_binds_full_reachability_assignment: Literal[True]
    replay_context_mismatch_reason: Literal["replayContextMismatch"]
    selectable_guard_and_target_role_instance_indices_disjoint: Literal[True]
    selectable_guard_and_target_instance_or_type_constraints_nonempty: Literal[True]
    retained_contradiction_role_instance_paths_disjoint: Literal[True]
    retained_contradiction_instance_or_type_constraints_nonempty: Literal[True]
    live_selectable_and_contradiction_name_arity_role_instance_resolution_complete: Literal[False]
    unknown_or_ambiguous_disposition: Literal["typed_not_applicable"]
    unknown_or_ambiguous_retained_prop_reason: Literal["n31RetainedContextUnknownOrAmbiguous"]
    blocks_n31_implementation_readiness: Literal[True]


class OptionalN31ProofAdapter(StrictModel):
    operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    parent_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    registry_entry_hash: Literal["b3ba4acd04c3adb7eff83e8a7b242a7c953f5d2b87ec659e9bbc9abafe81199a"]
    status: Literal["optional_not_implemented"]
    implementation_source_path: None
    checker_symbol: None
    required_for_primary_implementation_readiness: Literal[False]
    blocks_parent: Literal[False]
    blocks_wave: Literal[False]
    blocks_smoke: Literal[False]
    blocks_conformance: Literal[False]
    blocks_approximately_100_root_gate: Literal[False]
    optional_per_project: Literal[True]
    optional_per_root: Literal[True]
    unavailable_disposition: Literal["not_in_scope_for_n_proof"]
    candidate_truth_when_unavailable: Literal["unknown"]
    parent_candidate_and_root_pool_reuse_required: Literal[True]
    sidecar_upgrade_only: Literal[True]
    proof_upgrade_may_emit_additional_pair: Literal[False]
    counts_as_sixth_semantic_mechanism: Literal[False]
    counts_against_own_and_parent_caps: Literal[True]


class CacheReadiness(StrictModel):
    base_cache_contract_semantic_hash: Literal[
        "1ffb866d02376fbce8161fdfeaf75fd5d13770f36e7a2526e6d107f2b7091d43"
    ]
    base_execution_contract_semantic_hash: Literal[
        "ab98caf36f359d2a2599e166bb0e13cd07c2fe03b1392200b8348c78912412fa"
    ]
    exact_ordered_key_fields: tuple[NonEmptyStr, ...]
    hash_algorithm: Literal["sha256_canonical_json_v1"]
    hashing_must_occur_in_persistent_process_when_executed: Literal[True]
    cache_successes: Literal[True]
    cache_deterministic_terminal_failures: Literal[True]
    task_owned_key_builder_symbol: Literal["leanfaith.sft1.wave1_readiness.compute_wave1_cache_key"]
    central_cache_store_adapter_bound: Literal[False]
    central_cache_store_invoked: Literal[False]
    cache_replay_executed: Literal[False]

    @model_validator(mode="after")
    def _exact_cache_fields(self) -> CacheReadiness:
        if self.exact_ordered_key_fields != EXPECTED_CACHE_KEY_FIELDS:
            raise ValueError("Wave 1 cache key fields/order drift")
        return self


class ReprIntegrationContract(StrictModel):
    route: Literal["closed_expr_in_session"]
    endpoint_emitter: Literal["LeanFaith.GoalV1.emitClosedProp"]
    exact_emitter_calls_per_pair: Literal[2]
    reference_and_candidate_alive_in_same_persistent_meta_request: Literal[True]
    sft1_renderer_implementation_forbidden: Literal[True]
    sft1_universe_canonicalization_forbidden: Literal[True]
    candidate_declaration_or_proof_for_rendering_forbidden: Literal[True]
    pretty_print_or_text_reelaboration_for_serialization_forbidden: Literal[True]
    goal_v1_text_compilation_forbidden: Literal[True]
    universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    universe_profile_hash: Literal[
        "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
    ]
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Literal["5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"]
    renderer_api_hash: Literal["c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"]
    repr_spec_hash: Literal["68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"]


class ImplementationProhibitions(StrictModel):
    lean_project_compilation_or_meta_execution_started: Literal[False]
    transform_execution_started: Literal[False]
    gate_execution_started: Literal[False]
    census_processing_started: Literal[False]
    rows_generated_or_emitted: Literal[False]
    wave2_implementation_started: Literal[False]
    production_admitted_operation_count: Literal[0]
    production_admitted_negative_count: Literal[0]
    ten_k_authorized: Literal[False]
    scale_authorized: Literal[False]
    publication_authorized: Literal[False]
    shared_contract_modified: Literal[False]


class BoundaryIncident(StrictModel):
    incident_id: Literal["unauthorized_read_only_lean_print_prefix_v1"]
    incident_date: Literal["2026-08-30"]
    command: Literal["lean --print-prefix"]
    authorization_status: Literal["outside_authorized_scope_recorded_for_audit"]
    lean_executable_invoked: Literal[True]
    project_loaded: Literal[False]
    project_imported: Literal[False]
    project_compiled: Literal[False]
    meta_execution_started: Literal[False]
    transform_execution_started: Literal[False]
    gate_execution_started: Literal[False]
    row_generation_or_emission_started: Literal[False]
    files_or_artifacts_produced: Literal[False]


class Wave1ImplementationReadiness(StrictModel):
    schema_version: Literal[1]
    readiness_id: Literal["sft1_wave1_implementation_readiness_v0_3_4"]
    readiness_version: Literal["0.3.4"]
    status: Literal["static_authored_hash_bound_lean_and_gates_prohibited"]
    parent_freeze: ParentFreezeBinding
    user_authorization: ImplementationAuthorizationBinding
    authorization_scope: ImplementationAuthorizationScope
    authored_artifacts: AuthoredArtifacts
    hash_contracts: ReadinessHashContracts
    primary_bundles: tuple[PrimaryImplementationBundle, ...]
    verification_state: ImplementationVerificationState
    p01_identity_blocker: ReadinessP01IdentityBlocker
    n31_target_bank_state: N31TargetBankState
    optional_n31_proof_adapter: OptionalN31ProofAdapter
    cache_readiness: CacheReadiness
    repr_integration_contract: ReprIntegrationContract
    prohibitions: ImplementationProhibitions
    boundary_incidents: tuple[BoundaryIncident, ...] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def _exact_primary_bundle_projection(self) -> Wave1ImplementationReadiness:
        observed = tuple(
            (
                bundle.operation_id,
                bundle.mechanism_id,
                bundle.evidence_class,
                bundle.registry_entry_hash,
                (bundle.anchor_ref, bundle.anchor_hash),
            )
            for bundle in self.primary_bundles
        )
        expected = tuple(
            zip(
                EXPECTED_PRIMARY_OPERATION_IDS,
                EXPECTED_MECHANISM_IDS,
                EXPECTED_EVIDENCE_CLASSES,
                EXPECTED_REGISTRY_ENTRY_HASHES,
                EXPECTED_ANCHORS,
                strict=True,
            )
        )
        if observed != expected:
            raise ValueError("primary implementation bundle registry projection drift")
        expected_constructors = (
            "LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle",
            "LeanFaith.SFT1.Wave1.PrimaryOperation.p15SwapIffSides",
            "LeanFaith.SFT1.Wave1.PrimaryOperation.p18SymmetrizeEquality",
            "LeanFaith.SFT1.Wave1.PrimaryOperation.p21BetaReduce",
            "LeanFaith.SFT1.Wave1.PrimaryOperation.n31DropRequiredGuardRubric",
        )
        if tuple(bundle.operation_constructor for bundle in self.primary_bundles) != (
            expected_constructors
        ):
            raise ValueError("primary operation constructor inventory/order drift")
        if any(
            bundle.dispatch_symbol != "LeanFaith.SFT1.Wave1.dispatchAt"
            or bundle.checker_symbol != "LeanFaith.SFT1.Wave1.replayCertificate"
            for bundle in self.primary_bundles
        ):
            raise ValueError("primary dispatch/checker symbol drift")
        return self


class Wave1CacheKey(StrictModel):
    """Exact pure SFT1 cache-key preimage; constructing it performs no I/O."""

    source_closed_expr_hash: Sha256
    candidate_closed_expr_hash: Sha256
    canonical_universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    canonical_universe_profile_hash: Literal[
        "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
    ]
    source_expr_builder_version: NonEmptyStr
    candidate_expr_builder_version: NonEmptyStr
    lean_version: NonEmptyStr
    project_id: ProjectId
    project_revision: NonEmptyStr
    toolchain_revision: NonEmptyStr
    imports_hash: Sha256
    options_hash: Sha256
    synthesized_instance_hashes: tuple[Sha256, ...]
    operation_id: OperationId
    operation_registry_entry_hash: Sha256
    schema_lemma_procedure_hash: Sha256
    evidence_certificate_payload_hash: Sha256
    bank_resolved_lean_hash: Sha256
    transparency: NonEmptyStr
    allowed_axiom_profile: NonEmptyStr
    typed_meta_validator_version: NonEmptyStr
    evidence_replay_version: NonEmptyStr
    evaluation_blocklist_sha256: Literal[
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
    ]
    repr_replacement_commit: Literal["176a783842c5a73b84413dfa8347670608b615d9"]
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Literal["5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"]
    renderer_api_hash: Literal["c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"]
    repr_spec_hash: Literal["68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"]
    environment_fingerprint_hash: Sha256
    policy_config_hash: Sha256


def compute_wave1_cache_key_hash(key: Wave1CacheKey) -> str:
    """Return the canonical 30-field key hash without invoking Lean or storage."""

    if tuple(type(key).model_fields) != EXPECTED_CACHE_KEY_FIELDS:
        raise Wave1ReadinessError("Wave 1 cache-key field order differs from frozen policy")
    return hash_canonical(key.model_dump(mode="json"))


def compute_wave1_cache_key(key: Wave1CacheKey) -> str:
    """Policy-bound public name for the pure canonical cache-key hash."""

    return compute_wave1_cache_key_hash(key)


def compute_dispatch_binding_hash(
    bundle: PrimaryImplementationBundle,
    *,
    lean_source_file_sha256: str,
    bank_semantic_hash: str,
) -> str:
    """Compute the exact static dispatch-symbol binding hash."""

    return hash_canonical(
        {
            "operation_id": bundle.operation_id,
            "lean_source_file_sha256": lean_source_file_sha256,
            "operation_constructor": bundle.operation_constructor,
            "dispatch_symbol": bundle.dispatch_symbol,
            "bank_semantic_hash": bank_semantic_hash,
        }
    )


def compute_checker_binding_hash(
    bundle: PrimaryImplementationBundle,
    *,
    lean_source_file_sha256: str,
) -> str:
    """Compute the exact authored checker-symbol binding hash."""

    return hash_canonical(
        {
            "operation_id": bundle.operation_id,
            "lean_source_file_sha256": lean_source_file_sha256,
            "checker_symbol": bundle.checker_symbol,
            "registry_entry_hash": bundle.registry_entry_hash,
            "anchor_hash": bundle.anchor_hash,
            "operation_bank_entry_hash": bundle.operation_bank_entry_hash,
        }
    )


def compute_anchor_binding_hash(bundle: PrimaryImplementationBundle) -> str:
    """Bind the operation to its immutable design anchor without claiming compilation."""

    return hash_canonical(
        {
            "operation_id": bundle.operation_id,
            "anchor_ref": bundle.anchor_ref,
            "anchor_hash": bundle.anchor_hash,
            "registry_entry_hash": bundle.registry_entry_hash,
        }
    )


def compute_authored_bundle_hash(
    bundle: PrimaryImplementationBundle,
    *,
    lean_source_file_sha256: str,
    dispatch_binding_hash: str,
    checker_binding_hash: str,
    anchor_binding_hash: str,
) -> str:
    """Hash authored static components without asserting Lean/live readiness."""

    return hash_canonical(
        {
            "operation_id": bundle.operation_id,
            "mechanism_id": bundle.mechanism_id,
            "evidence_class": bundle.evidence_class,
            "registry_entry_hash": bundle.registry_entry_hash,
            "lean_source_file_sha256": lean_source_file_sha256,
            "dispatch_binding_hash": dispatch_binding_hash,
            "checker_binding_hash": checker_binding_hash,
            "anchor_binding_hash": anchor_binding_hash,
            "operation_bank_entry_hash": bundle.operation_bank_entry_hash,
            "fixture_aggregate_hash": bundle.fixture_aggregate_hash,
            "cache_contract_semantic_hash": EXPECTED_CACHE_CONTRACT_HASH,
            "execution_contract_semantic_hash": EXPECTED_EXECUTION_CONTRACT_HASH,
        }
    )


def import_stripped_preamble(source: str) -> str:
    """Apply the exact inherited import-line stripping policy."""

    lines = [line for line in source.splitlines() if not line.lstrip().startswith("import ")]
    return "\n".join(lines).strip()


def _repo_path(root: Path, relative: str | Path) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise Wave1ReadinessError(f"Wave 1 readiness path escapes repository: {relative}")
    return path


def _operation_map(parent: LoadedEffectiveWaveState) -> dict[str, OperationSpec]:
    policy = parent.loaded_admission.loaded_base_policy.config
    return {
        operation.operation_id: operation
        for operation in (*policy.operations, *policy.synthetic_track.operations)
    }


def validate_operation_banks(
    banks: Wave1OperationBanks,
    parent: LoadedEffectiveWaveState,
) -> None:
    """Replay the authored bank against the immutable 46-operation registry."""

    operations = _operation_map(parent)
    if len(operations) != 46:
        raise Wave1ReadinessError("the frozen registry must retain exactly 46 operations")
    for bank_entry, expected_hash in zip(
        banks.operation_banks, EXPECTED_REGISTRY_ENTRY_HASHES, strict=True
    ):
        operation = operations.get(bank_entry.operation_id)
        if operation is None:
            raise Wave1ReadinessError(f"missing primary operation: {bank_entry.operation_id}")
        observed_hash = hash_canonical(operation.model_dump(mode="json"))
        if observed_hash != expected_hash or bank_entry.registry_entry_hash != observed_hash:
            raise Wave1ReadinessError(f"registry entry hash drift: {bank_entry.operation_id}")
        if (
            operation.anchor.ref != bank_entry.anchor_ref
            or operation.anchor.schema_lemma_procedure_hash != bank_entry.anchor_hash
            or operation.mechanism_superclass is None
            or operation.evidence_class.value != bank_entry.evidence_class
            or tuple(operation.eligible_projects) != EXPECTED_PROJECT_IDS
        ):
            raise Wave1ReadinessError(f"registry/bank contract drift: {bank_entry.operation_id}")

    proof = operations.get(EXPECTED_OPTIONAL_PROOF_OPERATION_ID)
    if (
        proof is None
        or hash_canonical(proof.model_dump(mode="json"))
        != EXPECTED_OPTIONAL_PROOF_REGISTRY_ENTRY_HASH
        or proof.n_proof_subtype_of != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    ):
        raise Wave1ReadinessError("optional N31 N-PROOF parent/registry binding drift")

    n31 = load_n31_guard_bank(parent.path.parents[3])
    if n31.config_hash != "82bca9b16861412ebaf296591944338932e51f6aaaf8372baa4fd4c1f097f9e1":
        raise Wave1ReadinessError("frozen N31 guard design semantic hash drift")


@dataclass(frozen=True, slots=True)
class LoadedWave1OperationBanks:
    loaded: LoadedConfig[Wave1OperationBanks]
    file_sha256: str
    parent: LoadedEffectiveWaveState

    @property
    def config(self) -> Wave1OperationBanks:
        return self.loaded.config

    @property
    def config_hash(self) -> str:
        return self.loaded.config_hash

    @property
    def path(self) -> Path:
        return self.loaded.path


def load_wave1_operation_banks(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedWave1OperationBanks:
    """Load the static operation bank without importing or starting Lean."""

    root = find_repo_root(repo_root)
    expected_path = (root / DEFAULT_OPERATION_BANK_PATH).resolve()
    resolved = (path or expected_path).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved != expected_path:
        raise Wave1ReadinessError("operation-bank path differs from frozen task-owned path")
    observed_file_hash = hash_file(resolved)
    if observed_file_hash != EXPECTED_OPERATION_BANK_FILE_SHA256:
        raise Wave1ReadinessError("operation-bank raw-file hash drift")
    loaded = load_config(resolved, Wave1OperationBanks)
    if loaded.config_hash != EXPECTED_OPERATION_BANK_SEMANTIC_HASH:
        raise Wave1ReadinessError("operation-bank semantic hash drift")
    parent = load_effective_wave_state(root)
    validate_operation_banks(loaded.config, parent)
    return LoadedWave1OperationBanks(
        loaded=loaded,
        file_sha256=observed_file_hash,
        parent=parent,
    )


def validate_fixture_set(
    fixtures: Wave1FixtureSet,
    parent: LoadedEffectiveWaveState,
) -> None:
    """Cross-check static fixture contexts against the frozen source projection."""

    parent_sources = {
        source.source_id: source for source in parent.config.source_authorization.sources
    }
    if tuple(parent_sources) != EXPECTED_PROJECT_IDS:
        raise Wave1ReadinessError("parent source-authorization inventory drift")
    for context in fixtures.project_contexts:
        source = parent_sources[context.project_id]
        if context.source_revision != source.revision:
            raise Wave1ReadinessError(f"fixture source revision drift: {context.project_id}")
    if any(
        item.operation_id == EXPECTED_OPTIONAL_PROOF_OPERATION_ID for item in fixtures.templates
    ) or any(
        item.operation_id == EXPECTED_OPTIONAL_PROOF_OPERATION_ID
        for item in fixtures.fixture_matrix
    ):
        raise Wave1ReadinessError("optional N31 N-PROOF must contribute zero fixtures")


@dataclass(frozen=True, slots=True)
class LoadedWave1FixtureSet:
    loaded: LoadedConfig[Wave1FixtureSet]
    file_sha256: str
    parent: LoadedEffectiveWaveState

    @property
    def config(self) -> Wave1FixtureSet:
        return self.loaded.config

    @property
    def config_hash(self) -> str:
        return self.loaded.config_hash

    @property
    def path(self) -> Path:
        return self.loaded.path


def load_wave1_fixture_set(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedWave1FixtureSet:
    """Load the uncompiled 5x4x2 fixture specification without executing it."""

    root = find_repo_root(repo_root)
    expected_path = (root / DEFAULT_FIXTURE_PATH).resolve()
    resolved = (path or expected_path).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved != expected_path:
        raise Wave1ReadinessError("fixture path differs from frozen task-owned path")
    observed_file_hash = hash_file(resolved)
    if observed_file_hash != EXPECTED_FIXTURE_FILE_SHA256:
        raise Wave1ReadinessError("fixture raw-file hash drift")
    loaded = load_config(resolved, Wave1FixtureSet)
    if loaded.config_hash != EXPECTED_FIXTURE_SEMANTIC_HASH:
        raise Wave1ReadinessError("fixture semantic hash drift")
    parent = load_effective_wave_state(root)
    validate_fixture_set(loaded.config, parent)
    return LoadedWave1FixtureSet(
        loaded=loaded,
        file_sha256=observed_file_hash,
        parent=parent,
    )


def _verify_parent_freeze(
    root: Path,
    config: Wave1ImplementationReadiness,
    parent: LoadedEffectiveWaveState,
) -> None:
    if (
        config.parent_freeze.commit != EXPECTED_PARENT_COMMIT
        or config.parent_freeze.tree != EXPECTED_PARENT_TREE
        or parent.config_file_sha256 != EXPECTED_PARENT_EFFECTIVE_FILE_SHA256
        or parent.config_hash != EXPECTED_PARENT_EFFECTIVE_SEMANTIC_HASH
    ):
        raise Wave1ReadinessError("immutable revision-0.3.3 parent binding drift")
    if hash_file(_repo_path(root, config.parent_freeze.effective_loader_path)) != (
        EXPECTED_PARENT_LOADER_FILE_SHA256
    ):
        raise Wave1ReadinessError("immutable revision-0.3.3 loader raw-file drift")
    if parent.config.effective_wave1.implementation_ready:
        raise Wave1ReadinessError("parent revision unexpectedly claims implementation readiness")
    admission = parent.loaded_admission.config
    admitted_ids = tuple(item.operation_id for item in admission.approved_operations)
    if admitted_ids != (
        *EXPECTED_PRIMARY_OPERATION_IDS,
        EXPECTED_OPTIONAL_PROOF_OPERATION_ID,
    ):
        raise Wave1ReadinessError("inherited Wave 1 gate-admission inventory drift")
    if not all(item.gate_admitted for item in admission.approved_operations):
        raise Wave1ReadinessError("every inherited Wave 1 operation must remain gate-admitted")
    if any(item.production_admitted for item in admission.approved_operations):
        raise Wave1ReadinessError("no inherited Wave 1 operation is production-admitted")


def validate_authored_source_text(source: str) -> None:
    """Fail closed on forbidden execution, declaration, proof, and rendering APIs.

    This is deliberately a pure textual readiness check, not a claim that the
    Lean source parses or compiles.  Live Lean verification remains false and
    unauthorized in revision 0.3.4.
    """

    forbidden_patterns = (
        (
            "executable/declaration probe",
            r"(?m)^\s*(?:#eval|#check|run_meta|theorem|axiom|example)\b",
        ),
        (
            "dynamic declaration API",
            r"\b(?:addDecl|addAndCompile|mkAxiom|mkTheorem)\b|"
            r"\.(?:axiomDecl|thmDecl|opaqueDecl)\b",
        ),
        ("sorry construction", r"\b(?:sorry|mkSorry)\b"),
        (
            "candidate proof synthesis",
            r"\b(?:synthInstance|synthesizeSyntheticMVarsNoPostponing|runTactic)\b",
        ),
        (
            "REPR renderer ownership",
            r"\b(?:emitClosedProp|renderClosedProp)\b",
        ),
        (
            "surface pretty-printing",
            r"\b(?:ppExpr|delaborate|prettyPrint)\b",
        ),
        (
            "text parsing or re-elaboration",
            r"\b(?:elabTerm|elabType|runParserCategory)\b",
        ),
    )
    for failure_class, pattern in forbidden_patterns:
        if re.search(pattern, source):
            raise Wave1ReadinessError(f"authored Wave 1 source contains forbidden {failure_class}")
    if "import LeanFaith.Meta.TransformEngine" in source:
        raise Wave1ReadinessError("authored Wave 1 source imports the frozen shared engine")

    required_n31_contract_patterns = (
        (
            "exact empty N31 runtime admission",
            r"(?m)^def admittedN31BankIdentitiesV0_3_4 : "
            r"Array N31BankIdentity := #\[\]$",
        ),
        (
            "full typed N31 bank certificate binding",
            r"(?s)structure N31RubricCertificate where.*?"
            r"\n  bank : N31TargetBank.*?\n  reachability : N31ReachabilityEvidence",
        ),
        (
            "exact N31 replay-context equality",
            r"bank == value\.bank && reachability == value\.reachability",
        ),
        (
            "N31 replay-context rejection",
            r"if !certificate\.contextMatches context then",
        ),
    )
    for failure_class, pattern in required_n31_contract_patterns:
        if re.search(pattern, source) is None:
            raise Wave1ReadinessError(f"authored Wave 1 source lacks {failure_class}")

    admission_membership = "admittedN31BankIdentitiesV0_3_4.contains bank.identity"
    if source.count(admission_membership) != 2:
        raise Wave1ReadinessError(
            "authored Wave 1 source must guard N31 validation and discovery exactly once each"
        )


def _verify_authored_source(
    root: Path,
    config: Wave1ImplementationReadiness,
) -> str:
    binding = config.authored_artifacts.lean_source
    source_path = _repo_path(root, binding.path)
    observed_hash = hash_file(source_path)
    if observed_hash != binding.file_sha256 or observed_hash == "0" * 64:
        raise Wave1ReadinessError("authored Wave 1 Lean source raw-file hash drift")
    source = source_path.read_text(encoding="utf-8")
    preamble_hash = sha256_hex(import_stripped_preamble(source).encode("utf-8"))
    if preamble_hash != binding.import_stripped_preamble_sha256 or preamble_hash == "0" * 64:
        raise Wave1ReadinessError("authored Wave 1 import-stripped preamble hash drift")
    validate_authored_source_text(source)
    required_tokens = (
        binding.source_version_value,
        *(
            symbol.rsplit(".", maxsplit=1)[-1]
            for symbol in (
                binding.source_version_symbol,
                binding.public_discover_symbol,
                binding.public_dispatch_symbol,
                binding.public_checker_symbol,
                binding.n31_bank_admission_symbol,
            )
        ),
        *(
            bundle.operation_constructor.rsplit(".", maxsplit=1)[-1]
            for bundle in config.primary_bundles
        ),
    )
    for token in required_tokens:
        if re.search(rf"\b{re.escape(token)}\b", source) is None:
            raise Wave1ReadinessError(f"authored Wave 1 source symbol/token missing: {token}")
    return observed_hash


def _validate_artifact_bindings(
    root: Path,
    config: Wave1ImplementationReadiness,
    banks: LoadedWave1OperationBanks,
    fixtures: LoadedWave1FixtureSet,
) -> None:
    operation_bank = config.authored_artifacts.operation_bank
    fixture_spec = config.authored_artifacts.fixture_spec
    if (
        operation_bank.path != DEFAULT_OPERATION_BANK_PATH.as_posix()
        or operation_bank.file_sha256 != banks.file_sha256
        or operation_bank.semantic_hash != banks.config_hash
    ):
        raise Wave1ReadinessError("main readiness/operation-bank artifact binding drift")
    if (
        fixture_spec.path != DEFAULT_FIXTURE_PATH.as_posix()
        or fixture_spec.file_sha256 != fixtures.file_sha256
        or fixture_spec.semantic_hash != fixtures.config_hash
    ):
        raise Wave1ReadinessError("main readiness/fixture artifact binding drift")
    if hash_file(_repo_path(root, operation_bank.path)) != operation_bank.file_sha256:
        raise Wave1ReadinessError("operation-bank bytes changed after typed loading")
    if hash_file(_repo_path(root, fixture_spec.path)) != fixture_spec.file_sha256:
        raise Wave1ReadinessError("fixture bytes changed after typed loading")


def _validate_primary_bundle_hashes(
    config: Wave1ImplementationReadiness,
    banks: Wave1OperationBanks,
    fixtures: Wave1FixtureSet,
    *,
    source_file_sha256: str,
    bank_semantic_hash: str,
) -> None:
    for bundle in config.primary_bundles:
        expected_bank_hash = operation_bank_entry_hash(banks, bundle.operation_id)
        expected_fixture_hash = fixture_operation_bundle_hash(fixtures, bundle.operation_id)
        if bundle.operation_bank_entry_hash != expected_bank_hash:
            raise Wave1ReadinessError(f"operation-bank entry hash drift: {bundle.operation_id}")
        if bundle.fixture_aggregate_hash != expected_fixture_hash:
            raise Wave1ReadinessError(f"fixture aggregate hash drift: {bundle.operation_id}")
        dispatch_hash = compute_dispatch_binding_hash(
            bundle,
            lean_source_file_sha256=source_file_sha256,
            bank_semantic_hash=bank_semantic_hash,
        )
        checker_hash = compute_checker_binding_hash(
            bundle,
            lean_source_file_sha256=source_file_sha256,
        )
        anchor_hash = compute_anchor_binding_hash(bundle)
        authored_hash = compute_authored_bundle_hash(
            bundle,
            lean_source_file_sha256=source_file_sha256,
            dispatch_binding_hash=dispatch_hash,
            checker_binding_hash=checker_hash,
            anchor_binding_hash=anchor_hash,
        )
        if bundle.dispatch_binding_hash != dispatch_hash:
            raise Wave1ReadinessError(f"dispatch binding hash drift: {bundle.operation_id}")
        if bundle.checker_binding_hash != checker_hash:
            raise Wave1ReadinessError(f"checker binding hash drift: {bundle.operation_id}")
        if bundle.anchor_binding_hash != anchor_hash:
            raise Wave1ReadinessError(f"anchor binding hash drift: {bundle.operation_id}")
        if bundle.authored_bundle_hash != authored_hash:
            raise Wave1ReadinessError(f"authored bundle hash drift: {bundle.operation_id}")


def validate_wave1_implementation_readiness(
    config: Wave1ImplementationReadiness,
    *,
    root: Path,
    parent: LoadedEffectiveWaveState,
    banks: LoadedWave1OperationBanks,
    fixtures: LoadedWave1FixtureSet,
) -> None:
    """Validate static authorship while retaining every execution blocker."""

    _verify_parent_freeze(root, config, parent)
    validate_operation_banks(banks.config, parent)
    validate_fixture_set(fixtures.config, parent)
    _validate_artifact_bindings(root, config, banks, fixtures)
    source_hash = _verify_authored_source(root, config)
    _validate_primary_bundle_hashes(
        config,
        banks.config,
        fixtures.config,
        source_file_sha256=source_hash,
        bank_semantic_hash=banks.config_hash,
    )
    target_bank_hash = hash_canonical(banks.config.n31_guard_target_bank.model_dump(mode="json"))
    if config.n31_target_bank_state.bank_semantic_hash != target_bank_hash:
        raise Wave1ReadinessError("N31 target-head candidate bank semantic hash drift")
    if config.n31_target_bank_state.operation_bank_artifact_semantic_hash != banks.config_hash:
        raise Wave1ReadinessError("N31 runtime-contract operation-bank hash drift")
    if (
        config.p01_identity_blocker.blocker_id != banks.config.p01_identity_blocker.blocker_id
        or not config.p01_identity_blocker.blocks_operation_implementation_readiness
    ):
        raise Wave1ReadinessError("P01 closed-Expr identity blocker drift")
    if (
        config.optional_n31_proof_adapter.operation_id
        != banks.config.optional_n31_proof_adapter.operation_id
        or config.optional_n31_proof_adapter.required_for_primary_implementation_readiness
        or config.optional_n31_proof_adapter.blocks_parent
        or config.optional_n31_proof_adapter.blocks_wave
    ):
        raise Wave1ReadinessError("optional N31 N-PROOF adapter became a blocker")
    policy = parent.loaded_admission.loaded_base_policy.config
    if (
        hash_canonical(policy.cache_contract.model_dump(mode="json"))
        != config.cache_readiness.base_cache_contract_semantic_hash
        or hash_canonical(policy.execution_contract.model_dump(mode="json"))
        != config.cache_readiness.base_execution_contract_semantic_hash
    ):
        raise Wave1ReadinessError("frozen cache/execution contract semantic hash drift")


@dataclass(frozen=True, slots=True)
class LoadedWave1ImplementationReadiness:
    loaded: LoadedConfig[Wave1ImplementationReadiness]
    file_sha256: str
    parent: LoadedEffectiveWaveState
    banks: LoadedWave1OperationBanks
    fixtures: LoadedWave1FixtureSet

    @property
    def config(self) -> Wave1ImplementationReadiness:
        return self.loaded.config

    @property
    def config_hash(self) -> str:
        return self.loaded.config_hash

    @property
    def path(self) -> Path:
        return self.loaded.path


def load_wave1_implementation_readiness(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedWave1ImplementationReadiness:
    """Load revision 0.3.4 without compiling Lean or executing any gate."""

    root = find_repo_root(repo_root)
    expected_path = (root / DEFAULT_IMPLEMENTATION_READINESS_PATH).resolve()
    resolved = (path or expected_path).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved != expected_path:
        raise Wave1ReadinessError("readiness path differs from frozen task-owned path")
    observed_file_hash = hash_file(resolved)
    if observed_file_hash != EXPECTED_IMPLEMENTATION_READINESS_FILE_SHA256:
        raise Wave1ReadinessError("implementation-readiness raw-file hash drift")
    loaded = load_config(resolved, Wave1ImplementationReadiness)
    if loaded.config_hash != EXPECTED_IMPLEMENTATION_READINESS_SEMANTIC_HASH:
        raise Wave1ReadinessError("implementation-readiness semantic hash drift")
    parent = load_effective_wave_state(root)
    banks = load_wave1_operation_banks(root)
    fixtures = load_wave1_fixture_set(root)
    validate_wave1_implementation_readiness(
        loaded.config,
        root=root,
        parent=parent,
        banks=banks,
        fixtures=fixtures,
    )
    return LoadedWave1ImplementationReadiness(
        loaded=loaded,
        file_sha256=observed_file_hash,
        parent=parent,
        banks=banks,
        fixtures=fixtures,
    )


def authorization_text_hash(text: str) -> str:
    """Pure helper used by the strict authorization binding validator."""

    return sha256_hex(text.encode("utf-8"))


# Concise compatibility names for focused policy/invariant tests.  They remain
# pure validation/load aliases and expose no execution surface.
load_wave1_readiness = load_wave1_implementation_readiness
validate_wave1_readiness = validate_wave1_implementation_readiness


__all__ = [
    "DEFAULT_FIXTURE_PATH",
    "DEFAULT_IMPLEMENTATION_READINESS_PATH",
    "DEFAULT_OPERATION_BANK_PATH",
    "EXPECTED_CACHE_CONTRACT_HASH",
    "EXPECTED_EXECUTION_CONTRACT_HASH",
    "EXPECTED_FIXTURE_FILE_SHA256",
    "EXPECTED_FIXTURE_SEMANTIC_HASH",
    "EXPECTED_IMPLEMENTATION_READINESS_FILE_SHA256",
    "EXPECTED_IMPLEMENTATION_READINESS_SEMANTIC_HASH",
    "EXPECTED_OPERATION_BANK_FILE_SHA256",
    "EXPECTED_OPERATION_BANK_SEMANTIC_HASH",
    "EXPECTED_PRIMARY_OPERATION_IDS",
    "EXPECTED_PROJECT_IDS",
    "EXPECTED_USER_AUTHORIZATION_SHA256",
    "AuthoredArtifacts",
    "BoundYamlArtifact",
    "BoundaryIncident",
    "CacheReadiness",
    "ExactSiteSelector",
    "FixtureMatrixContract",
    "FixtureMatrixEntry",
    "FixtureProjectContext",
    "FixtureTemplate",
    "FutureTelescopeAssignment",
    "GuardAndTargetSelector",
    "GuardTargetShapeEntry",
    "HeadRolePattern",
    "ImplementationAuthorizationBinding",
    "ImplementationAuthorizationScope",
    "ImplementationProhibitions",
    "ImplementationVerificationState",
    "LeanSourceArtifact",
    "LoadedWave1FixtureSet",
    "LoadedWave1ImplementationReadiness",
    "LoadedWave1OperationBanks",
    "N31CertificateBindingContract",
    "N31GuardTargetBank",
    "N31RuntimeAdmissionContract",
    "N31RuntimeStructuralConstraints",
    "N31TargetBankState",
    "NotRunVerification",
    "OperationBankEntry",
    "OperationDiscoveryOrders",
    "OptionalN31ProofAdapter",
    "OptionalProofAdapterBank",
    "OuterBinderSelector",
    "P01IdentityBlocker",
    "ParentFreezeBinding",
    "PrimaryImplementationBundle",
    "ReadinessHashContracts",
    "ReadinessP01IdentityBlocker",
    "ReprIntegrationContract",
    "RetainedContradictionPatternEntry",
    "SelectionContract",
    "SymbolicArgumentPath",
    "SymbolicLiteralConstraint",
    "SymbolicNestedHeadConstraint",
    "TargetMatchingSemantics",
    "Wave1CacheKey",
    "Wave1FixtureSet",
    "Wave1ImplementationReadiness",
    "Wave1OperationBanks",
    "Wave1ReadinessError",
    "authorization_text_hash",
    "compute_anchor_binding_hash",
    "compute_authored_bundle_hash",
    "compute_checker_binding_hash",
    "compute_dispatch_binding_hash",
    "compute_wave1_cache_key",
    "compute_wave1_cache_key_hash",
    "fixture_operation_bundle_hash",
    "import_stripped_preamble",
    "load_wave1_fixture_set",
    "load_wave1_implementation_readiness",
    "load_wave1_operation_banks",
    "load_wave1_readiness",
    "operation_bank_entry_hash",
    "validate_authored_source_text",
    "validate_fixture_set",
    "validate_operation_banks",
    "validate_wave1_implementation_readiness",
    "validate_wave1_readiness",
]

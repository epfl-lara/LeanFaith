"""Lean-free Wave 1 census implementation for additive SFT1 revision 0.3.6.

The census deliberately produces candidate inventories, not labels.  It never
imports or invokes Lean.  Large scans use one SQLite state file, an append-only
journal, and a terminal marker so they are deterministic and restartable.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, cast

from pydantic import Field, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.schemas.enums import EvidenceKind
from leanfaith.schemas.evidence import AuditValue, EvidenceRecord
from leanfaith.sft1.wave1_runtime import TypedCertificateReceipt

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
PairId = Annotated[str, Field(pattern=r"^pair:[0-9a-f]{64}$", strict=True)]
TheoremId = Annotated[str, Field(pattern=r"^thm:[0-9a-f]{64}$", strict=True)]
RepresentationId = Annotated[str, Field(pattern=r"^repr:[0-9a-f]{64}$", strict=True)]
ContextId = Annotated[str, Field(pattern=r"^ctx:[0-9a-f]{64}$", strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
EvidenceLevel = Literal["surface_prefilter", "preexisting_typed", "typed_pending", "typed"]
SourceId = Literal["compiler_data", "cslib", "mathlib", "physlib"]
Tier = Literal["smoke", "selected_wave", "full_cross_source"]
DomainStratum = Literal[
    "mixed_compiler_training_data",
    "computer_science",
    "general_formal_mathematics",
    "physics",
]
ClosedExprRoute = Literal[
    "compiler_data_elaborated_signature_in_persistent_project_session_v1",
    "imported_constant_info_type_in_persistent_project_session_v1",
]
PrimaryOperation = Literal[
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
]
ExclusionReason = Literal[
    "none",
    "private_declaration",
    "proof_placeholder",
    "golden_blocklist_near_duplicate",
    "golden_blocklist_problem",
]

DEFAULT_CONFIG_PATH = Path("configs/transformations/sft1_value_first_v1/wave1_census_v0_3_6.yaml")
GOLDEN_BLOCKLIST_PATH = Path("data/benchmarks/golden_blocklist_v1.json")
GOLDEN_BLOCKLIST_SHA256: Literal[
    "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
] = "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
GOLDEN_BLOCKLIST_PROCEDURE_ID: Literal[
    "golden_blocklist_v1_normalize_headless_signature_near_dup_hash_root_screen_v1"
] = "golden_blocklist_v1_normalize_headless_signature_near_dup_hash_root_screen_v1"
RUNTIME_CONFIG_PATH = Path("configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml")
RUNTIME_LOADER_PATH = Path("src/leanfaith/sft1/wave1_live_readiness.py")
EXPECTED_SOURCE_IDS: tuple[str, ...] = ("compiler_data", "cslib", "mathlib", "physlib")
PRIMARY_OPERATIONS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
)
OPTIONAL_PROOF_OPERATION = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
EXPECTED_SOURCE_USE_SHA256 = "62a4daca09ca669aef0133cd4d0b0913e1d7795558560f3aac4b289efc75e95c"
EXPECTED_EFFECTIVE_READINESS_SHA256 = (
    "5673d2ee2e3d9b088bcc42ccec4d4d851096b6c0fc8cc5349b1c3b231f2b1474"
)
EXPECTED_IMPLEMENTATION_BASE_COMMIT = "fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48"
_COMMAND_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:(?P<attribute>@\[)|"
    r"(?:(?:private|protected|noncomputable|unsafe|public|local)\s+)*"
    r"(?P<kind>theorem|lemma|def|abbrev|instance|structure|class|inductive|namespace|end|"
    r"section|open|export|variable|variables|axiom|opaque|constant|constants|example|"
    r"set_option|attribute|include|omit|universe|universes|import|prelude|notation|"
    r"infixl|infixr|infix|prefix|postfix|syntax|macro|macro_rules|elab|elab_rules|"
    r"declare_syntax_cat|initialize|builtin_initialize|register_option|deriving|mutual|"
    r"scoped|library_note)\b|(?P<hash_command>\#\w+))"
)
_DECL_TOKEN_RE = re.compile(
    r"(?:(?:private|protected|noncomputable|unsafe|public|local)\s+)*"
    r"(?P<kind>theorem|lemma)\s+(?P<name>(?:«[^»]+»|[^\s:({\[]+))"
)
_SPACE_RE = re.compile(r"\s+")
_PLACEHOLDER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_'])(?P<token>sorry|admit|by\?)(?![A-Za-z0-9_'])"
)
EXPECTED_SOURCE_FREEZE: dict[str, dict[str, object]] = {
    "compiler_data": {
        "repository": "formalmathatepfl/compiler_data",
        "revision": "ca37d4701b11022f183e72b7b96ff543a8a615d3",
        "checkout_or_artifact_path": (
            "/storage/milikic/leanfaith/value_first/cpt2_v1/source_cache/answer_data.parquet"
        ),
        "artifact_sha256": "c45145de5b681efd5aa265fdc90b9dc68d542321dd931d139cdead693360a81a",
        "repo_binding_path": "configs/data/cpt2/cpt2_v1.yaml",
        "repo_binding_sha256": "917fe1061cc1e2b96b12e6e42f47c64739df25172aacb5528a37e579449c00c6",
        "globs": (),
        "expected_toolchain": None,
        "root_module": None,
        "default_evidence_level": "preexisting_typed",
        "domain_stratum": "mixed_compiler_training_data",
        "closed_expr_route": "compiler_data_elaborated_signature_in_persistent_project_session_v1",
        "compile_context_bound": False,
        "closed_expr_route_bound": False,
    },
    "cslib": {
        "repository": "https://github.com/leanprover/cslib",
        "revision": "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
        "checkout_or_artifact_path": (
            "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/checkouts/cslib"
        ),
        "artifact_sha256": None,
        "repo_binding_path": "configs/projects/cslib.yaml",
        "repo_binding_sha256": "1cd35e901ca9bed56454ee534e4dc48838fbc9012cb7fbfe7901c4e7ea31e4d5",
        "globs": ("Cslib/**/*.lean",),
        "expected_toolchain": "v4.31.0-rc1",
        "root_module": "Cslib",
        "default_evidence_level": "typed_pending",
        "domain_stratum": "computer_science",
        "closed_expr_route": "imported_constant_info_type_in_persistent_project_session_v1",
        "compile_context_bound": True,
        "closed_expr_route_bound": True,
    },
    "mathlib": {
        "repository": "https://github.com/leanprover-community/mathlib4",
        "revision": "d568c8c09630de097a046763c17b9ea99f95f950",
        "checkout_or_artifact_path": "/storage/milikic/leanfaith/mathlib4",
        "artifact_sha256": None,
        "repo_binding_path": "configs/projects/mathlib.yaml",
        "repo_binding_sha256": "dfaccff566a513c17aad970c7204b1e86bc65a15ea2e234e749301b9be5cb940",
        "globs": ("Mathlib/**/*.lean",),
        "expected_toolchain": "v4.31.0-rc1",
        "root_module": "Mathlib",
        "default_evidence_level": "typed_pending",
        "domain_stratum": "general_formal_mathematics",
        "closed_expr_route": "imported_constant_info_type_in_persistent_project_session_v1",
        "compile_context_bound": True,
        "closed_expr_route_bound": True,
    },
    "physlib": {
        "repository": "https://github.com/leanprover-community/physlib",
        "revision": "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
        "checkout_or_artifact_path": (
            "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/checkouts/physlib"
        ),
        "artifact_sha256": None,
        "repo_binding_path": "configs/projects/physlib.yaml",
        "repo_binding_sha256": "feab69e9e39792068d398cf1b7f17d787b219b2bd819ffb3239e648ef228edc1",
        "globs": ("Physlib/**/*.lean",),
        "expected_toolchain": "v4.30.0",
        "root_module": "Physlib",
        "default_evidence_level": "typed_pending",
        "domain_stratum": "physics",
        "closed_expr_route": "imported_constant_info_type_in_persistent_project_session_v1",
        "compile_context_bound": True,
        "closed_expr_route_bound": True,
    },
}


class CensusError(ValueError):
    """Raised when census configuration, state, or evidence fails closed."""


class ImplementationBinding(StrictModel):
    implementation_base_commit: GitCommit
    implementation_source_path: Literal["src/leanfaith/sft1/wave1_census_v0_3_6.py"]
    implementation_source_sha256: Sha256
    runtime_git_commit_recorded_in_state: Literal[True]

    @model_validator(mode="after")
    def _exact_base(self) -> ImplementationBinding:
        if self.implementation_base_commit != EXPECTED_IMPLEMENTATION_BASE_COMMIT:
            raise ValueError("implementation base commit drift")
        return self


class PolicyBinding(StrictModel):
    source_use_path: Literal["policies/source_use_v2.yaml"]
    source_use_sha256: Sha256
    effective_readiness_path: Literal[
        "configs/transformations/sft1_value_first_v1/wave1_effective_readiness_v0_3_3.yaml"
    ]
    effective_readiness_sha256: Literal[
        "5673d2ee2e3d9b088bcc42ccec4d4d851096b6c0fc8cc5349b1c3b231f2b1474"
    ]


class EvaluationBlocklistBinding(StrictModel):
    path: Literal["data/benchmarks/golden_blocklist_v1.json"]
    file_sha256: Literal["8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"]
    loader: Literal["leanfaith.collect2.postprocess.GoldenBlocklist.load"]
    normalization: Literal["leanfaith.representations.views.normalize_headless"]
    near_duplicate_hash: Literal["leanfaith.representations.views.signature_near_dup_hash"]
    procedure_id: Literal[
        "golden_blocklist_v1_normalize_headless_signature_near_dup_hash_root_screen_v1"
    ]
    screen_scope: Literal["raw_theorem_lemma_discovery_before_eligibility_v1"]
    placeholder_screen_is_additive: Literal[True]


class Authorization(StrictModel):
    invokes_lean: Literal[False]
    executes_transforms: Literal[False]
    emits_model_facing_rows: Literal[False]
    internal_gate_source_eligible: Literal[True]
    bounded_internal_pilot_source_eligible: Literal[True]
    pilot_execution_authorized_by_this_config: Literal[False]
    redistribution_review_complete: Literal[False]
    public_redistribution_eligible: Literal[False]
    publication_authorized: Literal[False]
    ten_k_authorized: Literal[False]
    scale_authorized: Literal[False]


class EvidenceRule(StrictModel):
    current_environment_typed: bool
    requires_upstream_typed_evidence: bool
    requires_compile_context: bool
    requires_closed_expr_route: bool
    requires_current_meta_receipt: bool


class EvidenceLevels(StrictModel):
    ordered: tuple[EvidenceLevel, ...]
    surface_prefilter: EvidenceRule
    preexisting_typed: EvidenceRule
    typed_pending: EvidenceRule
    typed: EvidenceRule

    @model_validator(mode="after")
    def _exact_lattice(self) -> EvidenceLevels:
        if self.ordered != (
            "surface_prefilter",
            "preexisting_typed",
            "typed_pending",
            "typed",
        ):
            raise ValueError("evidence levels/order drift")
        expected = {
            "surface_prefilter": (False, False, False, False, False),
            "preexisting_typed": (False, True, False, False, False),
            "typed_pending": (False, True, True, True, False),
            "typed": (True, True, True, True, True),
        }
        for name, values in expected.items():
            rule = cast(EvidenceRule, getattr(self, name))
            observed = (
                rule.current_environment_typed,
                rule.requires_upstream_typed_evidence,
                rule.requires_compile_context,
                rule.requires_closed_expr_route,
                rule.requires_current_meta_receipt,
            )
            if observed != values:
                raise ValueError(f"{name} evidence rule drift")
        return self


class OperationContract(StrictModel):
    primary_operation_ids: tuple[PrimaryOperation, ...]
    optional_n31_proof_operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    n31_proof_is_nested_under_parent_mutation: Literal[True]
    n31_proof_has_no_independent_root_pool: Literal[True]
    n31_proof_absence_does_not_block_rubric: Literal[True]
    n31_proof_activation_authorized: Literal[False]

    @model_validator(mode="after")
    def _exact_operations(self) -> OperationContract:
        if self.primary_operation_ids != PRIMARY_OPERATIONS:
            raise ValueError("primary operation inventory/order drift")
        return self


class TierSpec(StrictModel):
    tier_id: NonEmptyStr
    input_route: Literal[
        "hash_bound_root_manifest",
        "deterministic_bounded_sampling_frame_scan",
        "complete_streaming_source_scan",
    ]
    selection_rule: Literal[
        "global_minimum_stable_eligible_root_hash_v1",
        "round_robin_source_strata_then_root_hash_v1",
        "no_root_selection_full_inventory_only_v1",
    ]
    selection_operation_ids: tuple[PrimaryOperation, ...]
    target_per_primary_operation: NonNegativeInt
    minimum_gate_roots_per_primary_operation: NonNegativeInt | None = None
    source_scan_root_budget: PositiveInt | None = None
    scan_budget_unit: Literal["raw_theorem_lemma_discoveries"]
    completion_claim: Literal[
        "hash_bound_route_slice_complete_not_source_complete",
        "bounded_route_slice_complete_not_source_complete",
        "complete_source_inventory",
    ]
    blocks_two_row_smoke: bool
    blocks_approximately_100_root_gate: bool
    blocks_ten_k_and_scale: bool


class SmokeTierSpec(TierSpec):
    """Literal progression state for the exact two-entry smoke micro-census."""

    tier_id: Literal["root_specific_smoke_micro_census_v1"]
    input_route: Literal["hash_bound_root_manifest"]
    selection_rule: Literal["global_minimum_stable_eligible_root_hash_v1"]
    selection_operation_ids: tuple[
        Literal["P01_ALPHA_RENAME_SINGLE_V1"],
        Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"],
    ]
    target_per_primary_operation: Literal[1]
    minimum_gate_roots_per_primary_operation: Literal[None] = None
    source_scan_root_budget: Literal[None] = None
    scan_budget_unit: Literal["raw_theorem_lemma_discoveries"]
    completion_claim: Literal["hash_bound_route_slice_complete_not_source_complete"]
    blocks_two_row_smoke: Literal[True]
    blocks_approximately_100_root_gate: Literal[False]
    blocks_ten_k_and_scale: Literal[False]


class SelectedWaveTierSpec(TierSpec):
    """Literal progression state for the bounded approximately-100-root frame."""

    tier_id: Literal["selected_wave_sampling_frame_census_v1"]
    input_route: Literal["deterministic_bounded_sampling_frame_scan"]
    selection_rule: Literal["round_robin_source_strata_then_root_hash_v1"]
    selection_operation_ids: tuple[
        Literal["P01_ALPHA_RENAME_SINGLE_V1"],
        Literal["P15_SWAP_IFF_SIDES_V1"],
        Literal["P18_SYMMETRIZE_EQUALITY_V1"],
        Literal["P21_BETA_REDUCE_V1"],
        Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"],
    ]
    target_per_primary_operation: Literal[125]
    minimum_gate_roots_per_primary_operation: Literal[100]
    source_scan_root_budget: Literal[25000]
    scan_budget_unit: Literal["raw_theorem_lemma_discoveries"]
    completion_claim: Literal["bounded_route_slice_complete_not_source_complete"]
    blocks_two_row_smoke: Literal[False]
    blocks_approximately_100_root_gate: Literal[True]
    blocks_ten_k_and_scale: Literal[False]


class FullCrossSourceTierSpec(TierSpec):
    """Literal progression state for the complete cross-source inventory."""

    tier_id: Literal["complete_cross_source_census_v1"]
    input_route: Literal["complete_streaming_source_scan"]
    selection_rule: Literal["no_root_selection_full_inventory_only_v1"]
    selection_operation_ids: tuple[()]
    target_per_primary_operation: Literal[0]
    minimum_gate_roots_per_primary_operation: Literal[None] = None
    source_scan_root_budget: Literal[None]
    scan_budget_unit: Literal["raw_theorem_lemma_discoveries"]
    completion_claim: Literal["complete_source_inventory"]
    blocks_two_row_smoke: Literal[False]
    blocks_approximately_100_root_gate: Literal[False]
    blocks_ten_k_and_scale: Literal[True]


class TierSpecs(StrictModel):
    smoke: SmokeTierSpec
    selected_wave: SelectedWaveTierSpec
    full_cross_source: FullCrossSourceTierSpec

    @model_validator(mode="after")
    def _exact_progression(self) -> TierSpecs:
        if (
            self.smoke.tier_id != "root_specific_smoke_micro_census_v1"
            or self.smoke.input_route != "hash_bound_root_manifest"
            or self.smoke.selection_rule != "global_minimum_stable_eligible_root_hash_v1"
            or self.smoke.selection_operation_ids
            != ("P01_ALPHA_RENAME_SINGLE_V1", "N31_DROP_REQUIRED_GUARD_RUBRIC_V1")
            or self.smoke.target_per_primary_operation != 1
            or self.smoke.scan_budget_unit != "raw_theorem_lemma_discoveries"
            or self.smoke.completion_claim != "hash_bound_route_slice_complete_not_source_complete"
            or not self.smoke.blocks_two_row_smoke
        ):
            raise ValueError("smoke micro-census contract drift")
        if (
            self.selected_wave.tier_id != "selected_wave_sampling_frame_census_v1"
            or self.selected_wave.input_route != "deterministic_bounded_sampling_frame_scan"
            or self.selected_wave.selection_rule != "round_robin_source_strata_then_root_hash_v1"
            or self.selected_wave.selection_operation_ids != PRIMARY_OPERATIONS
            or self.selected_wave.minimum_gate_roots_per_primary_operation != 100
            or self.selected_wave.target_per_primary_operation != 125
            or self.selected_wave.source_scan_root_budget != 25000
            or self.selected_wave.scan_budget_unit != "raw_theorem_lemma_discoveries"
            or self.selected_wave.completion_claim
            != "bounded_route_slice_complete_not_source_complete"
            or not self.selected_wave.blocks_approximately_100_root_gate
        ):
            raise ValueError("selected-wave census contract drift")
        if (
            self.full_cross_source.tier_id != "complete_cross_source_census_v1"
            or self.full_cross_source.input_route != "complete_streaming_source_scan"
            or self.full_cross_source.selection_rule != "no_root_selection_full_inventory_only_v1"
            or self.full_cross_source.selection_operation_ids
            or self.full_cross_source.source_scan_root_budget is not None
            or self.full_cross_source.scan_budget_unit != "raw_theorem_lemma_discoveries"
            or self.full_cross_source.completion_claim != "complete_source_inventory"
            or not self.full_cross_source.blocks_ten_k_and_scale
        ):
            raise ValueError("full cross-source census contract drift")
        return self


class Durability(StrictModel):
    state_backend: Literal["sqlite_wal_v1"]
    append_only_journal_required: Literal[True]
    terminal_marker_required: Literal[True]
    receipt_write: Literal["immutable_create_or_identical_v2"]
    resume_rule: Literal["restart_incomplete_source_scan_with_root_id_dedup_v1"]
    stable_root_id: Literal["sha256_canonical_source_revision_locator_and_surface_hash_v1"]
    candidate_set_hash: Literal["sha256_len_prefixed_sorted_root_ids_v1"]
    state_evidence_hash: Literal["sha256_len_prefixed_canonical_sorted_sqlite_rows_v1"]
    journal_chain: Literal["sha256_canonical_event_with_sequence_and_previous_hash_v1"]
    journal_path_bound_in_state_receipt_and_marker: Literal[True]
    no_per_row_process_spawn: Literal[True]


class ClusterContract(StrictModel):
    exact_key: Literal["whitespace_normalized_bounded_signature_without_declared_name_v1"]
    alpha_key: Literal["binder_names_normalized_bounded_signature_v1"]
    structure_key: Literal["identifiers_and_numerals_normalized_bounded_signature_v1"]
    memberships_persisted_in_sqlite: Literal[True]
    per_operation_pool_hashes_required: Literal[True]
    selected_wave_clusters_must_remain_intact: Literal[True]
    oversized_cluster_policy: Literal["skip_cluster_without_splitting_v1"]
    target_125_is_soft_after_cluster_integrity: Literal[True]


class SurfaceProcedure(StrictModel):
    procedure_id: Literal["conservative_lean_declaration_surface_prefilter_v1"]
    declaration_kinds: tuple[Literal["theorem", "lemma"], ...]
    proof_placeholder_tokens: tuple[Literal["sorry", "admit", "by?"], ...]
    proof_placeholder_matching: Literal["masked_lean_token_boundaries_v1"]
    raw_discovery_accounting: Literal["all_bounded_theorem_lemma_commands_v1"]
    exclusion_reasons: tuple[
        Literal[
            "private_declaration",
            "proof_placeholder",
            "golden_blocklist_near_duplicate",
            "golden_blocklist_problem",
        ],
        ...,
    ]
    surface_identity: Literal["whitespace_normalized_bounded_signature_without_declared_name_v1"]
    near_identity: Literal["binder_names_normalized_bounded_signature_v1"]
    applicability_is_typed_claim: Literal[False]
    operation_applicability_status: Literal["typed_validation_pending"]
    zero_lean_operation_applicability_claimed: Literal[False]

    @model_validator(mode="after")
    def _exact_surface_policy(self) -> SurfaceProcedure:
        if self.declaration_kinds != ("theorem", "lemma"):
            raise ValueError("declaration kinds/order drift")
        if self.proof_placeholder_tokens != ("sorry", "admit", "by?"):
            raise ValueError("placeholder token inventory/order drift")
        if self.exclusion_reasons != (
            "private_declaration",
            "proof_placeholder",
            "golden_blocklist_near_duplicate",
            "golden_blocklist_problem",
        ):
            raise ValueError("surface exclusion-reason inventory/order drift")
        return self


class SourceSpec(StrictModel):
    source_id: SourceId
    kind: Literal["parquet_source_code", "git_lean_source"]
    repository: NonEmptyStr
    revision: GitCommit
    checkout_or_artifact_path: NonEmptyStr
    artifact_sha256: Sha256 | None
    repo_binding_path: NonEmptyStr
    repo_binding_sha256: Sha256
    globs: tuple[NonEmptyStr, ...]
    expected_toolchain: NonEmptyStr | None
    root_module: NonEmptyStr | None
    default_evidence_level: Literal["preexisting_typed", "typed_pending"]
    domain_stratum: DomainStratum
    closed_expr_route: ClosedExprRoute
    compile_context_bound: bool
    closed_expr_route_bound: bool
    internal_gate_eligible: Literal[True]
    bounded_internal_pilot_eligible: Literal[True]
    redistribution_review_complete: Literal[False]
    publication_eligible: Literal[False]

    @model_validator(mode="after")
    def _coherent_source(self) -> SourceSpec:
        if self.kind == "parquet_source_code":
            if self.source_id != "compiler_data" or self.artifact_sha256 is None or self.globs:
                raise ValueError("compiler_data source binding is incoherent")
            if self.compile_context_bound or self.closed_expr_route_bound:
                raise ValueError("compiler_data context/closed-Expr route is not census-bound")
            if (
                self.domain_stratum != "mixed_compiler_training_data"
                or self.closed_expr_route
                != "compiler_data_elaborated_signature_in_persistent_project_session_v1"
            ):
                raise ValueError("compiler_data domain/closed-Expr route drift")
        else:
            if self.source_id == "compiler_data" or self.artifact_sha256 is not None:
                raise ValueError("git source binding is incoherent")
            if not self.globs or not self.compile_context_bound or not self.closed_expr_route_bound:
                raise ValueError("git source must bind glob, context, and closed-Expr route")
            if self.default_evidence_level != "typed_pending":
                raise ValueError("git source declarations must remain typed_pending")
            if (
                self.closed_expr_route
                != "imported_constant_info_type_in_persistent_project_session_v1"
            ):
                raise ValueError("git sources must use imported ConstantInfo.type")
        return self


class Wave1CensusConfig(StrictModel):
    schema_version: Literal[1]
    census_id: Literal["sft1_wave1_zero_lean_census_v0_3_6"]
    revision: Literal["0.3.6"]
    status: Literal["implementation_ready_execution_separately_authorized"]
    implementation_binding: ImplementationBinding
    policy_binding: PolicyBinding
    evaluation_blocklist_binding: EvaluationBlocklistBinding
    authorization: Authorization
    evidence_levels: EvidenceLevels
    operation_contract: OperationContract
    tiers: TierSpecs
    durability: Durability
    cluster_contract: ClusterContract
    surface_procedure: SurfaceProcedure
    sources: tuple[SourceSpec, ...]

    @model_validator(mode="after")
    def _exact_sources(self) -> Wave1CensusConfig:
        if tuple(source.source_id for source in self.sources) != EXPECTED_SOURCE_IDS:
            raise ValueError("source inventory/order drift")
        for source in self.sources:
            observed = {
                key: getattr(source, key) for key in EXPECTED_SOURCE_FREEZE[source.source_id]
            }
            if observed != EXPECTED_SOURCE_FREEZE[source.source_id]:
                raise ValueError(f"exact source freeze drift for {source.source_id}")
        return self


class ArtifactBinding(StrictModel):
    path: NonEmptyStr
    file_sha256: Sha256
    byte_count: PositiveInt


class RawLeanRequestArtifact(StrictModel):
    request_id: NonEmptyStr
    context_id: NonEmptyStr
    code: NonEmptyStr | None
    file_path: NonEmptyStr | None
    declarations: bool
    root_goals: bool
    infotree: Literal["none", "substantive", "full"]
    allow_sorry: bool
    timeout_seconds: Annotated[int | float, Field(gt=0)]

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> RawLeanRequestArtifact:
        if (self.code is None) == (self.file_path is None):
            raise ValueError("raw Lean request must bind exactly one payload route")
        return self


class RawTransportIsolationArtifact(StrictModel):
    version: NonEmptyStr
    attempt: NonEmptyStr
    prefix_sha256: Sha256
    prefix_width: NonNegativeInt


class RawLeanResponseArtifact(StrictModel):
    """Typed replay surface of the central Lean backend's persisted wire envelope."""

    request: RawLeanRequestArtifact
    transport_isolation: RawTransportIsolationArtifact | None
    request_hash: Sha256
    method_version: NonEmptyStr
    response: dict[str, object] | None
    error: str | None

    @model_validator(mode="after")
    def _terminal_shape(self) -> RawLeanResponseArtifact:
        if self.response is not None and self.error is not None:
            raise ValueError("raw Lean response cannot carry both response and transport error")
        return self


class CentralCacheKeyArtifact(StrictModel):
    """Exact central-cache key surface required by the Wave 1 smoke route."""

    schema_version: Literal[1]
    pair_id: PairId
    theorem_a_id: TheoremId
    theorem_b_id: TheoremId
    theorem_a_statement_hash: Sha256
    theorem_b_statement_hash: Sha256
    representation_a_id: RepresentationId
    representation_b_id: RepresentationId
    representation_a_content_hash: Sha256
    representation_b_content_hash: Sha256
    representation_version: NonEmptyStr
    context_id: ContextId
    context_fingerprint: Sha256
    environment_schema_version: PositiveInt
    environment_hash: Sha256
    evidence_kind: EvidenceKind
    evidence_direction: Literal["none", "A_to_B", "B_to_A", "equivalence_only"]
    method_version: NonEmptyStr
    timeout_seconds: Annotated[int | float, Field(gt=0)]
    config_hash: Sha256
    semantic_policy_version: NonEmptyStr
    semantic_policy_hash: Sha256
    lean_version: NonEmptyStr
    lean_interact_version: NonEmptyStr
    repl_revision: NonEmptyStr
    project_revision: NonEmptyStr


def _central_cache_evidence_semantic_payload(
    evidence: EvidenceRecord,
) -> dict[str, object]:
    value = evidence.model_dump(mode="json")
    value.pop("created_at", None)
    value.pop("raw_artifact", None)
    metadata = dict(cast(dict[str, object], value.get("metadata", {})))
    for key in ("cache_hit", "raw_artifact_sha256", "collected_at", "run_id"):
        metadata.pop(key, None)
    value["metadata"] = metadata
    return cast(dict[str, object], value)


class CentralCacheEntryArtifact(StrictModel):
    """Lean-free mirror of the immutable cache envelope used by this route."""

    schema_version: Literal[1]
    cache_key_hash: Sha256
    cache_key: CentralCacheKeyArtifact
    evidence_hash: Sha256
    evidence: EvidenceRecord
    auxiliary_evidence_hash: Sha256
    auxiliary_evidence: tuple[()]
    generated_code_hash: Literal[None]
    lean_request_hashes: tuple[Sha256, ...]
    certificate_dependency_hash: Sha256
    artifact_hashes: dict[str, Sha256]

    @model_validator(mode="after")
    def _replay_envelope(self) -> CentralCacheEntryArtifact:
        if self.cache_key_hash != hash_canonical(self.cache_key.model_dump(mode="json")):
            raise ValueError("central cache key hash does not replay")
        if self.evidence_hash != hash_canonical(
            _central_cache_evidence_semantic_payload(self.evidence)
        ):
            raise ValueError("central cache evidence hash does not replay")
        if self.auxiliary_evidence_hash != hash_canonical([]):
            raise ValueError("Wave 1 smoke cache must bind the exact empty auxiliary evidence")
        if (
            self.evidence.target_id != self.cache_key.pair_id
            or self.evidence.kind != self.cache_key.evidence_kind
            or self.evidence.method_version != self.cache_key.method_version
            or self.evidence.config_hash != self.cache_key.config_hash
        ):
            raise ValueError("central cache evidence/key lineage drift")
        for path, digest in self.artifact_hashes.items():
            if not path or ".." in Path(path).parts or len(digest) != 64:
                raise ValueError("central cache artifact binding is malformed")
        referenced = {item for item in (self.evidence.raw_artifact,) if item is not None}
        if isinstance(self.evidence.value, AuditValue) and (
            self.evidence.value.detail_artifact is not None
        ):
            referenced.add(self.evidence.value.detail_artifact)
        if not referenced.issubset(self.artifact_hashes):
            raise ValueError("central cache evidence artifact is not content-bound")
        return self


class N31AdmittedBankIdentity(StrictModel):
    project_id: SourceId
    bank_id: NonEmptyStr
    resolved_lean_hash: Sha256
    resolution_receipt_hash: Sha256


class RuntimeOperationSmokeBinding(StrictModel):
    operation_id: PrimaryOperation
    registry_entry_hash: Sha256
    anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    runtime_fixture_bundle_hash: Sha256
    dispatch_symbol: NonEmptyStr
    checker_symbol: NonEmptyStr
    runtime_status: NonEmptyStr


class RuntimeCompileContextBinding(StrictModel):
    source_id: SourceId
    compile_context_identity: Annotated[str, Field(pattern=r"^ctx:[0-9a-f]{64}$", strict=True)]
    compile_context_fingerprint: Sha256

    @model_validator(mode="after")
    def _identity_replays(self) -> RuntimeCompileContextBinding:
        if self.compile_context_identity != f"ctx:{self.compile_context_fingerprint}":
            raise ValueError("compile-context identity does not replay from fingerprint")
        return self


class FinalizedRuntimeSmokeBinding(StrictModel):
    runtime_config_path: Literal[
        "configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"
    ]
    runtime_config_file_sha256: Sha256
    runtime_config_semantic_hash: Sha256
    runtime_loader_path: Literal["src/leanfaith/sft1/wave1_live_readiness.py"]
    runtime_loader_file_sha256: Sha256
    runtime_helper_path: Literal["LeanFaith/Meta/SFT1/Wave1Runtime.lean"]
    runtime_helper_file_sha256: Sha256
    assembled_preamble_sha256: Sha256
    operations: tuple[RuntimeOperationSmokeBinding, ...]
    compile_contexts: tuple[RuntimeCompileContextBinding, ...]
    n31_activation_authorized: bool
    n31_admitted_identities: tuple[N31AdmittedBankIdentity, ...]

    @model_validator(mode="after")
    def _exact_inventory(self) -> FinalizedRuntimeSmokeBinding:
        if tuple(item.operation_id for item in self.operations) != PRIMARY_OPERATIONS:
            raise ValueError("finalized runtime operation inventory/order drift")
        if tuple(item.source_id for item in self.compile_contexts) != EXPECTED_SOURCE_IDS:
            raise ValueError("finalized runtime compile-context inventory/order drift")
        identities = [item.model_dump_json() for item in self.n31_admitted_identities]
        if len(identities) != len(set(identities)):
            raise ValueError("finalized runtime repeats an N31 admitted identity")
        return self


class TypedMetaReceipt(StrictModel):
    schema_version: Literal[1]
    receipt_kind: Literal["sft1_wave1_typed_applicability_v1"]
    root_id: Sha256
    source_id: SourceId
    source_revision: GitCommit
    source_locator: NonEmptyStr
    source_text_hash: Sha256
    signature_text_hash: Sha256
    selected_operation_id: PrimaryOperation
    typed_applicable_operations: tuple[PrimaryOperation, ...]
    # Historical field name retained for the canonical source endpoint.
    closed_expr_hash: Sha256
    candidate_closed_expr_hash: Sha256
    source_sidecar_sha256: Sha256
    candidate_sidecar_sha256: Sha256
    render_request_hash: Sha256
    compile_context_identity: Annotated[str, Field(pattern=r"^ctx:[0-9a-f]{64}$", strict=True)]
    compile_context_fingerprint: Sha256
    meta_request_hash: Sha256
    typed_certificate_payload_hash: Sha256
    central_cache_key_hash: Sha256
    runtime_config_path: Literal[
        "configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"
    ]
    runtime_config_file_sha256: Sha256
    runtime_config_semantic_hash: Sha256
    runtime_loader_path: Literal["src/leanfaith/sft1/wave1_live_readiness.py"]
    runtime_loader_file_sha256: Sha256
    runtime_helper_path: Literal["LeanFaith/Meta/SFT1/Wave1Runtime.lean"]
    runtime_helper_file_sha256: Sha256
    assembled_preamble_sha256: Sha256
    operation_registry_entry_hash: Sha256
    operation_anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    runtime_fixture_bundle_hash: Sha256
    dispatch_symbol: NonEmptyStr
    checker_symbol: NonEmptyStr
    n31_admitted_bank_identity: N31AdmittedBankIdentity | None
    raw_lean_response_artifact: ArtifactBinding
    typed_replay_artifact: ArtifactBinding
    central_cache_artifact: ArtifactBinding
    persistent_same_request: Literal[True]
    certificate_replay_passed: Literal[True]
    typed_applicability_passed: Literal[True]
    lean_invoked: Literal[True]

    @model_validator(mode="after")
    def _typed_operations(self) -> TypedMetaReceipt:
        if self.typed_applicable_operations != (self.selected_operation_id,):
            raise ValueError("typed Meta receipt must bind exactly its selected operation")
        if self.compile_context_identity != f"ctx:{self.compile_context_fingerprint}":
            raise ValueError("typed Meta receipt compile-context identity does not replay")
        if self.meta_request_hash != self.render_request_hash:
            raise ValueError("typed Meta receipt must bind its same-request render request")
        if self.source_sidecar_sha256 == self.candidate_sidecar_sha256:
            raise ValueError("typed Meta receipt must bind distinct complete endpoint sidecars")
        if self.selected_operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1":
            if self.n31_admitted_bank_identity is None:
                raise ValueError("N31 typed receipt lacks an admitted bank identity")
            if self.n31_admitted_bank_identity.project_id != self.source_id:
                raise ValueError("N31 admitted bank identity/project drift")
        elif self.n31_admitted_bank_identity is not None:
            raise ValueError("positive typed receipt cannot bind an N31 bank identity")
        paths = (
            self.raw_lean_response_artifact.path,
            self.typed_replay_artifact.path,
            self.central_cache_artifact.path,
        )
        if len(set(paths)) != len(paths):
            raise ValueError("typed Meta artifacts must use distinct paths")
        return self


class RootRecord(StrictModel):
    root_id: Sha256
    source_id: SourceId
    source_revision: GitCommit
    source_locator: NonEmptyStr
    source_text_hash: Sha256
    signature_text_hash: Sha256
    surface_identity_hash: Sha256
    near_identity_hash: Sha256
    structure_identity_hash: Sha256
    evidence_level: EvidenceLevel
    upstream_evidence_kind: Literal["none", "git_source_declaration", "compiler_data_validation"]
    upstream_typed_evidence_hash: Sha256 | None
    compile_context_available: bool
    closed_expr_route_available: bool
    current_meta_receipt_hash: Sha256 | None
    blocklist_screened: Literal[True]
    blocklist_file_sha256: Literal[
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
    ]
    blocklist_procedure_id: Literal[
        "golden_blocklist_v1_normalize_headless_signature_near_dup_hash_root_screen_v1"
    ]
    golden_near_dup_hash: Sha256
    private_declaration: bool
    proof_placeholder_detected: bool
    golden_blocklist_hit: bool
    exclusion_reason: ExclusionReason
    root_blocklisted: bool
    internal_gate_eligible: Literal[True]
    operation_candidates: tuple[PrimaryOperation, ...]
    typed_applicable_operations: tuple[PrimaryOperation, ...]
    n31_proof_status: Literal["unknown", "unavailable", "available"]
    n31_proof_payload_hash: Sha256 | None

    @model_validator(mode="after")
    def _evidence_discipline(self) -> RootRecord:
        expected_root_id = make_root_id(
            self.source_id, self.source_revision, self.source_locator, self.source_text_hash
        )
        if self.root_id != expected_root_id:
            raise ValueError("root_id does not replay")
        expected_reason = "none"
        if self.private_declaration:
            expected_reason = "private_declaration"
        elif self.proof_placeholder_detected:
            expected_reason = "proof_placeholder"
        elif self.golden_blocklist_hit:
            if self.exclusion_reason not in (
                "golden_blocklist_near_duplicate",
                "golden_blocklist_problem",
            ):
                raise ValueError("golden blocklist hit lacks its exact exclusion class")
            expected_reason = self.exclusion_reason
        if self.exclusion_reason != expected_reason:
            raise ValueError("root exclusion reason does not match deterministic precedence")
        if self.root_blocklisted != (self.exclusion_reason != "none"):
            raise ValueError("root blocklisted flag does not match exclusion reason")
        if tuple(dict.fromkeys(self.operation_candidates)) != self.operation_candidates:
            raise ValueError("operation candidates must be unique and ordered")
        if tuple(op for op in PRIMARY_OPERATIONS if op in self.operation_candidates) != (
            self.operation_candidates
        ):
            raise ValueError("operation candidates differ from primary registry order")
        needs_upstream = self.evidence_level != "surface_prefilter"
        if needs_upstream != (self.upstream_typed_evidence_hash is not None):
            raise ValueError("upstream typed evidence does not match evidence level")
        if needs_upstream != (self.upstream_evidence_kind != "none"):
            raise ValueError("upstream evidence kind does not match evidence level")
        if needs_upstream:
            expected_kind = (
                "compiler_data_validation"
                if self.source_id == "compiler_data"
                else "git_source_declaration"
            )
            if self.upstream_evidence_kind != expected_kind:
                raise ValueError("upstream evidence kind does not match source route")
        needs_context = self.evidence_level in ("typed_pending", "typed")
        if needs_context and not (
            self.compile_context_available and self.closed_expr_route_available
        ):
            raise ValueError("typed_pending/typed root lacks context or closed-Expr route")
        if self.evidence_level == "typed":
            if self.current_meta_receipt_hash is None:
                raise ValueError("typed root lacks current Meta receipt")
        elif self.current_meta_receipt_hash is not None:
            raise ValueError("non-typed root cannot bind a current Meta receipt")
        if tuple(dict.fromkeys(self.typed_applicable_operations)) != (
            self.typed_applicable_operations
        ):
            raise ValueError("typed-applicable operations must be unique and ordered")
        if tuple(op for op in PRIMARY_OPERATIONS if op in self.typed_applicable_operations) != (
            self.typed_applicable_operations
        ):
            raise ValueError("typed-applicable operations differ from registry order")
        if self.evidence_level != "typed" and self.typed_applicable_operations:
            raise ValueError("non-typed root cannot claim typed operation applicability")
        if not set(self.typed_applicable_operations).issubset(self.operation_candidates):
            raise ValueError("typed applicability must refine surface candidates")
        parent = "N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in self.operation_candidates
        if self.n31_proof_status == "available":
            if not parent or self.n31_proof_payload_hash is None:
                raise ValueError("N31 proof evidence must be nested under its parent root")
        elif self.n31_proof_payload_hash is not None:
            raise ValueError("unavailable/unknown N31 proof cannot bind a payload")
        return self


class SmokeManifestEntry(StrictModel):
    schema_version: Literal[1]
    selection_operation_id: Literal[
        "P01_ALPHA_RENAME_SINGLE_V1", "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    ]
    root: RootRecord
    typed_meta_receipt_path: NonEmptyStr
    typed_meta_receipt_sha256: Sha256

    @model_validator(mode="after")
    def _exact_typed_selection(self) -> SmokeManifestEntry:
        if self.root.evidence_level != "typed":
            raise ValueError("smoke root must be current-environment typed")
        if self.root.root_blocklisted:
            raise ValueError("smoke root must pass every root-level exclusion screen")
        if self.selection_operation_id not in self.root.typed_applicable_operations:
            raise ValueError("smoke selection lacks typed operation applicability")
        if self.root.current_meta_receipt_hash != self.typed_meta_receipt_sha256:
            raise ValueError("smoke root does not bind the exact typed Meta receipt")
        return self


class FixedEvidenceCounts(StrictModel):
    surface_prefilter: NonNegativeInt
    preexisting_typed: NonNegativeInt
    typed_pending: NonNegativeInt
    typed: NonNegativeInt


class FixedOperationCounts(StrictModel):
    P01_ALPHA_RENAME_SINGLE_V1: NonNegativeInt
    P15_SWAP_IFF_SIDES_V1: NonNegativeInt
    P18_SYMMETRIZE_EQUALITY_V1: NonNegativeInt
    P21_BETA_REDUCE_V1: NonNegativeInt
    N31_DROP_REQUIRED_GUARD_RUBRIC_V1: NonNegativeInt


class FixedOperationHashes(StrictModel):
    P01_ALPHA_RENAME_SINGLE_V1: Sha256
    P15_SWAP_IFF_SIDES_V1: Sha256
    P18_SYMMETRIZE_EQUALITY_V1: Sha256
    P21_BETA_REDUCE_V1: Sha256
    N31_DROP_REQUIRED_GUARD_RUBRIC_V1: Sha256


class FixedSourceCounts(StrictModel):
    compiler_data: NonNegativeInt
    cslib: NonNegativeInt
    mathlib: NonNegativeInt
    physlib: NonNegativeInt


class ExclusionCounts(StrictModel):
    private_declaration: NonNegativeInt
    proof_placeholder: NonNegativeInt
    golden_blocklist_near_duplicate: NonNegativeInt
    golden_blocklist_problem: NonNegativeInt


class SignatureStrataCounts(StrictModel):
    explicit_binder_surface_candidate_count: NonNegativeInt
    iff_surface_candidate_count: NonNegativeInt
    equality_surface_candidate_count: NonNegativeInt
    beta_redex_surface_candidate_count: NonNegativeInt
    required_guard_surface_candidate_count: NonNegativeInt
    other_surface_root_count: NonNegativeInt


class RouteAvailability(StrictModel):
    expected_closed_expr_route: ClosedExprRoute
    compile_context_available_count: NonNegativeInt
    closed_expr_route_available_count: NonNegativeInt
    current_meta_typed_root_count: NonNegativeInt
    typed_applicability_receipt_root_count: NonNegativeInt


class N31ProofCounts(StrictModel):
    parent_root_count: NonNegativeInt
    available: NonNegativeInt
    unavailable: NonNegativeInt
    unknown: NonNegativeInt
    independent_root_pool_count: Literal[0]
    activation_authorized: Literal[False]

    @model_validator(mode="after")
    def _nested_partition(self) -> N31ProofCounts:
        if self.available + self.unavailable + self.unknown != self.parent_root_count:
            raise ValueError("N31 proof statuses do not partition the parent root pool")
        return self


class SourceResult(StrictModel):
    source_id: SourceId
    source_revision: GitCommit
    domain_stratum: DomainStratum
    expected_closed_expr_route: ClosedExprRoute
    completion_scope: Literal[
        "hash_bound_manifest_route_slice",
        "bounded_sampling_frame_route_slice",
        "complete_source_inventory",
    ]
    scan_complete_semantics: Literal["route_slice_complete_not_necessarily_source_complete_v2"]
    scan_complete: bool
    source_inventory_complete: bool
    root_count: NonNegativeInt
    raw_declaration_count: NonNegativeInt
    eligible_root_count: NonNegativeInt
    excluded_declaration_count: NonNegativeInt
    exclusion_counts: ExclusionCounts
    blocklisted_root_count: NonNegativeInt
    internal_gate_candidate_count: NonNegativeInt
    evidence_counts: FixedEvidenceCounts
    operation_candidate_counts: FixedOperationCounts
    typed_operation_applicability_counts: FixedOperationCounts
    signature_strata: SignatureStrataCounts
    route_availability: RouteAvailability
    n31_proof_route_coverage: N31ProofCounts

    @model_validator(mode="after")
    def _coherent_counts(self) -> SourceResult:
        evidence_total = sum(self.evidence_counts.model_dump().values())
        if evidence_total != self.root_count:
            raise ValueError("source evidence counts do not sum to root count")
        if self.raw_declaration_count != self.root_count:
            raise ValueError("raw declaration count differs from persisted discovery records")
        if self.eligible_root_count + self.excluded_declaration_count != self.raw_declaration_count:
            raise ValueError("eligible and excluded declarations do not partition raw discoveries")
        if sum(self.exclusion_counts.model_dump().values()) != self.excluded_declaration_count:
            raise ValueError("reason-coded exclusions do not sum to excluded declarations")
        if self.blocklisted_root_count != self.excluded_declaration_count:
            raise ValueError("blocklisted roots differ from reason-coded exclusions")
        if self.internal_gate_candidate_count != self.eligible_root_count:
            raise ValueError("internal-gate candidates differ from eligible roots")
        if self.completion_scope == "complete_source_inventory":
            if self.scan_complete != self.source_inventory_complete:
                raise ValueError("complete-source inventory completion drift")
        elif self.source_inventory_complete:
            raise ValueError("a route slice must never claim complete-source inventory")
        if self.blocklisted_root_count > self.root_count:
            raise ValueError("blocklisted count exceeds source roots")
        if self.internal_gate_candidate_count > self.root_count:
            raise ValueError("internal candidate count exceeds source roots")
        for operation in PRIMARY_OPERATIONS:
            if getattr(self.typed_operation_applicability_counts, operation) > getattr(
                self.operation_candidate_counts, operation
            ):
                raise ValueError("typed applicability exceeds its surface candidate count")
        route = self.route_availability
        if route.expected_closed_expr_route != self.expected_closed_expr_route:
            raise ValueError("source closed-Expr route reporting drift")
        for value in (
            route.compile_context_available_count,
            route.closed_expr_route_available_count,
            route.current_meta_typed_root_count,
            route.typed_applicability_receipt_root_count,
        ):
            if value > self.root_count:
                raise ValueError("route/applicability count exceeds source roots")
        return self


class DuplicateSummary(StrictModel):
    exact_duplicate_cluster_count: NonNegativeInt
    exact_duplicate_member_count: NonNegativeInt
    alpha_duplicate_cluster_count: NonNegativeInt
    alpha_duplicate_member_count: NonNegativeInt
    structure_duplicate_cluster_count: NonNegativeInt
    structure_duplicate_member_count: NonNegativeInt
    cross_source_exact_cluster_count: NonNegativeInt


class CensusReceipt(StrictModel):
    schema_version: Literal[1]
    census_id: Literal["sft1_wave1_zero_lean_census_v0_3_6"]
    tier: Tier
    tier_id: NonEmptyStr
    config_path: Literal["configs/transformations/sft1_value_first_v1/wave1_census_v0_3_6.yaml"]
    config_file_sha256: Sha256
    config_semantic_hash: Sha256
    implementation_source_sha256: Sha256
    runtime_git_commit: GitCommit
    input_manifest_path: str | None
    input_manifest_sha256: Sha256 | None
    state_db_path: NonEmptyStr
    state_route_id: Sha256
    journal_path: NonEmptyStr
    journal_final_chain_hash: Sha256
    state_backend: Literal["sqlite_wal_v1"]
    lean_invoked: Literal[False]
    transforms_executed: Literal[False]
    model_facing_rows_emitted: Literal[False]
    complete: bool
    sampling_frame_sufficient: bool
    zero_lean_operation_applicability_claimed: Literal[False]
    total_root_count: NonNegativeInt
    total_raw_declaration_count: NonNegativeInt
    total_eligible_root_count: NonNegativeInt
    total_excluded_declaration_count: NonNegativeInt
    evaluation_blocklist_path: Literal["data/benchmarks/golden_blocklist_v1.json"]
    evaluation_blocklist_file_sha256: Literal[
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
    ]
    evaluation_blocklist_procedure_id: Literal[
        "golden_blocklist_v1_normalize_headless_signature_near_dup_hash_root_screen_v1"
    ]
    candidate_set_hash: Sha256
    state_evidence_hash: Sha256
    source_results: tuple[SourceResult, ...]
    operation_candidate_counts: FixedOperationCounts
    operation_pool_hashes: FixedOperationHashes
    typed_operation_applicability_counts: FixedOperationCounts
    selection_rule: NonEmptyStr
    selection_operation_ids: tuple[PrimaryOperation, ...]
    selected_root_ids: dict[PrimaryOperation, tuple[Sha256, ...]]
    selected_cluster_membership_hashes: dict[PrimaryOperation, tuple[Sha256, ...]]
    selected_source_counts: dict[PrimaryOperation, FixedSourceCounts]
    n31_proof_route_coverage: N31ProofCounts
    duplicates: DuplicateSummary

    @model_validator(mode="after")
    def _receipt_invariants(self) -> CensusReceipt:
        if tuple(item.source_id for item in self.source_results) != EXPECTED_SOURCE_IDS:
            raise ValueError("receipt source inventory/order drift")
        if sum(item.root_count for item in self.source_results) != self.total_root_count:
            raise ValueError("receipt source roots do not sum to total")
        if sum(item.raw_declaration_count for item in self.source_results) != (
            self.total_raw_declaration_count
        ):
            raise ValueError("receipt source raw discoveries do not sum to total")
        if sum(item.eligible_root_count for item in self.source_results) != (
            self.total_eligible_root_count
        ):
            raise ValueError("receipt source eligible roots do not sum to total")
        if sum(item.excluded_declaration_count for item in self.source_results) != (
            self.total_excluded_declaration_count
        ):
            raise ValueError("receipt source exclusions do not sum to total")
        if set(self.selected_root_ids) != set(PRIMARY_OPERATIONS):
            raise ValueError("selected-root operation inventory drift")
        if set(self.selected_source_counts) != set(PRIMARY_OPERATIONS):
            raise ValueError("selected-source operation inventory drift")
        if set(self.selected_cluster_membership_hashes) != set(PRIMARY_OPERATIONS):
            raise ValueError("selected-cluster operation inventory drift")
        for op in PRIMARY_OPERATIONS:
            if len(self.selected_root_ids[cast(PrimaryOperation, op)]) > getattr(
                self.operation_candidate_counts, op
            ):
                raise ValueError("selected root count exceeds operation candidates")
            selected_source_total = sum(
                self.selected_source_counts[cast(PrimaryOperation, op)].model_dump().values()
            )
            if selected_source_total != len(self.selected_root_ids[cast(PrimaryOperation, op)]):
                raise ValueError("selected source strata do not sum to selected roots")
            if getattr(self.typed_operation_applicability_counts, op) > getattr(
                self.operation_candidate_counts, op
            ):
                raise ValueError("typed applicability exceeds its surface candidate count")
        for op in PRIMARY_OPERATIONS:
            if (
                op not in self.selection_operation_ids
                and self.selected_root_ids[cast(PrimaryOperation, op)]
            ):
                raise ValueError("unselected operation has selected roots")
        return self


@dataclass(frozen=True, slots=True)
class LoadedCensusConfig:
    config: Wave1CensusConfig
    repo_root: Path
    path: Path
    config_hash: str
    config_file_sha256: str
    golden_blocklist: GoldenBlocklist


def make_root_id(
    source_id: str, source_revision: str, source_locator: str, source_text_hash: str
) -> str:
    return hash_canonical(
        {
            "procedure": "sha256_canonical_source_revision_locator_and_surface_hash_v1",
            "source_id": source_id,
            "source_revision": source_revision,
            "source_locator": source_locator,
            "source_text_hash": source_text_hash,
        }
    )


def _safe_path(
    path: Path,
    *,
    purpose: str,
    require_exists: bool,
    require_file: bool = False,
    containment_root: Path | None = None,
) -> Path:
    """Resolve only after rejecting symlinks in the final path and every ancestor."""
    lexical = Path(os.path.abspath(os.fspath(path)))
    root_lexical = (
        Path(os.path.abspath(os.fspath(containment_root))) if containment_root is not None else None
    )
    if root_lexical is not None and not lexical.is_relative_to(root_lexical):
        raise CensusError(f"{purpose} path escapes its trusted root")
    current = Path(lexical.anchor)
    missing_seen = False
    for part in lexical.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing_seen = True
            continue
        if missing_seen:
            raise CensusError(f"{purpose} path has an existing child below a missing ancestor")
        if stat.S_ISLNK(metadata.st_mode):
            raise CensusError(f"{purpose} path or ancestor may not be a symlink")
        if current != lexical and not stat.S_ISDIR(metadata.st_mode):
            raise CensusError(f"{purpose} path has a non-directory ancestor")
    if require_exists and not lexical.exists():
        raise CensusError(f"{purpose} path does not exist")
    if require_file and (not lexical.is_file() or lexical.is_symlink()):
        raise CensusError(f"{purpose} path is not a regular non-symlink file")
    resolved = lexical.resolve(strict=require_exists)
    if root_lexical is not None:
        root_resolved = root_lexical.resolve(strict=True)
        if not resolved.is_relative_to(root_resolved):
            raise CensusError(f"{purpose} resolved path escapes its trusted root")
    return resolved


def _reject_path_aliases(paths: dict[str, Path]) -> None:
    seen_paths: dict[Path, str] = {}
    seen_inodes: dict[tuple[int, int], str] = {}
    for purpose, path in paths.items():
        previous = seen_paths.setdefault(path, purpose)
        if previous != purpose:
            raise CensusError(f"{purpose} path aliases {previous}")
        if path.exists():
            metadata = path.stat()
            inode = (metadata.st_dev, metadata.st_ino)
            inode_previous = seen_inodes.setdefault(inode, purpose)
            if inode_previous != purpose:
                raise CensusError(f"{purpose} path aliases {inode_previous}")


def _reject_sqlite_sidecar_aliases(state_path: Path, paths: dict[str, Path]) -> None:
    reserved = {
        state_path.with_name(f"{state_path.name}-wal"),
        state_path.with_name(f"{state_path.name}-shm"),
        state_path.with_name(f"{state_path.name}-journal"),
    }
    for purpose, path in paths.items():
        if purpose != "census SQLite state" and path in reserved:
            raise CensusError(f"{purpose} path aliases a reserved SQLite sidecar")


def _git_stdout(repo_root: Path, arguments: Sequence[str], *, text: bool) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CensusError(f"cannot verify census Git identity: git {' '.join(arguments)}") from exc
    return cast(str | bytes, result.stdout)


def _current_runtime_commit(repo_root: Path) -> str:
    observed = cast(str, _git_stdout(repo_root, ("rev-parse", "HEAD"), text=True)).strip()
    if re.fullmatch(r"[0-9a-f]{40}", observed) is None:
        raise CensusError("runtime Git commit is malformed")
    return observed


def _runtime_commit_file_bindings(loaded: LoadedCensusConfig) -> dict[str, str]:
    """Return every repo-relative file whose bytes define this census route."""
    bindings = {
        loaded.path.relative_to(loaded.repo_root).as_posix(): loaded.config_file_sha256,
        loaded.config.implementation_binding.implementation_source_path: (
            loaded.config.implementation_binding.implementation_source_sha256
        ),
        loaded.config.policy_binding.source_use_path: (
            loaded.config.policy_binding.source_use_sha256
        ),
        loaded.config.policy_binding.effective_readiness_path: (
            loaded.config.policy_binding.effective_readiness_sha256
        ),
        loaded.config.evaluation_blocklist_binding.path: (
            loaded.config.evaluation_blocklist_binding.file_sha256
        ),
    }
    for source in loaded.config.sources:
        existing = bindings.setdefault(source.repo_binding_path, source.repo_binding_sha256)
        if existing != source.repo_binding_sha256:
            raise CensusError("runtime commit file binding has conflicting expected hashes")
    return bindings


def _verify_recorded_runtime_commit(loaded: LoadedCensusConfig, runtime_commit: str) -> None:
    """Verify a historical build commit without requiring it to remain ``HEAD``."""
    if re.fullmatch(r"[0-9a-f]{40}", runtime_commit) is None:
        raise CensusError("recorded runtime Git commit is malformed")
    _git_stdout(
        loaded.repo_root,
        ("cat-file", "-e", f"{runtime_commit}^{{commit}}"),
        text=False,
    )
    for relative, expected_sha256 in _runtime_commit_file_bindings(loaded).items():
        blob = cast(
            bytes,
            _git_stdout(loaded.repo_root, ("show", f"{runtime_commit}:{relative}"), text=False),
        )
        if sha256_hex(blob) != expected_sha256:
            raise CensusError(
                f"recorded runtime Git commit does not contain the bound bytes: {relative}"
            )


def _bind_clean_runtime_commit(
    loaded: LoadedCensusConfig, *, allowed_dirty_paths: Sequence[Path] = ()
) -> str:
    """Bind a build to a clean committed checkout, excluding its own output paths."""
    allowed = {Path(os.path.abspath(os.fspath(path))) for path in allowed_dirty_paths}
    changed = cast(
        bytes,
        _git_stdout(
            loaded.repo_root,
            ("diff", "--name-only", "-z", "HEAD", "--"),
            text=False,
        ),
    )
    untracked = cast(
        bytes,
        _git_stdout(
            loaded.repo_root,
            ("ls-files", "--others", "--exclude-standard", "-z"),
            text=False,
        ),
    )
    dirty: list[str] = []
    for encoded in (*changed.split(b"\0"), *untracked.split(b"\0")):
        if not encoded:
            continue
        relative = os.fsdecode(encoded)
        absolute = Path(os.path.abspath(os.fspath(loaded.repo_root / relative)))
        if absolute not in allowed:
            dirty.append(relative)
    if dirty:
        preview = ", ".join(sorted(set(dirty))[:5])
        raise CensusError(f"census build requires a clean committed checkout; dirty: {preview}")
    runtime_commit = _current_runtime_commit(loaded.repo_root)
    _verify_recorded_runtime_commit(loaded, runtime_commit)
    return runtime_commit


def load_wave1_census_config(
    root: Path | None = None, path: Path = DEFAULT_CONFIG_PATH
) -> LoadedCensusConfig:
    root = _safe_path(
        root or find_repo_root(),
        purpose="repository root",
        require_exists=True,
    )
    requested = path if path.is_absolute() else root / path
    resolved = _safe_path(
        requested,
        purpose="census config",
        require_exists=True,
        require_file=True,
        containment_root=root,
    )
    loaded: LoadedConfig[Wave1CensusConfig] = load_config(resolved, Wave1CensusConfig)
    config = loaded.config
    if config.policy_binding.source_use_sha256 != EXPECTED_SOURCE_USE_SHA256:
        raise CensusError("source-use policy hash differs from the owner authorization")
    if config.policy_binding.effective_readiness_sha256 != EXPECTED_EFFECTIVE_READINESS_SHA256:
        raise CensusError("effective-readiness dependency hash drift")
    bindings = (
        (config.policy_binding.source_use_path, config.policy_binding.source_use_sha256),
        (
            config.policy_binding.effective_readiness_path,
            config.policy_binding.effective_readiness_sha256,
        ),
        *((source.repo_binding_path, source.repo_binding_sha256) for source in config.sources),
    )
    for relative, expected in bindings:
        bound = _safe_path(
            root / relative,
            purpose=f"repo binding {relative}",
            require_exists=True,
            require_file=True,
            containment_root=root,
        )
        if hash_file(bound) != expected:
            raise CensusError(f"repo binding drift: {relative}")
    blocklist_binding = config.evaluation_blocklist_binding
    blocklist_path = _safe_path(
        root / blocklist_binding.path,
        purpose="golden evaluation blocklist",
        require_exists=True,
        require_file=True,
        containment_root=root,
    )
    if hash_file(blocklist_path) != blocklist_binding.file_sha256:
        raise CensusError("golden evaluation blocklist hash drift")
    try:
        golden_blocklist = GoldenBlocklist.load(blocklist_path)
    except ValueError as exc:
        raise CensusError("golden evaluation blocklist is malformed") from exc
    implementation_path = _safe_path(
        root / config.implementation_binding.implementation_source_path,
        purpose="census implementation",
        require_exists=True,
        require_file=True,
        containment_root=root,
    )
    if hash_file(implementation_path) != config.implementation_binding.implementation_source_sha256:
        raise CensusError("census implementation source hash drift")
    return LoadedCensusConfig(
        config,
        root,
        resolved,
        loaded.config_hash,
        hash_file(resolved),
        golden_blocklist,
    )


def _mask_lean_comments_and_strings(text: str) -> str:
    """Mask non-code while preserving offsets and newlines for declaration spans."""
    output = list(text)
    index = 0
    block_depth = 0
    in_line_comment = False
    in_string = False
    escaped = False
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if current == "\n":
                in_line_comment = False
            else:
                output[index] = " "
            index += 1
            continue
        if block_depth:
            if current == "/" and following == "-":
                output[index] = output[index + 1] = " "
                block_depth += 1
                index += 2
            elif current == "-" and following == "/":
                output[index] = output[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if current != "\n":
                    output[index] = " "
                index += 1
            continue
        if in_string:
            if current != "\n":
                output[index] = " "
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            index += 1
            continue
        if current == "-" and following == "-":
            output[index] = output[index + 1] = " "
            in_line_comment = True
            index += 2
        elif current == "/" and following == "-":
            output[index] = output[index + 1] = " "
            block_depth = 1
            index += 2
        elif current == '"':
            output[index] = " "
            in_string = True
            index += 1
        else:
            index += 1
    return "".join(output)


def _strip_lean_comments(text: str) -> str:
    """Remove comments while retaining source-significant strings and offsets."""
    output = list(text)
    index = 0
    block_depth = 0
    in_line_comment = False
    in_string = False
    escaped = False
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if current == "\n":
                in_line_comment = False
            else:
                output[index] = " "
            index += 1
            continue
        if block_depth:
            if current == "/" and following == "-":
                output[index] = output[index + 1] = " "
                block_depth += 1
                index += 2
            elif current == "-" and following == "/":
                output[index] = output[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if current != "\n":
                    output[index] = " "
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            index += 1
            continue
        if current == "-" and following == "-":
            output[index] = output[index + 1] = " "
            in_line_comment = True
            index += 2
        elif current == "/" and following == "-":
            output[index] = output[index + 1] = " "
            block_depth = 1
            index += 2
        elif current == '"':
            in_string = True
            index += 1
        else:
            index += 1
    return "".join(output)


@dataclass(frozen=True, slots=True)
class _DeclarationSpan:
    start: int
    end: int
    name: str
    private_declaration: bool


def _command_indentation(command: re.Match[str]) -> int:
    """Return a deterministic display-column indentation for one command candidate."""
    return len(command.group("indent").expandtabs(8))


def _next_peer_command_start(
    commands: Sequence[re.Match[str]], index: int, declaration_indent: int, text_length: int
) -> int:
    """Find the next command at the declaration's layout level or an outer one.

    Command-like syntax may also occur inside a declaration.  In particular,
    ``set_option ... in`` is a valid, commonly indented tactic/term construct.
    Treating the next regex match as an unconditional command boundary truncates
    the proof and can hide a later placeholder.  Lean's command layout gives us
    the conservative zero-Lean distinction needed here: nested matches are more
    indented than the declaration, while the next peer/outer command is not.
    """
    for following in commands[index + 1 :]:
        if _command_indentation(following) <= declaration_indent:
            return following.start()
    return text_length


def _bounded_declaration_spans(text: str) -> tuple[_DeclarationSpan, ...]:
    """Find conservative top-level declaration commands without crossing commands."""
    masked = _mask_lean_comments_and_strings(text)
    commands = tuple(_COMMAND_RE.finditer(masked))
    spans: list[_DeclarationSpan] = []
    seen_tokens: set[int] = set()
    for index, command in enumerate(commands):
        if command.group("kind") not in ("theorem", "lemma") and command.group("attribute") is None:
            continue
        declaration_indent = _command_indentation(command)
        command_end = _next_peer_command_start(commands, index, declaration_indent, len(text))
        segment = masked[command.start() : command_end]
        token = _DECL_TOKEN_RE.search(segment)
        if token is None:
            continue
        absolute_token = command.start() + token.start()
        if absolute_token in seen_tokens:
            continue
        seen_tokens.add(absolute_token)
        start = command.start()
        if command.group("kind") in ("theorem", "lemma"):
            cursor = index - 1
            while cursor >= 0 and commands[cursor].group("attribute") is not None:
                attribute_end = commands[cursor + 1].start()
                if _DECL_TOKEN_RE.search(masked[commands[cursor].start() : attribute_end]):
                    break
                start = commands[cursor].start()
                cursor -= 1
        masked_command = masked[start:command_end]
        last_code = len(masked_command.rstrip())
        if last_code == 0:
            continue
        spans.append(
            _DeclarationSpan(
                start=start,
                end=start + last_code,
                name=token.group("name"),
                private_declaration=bool(re.search(r"\bprivate\b", token.group(0))),
            )
        )
    return tuple(spans)


def _bounded_signature(block: str) -> str:
    masked = _mask_lean_comments_and_strings(block)
    comment_free = _strip_lean_comments(block)
    token = _DECL_TOKEN_RE.search(masked)
    if token is None:
        raise CensusError("bounded declaration lost its theorem/lemma token")
    round_depth = square_depth = curly_depth = 0
    index = token.end()
    while index < len(masked) - 1:
        current = masked[index]
        following = masked[index + 1]
        if current == "(":
            round_depth += 1
        elif current == ")":
            round_depth = max(0, round_depth - 1)
        elif current == "[":
            square_depth += 1
        elif current == "]":
            square_depth = max(0, square_depth - 1)
        elif current == "{":
            curly_depth += 1
        elif current == "}":
            curly_depth = max(0, curly_depth - 1)
        elif (
            current == ":" and following == "=" and round_depth == square_depth == curly_depth == 0
        ):
            return comment_free[token.start() : index].rstrip()
        index += 1
    return comment_free[token.start() :].rstrip()


def _signature_fingerprints(signature: str, declared_name: str) -> tuple[str, str, str]:
    token = _DECL_TOKEN_RE.search(_mask_lean_comments_and_strings(signature))
    if token is None or token.group("name") != declared_name:
        raise CensusError("bounded signature declaration name does not replay")
    without_name = signature[: token.start("name")] + "<DECL>" + signature[token.end("name") :]
    exact = _SPACE_RE.sub(" ", without_name).strip()
    alpha = exact
    binder_names: list[str] = []
    for match in re.finditer(r"[({\[]\s*([A-Za-z_][A-Za-z0-9_' ]*)\s*:", exact):
        binder_names.extend(match.group(1).split())
    for match in re.finditer(r"(?:∀|forall|fun|λ)\s+([A-Za-z_][A-Za-z0-9_' ]*)\s*(?::|=>)", exact):
        binder_names.extend(match.group(1).split())
    for ordinal, name in enumerate(dict.fromkeys(binder_names)):
        alpha = re.sub(rf"\b{re.escape(name)}\b", f"<B{ordinal}>", alpha)
    structure = re.sub(r"\b\d+\b", "<NUM>", alpha)
    structure = re.sub(
        r"(?<!<)\b[A-Za-z_][A-Za-z0-9_']*\b(?!>)",
        "<ID>",
        structure,
    )
    return exact, alpha, structure


def _surface_candidates(header: str) -> tuple[PrimaryOperation, ...]:
    found: set[str] = set()
    if re.search(r"[({]\s*[A-Za-z_][A-Za-z0-9_']*\s*:", header):
        found.add("P01_ALPHA_RENAME_SINGLE_V1")
    if "↔" in header or " Iff " in f" {header} ":
        found.add("P15_SWAP_IFF_SIDES_V1")
    if re.search(r"(?<![!<>=:])=(?!=|>)", header):
        found.add("P18_SYMMETRIZE_EQUALITY_V1")
    if re.search(r"\(\s*(?:fun|λ)\b.*?=>.*?\)\s*(?:\(|[A-Za-z_«0-9])", header):
        found.add("P21_BETA_REDUCE_V1")
    has_guard = bool(
        "≠" in header
        or re.search(r"\bNot\b", header)
        or "≤" in header
        or re.search(r"(?<![-=])<(?![=>])", header)
        or "∈" in header
        or re.search(r"\bMembership\.mem\b", header)
        or "→" in header
        or "∧" in header
        or re.search(
            r"\b(?:And|Ne|LT\.lt|LE\.le|IsUnit|Coprime|Disjoint|Nonempty|Nontrivial|"
            r"Injective|Surjective|Monotone|StrictMono)\b",
            header,
        )
    )
    if has_guard:
        found.add("N31_DROP_REQUIRED_GUARD_RUBRIC_V1")
    return cast(tuple[PrimaryOperation, ...], tuple(op for op in PRIMARY_OPERATIONS if op in found))


def _records_from_lean_text(
    loaded: LoadedCensusConfig,
    source: SourceSpec,
    locator: str,
    text: str,
    *,
    upstream_evidence_seed: str | None = None,
    upstream_evidence_kind: Literal[
        "git_source_declaration", "compiler_data_validation"
    ] = "git_source_declaration",
) -> Iterator[RootRecord]:
    for index, span in enumerate(_bounded_declaration_spans(text)):
        block = text[span.start : span.end]
        name = span.name
        signature = _bounded_signature(block)
        surface, near, structure = _signature_fingerprints(signature, name)
        text_hash = sha256_hex(block.encode("utf-8"))
        signature_hash = sha256_hex(signature.encode("utf-8"))
        source_locator = f"{locator}#decl={index}:{name}"
        masked_block = _mask_lean_comments_and_strings(block)
        proof_placeholder = _PLACEHOLDER_TOKEN_RE.search(masked_block) is not None
        headless = normalize_headless(signature)
        if headless is None:
            raise CensusError("bounded declaration cannot produce a headless blocklist view")
        golden_near_hash = signature_near_dup_hash(headless)
        golden_near_hit = golden_near_hash in loaded.golden_blocklist.near_dup_hashes
        golden_problem_hit = loaded.golden_blocklist.problem_is_blocked(
            name
        ) or loaded.golden_blocklist.problem_is_blocked(source_locator)
        golden_hit = golden_near_hit or golden_problem_hit
        exclusion_reason: ExclusionReason
        if span.private_declaration:
            exclusion_reason = "private_declaration"
        elif proof_placeholder:
            exclusion_reason = "proof_placeholder"
        elif golden_near_hit:
            exclusion_reason = "golden_blocklist_near_duplicate"
        elif golden_problem_hit:
            exclusion_reason = "golden_blocklist_problem"
        else:
            exclusion_reason = "none"
        evidence_seed = upstream_evidence_seed or hash_canonical(
            {
                "procedure": "git_source_revision_locator_evidence_v1",
                "source": source.source_id,
                "revision": source.revision,
                "file_locator": locator,
            }
        )
        upstream_hash = hash_canonical(
            {
                "procedure": "exact_declaration_upstream_typed_evidence_v1",
                "evidence_kind": upstream_evidence_kind,
                "evidence_seed": evidence_seed,
                "source": source.source_id,
                "revision": source.revision,
                "locator": source_locator,
                "source_text_hash": text_hash,
            }
        )
        yield RootRecord(
            root_id=make_root_id(source.source_id, source.revision, source_locator, text_hash),
            source_id=source.source_id,
            source_revision=source.revision,
            source_locator=source_locator,
            source_text_hash=text_hash,
            signature_text_hash=signature_hash,
            surface_identity_hash=sha256_hex(surface.encode("utf-8")),
            near_identity_hash=sha256_hex(near.encode("utf-8")),
            structure_identity_hash=sha256_hex(structure.encode("utf-8")),
            evidence_level=source.default_evidence_level,
            upstream_evidence_kind=upstream_evidence_kind,
            upstream_typed_evidence_hash=upstream_hash,
            compile_context_available=source.compile_context_bound,
            closed_expr_route_available=source.closed_expr_route_bound,
            current_meta_receipt_hash=None,
            blocklist_screened=True,
            blocklist_file_sha256=GOLDEN_BLOCKLIST_SHA256,
            blocklist_procedure_id=GOLDEN_BLOCKLIST_PROCEDURE_ID,
            golden_near_dup_hash=golden_near_hash,
            private_declaration=span.private_declaration,
            proof_placeholder_detected=proof_placeholder,
            golden_blocklist_hit=golden_hit,
            exclusion_reason=exclusion_reason,
            root_blocklisted=exclusion_reason != "none",
            internal_gate_eligible=source.internal_gate_eligible,
            operation_candidates=_surface_candidates(signature),
            typed_applicable_operations=(),
            n31_proof_status="unknown",
            n31_proof_payload_hash=None,
        )


def _verify_git_source_identity(source: SourceSpec) -> Path:
    checkout = _safe_path(
        Path(source.checkout_or_artifact_path),
        purpose=f"{source.source_id} checkout",
        require_exists=True,
    )
    if not checkout.is_dir():
        raise CensusError(f"git source checkout is not a directory: {source.source_id}")
    try:
        observed = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CensusError(f"cannot verify git source {source.source_id}") from exc
    if observed != source.revision:
        raise CensusError(f"git revision drift for {source.source_id}: {observed}")
    try:
        dirty = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CensusError(f"cannot verify git worktree {source.source_id}") from exc
    if dirty:
        raise CensusError(f"git source worktree is not clean: {source.source_id}")
    return checkout


def iter_git_source(loaded: LoadedCensusConfig, source: SourceSpec) -> Iterator[RootRecord]:
    checkout = _verify_git_source_identity(source)
    for glob in source.globs:
        for path in sorted(checkout.glob(glob)):
            if path.is_file():
                source_path = _safe_path(
                    path,
                    purpose=f"{source.source_id} Lean source file",
                    require_exists=True,
                    require_file=True,
                    containment_root=checkout,
                )
                relative = source_path.relative_to(checkout).as_posix()
                yield from _records_from_lean_text(
                    loaded,
                    source,
                    relative,
                    source_path.read_text(encoding="utf-8", errors="strict"),
                )


def _compiler_validation_evidence_seed(
    source: SourceSpec,
    row_index: int,
    text: str,
    raw_is_valid: bool | None,
    raw_validation: object,
) -> str:
    if raw_is_valid is not True and raw_is_valid is not False and raw_is_valid is not None:
        raise CensusError("compiler_data isValid contains a non-boolean value")
    try:
        validation_bytes = canonical_json_bytes(raw_validation)
    except (TypeError, ValueError) as exc:
        raise CensusError("compiler_data validation contains non-canonical data") from exc
    return hash_canonical(
        {
            "procedure": "compiler_data_exact_validation_evidence_v1",
            "source_revision": source.revision,
            "row_index": row_index,
            "source_code_sha256": sha256_hex(text.encode("utf-8")),
            "isValid": raw_is_valid,
            "validation_sha256": sha256_hex(validation_bytes),
        }
    )


def iter_parquet_source(
    loaded: LoadedCensusConfig, source: SourceSpec, *, batch_size: int = 4096
) -> Iterator[RootRecord]:
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    path = _safe_path(
        Path(source.checkout_or_artifact_path),
        purpose="compiler_data parquet",
        require_exists=True,
        require_file=True,
    )
    if source.artifact_sha256 is None or hash_file(path) != source.artifact_sha256:
        raise CensusError("compiler_data artifact hash drift")
    parquet = pq.ParquetFile(path)
    columns = set(parquet.schema.names)
    if "source_code" not in columns:
        raise CensusError("compiler_data parquet lacks source_code")
    requested = [name for name in ("source_code", "isValid", "validation") if name in columns]
    row_index = 0
    for batch in parquet.iter_batches(columns=requested, batch_size=batch_size):
        payload = batch.to_pydict()
        for index, value in enumerate(payload["source_code"]):
            if not isinstance(value, str):
                raise CensusError("compiler_data source_code contains a non-string value")
            text = value
            raw_is_valid = payload.get("isValid", [None] * batch.num_rows)[index]
            raw_validation = payload.get("validation", [None] * batch.num_rows)[index]
            evidence_seed = _compiler_validation_evidence_seed(
                source, row_index, text, raw_is_valid, raw_validation
            )
            is_valid = raw_is_valid is True
            evidence = "preexisting_typed" if is_valid else "surface_prefilter"
            adjusted = source.model_copy(update={"default_evidence_level": evidence})
            yield from _records_from_lean_text(
                loaded,
                adjusted,
                f"row={row_index}",
                text,
                upstream_evidence_seed=evidence_seed,
                upstream_evidence_kind="compiler_data_validation",
            )
            row_index += 1


def _iter_smoke_manifest_entries(path: Path) -> Iterator[SmokeManifestEntry]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                yield SmokeManifestEntry.model_validate(raw)
            except Exception as exc:
                raise CensusError(f"invalid smoke manifest entry at line {line_number}") from exc


_GIT_LOCATOR_RE = re.compile(r"^(?P<path>.+)#decl=(?P<decl>[0-9]+):(?P<name>.+)$")
_PARQUET_LOCATOR_RE = re.compile(r"^row=(?P<row>[0-9]+)#decl=(?P<decl>[0-9]+):(?P<name>.+)$")


def _matches_frozen_glob(relative: PurePosixPath, pattern: str) -> bool:
    """Mirror Path.glob's zero-or-more-directory semantics for frozen ``**/``."""
    return relative.match(pattern) or relative.match(pattern.replace("**/", ""))


def _read_compiler_row(source: SourceSpec, row_index: int) -> tuple[str, bool | None, object]:
    """Read exactly one pinned Parquet row without scanning the source corpus."""
    import pyarrow.parquet as pq

    path = _safe_path(
        Path(source.checkout_or_artifact_path),
        purpose="compiler_data parquet",
        require_exists=True,
        require_file=True,
    )
    parquet = pq.ParquetFile(path)
    if row_index < 0 or row_index >= parquet.metadata.num_rows:
        raise CensusError("compiler_data smoke locator row is out of range")
    columns = set(parquet.schema.names)
    if "source_code" not in columns:
        raise CensusError("compiler_data parquet lacks source_code")
    requested = [name for name in ("source_code", "isValid", "validation") if name in columns]
    offset = row_index
    for row_group in range(parquet.num_row_groups):
        group_rows = parquet.metadata.row_group(row_group).num_rows
        if offset >= group_rows:
            offset -= group_rows
            continue
        payload = parquet.read_row_group(row_group, columns=requested).slice(offset, 1).to_pydict()
        value = payload["source_code"][0]
        if not isinstance(value, str):
            raise CensusError("compiler_data source_code contains a non-string value")
        raw_is_valid = payload.get("isValid", [None])[0]
        raw_validation = payload.get("validation", [None])[0]
        if raw_is_valid is not True and raw_is_valid is not False and raw_is_valid is not None:
            raise CensusError("compiler_data isValid contains a non-boolean value")
        return value, raw_is_valid, raw_validation
    raise CensusError("compiler_data row-group lookup failed")


def _recompute_smoke_source_root(
    loaded: LoadedCensusConfig,
    root: RootRecord,
    *,
    verified_git: dict[str, Path],
    verified_artifacts: set[str],
) -> RootRecord:
    """Recompute one manifest root from the exact pinned source bytes."""
    source = next(
        (item for item in loaded.config.sources if item.source_id == root.source_id), None
    )
    if source is None:
        raise CensusError("smoke root source is outside the exact source registry")
    if source.kind == "git_lean_source":
        match = _GIT_LOCATOR_RE.fullmatch(root.source_locator)
        if match is None:
            raise CensusError("git smoke root has a malformed source locator")
        relative = PurePosixPath(match.group("path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise CensusError("git smoke root locator escapes its pinned checkout")
        if not any(_matches_frozen_glob(relative, glob) for glob in source.globs):
            raise CensusError("git smoke root locator is outside the frozen source globs")
        if source.source_id not in verified_git:
            verified_git[source.source_id] = _verify_git_source_identity(source)
        checkout = verified_git[source.source_id]
        resolved_checkout = checkout
        source_path = _safe_path(
            resolved_checkout / Path(*relative.parts),
            purpose="git smoke root source file",
            require_exists=True,
            require_file=True,
            containment_root=resolved_checkout,
        )
        records = tuple(
            _records_from_lean_text(
                loaded,
                source,
                relative.as_posix(),
                source_path.read_text(encoding="utf-8", errors="strict"),
            )
        )
        declaration_index = int(match.group("decl"))
    else:
        match = _PARQUET_LOCATOR_RE.fullmatch(root.source_locator)
        if match is None:
            raise CensusError("compiler_data smoke root has a malformed source locator")
        if source.artifact_sha256 is None:
            raise CensusError("compiler_data artifact hash is not frozen")
        if source.source_id not in verified_artifacts:
            artifact_path = _safe_path(
                Path(source.checkout_or_artifact_path),
                purpose="compiler_data parquet",
                require_exists=True,
                require_file=True,
            )
            if hash_file(artifact_path) != source.artifact_sha256:
                raise CensusError("compiler_data artifact hash drift")
            verified_artifacts.add(source.source_id)
        row_index = int(match.group("row"))
        text, raw_is_valid, raw_validation = _read_compiler_row(source, row_index)
        evidence_seed = _compiler_validation_evidence_seed(
            source, row_index, text, raw_is_valid, raw_validation
        )
        evidence: Literal["surface_prefilter", "preexisting_typed"] = (
            "preexisting_typed" if raw_is_valid is True else "surface_prefilter"
        )
        adjusted = source.model_copy(update={"default_evidence_level": evidence})
        records = tuple(
            _records_from_lean_text(
                loaded,
                adjusted,
                f"row={row_index}",
                text,
                upstream_evidence_seed=evidence_seed,
                upstream_evidence_kind="compiler_data_validation",
            )
        )
        declaration_index = int(match.group("decl"))
    if declaration_index >= len(records):
        raise CensusError("smoke root declaration index is out of range")
    recomputed = records[declaration_index]
    if recomputed.source_locator != root.source_locator:
        raise CensusError("smoke root declaration name/index does not replay")
    return recomputed


_SOURCE_AUTHENTICATED_ROOT_FIELDS: tuple[str, ...] = (
    "root_id",
    "source_id",
    "source_revision",
    "source_locator",
    "source_text_hash",
    "signature_text_hash",
    "surface_identity_hash",
    "near_identity_hash",
    "structure_identity_hash",
    "upstream_evidence_kind",
    "upstream_typed_evidence_hash",
    "blocklist_screened",
    "blocklist_file_sha256",
    "blocklist_procedure_id",
    "golden_near_dup_hash",
    "private_declaration",
    "proof_placeholder_detected",
    "golden_blocklist_hit",
    "exclusion_reason",
    "root_blocklisted",
    "internal_gate_eligible",
    "operation_candidates",
)


def _load_finalized_runtime_binding(
    loaded: LoadedCensusConfig,
) -> FinalizedRuntimeSmokeBinding:
    """Load the finalized task-owned runtime without invoking Lean."""
    try:
        from leanfaith.sft1.wave1_live_readiness import (
            assemble_runtime_preamble,
            build_fixture_compile_context,
            load_wave1_live_readiness,
        )

        runtime = load_wave1_live_readiness(loaded.repo_root)
        preamble = assemble_runtime_preamble(loaded.repo_root, runtime.config.source_bindings)
    except (OSError, ValueError) as exc:
        raise CensusError("finalized Wave 1 runtime dependency is unavailable") from exc
    if runtime.config_path != _safe_path(
        loaded.repo_root / RUNTIME_CONFIG_PATH,
        purpose="Wave 1 runtime config",
        require_exists=True,
        require_file=True,
        containment_root=loaded.repo_root,
    ):
        raise CensusError("finalized Wave 1 runtime config path drift")
    source_bindings = {item.role: item for item in runtime.config.source_bindings}
    helper = source_bindings.get("lean_runtime_helper")
    if helper is None:
        raise CensusError("finalized Wave 1 runtime lacks its Lean helper binding")
    loader_path = _safe_path(
        loaded.repo_root / RUNTIME_LOADER_PATH,
        purpose="Wave 1 runtime loader",
        require_exists=True,
        require_file=True,
        containment_root=loaded.repo_root,
    )
    contexts: list[RuntimeCompileContextBinding] = []
    for source_id in EXPECTED_SOURCE_IDS:
        project = next(
            (item for item in runtime.fixtures.project_contexts if item.project_id == source_id),
            None,
        )
        if project is None:
            raise CensusError(f"finalized runtime lacks compile context for {source_id}")
        context = build_fixture_compile_context(project, assembled_preamble=preamble.text)
        contexts.append(
            RuntimeCompileContextBinding(
                source_id=cast(SourceId, source_id),
                compile_context_identity=context.compile_context_id,
                compile_context_fingerprint=context.fingerprint,
            )
        )
    raw_identities = getattr(runtime.config.n31_contract, "admitted_identities", ())
    try:
        admitted = tuple(
            N31AdmittedBankIdentity.model_validate(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            )
            for item in raw_identities
        )
    except Exception as exc:
        raise CensusError("finalized runtime has malformed N31 admitted identities") from exc
    return FinalizedRuntimeSmokeBinding(
        runtime_config_path=cast(
            Literal["configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"],
            RUNTIME_CONFIG_PATH.as_posix(),
        ),
        runtime_config_file_sha256=runtime.config_file_sha256,
        runtime_config_semantic_hash=runtime.config_hash,
        runtime_loader_path=cast(
            Literal["src/leanfaith/sft1/wave1_live_readiness.py"],
            RUNTIME_LOADER_PATH.as_posix(),
        ),
        runtime_loader_file_sha256=hash_file(loader_path),
        runtime_helper_path=cast(Literal["LeanFaith/Meta/SFT1/Wave1Runtime.lean"], helper.path),
        runtime_helper_file_sha256=helper.file_sha256,
        assembled_preamble_sha256=preamble.sha256,
        operations=tuple(
            RuntimeOperationSmokeBinding(
                operation_id=cast(PrimaryOperation, item.operation_id),
                registry_entry_hash=item.registry_entry_hash,
                anchor_hash=item.anchor_hash,
                operation_bank_entry_hash=item.operation_bank_entry_hash,
                runtime_fixture_bundle_hash=item.runtime_fixture_bundle_hash,
                dispatch_symbol=item.dispatch_symbol,
                checker_symbol=item.checker_symbol,
                runtime_status=item.runtime_status,
            )
            for item in runtime.config.operations
        ),
        compile_contexts=tuple(contexts),
        n31_activation_authorized=runtime.config.n31_contract.activation_authorized,
        n31_admitted_identities=admitted,
    )


def _verify_typed_artifact(receipt_path: Path, binding: ArtifactBinding, purpose: str) -> Path:
    relative = PurePosixPath(binding.path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in binding.path:
        raise CensusError(f"{purpose} artifact path must be a contained relative POSIX path")
    artifact = _safe_path(
        receipt_path.parent / Path(*relative.parts),
        purpose=f"{purpose} artifact",
        require_exists=True,
        require_file=True,
        containment_root=receipt_path.parent,
    )
    if artifact.stat().st_size != binding.byte_count or hash_file(artifact) != binding.file_sha256:
        raise CensusError(f"{purpose} artifact byte/hash drift")
    return artifact


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _read_strict_json_object(path: Path, purpose: str) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CensusError(f"invalid {purpose} artifact JSON") from exc
    if not isinstance(value, dict):
        raise CensusError(f"{purpose} artifact must contain one JSON object")
    return value


def _replay_typed_smoke_artifacts(
    typed_receipt: TypedMetaReceipt,
    *,
    raw_path: Path,
    replay_path: Path,
    cache_path: Path,
) -> None:
    """Type and cross-replay the three artifacts named by one smoke Meta receipt."""

    raw_payload = _read_strict_json_object(raw_path, "raw Lean response")
    replay_payload = _read_strict_json_object(replay_path, "typed replay")
    cache_payload = _read_strict_json_object(cache_path, "central cache")
    try:
        raw = RawLeanResponseArtifact.model_validate(raw_payload)
        replay = TypedCertificateReceipt.model_validate(replay_payload)
        cache = CentralCacheEntryArtifact.model_validate(cache_payload)
    except ValueError as exc:
        raise CensusError("typed smoke artifact schema validation failed") from exc

    # These are the canonical serializers used by the backend, replay writer,
    # and central cache respectively. Alternate encodings do not replay.
    expected_raw_bytes = json.dumps(
        raw.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    if raw_path.read_bytes() != expected_raw_bytes:
        raise CensusError("raw Lean response artifact is not canonical backend JSON")
    if replay_path.read_bytes() != canonical_json_bytes(replay.model_dump(mode="json")) + b"\n":
        raise CensusError("typed replay artifact is not canonical JSON")
    if cache_path.read_bytes() != canonical_json_bytes(cache.model_dump(mode="json")) + b"\n":
        raise CensusError("central cache artifact is not canonical JSON")

    if (
        raw.request_hash != typed_receipt.meta_request_hash
        or raw.request_hash != typed_receipt.render_request_hash
        or raw.request.context_id != typed_receipt.compile_context_identity
        or raw.request.code is None
        or raw.request.file_path is not None
        or raw.request.allow_sorry
        or raw.response is None
        or raw.error is not None
        or raw.request.code.count("LeanFaith.GoalV1.emitClosedProp") != 2
    ):
        raise CensusError("raw Lean response does not replay the exact same-request Meta route")

    replay_hash = hash_canonical(replay.model_dump(mode="json"))
    if (
        replay.operation_id != typed_receipt.selected_operation_id
        or replay.source_closed_expr_hash != typed_receipt.closed_expr_hash
        or replay.candidate_closed_expr_hash != typed_receipt.candidate_closed_expr_hash
        or replay.source_sidecar_sha256 != typed_receipt.source_sidecar_sha256
        or replay.candidate_sidecar_sha256 != typed_receipt.candidate_sidecar_sha256
        or replay.render_request_hash != typed_receipt.render_request_hash
        or replay_hash != typed_receipt.typed_certificate_payload_hash
    ):
        raise CensusError("typed replay artifact does not cross-link to the Meta receipt")

    key = cache.cache_key
    evidence = cache.evidence
    audit = evidence.value
    raw_artifact_path = str(raw_path.resolve())
    replay_artifact_path = str(replay_path.resolve())
    required_checks = (
        "typed_meta_validation",
        "typed_certificate_replay",
        "same_request_repr",
        "sidecars_persisted",
    )
    if (
        cache.cache_key_hash != typed_receipt.central_cache_key_hash
        or key.theorem_a_statement_hash != typed_receipt.closed_expr_hash
        or key.theorem_b_statement_hash != typed_receipt.candidate_closed_expr_hash
        or key.representation_a_content_hash != typed_receipt.source_sidecar_sha256
        or key.representation_b_content_hash != typed_receipt.candidate_sidecar_sha256
        or key.context_id != typed_receipt.compile_context_identity
        or key.context_fingerprint != typed_receipt.compile_context_fingerprint
        or key.config_hash != typed_receipt.runtime_config_semantic_hash
        or key.semantic_policy_hash != typed_receipt.operation_registry_entry_hash
        or cache.certificate_dependency_hash != typed_receipt.typed_certificate_payload_hash
        or typed_receipt.render_request_hash not in cache.lean_request_hashes
        or cache.artifact_hashes.get(raw_artifact_path)
        != typed_receipt.raw_lean_response_artifact.file_sha256
        or cache.artifact_hashes.get(replay_artifact_path)
        != typed_receipt.typed_replay_artifact.file_sha256
        or not {
            typed_receipt.source_sidecar_sha256,
            typed_receipt.candidate_sidecar_sha256,
        }.issubset(set(cache.artifact_hashes.values()))
        or not isinstance(audit, AuditValue)
        or any(audit.checks.get(check) is not True for check in required_checks)
        or audit.violation_codes
        or audit.detail_artifact != replay_artifact_path
        or evidence.raw_artifact != raw_artifact_path
        or evidence.metadata.get("typed_replay_artifact_sha256")
        != typed_receipt.typed_replay_artifact.file_sha256
        or evidence.metadata.get("raw_artifact_sha256")
        != typed_receipt.raw_lean_response_artifact.file_sha256
    ):
        raise CensusError("central cache artifact does not replay the Meta and certificate chain")


def _authenticate_smoke_entry(
    loaded: LoadedCensusConfig,
    manifest_path: Path,
    entry: SmokeManifestEntry,
    *,
    verified_git: dict[str, Path],
    verified_artifacts: set[str],
) -> TypedMetaReceipt:
    recomputed = _recompute_smoke_source_root(
        loaded,
        entry.root,
        verified_git=verified_git,
        verified_artifacts=verified_artifacts,
    )
    for field in _SOURCE_AUTHENTICATED_ROOT_FIELDS:
        if getattr(entry.root, field) != getattr(recomputed, field):
            raise CensusError(f"smoke root source authentication drift: {field}")
    if recomputed.evidence_level == "surface_prefilter":
        raise CensusError("smoke root lacks pinned upstream typed evidence")
    receipt_relative = PurePosixPath(entry.typed_meta_receipt_path)
    if receipt_relative.is_absolute() or ".." in receipt_relative.parts:
        raise CensusError("smoke typed Meta receipt path must be relative and contained")
    receipt_path = _safe_path(
        manifest_path.parent / Path(*receipt_relative.parts),
        purpose="smoke typed Meta receipt",
        require_exists=True,
        require_file=True,
        containment_root=manifest_path.parent,
    )
    if hash_file(receipt_path) != entry.typed_meta_receipt_sha256:
        raise CensusError("smoke typed Meta receipt path/hash drift")
    try:
        typed_receipt = TypedMetaReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise CensusError("invalid typed Meta receipt") from exc
    expected_receipt_fields = {
        "root_id": entry.root.root_id,
        "source_id": entry.root.source_id,
        "source_revision": entry.root.source_revision,
        "source_locator": entry.root.source_locator,
        "source_text_hash": entry.root.source_text_hash,
        "signature_text_hash": entry.root.signature_text_hash,
        "selected_operation_id": entry.selection_operation_id,
        "typed_applicable_operations": (entry.selection_operation_id,),
    }
    for field, expected in expected_receipt_fields.items():
        if getattr(typed_receipt, field) != expected:
            raise CensusError(f"typed Meta receipt/root drift: {field}")
    runtime = _load_finalized_runtime_binding(loaded)
    operation = next(
        (item for item in runtime.operations if item.operation_id == entry.selection_operation_id),
        None,
    )
    context = next(
        (item for item in runtime.compile_contexts if item.source_id == entry.root.source_id), None
    )
    if operation is None or context is None:
        raise CensusError("selected smoke operation/context is absent from finalized runtime")
    exact_runtime_fields = {
        "runtime_config_path": runtime.runtime_config_path,
        "runtime_config_file_sha256": runtime.runtime_config_file_sha256,
        "runtime_config_semantic_hash": runtime.runtime_config_semantic_hash,
        "runtime_loader_path": runtime.runtime_loader_path,
        "runtime_loader_file_sha256": runtime.runtime_loader_file_sha256,
        "runtime_helper_path": runtime.runtime_helper_path,
        "runtime_helper_file_sha256": runtime.runtime_helper_file_sha256,
        "assembled_preamble_sha256": runtime.assembled_preamble_sha256,
        "operation_registry_entry_hash": operation.registry_entry_hash,
        "operation_anchor_hash": operation.anchor_hash,
        "operation_bank_entry_hash": operation.operation_bank_entry_hash,
        "runtime_fixture_bundle_hash": operation.runtime_fixture_bundle_hash,
        "dispatch_symbol": operation.dispatch_symbol,
        "checker_symbol": operation.checker_symbol,
        "compile_context_identity": context.compile_context_identity,
        "compile_context_fingerprint": context.compile_context_fingerprint,
    }
    for field, expected in exact_runtime_fields.items():
        if getattr(typed_receipt, field) != expected:
            raise CensusError(f"typed Meta receipt/finalized runtime drift: {field}")
    if entry.selection_operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1":
        identity = typed_receipt.n31_admitted_bank_identity
        if (
            not runtime.n31_activation_authorized
            or operation.runtime_status == "n31_resolution_proposal_only_not_admitted"
            or identity is None
            or identity not in runtime.n31_admitted_identities
        ):
            raise CensusError("N31 smoke receipt is proposal-only or lacks exact user admission")
    elif operation.runtime_status == "n31_resolution_proposal_only_not_admitted":
        raise CensusError("positive smoke operation has a proposal-only runtime binding")
    artifact_paths = (
        _verify_typed_artifact(
            receipt_path, typed_receipt.raw_lean_response_artifact, "raw Lean response"
        ),
        _verify_typed_artifact(receipt_path, typed_receipt.typed_replay_artifact, "typed replay"),
        _verify_typed_artifact(receipt_path, typed_receipt.central_cache_artifact, "central cache"),
    )
    _reject_path_aliases(
        {
            "smoke manifest": manifest_path,
            "typed Meta receipt": receipt_path,
            "raw Lean response": artifact_paths[0],
            "typed replay": artifact_paths[1],
            "cache": artifact_paths[2],
        }
    )
    _replay_typed_smoke_artifacts(
        typed_receipt,
        raw_path=artifact_paths[0],
        replay_path=artifact_paths[1],
        cache_path=artifact_paths[2],
    )
    return typed_receipt


def iter_authenticated_smoke_manifest(
    loaded: LoadedCensusConfig, path: Path
) -> Iterator[SmokeManifestEntry]:
    """Yield only source-recomputed, exact-Meta-authenticated smoke entries."""
    resolved = _safe_path(
        path,
        purpose="smoke manifest",
        require_exists=True,
        require_file=True,
    )
    entries = tuple(_iter_smoke_manifest_entries(resolved))
    expected_operations = (
        "P01_ALPHA_RENAME_SINGLE_V1",
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    )
    if tuple(entry.selection_operation_id for entry in entries) != expected_operations:
        raise CensusError("smoke manifest requires exactly one ordered P01 and one N31 entry")
    verified_git: dict[str, Path] = {}
    verified_artifacts: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.root.root_id, entry.selection_operation_id)
        if key in seen:
            raise CensusError("duplicate smoke root/operation manifest entry")
        seen.add(key)
        _authenticate_smoke_entry(
            loaded,
            resolved,
            entry,
            verified_git=verified_git,
            verified_artifacts=verified_artifacts,
        )
    # Do not expose a prefix of an invalid manifest to callers: authenticate
    # the complete exact inventory before yielding either entry.
    yield from entries


@dataclass(frozen=True, slots=True)
class StateBinding:
    config_file_sha256: str
    config_semantic_hash: str
    implementation_source_sha256: str
    runtime_git_commit: str
    evaluation_blocklist_file_sha256: str
    evaluation_blocklist_procedure_id: str
    journal_path: str
    route_kind: Literal[
        "smoke_manifest",
        "bounded_sampling_frame_scan",
        "complete_streaming_source_scan",
    ]
    route_input_hash: str
    route_id: str

    def metadata(self) -> dict[str, str]:
        return {
            "state_schema_version": "wave1_census_sqlite_v4",
            "config_file_sha256": self.config_file_sha256,
            "config_semantic_hash": self.config_semantic_hash,
            "implementation_source_sha256": self.implementation_source_sha256,
            "runtime_git_commit": self.runtime_git_commit,
            "evaluation_blocklist_file_sha256": self.evaluation_blocklist_file_sha256,
            "evaluation_blocklist_procedure_id": self.evaluation_blocklist_procedure_id,
            "journal_path": self.journal_path,
            "route_kind": self.route_kind,
            "route_input_hash": self.route_input_hash,
            "route_id": self.route_id,
        }


def make_state_binding(
    loaded: LoadedCensusConfig,
    tier: Tier,
    manifest_sha256: str | None,
    journal_path: Path,
    *,
    runtime_git_commit: str | None = None,
    require_clean_current_head: bool = False,
    allowed_dirty_paths: Sequence[Path] = (),
) -> StateBinding:
    if tier == "smoke":
        if manifest_sha256 is None:
            raise CensusError("smoke SQLite state requires the exact manifest hash")
        route_kind: Literal[
            "smoke_manifest",
            "bounded_sampling_frame_scan",
            "complete_streaming_source_scan",
        ] = "smoke_manifest"
        route_input_hash = manifest_sha256
    elif tier == "selected_wave":
        if manifest_sha256 is not None:
            raise CensusError("sampling-frame SQLite state cannot bind a smoke manifest")
        route_kind = "bounded_sampling_frame_scan"
        route_input_hash = hash_canonical(
            {
                "procedure": "deterministic_bounded_sampling_frame_scan_route_v1",
                "sources": [source.model_dump(mode="json") for source in loaded.config.sources],
                "surface_procedure": loaded.config.surface_procedure.model_dump(mode="json"),
                "tier": loaded.config.tiers.selected_wave.model_dump(mode="json"),
            }
        )
    else:
        if manifest_sha256 is not None:
            raise CensusError("complete-streaming SQLite state cannot bind a smoke manifest")
        route_kind = "complete_streaming_source_scan"
        route_input_hash = hash_canonical(
            {
                "procedure": "complete_streaming_source_scan_route_v1",
                "sources": [source.model_dump(mode="json") for source in loaded.config.sources],
                "surface_procedure": loaded.config.surface_procedure.model_dump(mode="json"),
                "tier": loaded.config.tiers.full_cross_source.model_dump(mode="json"),
            }
        )
    if runtime_git_commit is not None:
        if require_clean_current_head:
            raise CensusError("recorded runtime commit and clean-current binding are exclusive")
        _verify_recorded_runtime_commit(loaded, runtime_git_commit)
        runtime_commit = runtime_git_commit
    elif require_clean_current_head:
        runtime_commit = _bind_clean_runtime_commit(
            loaded,
            allowed_dirty_paths=allowed_dirty_paths,
        )
    else:
        runtime_commit = _current_runtime_commit(loaded.repo_root)
    resolved_journal = str(
        _safe_path(
            journal_path,
            purpose="census journal",
            require_exists=False,
        )
    )
    route_id = hash_canonical(
        {
            "procedure": "sft1_wave1_census_state_route_v1",
            "config_file_sha256": loaded.config_file_sha256,
            "config_semantic_hash": loaded.config_hash,
            "implementation_source_sha256": (
                loaded.config.implementation_binding.implementation_source_sha256
            ),
            "runtime_git_commit": runtime_commit,
            "evaluation_blocklist_file_sha256": GOLDEN_BLOCKLIST_SHA256,
            "evaluation_blocklist_procedure_id": GOLDEN_BLOCKLIST_PROCEDURE_ID,
            "journal_path": resolved_journal,
            "route_kind": route_kind,
            "route_input_hash": route_input_hash,
        }
    )
    return StateBinding(
        config_file_sha256=loaded.config_file_sha256,
        config_semantic_hash=loaded.config_hash,
        implementation_source_sha256=(
            loaded.config.implementation_binding.implementation_source_sha256
        ),
        runtime_git_commit=runtime_commit,
        evaluation_blocklist_file_sha256=GOLDEN_BLOCKLIST_SHA256,
        evaluation_blocklist_procedure_id=GOLDEN_BLOCKLIST_PROCEDURE_ID,
        journal_path=resolved_journal,
        route_kind=route_kind,
        route_input_hash=route_input_hash,
        route_id=route_id,
    )


def _root_state_row(root: RootRecord) -> tuple[object, ...]:
    operations = set(root.operation_candidates)
    typed = set(root.typed_applicable_operations)
    return (
        root.root_id,
        root.source_id,
        root.source_revision,
        root.source_locator,
        root.source_text_hash,
        root.signature_text_hash,
        root.upstream_evidence_kind,
        root.upstream_typed_evidence_hash,
        int(root.compile_context_available),
        int(root.closed_expr_route_available),
        root.current_meta_receipt_hash,
        root.evidence_level,
        root.surface_identity_hash,
        root.near_identity_hash,
        root.structure_identity_hash,
        int(root.blocklist_screened),
        root.blocklist_file_sha256,
        root.blocklist_procedure_id,
        root.golden_near_dup_hash,
        int(root.private_declaration),
        int(root.proof_placeholder_detected),
        int(root.golden_blocklist_hit),
        root.exclusion_reason,
        int(root.root_blocklisted),
        int(root.internal_gate_eligible),
        int("P01_ALPHA_RENAME_SINGLE_V1" in operations),
        int("P15_SWAP_IFF_SIDES_V1" in operations),
        int("P18_SYMMETRIZE_EQUALITY_V1" in operations),
        int("P21_BETA_REDUCE_V1" in operations),
        int("N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in operations),
        (root.n31_proof_status if "N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in operations else "na"),
        root.n31_proof_payload_hash,
        int("P01_ALPHA_RENAME_SINGLE_V1" in typed),
        int("P15_SWAP_IFF_SIDES_V1" in typed),
        int("P18_SYMMETRIZE_EQUALITY_V1" in typed),
        int("P21_BETA_REDUCE_V1" in typed),
        int("N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in typed),
    )


class CensusState:
    """Persistent, deduplicating census accumulator."""

    def __init__(self, path: Path, binding: StateBinding, *, create: bool = True) -> None:
        self.path = _safe_path(
            path,
            purpose="census SQLite state",
            require_exists=not create,
            require_file=not create,
        )
        self.binding = binding
        if not create and not self.path.is_file():
            raise CensusError("bound SQLite state file does not exist")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path = _safe_path(
                self.path,
                purpose="census SQLite state",
                require_exists=False,
            )
            self.connection = sqlite3.connect(self.path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=60000")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS state_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS roots (
                root_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
                source_locator TEXT NOT NULL, source_text_hash TEXT NOT NULL,
                signature_text_hash TEXT NOT NULL,
                upstream_evidence_kind TEXT NOT NULL, upstream_typed_evidence_hash TEXT,
                compile_context INTEGER NOT NULL,
                closed_expr_route INTEGER NOT NULL, current_meta_receipt_hash TEXT,
                evidence_level TEXT NOT NULL, surface_hash TEXT NOT NULL, near_hash TEXT NOT NULL,
                structure_hash TEXT NOT NULL,
                blocklist_screened INTEGER NOT NULL, blocklist_file_sha256 TEXT NOT NULL,
                blocklist_procedure_id TEXT NOT NULL, golden_near_dup_hash TEXT NOT NULL,
                private_declaration INTEGER NOT NULL, proof_placeholder INTEGER NOT NULL,
                golden_blocklist_hit INTEGER NOT NULL, exclusion_reason TEXT NOT NULL,
                blocklisted INTEGER NOT NULL, internal_gate INTEGER NOT NULL,
                p01 INTEGER NOT NULL, p15 INTEGER NOT NULL, p18 INTEGER NOT NULL,
                p21 INTEGER NOT NULL, n31 INTEGER NOT NULL, n31_proof_status TEXT NOT NULL,
                n31_proof_payload_hash TEXT, typed_p01 INTEGER NOT NULL,
                typed_p15 INTEGER NOT NULL, typed_p18 INTEGER NOT NULL,
                typed_p21 INTEGER NOT NULL, typed_n31 INTEGER NOT NULL)"""
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS completed_sources (source_id TEXT PRIMARY KEY)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS completion_markers "
                "(marker TEXT PRIMARY KEY CHECK(marker='route_complete'))"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS smoke_manifest_operations "
                "(root_id TEXT NOT NULL, operation_id TEXT NOT NULL, "
                "meta_receipt_hash TEXT NOT NULL, "
                "PRIMARY KEY(root_id, operation_id))"
            )
        else:
            self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            observed = dict(self.connection.execute("SELECT key, value FROM state_metadata"))
        except sqlite3.Error as exc:
            self.connection.close()
            raise CensusError("SQLite state schema/binding metadata is missing") from exc
        expected = binding.metadata()
        if observed and observed != expected:
            self.connection.close()
            raise CensusError("SQLite state binding differs from config or census route")
        if not observed:
            if not create:
                self.connection.close()
                raise CensusError("SQLite state has no binding metadata")
            self.connection.executemany(
                "INSERT INTO state_metadata(key, value) VALUES (?, ?)", expected.items()
            )
        self.connection.commit()

    def add(self, root: RootRecord) -> None:
        row = _root_state_row(root)
        existing = self.connection.execute(
            "SELECT * FROM roots WHERE root_id=?", (root.root_id,)
        ).fetchone()
        if existing is not None:
            if tuple(existing) != row:
                raise CensusError("root_id conflict: stored evidence row differs")
            return
        try:
            self.connection.execute(
                f"INSERT INTO roots VALUES ({','.join('?' for _ in row)})",
                row,
            )
        except sqlite3.IntegrityError as exc:
            concurrent = self.connection.execute(
                "SELECT * FROM roots WHERE root_id=?", (root.root_id,)
            ).fetchone()
            if concurrent is None or tuple(concurrent) != row:
                raise CensusError("root_id conflict: stored evidence row differs") from exc

    def mark_complete(self, source_id: str) -> None:
        if source_id not in EXPECTED_SOURCE_IDS:
            raise CensusError("completed source is outside the exact source registry")
        if self.binding.route_kind == "smoke_manifest":
            present = self.connection.execute(
                "SELECT 1 FROM roots WHERE source_id=? LIMIT 1", (source_id,)
            ).fetchone()
            if present is None:
                raise CensusError("smoke cannot mark an absent source complete")
        self.connection.execute(
            "INSERT OR REPLACE INTO completed_sources(source_id) VALUES (?)", (source_id,)
        )
        self.connection.commit()

    def add_smoke_manifest_operation(
        self, root_id: str, operation_id: str, meta_receipt_hash: str
    ) -> None:
        if self.binding.route_kind != "smoke_manifest":
            raise CensusError("smoke manifest operation cannot enter a non-smoke state")
        if operation_id not in (
            "P01_ALPHA_RENAME_SINGLE_V1",
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        ):
            raise CensusError("smoke manifest operation is outside exact P01/N31 scope")
        existing = self.connection.execute(
            "SELECT meta_receipt_hash FROM smoke_manifest_operations "
            "WHERE root_id=? AND operation_id=?",
            (root_id, operation_id),
        ).fetchone()
        if existing is not None and str(existing[0]) != meta_receipt_hash:
            raise CensusError("smoke manifest operation receipt conflict")
        self.connection.execute(
            "INSERT OR IGNORE INTO smoke_manifest_operations VALUES (?, ?, ?)",
            (root_id, operation_id, meta_receipt_hash),
        )

    def is_complete(self, source_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM completed_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        return row is not None

    def mark_route_complete(self) -> None:
        completed = {
            str(row[0])
            for row in self.connection.execute("SELECT source_id FROM completed_sources")
        }
        if self.binding.route_kind == "smoke_manifest":
            required = {
                str(row[0])
                for row in self.connection.execute("SELECT DISTINCT source_id FROM roots")
            }
            operations = {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT DISTINCT operation_id FROM smoke_manifest_operations"
                )
            }
            if operations != {
                "P01_ALPHA_RENAME_SINGLE_V1",
                "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            }:
                raise CensusError("smoke route requires exact authenticated P01 and N31 entries")
        else:
            required = set(EXPECTED_SOURCE_IDS)
        if not required or completed != required:
            raise CensusError("route completion requires its exact completed-source set")
        self.connection.execute(
            "INSERT OR REPLACE INTO completion_markers(marker) VALUES ('route_complete')"
        )
        self.connection.commit()

    def route_complete(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM completion_markers WHERE marker='route_complete'"
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()


def _verify_smoke_state_matches_manifest(
    state: CensusState, entries: Sequence[SmokeManifestEntry]
) -> None:
    expected_roots = {entry.root.root_id: _root_state_row(entry.root) for entry in entries}
    observed_roots = {
        str(row[0]): tuple(row) for row in state.connection.execute("SELECT * FROM roots")
    }
    if observed_roots != expected_roots:
        raise CensusError("smoke SQLite roots differ from the authenticated manifest")
    expected_operations = {
        (entry.root.root_id, entry.selection_operation_id, entry.typed_meta_receipt_sha256)
        for entry in entries
    }
    observed_operations = {
        tuple(str(value) for value in row)
        for row in state.connection.execute(
            "SELECT root_id, operation_id, meta_receipt_hash FROM smoke_manifest_operations"
        )
    }
    if observed_operations != expected_operations:
        raise CensusError("smoke SQLite operation evidence differs from the manifest")


_OP_COLUMNS = {
    "P01_ALPHA_RENAME_SINGLE_V1": "p01",
    "P15_SWAP_IFF_SIDES_V1": "p15",
    "P18_SYMMETRIZE_EQUALITY_V1": "p18",
    "P21_BETA_REDUCE_V1": "p21",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1": "n31",
}


def _one(connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> int:
    row = connection.execute(sql, parameters).fetchone()
    assert row is not None
    return int(row[0])


def _fixed_evidence(connection: sqlite3.Connection, source_id: str | None) -> FixedEvidenceCounts:
    suffix = "" if source_id is None else " WHERE source_id=?"
    parameters: tuple[object, ...] = () if source_id is None else (source_id,)
    counts = dict.fromkeys(("surface_prefilter", "preexisting_typed", "typed_pending", "typed"), 0)
    for level, count in connection.execute(
        f"SELECT evidence_level, COUNT(*) FROM roots{suffix} GROUP BY evidence_level", parameters
    ):
        counts[str(level)] = int(count)
    return FixedEvidenceCounts.model_validate(counts)


def _exclusion_counts(connection: sqlite3.Connection, source_id: str | None) -> ExclusionCounts:
    suffix = "" if source_id is None else " WHERE source_id=?"
    parameters: tuple[object, ...] = () if source_id is None else (source_id,)
    counts = dict.fromkeys(
        (
            "private_declaration",
            "proof_placeholder",
            "golden_blocklist_near_duplicate",
            "golden_blocklist_problem",
        ),
        0,
    )
    for reason, count in connection.execute(
        f"SELECT exclusion_reason, COUNT(*) FROM roots{suffix} "
        "AND exclusion_reason!='none' GROUP BY exclusion_reason"
        if suffix
        else "SELECT exclusion_reason, COUNT(*) FROM roots "
        "WHERE exclusion_reason!='none' GROUP BY exclusion_reason",
        parameters,
    ):
        if str(reason) not in counts:
            raise CensusError("SQLite state has an unknown exclusion reason")
        counts[str(reason)] = int(count)
    return ExclusionCounts.model_validate(counts)


def _fixed_operations(
    connection: sqlite3.Connection, source_id: str | None, *, typed: bool = False
) -> FixedOperationCounts:
    where = "internal_gate=1 AND blocklisted=0"
    parameters: tuple[object, ...] = ()
    if source_id is not None:
        where += " AND source_id=?"
        parameters = (source_id,)
    return FixedOperationCounts.model_validate(
        {
            operation: _one(
                connection,
                f"SELECT COUNT(*) FROM roots WHERE {where} "
                f"AND {'typed_' if typed else ''}{column}=1",
                parameters,
            )
            for operation, column in _OP_COLUMNS.items()
        }
    )


def _n31_counts(connection: sqlite3.Connection, source_id: str | None) -> N31ProofCounts:
    where = "internal_gate=1 AND blocklisted=0 AND n31=1"
    parameters: tuple[object, ...] = ()
    if source_id is not None:
        where += " AND source_id=?"
        parameters = (source_id,)
    parent = _one(connection, f"SELECT COUNT(*) FROM roots WHERE {where}", parameters)
    statuses = {"available": 0, "unavailable": 0, "unknown": 0}
    for status, count in connection.execute(
        f"SELECT n31_proof_status, COUNT(*) FROM roots WHERE {where} GROUP BY n31_proof_status",
        parameters,
    ):
        statuses[str(status)] = int(count)
    return N31ProofCounts(
        parent_root_count=parent,
        available=statuses["available"],
        unavailable=statuses["unavailable"],
        unknown=statuses["unknown"],
        independent_root_pool_count=0,
        activation_authorized=False,
    )


def _candidate_set_hash(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for (root_id,) in connection.execute("SELECT root_id FROM roots ORDER BY root_id"):
        encoded = str(root_id).encode("ascii")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _state_evidence_hash(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    queries = (
        ("state_metadata", "SELECT key, value FROM state_metadata ORDER BY key"),
        ("completed_sources", "SELECT source_id FROM completed_sources ORDER BY source_id"),
        ("completion_markers", "SELECT marker FROM completion_markers ORDER BY marker"),
        (
            "smoke_manifest_operations",
            "SELECT root_id, operation_id, meta_receipt_hash FROM smoke_manifest_operations "
            "ORDER BY operation_id, root_id",
        ),
        ("roots", "SELECT * FROM roots ORDER BY root_id"),
    )
    for table, query in queries:
        table_bytes = table.encode("ascii")
        digest.update(len(table_bytes).to_bytes(8, "big"))
        digest.update(table_bytes)
        for row in connection.execute(query):
            encoded = canonical_json_bytes(list(row))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _validate_state_rows(loaded: LoadedCensusConfig, state: CensusState) -> None:
    connection = state.connection

    def reject_if_any(sql: str, message: str) -> None:
        if _one(connection, f"SELECT COUNT(*) FROM roots WHERE {sql}"):
            raise CensusError(message)

    completed = {
        str(row[0]) for row in connection.execute("SELECT source_id FROM completed_sources")
    }
    if not completed.issubset(EXPECTED_SOURCE_IDS):
        raise CensusError("SQLite state has a completed source outside the registry")
    expected_revisions = {source.source_id: source.revision for source in loaded.config.sources}
    observed_pairs = {
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT DISTINCT source_id, source_revision FROM roots")
    }
    if any(
        source_id not in expected_revisions or expected_revisions[source_id] != revision
        for source_id, revision in observed_pairs
    ):
        raise CensusError("SQLite root source/revision differs from the exact source registry")
    if state.route_complete():
        observed_sources = {source_id for source_id, _revision in observed_pairs}
        required_completed = (
            observed_sources
            if state.binding.route_kind == "smoke_manifest"
            else set(EXPECTED_SOURCE_IDS)
        )
        if completed != required_completed:
            raise CensusError("SQLite route completion/source set is incoherent")
    smoke_rows = tuple(
        connection.execute(
            "SELECT root_id, operation_id, meta_receipt_hash FROM smoke_manifest_operations"
        )
    )
    if state.binding.route_kind != "smoke_manifest" and smoke_rows:
        raise CensusError("non-smoke SQLite state contains smoke manifest entries")
    if (
        state.route_complete()
        and state.binding.route_kind == "smoke_manifest"
        and {str(row[1]) for row in smoke_rows}
        != {
            "P01_ALPHA_RENAME_SINGLE_V1",
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        }
    ):
        raise CensusError("completed smoke state lacks exact P01/N31 typed admissions")
    for root_id, operation_id, meta_receipt_hash in smoke_rows:
        column = {
            "P01_ALPHA_RENAME_SINGLE_V1": "typed_p01",
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1": "typed_n31",
        }.get(str(operation_id))
        if column is None:
            raise CensusError("SQLite smoke manifest operation drift")
        matched = connection.execute(
            f"SELECT 1 FROM roots WHERE root_id=? AND current_meta_receipt_hash=? AND {column}=1",
            (root_id, meta_receipt_hash),
        ).fetchone()
        if matched is None:
            raise CensusError("SQLite smoke entry lacks its exact typed Meta evidence")
    reject_if_any(
        "evidence_level NOT IN ('surface_prefilter','preexisting_typed','typed_pending','typed')",
        "SQLite root has an unknown evidence level",
    )
    reject_if_any(
        "(evidence_level='surface_prefilter' AND "
        "(upstream_evidence_kind!='none' OR upstream_typed_evidence_hash IS NOT NULL)) OR "
        "(evidence_level!='surface_prefilter' AND "
        "(upstream_evidence_kind='none' OR upstream_typed_evidence_hash IS NULL))",
        "SQLite upstream typed-evidence discipline drift",
    )
    reject_if_any(
        "(source_id='compiler_data' AND evidence_level!='surface_prefilter' "
        "AND upstream_evidence_kind!='compiler_data_validation') OR "
        "(source_id!='compiler_data' AND evidence_level!='surface_prefilter' "
        "AND upstream_evidence_kind!='git_source_declaration')",
        "SQLite upstream evidence source route drift",
    )
    reject_if_any(
        "(evidence_level IN ('typed_pending','typed') "
        "AND (compile_context!=1 OR closed_expr_route!=1))",
        "SQLite typed-pending/typed route availability drift",
    )
    reject_if_any(
        "(evidence_level='typed' AND current_meta_receipt_hash IS NULL) OR "
        "(evidence_level!='typed' AND current_meta_receipt_hash IS NOT NULL)",
        "SQLite current Meta receipt discipline drift",
    )
    reject_if_any(
        "private_declaration NOT IN (0,1) OR proof_placeholder NOT IN (0,1) OR "
        "golden_blocklist_hit NOT IN (0,1) OR blocklisted NOT IN (0,1) OR "
        "compile_context NOT IN (0,1) OR closed_expr_route NOT IN (0,1) OR "
        "internal_gate!=1 OR p01 NOT IN (0,1) OR p15 NOT IN (0,1) OR "
        "p18 NOT IN (0,1) OR p21 NOT IN (0,1) OR n31 NOT IN (0,1) OR "
        "typed_p01 NOT IN (0,1) OR typed_p15 NOT IN (0,1) OR "
        "typed_p18 NOT IN (0,1) OR typed_p21 NOT IN (0,1) OR typed_n31 NOT IN (0,1)",
        "SQLite root has a non-boolean evidence axis",
    )
    reject_if_any(
        "blocklist_screened!=1 OR "
        f"blocklist_file_sha256!='{GOLDEN_BLOCKLIST_SHA256}' OR "
        f"blocklist_procedure_id!='{GOLDEN_BLOCKLIST_PROCEDURE_ID}' OR "
        "length(golden_near_dup_hash)!=64 OR "
        "golden_near_dup_hash GLOB '*[^0-9a-f]*'",
        "SQLite root-level blocklist evidence drift",
    )
    reject_if_any(
        "(private_declaration=1 AND exclusion_reason!='private_declaration') OR "
        "(private_declaration=0 AND proof_placeholder=1 "
        "AND exclusion_reason!='proof_placeholder') OR "
        "(private_declaration=0 AND proof_placeholder=0 AND golden_blocklist_hit=1 "
        "AND exclusion_reason NOT IN "
        "('golden_blocklist_near_duplicate','golden_blocklist_problem')) OR "
        "(private_declaration=0 AND proof_placeholder=0 AND golden_blocklist_hit=0 "
        "AND exclusion_reason!='none') OR "
        "(blocklisted=1 AND exclusion_reason='none') OR "
        "(blocklisted=0 AND exclusion_reason!='none')",
        "SQLite deterministic exclusion discipline drift",
    )
    typed_any = "(typed_p01=1 OR typed_p15=1 OR typed_p18=1 OR typed_p21=1 OR typed_n31=1)"
    reject_if_any(
        f"evidence_level!='typed' AND {typed_any}",
        "SQLite non-typed root claims typed operation applicability",
    )
    reject_if_any(
        "typed_p01>p01 OR typed_p15>p15 OR typed_p18>p18 OR typed_p21>p21 OR typed_n31>n31",
        "SQLite typed applicability does not refine surface candidacy",
    )
    reject_if_any(
        "(n31=0 AND (n31_proof_status!='na' OR n31_proof_payload_hash IS NOT NULL)) OR "
        "(n31=1 AND n31_proof_status NOT IN ('unknown','unavailable','available')) OR "
        "(n31=1 AND n31_proof_status='available' AND n31_proof_payload_hash IS NULL) OR "
        "(n31=1 AND n31_proof_status!='available' AND n31_proof_payload_hash IS NOT NULL)",
        "SQLite N31 proof route is not nested/coherent",
    )
    for root_id, source_id, revision, locator, text_hash in connection.execute(
        "SELECT root_id, source_id, source_revision, source_locator, source_text_hash "
        "FROM roots ORDER BY root_id"
    ):
        if str(root_id) != make_root_id(
            str(source_id), str(revision), str(locator), str(text_hash)
        ):
            raise CensusError("SQLite root_id does not replay from stored provenance")


def _signature_strata(
    connection: sqlite3.Connection, source_id: str | None
) -> SignatureStrataCounts:
    where = " WHERE internal_gate=1 AND blocklisted=0"
    parameters: tuple[object, ...] = () if source_id is None else (source_id,)
    if source_id is not None:
        where += " AND source_id=?"

    def count(extra: str) -> int:
        separator = " AND "
        return _one(connection, f"SELECT COUNT(*) FROM roots{where}{separator}{extra}", parameters)

    return SignatureStrataCounts(
        explicit_binder_surface_candidate_count=count("p01=1"),
        iff_surface_candidate_count=count("p15=1"),
        equality_surface_candidate_count=count("p18=1"),
        beta_redex_surface_candidate_count=count("p21=1"),
        required_guard_surface_candidate_count=count("n31=1"),
        other_surface_root_count=count("p01=0 AND p15=0 AND p18=0 AND p21=0 AND n31=0"),
    )


def _route_availability(connection: sqlite3.Connection, source: SourceSpec) -> RouteAvailability:
    parameters = (source.source_id,)
    typed_any = "(typed_p01=1 OR typed_p15=1 OR typed_p18=1 OR typed_p21=1 OR typed_n31=1)"
    return RouteAvailability(
        expected_closed_expr_route=source.closed_expr_route,
        compile_context_available_count=_one(
            connection,
            "SELECT COUNT(*) FROM roots WHERE source_id=? AND compile_context=1",
            parameters,
        ),
        closed_expr_route_available_count=_one(
            connection,
            "SELECT COUNT(*) FROM roots WHERE source_id=? AND closed_expr_route=1",
            parameters,
        ),
        current_meta_typed_root_count=_one(
            connection,
            "SELECT COUNT(*) FROM roots WHERE source_id=? AND evidence_level='typed'",
            parameters,
        ),
        typed_applicability_receipt_root_count=_one(
            connection,
            f"SELECT COUNT(*) FROM roots WHERE source_id=? AND {typed_any}",
            parameters,
        ),
    )


def _cluster_find(parent: dict[str, str], root_id: str) -> str:
    while parent[root_id] != root_id:
        parent[root_id] = parent[parent[root_id]]
        root_id = parent[root_id]
    return root_id


def _cluster_union(parent: dict[str, str], left: str, right: str) -> None:
    left_root = _cluster_find(parent, left)
    right_root = _cluster_find(parent, right)
    if left_root != right_root:
        parent[max(left_root, right_root)] = min(left_root, right_root)


def _select_roots(
    connection: sqlite3.Connection, spec: TierSpec
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, FixedSourceCounts],
    dict[str, tuple[str, ...]],
]:
    selected: dict[str, tuple[str, ...]] = {}
    selected_sources: dict[str, FixedSourceCounts] = {}
    selected_cluster_hashes: dict[str, tuple[str, ...]] = {}
    allowed = set(spec.selection_operation_ids)
    for operation, column in _OP_COLUMNS.items():
        roots: tuple[str, ...] = ()
        cluster_hashes: tuple[str, ...] = ()
        if operation not in allowed or spec.target_per_primary_operation == 0:
            pass
        else:
            applicability_column = (
                f"typed_{column}"
                if spec.selection_rule == "global_minimum_stable_eligible_root_hash_v1"
                else column
            )
            smoke_clause = ""
            parameters: tuple[object, ...] = ()
            if spec.selection_rule == "global_minimum_stable_eligible_root_hash_v1":
                smoke_clause = (
                    " AND EXISTS (SELECT 1 FROM smoke_manifest_operations smo "
                    "WHERE smo.root_id=roots.root_id AND smo.operation_id=?)"
                )
                parameters = (operation,)
            rows = [
                tuple(str(value) for value in row)
                for row in connection.execute(
                    f"SELECT root_id, source_id, surface_hash, near_hash, structure_hash "
                    f"FROM roots WHERE internal_gate=1 AND blocklisted=0 "
                    f"AND {applicability_column}=1{smoke_clause} ORDER BY root_id",
                    parameters,
                )
            ]
            # This query deliberately has no source predicate: exact/alpha/
            # structure components are global across every source persisted in
            # this route.  For selected_wave that universe is the complete
            # bounded route slice, not undiscovered roots beyond its explicit
            # scan budget; only full_cross_source can make a source-global
            # inventory claim.
            by_id = {row[0]: row for row in rows}
            parent = {root_id: root_id for root_id in by_id}

            key_owner: dict[tuple[str, str], str] = {}
            for root_id, _source_id, exact_key, alpha_key, structure_key in rows:
                for kind, key in (
                    ("exact", exact_key),
                    ("alpha", alpha_key),
                    ("structure", structure_key),
                ):
                    previous = key_owner.setdefault((kind, key), root_id)
                    _cluster_union(parent, previous, root_id)
            components: dict[str, list[str]] = {}
            for root_id in by_id:
                components.setdefault(_cluster_find(parent, root_id), []).append(root_id)
            for members in components.values():
                members.sort()

            if spec.selection_rule == "global_minimum_stable_eligible_root_hash_v1":
                ordered_seeds = sorted(by_id)
            else:
                by_source = {
                    source_id: sorted(
                        root_id for root_id, row in by_id.items() if row[1] == source_id
                    )
                    for source_id in EXPECTED_SOURCE_IDS
                }
                positions = dict.fromkeys(EXPECTED_SOURCE_IDS, 0)
                seed_list: list[str] = []
                while True:
                    advanced = False
                    for source_id in EXPECTED_SOURCE_IDS:
                        position = positions[source_id]
                        pool = by_source[source_id]
                        if position < len(pool):
                            seed_list.append(pool[position])
                            positions[source_id] += 1
                            advanced = True
                    if not advanced:
                        break
                ordered_seeds = seed_list

            chosen_components: set[str]
            if spec.selection_rule == "global_minimum_stable_eligible_root_hash_v1":
                chosen = ordered_seeds[: spec.target_per_primary_operation]
                roots = tuple(chosen)
                chosen_components = {_cluster_find(parent, root_id) for root_id in chosen}
            else:
                chosen_roots: list[str] = []
                chosen_components = set()
                considered: set[str] = set()
                for seed in ordered_seeds:
                    component = _cluster_find(parent, seed)
                    if component in considered:
                        continue
                    considered.add(component)
                    members = components[component]
                    if len(chosen_roots) + len(members) > spec.target_per_primary_operation:
                        continue
                    chosen_components.add(component)
                    chosen_roots.extend(members)
                    if len(chosen_roots) == spec.target_per_primary_operation:
                        break
                roots = tuple(chosen_roots)

            hashes = []
            for component in sorted(chosen_components):
                members = components[component]
                hashes.append(
                    hash_canonical(
                        {
                            "procedure": (
                                "cross_source_persisted_route_slice_"
                                "exact_alpha_structure_cluster_membership_v1"
                            ),
                            "operation_id": operation,
                            "members": [list(by_id[root_id]) for root_id in members],
                        }
                    )
                )
            cluster_hashes = tuple(hashes)
        selected[operation] = roots
        selected_cluster_hashes[operation] = cluster_hashes
        counts = dict.fromkeys(EXPECTED_SOURCE_IDS, 0)
        if roots:
            placeholders = ",".join("?" for _ in roots)
            for source_id, count in connection.execute(
                f"SELECT source_id, COUNT(*) FROM roots WHERE root_id IN ({placeholders}) "
                "GROUP BY source_id",
                roots,
            ):
                counts[str(source_id)] = int(count)
        selected_sources[operation] = FixedSourceCounts.model_validate(counts)
    return selected, selected_sources, selected_cluster_hashes


def _duplicate_summary(connection: sqlite3.Connection) -> DuplicateSummary:
    def clusters(column: str) -> tuple[int, int]:
        rows = tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM roots WHERE internal_gate=1 AND blocklisted=0 "
                f"GROUP BY {column} HAVING COUNT(*) > 1"
            )
        )
        return len(rows), sum(int(row[0]) for row in rows)

    exact_clusters, exact_members = clusters("surface_hash")
    alpha_clusters, alpha_members = clusters("near_hash")
    structure_clusters, structure_members = clusters("structure_hash")
    cross = _one(
        connection,
        "SELECT COUNT(*) FROM (SELECT surface_hash FROM roots "
        "WHERE internal_gate=1 AND blocklisted=0 GROUP BY surface_hash "
        "HAVING COUNT(DISTINCT source_id) > 1)",
    )
    return DuplicateSummary(
        exact_duplicate_cluster_count=exact_clusters,
        exact_duplicate_member_count=exact_members,
        alpha_duplicate_cluster_count=alpha_clusters,
        alpha_duplicate_member_count=alpha_members,
        structure_duplicate_cluster_count=structure_clusters,
        structure_duplicate_member_count=structure_members,
        cross_source_exact_cluster_count=cross,
    )


def _operation_pool_hashes(connection: sqlite3.Connection) -> FixedOperationHashes:
    values: dict[str, str] = {}
    for operation, column in _OP_COLUMNS.items():
        digest = hashlib.sha256()
        for row in connection.execute(
            f"SELECT root_id, surface_hash, near_hash, structure_hash FROM roots "
            f"WHERE internal_gate=1 AND blocklisted=0 AND {column}=1 ORDER BY root_id"
        ):
            encoded = canonical_json_bytes(list(row))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        values[operation] = digest.hexdigest()
    return FixedOperationHashes.model_validate(values)


def build_receipt(
    loaded: LoadedCensusConfig,
    tier: Tier,
    state: CensusState,
    *,
    input_manifest_path: str | None,
    input_manifest_sha256: str | None,
    journal_path: str,
    journal_final_chain_hash: str,
) -> CensusReceipt:
    spec = cast(TierSpec, getattr(loaded.config.tiers, tier))
    expected_binding = make_state_binding(
        loaded,
        tier,
        input_manifest_sha256,
        Path(journal_path),
        runtime_git_commit=state.binding.runtime_git_commit,
    )
    if state.binding != expected_binding:
        raise CensusError("SQLite state route does not match requested receipt tier/input")
    _validate_state_rows(loaded, state)
    connection = state.connection
    state_evidence_hash = _state_evidence_hash(connection)
    journal_evidence = _read_journal(Path(journal_path))
    if (
        journal_evidence.final_chain_hash != journal_final_chain_hash
        or journal_evidence.final_event != "census_state_finalized"
    ):
        raise CensusError("receipt journal does not end at the exact finalized-state event")
    expected_final_event = {
        "event": "census_state_finalized",
        "tier": tier,
        "state_route_id": state.binding.route_id,
        "state_evidence_hash": state_evidence_hash,
        "config_file_sha256": loaded.config_file_sha256,
        "config_semantic_hash": loaded.config_hash,
        "implementation_source_sha256": state.binding.implementation_source_sha256,
        "runtime_git_commit": state.binding.runtime_git_commit,
        "evaluation_blocklist_file_sha256": state.binding.evaluation_blocklist_file_sha256,
        "evaluation_blocklist_procedure_id": state.binding.evaluation_blocklist_procedure_id,
    }
    if any(
        journal_evidence.final_payload.get(key) != value
        for key, value in expected_final_event.items()
    ):
        raise CensusError("final journal event does not bind the exact census state")
    selected, selected_sources, selected_cluster_hashes = _select_roots(connection, spec)
    operation_counts = _fixed_operations(connection, None)
    typed_operation_counts = _fixed_operations(connection, None, typed=True)
    completed = {
        str(row[0]) for row in connection.execute("SELECT source_id FROM completed_sources")
    }
    source_results = []
    for source in loaded.config.sources:
        root_count = _one(
            connection, "SELECT COUNT(*) FROM roots WHERE source_id=?", (source.source_id,)
        )
        source_results.append(
            SourceResult(
                source_id=source.source_id,
                source_revision=source.revision,
                domain_stratum=source.domain_stratum,
                expected_closed_expr_route=source.closed_expr_route,
                completion_scope=(
                    "hash_bound_manifest_route_slice"
                    if tier == "smoke"
                    else (
                        "bounded_sampling_frame_route_slice"
                        if tier == "selected_wave"
                        else "complete_source_inventory"
                    )
                ),
                scan_complete_semantics="route_slice_complete_not_necessarily_source_complete_v2",
                scan_complete=source.source_id in completed,
                source_inventory_complete=(
                    tier == "full_cross_source" and source.source_id in completed
                ),
                root_count=root_count,
                raw_declaration_count=root_count,
                eligible_root_count=_one(
                    connection,
                    "SELECT COUNT(*) FROM roots WHERE source_id=? AND internal_gate=1 "
                    "AND blocklisted=0",
                    (source.source_id,),
                ),
                excluded_declaration_count=_one(
                    connection,
                    "SELECT COUNT(*) FROM roots WHERE source_id=? AND exclusion_reason!='none'",
                    (source.source_id,),
                ),
                exclusion_counts=_exclusion_counts(connection, source.source_id),
                blocklisted_root_count=_one(
                    connection,
                    "SELECT COUNT(*) FROM roots WHERE source_id=? AND blocklisted=1",
                    (source.source_id,),
                ),
                internal_gate_candidate_count=_one(
                    connection,
                    "SELECT COUNT(*) FROM roots WHERE source_id=? AND internal_gate=1 "
                    "AND blocklisted=0",
                    (source.source_id,),
                ),
                evidence_counts=_fixed_evidence(connection, source.source_id),
                operation_candidate_counts=_fixed_operations(connection, source.source_id),
                typed_operation_applicability_counts=_fixed_operations(
                    connection, source.source_id, typed=True
                ),
                signature_strata=_signature_strata(connection, source.source_id),
                route_availability=_route_availability(connection, source),
                n31_proof_route_coverage=_n31_counts(connection, source.source_id),
            )
        )
    complete = state.route_complete()
    if tier == "smoke":
        sampling_sufficient = (
            complete
            and len(selected["P01_ALPHA_RENAME_SINGLE_V1"]) == 1
            and len(selected["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]) == 1
        )
    elif tier == "selected_wave":
        minimum = spec.minimum_gate_roots_per_primary_operation
        assert minimum is not None
        sampling_sufficient = complete and all(
            len(selected[operation]) >= minimum for operation in PRIMARY_OPERATIONS
        )
    else:
        sampling_sufficient = complete
    return CensusReceipt(
        schema_version=1,
        census_id=loaded.config.census_id,
        tier=tier,
        tier_id=spec.tier_id,
        config_path=cast(
            Literal["configs/transformations/sft1_value_first_v1/wave1_census_v0_3_6.yaml"],
            DEFAULT_CONFIG_PATH.as_posix(),
        ),
        config_file_sha256=loaded.config_file_sha256,
        config_semantic_hash=loaded.config_hash,
        implementation_source_sha256=state.binding.implementation_source_sha256,
        runtime_git_commit=state.binding.runtime_git_commit,
        input_manifest_path=input_manifest_path,
        input_manifest_sha256=input_manifest_sha256,
        state_db_path=str(state.path),
        state_route_id=state.binding.route_id,
        journal_path=str(
            _safe_path(
                Path(journal_path),
                purpose="census journal",
                require_exists=True,
                require_file=True,
            )
        ),
        journal_final_chain_hash=journal_final_chain_hash,
        state_backend=loaded.config.durability.state_backend,
        lean_invoked=False,
        transforms_executed=False,
        model_facing_rows_emitted=False,
        complete=complete,
        sampling_frame_sufficient=sampling_sufficient,
        zero_lean_operation_applicability_claimed=False,
        total_root_count=sum(item.root_count for item in source_results),
        total_raw_declaration_count=sum(item.raw_declaration_count for item in source_results),
        total_eligible_root_count=sum(item.eligible_root_count for item in source_results),
        total_excluded_declaration_count=sum(
            item.excluded_declaration_count for item in source_results
        ),
        evaluation_blocklist_path=loaded.config.evaluation_blocklist_binding.path,
        evaluation_blocklist_file_sha256=(loaded.config.evaluation_blocklist_binding.file_sha256),
        evaluation_blocklist_procedure_id=(loaded.config.evaluation_blocklist_binding.procedure_id),
        candidate_set_hash=_candidate_set_hash(connection),
        state_evidence_hash=state_evidence_hash,
        source_results=tuple(source_results),
        operation_candidate_counts=operation_counts,
        operation_pool_hashes=_operation_pool_hashes(connection),
        typed_operation_applicability_counts=typed_operation_counts,
        selection_rule=spec.selection_rule,
        selection_operation_ids=spec.selection_operation_ids,
        selected_root_ids=cast(dict[PrimaryOperation, tuple[Sha256, ...]], selected),
        selected_cluster_membership_hashes=cast(
            dict[PrimaryOperation, tuple[Sha256, ...]], selected_cluster_hashes
        ),
        selected_source_counts=cast(dict[PrimaryOperation, FixedSourceCounts], selected_sources),
        n31_proof_route_coverage=_n31_counts(connection, None),
        duplicates=_duplicate_summary(connection),
    )


@dataclass(frozen=True, slots=True)
class JournalEvidence:
    final_chain_hash: str
    final_event: str
    event_count: int
    final_payload: dict[str, object]


def _parse_journal_bytes(raw: bytes) -> JournalEvidence:
    if not raw or not raw.endswith(b"\n"):
        raise CensusError("census journal is empty or lacks a durable final newline")
    previous = "0" * 64
    final_event = ""
    final_payload: dict[str, object] = {}
    count = 0
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            payload = json.loads(line)
        except Exception as exc:
            raise CensusError(f"invalid census journal JSON at line {line_number}") from exc
        if not isinstance(payload, dict):
            raise CensusError("census journal event must be a mapping")
        observed_hash = payload.get("event_hash")
        event = payload.get("event")
        if (
            payload.get("sequence") != line_number
            or payload.get("previous_event_hash") != previous
            or not isinstance(event, str)
            or not event
            or not isinstance(observed_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", observed_hash) is None
        ):
            raise CensusError(f"census journal chain metadata drift at line {line_number}")
        hash_payload = dict(payload)
        del hash_payload["event_hash"]
        expected_hash = hash_canonical(hash_payload)
        if observed_hash != expected_hash:
            raise CensusError(f"census journal event hash drift at line {line_number}")
        if canonical_json_bytes(payload) != line:
            raise CensusError(f"census journal event is not canonical at line {line_number}")
        previous = observed_hash
        final_event = event
        final_payload = payload
        count = line_number
    return JournalEvidence(previous, final_event, count, final_payload)


def _read_journal(path: Path) -> JournalEvidence:
    resolved = _safe_path(
        path,
        purpose="bound census journal",
        require_exists=True,
        require_file=True,
    )
    return _parse_journal_bytes(resolved.read_bytes())


class JournalWriter:
    """Append-only, hash-chained journal with strict replay on every open."""

    def __init__(self, path: Path) -> None:
        self.path = _safe_path(
            path,
            purpose="census journal",
            require_exists=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path = _safe_path(
            self.path,
            purpose="census journal",
            require_exists=False,
        )
        if self.path.exists():
            evidence = _read_journal(self.path)
            self.previous = evidence.final_chain_hash
            self.sequence = evidence.event_count
            self.final_event = evidence.final_event
        else:
            self.previous = "0" * 64
            self.sequence = 0
            self.final_event = ""

    def append(self, event: dict[str, object]) -> str:
        if any(key in event for key in ("sequence", "previous_event_hash", "event_hash")):
            raise CensusError("journal payload attempts to override chain metadata")
        event_name = event.get("event")
        if not isinstance(event_name, str) or not event_name:
            raise CensusError("journal event requires a nonempty event name")
        payload = {
            "sequence": self.sequence + 1,
            "previous_event_hash": self.previous,
            **event,
        }
        event_hash = hash_canonical(payload)
        payload["event_hash"] = event_hash
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o644)
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CensusError("census journal target is not a regular file")
            handle.seek(0)
            raw = handle.read()
            if raw:
                observed = _parse_journal_bytes(raw)
                if (
                    observed.event_count != self.sequence
                    or observed.final_chain_hash != self.previous
                    or observed.final_event != self.final_event
                ):
                    raise CensusError("census journal changed after writer initialization")
            elif self.sequence != 0 or self.previous != "0" * 64 or self.final_event:
                raise CensusError("census journal was truncated after writer initialization")
            handle.seek(0, os.SEEK_END)
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.sequence += 1
        self.previous = event_hash
        self.final_event = event_name
        return event_hash


def _encoded_json(payload: object) -> tuple[bytes, str]:
    encoded = canonical_json_bytes(payload) + b"\n"
    return encoded, sha256_hex(encoded)


def _write_immutable_json(path: Path, payload: object, *, purpose: str) -> str:
    """Create a success artifact once; allow only byte-identical replay."""
    resolved = _safe_path(path, purpose=purpose, require_exists=False)
    encoded, digest = _encoded_json(payload)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = _safe_path(resolved, purpose=purpose, require_exists=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags, 0o644)
    except FileExistsError:
        existing = _safe_path(
            resolved,
            purpose=purpose,
            require_exists=True,
            require_file=True,
        )
        if existing.read_bytes() != encoded:
            raise CensusError(
                f"{purpose} is immutable and already contains conflicting bytes"
            ) from None
        return digest
    with os.fdopen(descriptor, "wb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CensusError(f"{purpose} target is not a regular file")
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(resolved.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return digest


_TERMINAL_ROUTE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "tier",
    "config_file_sha256",
    "config_semantic_hash",
    "implementation_source_sha256",
    "runtime_git_commit",
    "state_db_path",
    "state_route_id",
    "journal_path",
    "evaluation_blocklist_file_sha256",
    "evaluation_blocklist_procedure_id",
    "lean_invoked",
)


def _same_terminal_route(left: dict[str, object], right: dict[str, object]) -> bool:
    return all(left.get(field) == right.get(field) for field in _TERMINAL_ROUTE_FIELDS)


def _replace_terminal_marker(path: Path, encoded: bytes, *, suffix: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{suffix}.tmp")
    temporary = _safe_path(
        temporary,
        purpose=f"temporary {suffix} marker",
        require_exists=False,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_failure_marker(path: Path, payload: object) -> str:
    """Durably replace failed evidence, but never overwrite a completed marker."""
    resolved = _safe_path(path, purpose="terminal marker", require_exists=False)
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "failed"
        or any(field not in payload for field in _TERMINAL_ROUTE_FIELDS)
    ):
        raise CensusError("failure terminal marker payload is malformed")
    if resolved.exists():
        try:
            current = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CensusError("existing terminal marker is malformed") from exc
        if isinstance(current, dict) and current.get("status") == "complete":
            raise CensusError("completed terminal marker is immutable")
        if (
            not isinstance(current, dict)
            or current.get("status") != "failed"
            or not _same_terminal_route(current, payload)
        ):
            raise CensusError("existing failed terminal marker belongs to another census route")
    encoded, digest = _encoded_json(payload)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = _safe_path(resolved, purpose="terminal marker", require_exists=False)
    _replace_terminal_marker(resolved, encoded, suffix="failed")
    return digest


def _write_success_terminal_marker(path: Path, payload: dict[str, object]) -> str:
    """Create a completion marker or promote only its exact failed-route predecessor."""
    if payload.get("status") != "complete" or any(
        field not in payload for field in _TERMINAL_ROUTE_FIELDS
    ):
        raise CensusError("success terminal marker payload is malformed")
    resolved = _safe_path(path, purpose="terminal marker", require_exists=False)
    encoded, digest = _encoded_json(payload)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = _safe_path(resolved, purpose="terminal marker", require_exists=False)
    if not resolved.exists():
        return _write_immutable_json(
            resolved,
            payload,
            purpose="successful terminal marker",
        )
    try:
        current = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CensusError("existing terminal marker is malformed") from exc
    if isinstance(current, dict) and current.get("status") == "complete":
        if resolved.read_bytes() != encoded:
            raise CensusError("completed terminal marker is immutable")
        return digest
    if (
        not isinstance(current, dict)
        or current.get("status") != "failed"
        or not _same_terminal_route(current, payload)
    ):
        raise CensusError("failed terminal marker belongs to another census route")
    _replace_terminal_marker(resolved, encoded, suffix="complete")
    return digest


def _persist_success_artifacts(
    loaded: LoadedCensusConfig,
    receipt: CensusReceipt,
    binding: StateBinding,
    *,
    output: Path,
    state_db: Path,
    journal: Path,
    terminal_marker: Path,
) -> None:
    """Install the immutable receipt and its exact terminal marker."""
    receipt_sha = _write_immutable_json(
        output,
        receipt.model_dump(mode="json"),
        purpose="successful census receipt",
    )
    marker = {
        "schema_version": 1,
        "status": "complete",
        "tier": receipt.tier,
        "receipt_path": str(output),
        "receipt_sha256": receipt_sha,
        "config_file_sha256": loaded.config_file_sha256,
        "config_semantic_hash": loaded.config_hash,
        "implementation_source_sha256": binding.implementation_source_sha256,
        "runtime_git_commit": binding.runtime_git_commit,
        "state_db_path": str(state_db),
        "state_route_id": binding.route_id,
        "state_evidence_hash": receipt.state_evidence_hash,
        "journal_path": str(journal),
        "journal_final_chain_hash": receipt.journal_final_chain_hash,
        "evaluation_blocklist_file_sha256": binding.evaluation_blocklist_file_sha256,
        "evaluation_blocklist_procedure_id": binding.evaluation_blocklist_procedure_id,
        "lean_invoked": False,
    }
    _write_success_terminal_marker(terminal_marker, marker)


def run_build(
    loaded: LoadedCensusConfig,
    tier: Tier,
    *,
    output: Path,
    journal: Path,
    terminal_marker: Path,
    state_db: Path,
    input_manifest: Path | None,
) -> CensusReceipt:
    if tier == "smoke" and input_manifest is None:
        raise CensusError("smoke census requires --input-manifest")
    if tier != "smoke" and input_manifest is not None:
        raise CensusError("source-scan census cannot use --input-manifest")
    resolved_output = _safe_path(output, purpose="census receipt", require_exists=False)
    resolved_state = _safe_path(state_db, purpose="census SQLite state", require_exists=False)
    resolved_journal = _safe_path(journal, purpose="census journal", require_exists=False)
    resolved_marker = _safe_path(terminal_marker, purpose="terminal marker", require_exists=False)
    resolved_manifest = (
        _safe_path(
            input_manifest,
            purpose="smoke manifest",
            require_exists=True,
            require_file=True,
        )
        if input_manifest is not None
        else None
    )
    path_inventory = {
        "census receipt": resolved_output,
        "census SQLite state": resolved_state,
        "census journal": resolved_journal,
        "terminal marker": resolved_marker,
    }
    if resolved_manifest is not None:
        path_inventory["smoke manifest"] = resolved_manifest
    _reject_path_aliases(path_inventory)
    _reject_sqlite_sidecar_aliases(resolved_state, path_inventory)
    manifest_hash = hash_file(resolved_manifest) if resolved_manifest is not None else None
    journal_writer = JournalWriter(resolved_journal)
    if journal_writer.final_event == "census_state_finalized":
        journal_evidence = _read_journal(resolved_journal)
        recorded_commit = journal_evidence.final_payload.get("runtime_git_commit")
        if not isinstance(recorded_commit, str):
            raise CensusError("finalized census journal lacks its recorded runtime commit")
        binding = make_state_binding(
            loaded,
            tier,
            manifest_hash,
            resolved_journal,
            runtime_git_commit=recorded_commit,
        )
        recovered_state = CensusState(resolved_state, binding, create=False)
        try:
            receipt = build_receipt(
                loaded,
                tier,
                recovered_state,
                input_manifest_path=(
                    str(resolved_manifest) if resolved_manifest is not None else None
                ),
                input_manifest_sha256=manifest_hash,
                journal_path=str(resolved_journal),
                journal_final_chain_hash=journal_evidence.final_chain_hash,
            )
        finally:
            recovered_state.close()
        _persist_success_artifacts(
            loaded,
            receipt,
            binding,
            output=resolved_output,
            state_db=resolved_state,
            journal=resolved_journal,
            terminal_marker=resolved_marker,
        )
        return receipt
    allowed_dirty_paths = [
        resolved_output,
        resolved_state,
        resolved_state.with_name(f"{resolved_state.name}-wal"),
        resolved_state.with_name(f"{resolved_state.name}-shm"),
        resolved_state.with_name(f"{resolved_state.name}-journal"),
        resolved_journal,
        resolved_marker,
    ]
    if resolved_manifest is not None:
        allowed_dirty_paths.append(resolved_manifest)
    binding = make_state_binding(
        loaded,
        tier,
        manifest_hash,
        resolved_journal,
        require_clean_current_head=True,
        allowed_dirty_paths=allowed_dirty_paths,
    )
    state: CensusState | None = None
    try:
        state = CensusState(resolved_state, binding)
        journal_writer.append(
            {
                "event": "start",
                "tier": tier,
                "state_route_id": binding.route_id,
                "implementation_source_sha256": binding.implementation_source_sha256,
                "runtime_git_commit": binding.runtime_git_commit,
                "evaluation_blocklist_file_sha256": binding.evaluation_blocklist_file_sha256,
                "evaluation_blocklist_procedure_id": binding.evaluation_blocklist_procedure_id,
            }
        )
        if resolved_manifest is not None:
            entries = tuple(iter_authenticated_smoke_manifest(loaded, resolved_manifest))
            for entry in entries:
                state.add(entry.root)
                state.add_smoke_manifest_operation(
                    entry.root.root_id,
                    entry.selection_operation_id,
                    entry.typed_meta_receipt_sha256,
                )
            _verify_smoke_state_matches_manifest(state, entries)
            for source_id in sorted({entry.root.source_id for entry in entries}):
                state.mark_complete(source_id)
        else:
            scan_budget = cast(TierSpec, getattr(loaded.config.tiers, tier)).source_scan_root_budget
            for source in loaded.config.sources:
                if state.is_complete(source.source_id):
                    journal_writer.append(
                        {"event": "source_resume_hit", "source_id": source.source_id}
                    )
                    continue
                journal_writer.append(
                    {
                        "event": "source_start",
                        "source_id": source.source_id,
                        "root_budget": scan_budget,
                    }
                )
                records = (
                    iter_parquet_source(loaded, source)
                    if source.kind == "parquet_source_code"
                    else iter_git_source(loaded, source)
                )
                bounded_records = records if scan_budget is None else islice(records, scan_budget)
                observed = 0
                for index, record in enumerate(bounded_records, start=1):
                    state.add(record)
                    observed = index
                    if index % 10_000 == 0:
                        state.connection.commit()
                        journal_writer.append(
                            {
                                "event": "source_heartbeat",
                                "source_id": source.source_id,
                                "raw_declarations_observed": index,
                            },
                        )
                state.mark_complete(source.source_id)
                journal_writer.append(
                    {
                        "event": "source_route_slice_complete",
                        "source_id": source.source_id,
                        "raw_declarations_observed": observed,
                        "completion_scope": (
                            "bounded_sampling_frame_route_slice"
                            if scan_budget is not None
                            else "complete_source_inventory"
                        ),
                        "source_inventory_complete": scan_budget is None,
                    }
                )
        state.mark_route_complete()
        state.connection.commit()
        state_evidence_hash = _state_evidence_hash(state.connection)
        journal_final_chain_hash = journal_writer.append(
            {
                "event": "census_state_finalized",
                "tier": tier,
                "state_route_id": binding.route_id,
                "state_evidence_hash": state_evidence_hash,
                "config_file_sha256": loaded.config_file_sha256,
                "config_semantic_hash": loaded.config_hash,
                "implementation_source_sha256": binding.implementation_source_sha256,
                "runtime_git_commit": binding.runtime_git_commit,
                "evaluation_blocklist_file_sha256": binding.evaluation_blocklist_file_sha256,
                "evaluation_blocklist_procedure_id": binding.evaluation_blocklist_procedure_id,
            }
        )
        receipt = build_receipt(
            loaded,
            tier,
            state,
            input_manifest_path=str(resolved_manifest) if resolved_manifest is not None else None,
            input_manifest_sha256=manifest_hash,
            journal_path=str(resolved_journal),
            journal_final_chain_hash=journal_final_chain_hash,
        )
        _persist_success_artifacts(
            loaded,
            receipt,
            binding,
            output=resolved_output,
            state_db=resolved_state,
            journal=resolved_journal,
            terminal_marker=resolved_marker,
        )
        return receipt
    except Exception as exc:
        if journal_writer.final_event == "census_state_finalized":
            raise
        failure_chain_hash = journal_writer.append(
            {
                "event": "failed",
                "failure_class": type(exc).__name__,
                "tier": tier,
                "state_route_id": binding.route_id,
            }
        )
        with suppress(CensusError):
            _write_failure_marker(
                resolved_marker,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "tier": tier,
                    "failure_class": type(exc).__name__,
                    "config_file_sha256": loaded.config_file_sha256,
                    "config_semantic_hash": loaded.config_hash,
                    "implementation_source_sha256": binding.implementation_source_sha256,
                    "runtime_git_commit": binding.runtime_git_commit,
                    "state_db_path": str(resolved_state),
                    "state_route_id": binding.route_id,
                    "journal_path": str(resolved_journal),
                    "journal_final_chain_hash": failure_chain_hash,
                    "evaluation_blocklist_file_sha256": binding.evaluation_blocklist_file_sha256,
                    "evaluation_blocklist_procedure_id": binding.evaluation_blocklist_procedure_id,
                    "lean_invoked": False,
                },
            )
        raise
    finally:
        if state is not None:
            state.close()


def verify_receipt(
    loaded: LoadedCensusConfig,
    receipt_path: Path,
    terminal_marker: Path,
    state_db: Path,
    journal: Path,
) -> CensusReceipt:
    resolved_receipt = _safe_path(
        receipt_path,
        purpose="census receipt",
        require_exists=True,
        require_file=True,
    )
    resolved_state = _safe_path(
        state_db,
        purpose="census SQLite state",
        require_exists=True,
        require_file=True,
    )
    resolved_journal = _safe_path(
        journal,
        purpose="census journal",
        require_exists=True,
        require_file=True,
    )
    resolved_marker = _safe_path(
        terminal_marker,
        purpose="terminal marker",
        require_exists=True,
        require_file=True,
    )
    _reject_path_aliases(
        {
            "census receipt": resolved_receipt,
            "census SQLite state": resolved_state,
            "census journal": resolved_journal,
            "terminal marker": resolved_marker,
        }
    )
    _reject_sqlite_sidecar_aliases(
        resolved_state,
        {
            "census receipt": resolved_receipt,
            "census SQLite state": resolved_state,
            "census journal": resolved_journal,
            "terminal marker": resolved_marker,
        },
    )
    receipt = CensusReceipt.model_validate_json(resolved_receipt.read_text(encoding="utf-8"))
    if (
        receipt.config_file_sha256 != loaded.config_file_sha256
        or receipt.config_semantic_hash != loaded.config_hash
    ):
        raise CensusError("receipt config binding drift")
    if Path(receipt.state_db_path) != resolved_state:
        raise CensusError("receipt SQLite state path binding drift")
    if Path(receipt.journal_path) != resolved_journal:
        raise CensusError("receipt journal path binding drift")
    manifest_entries: tuple[SmokeManifestEntry, ...] = ()
    if receipt.tier == "smoke":
        if receipt.input_manifest_path is None or receipt.input_manifest_sha256 is None:
            raise CensusError("smoke receipt lacks its exact manifest binding")
        manifest_path = Path(receipt.input_manifest_path)
        if not manifest_path.is_absolute():
            raise CensusError("input manifest binding is not absolute")
        manifest = _safe_path(
            manifest_path,
            purpose="bound smoke manifest",
            require_exists=True,
            require_file=True,
        )
        bound_paths = {
            "census receipt": resolved_receipt,
            "census SQLite state": resolved_state,
            "census journal": resolved_journal,
            "terminal marker": resolved_marker,
            "smoke manifest": manifest,
        }
        _reject_path_aliases(bound_paths)
        _reject_sqlite_sidecar_aliases(resolved_state, bound_paths)
        if hash_file(manifest) != receipt.input_manifest_sha256:
            raise CensusError("input manifest binding drift")
        manifest_entries = tuple(iter_authenticated_smoke_manifest(loaded, manifest))
        manifest_hash: str | None = receipt.input_manifest_sha256
    else:
        if receipt.input_manifest_path is not None or receipt.input_manifest_sha256 is not None:
            raise CensusError("complete-streaming receipt cannot bind a smoke manifest")
        manifest_hash = None
    binding = make_state_binding(
        loaded,
        receipt.tier,
        manifest_hash,
        resolved_journal,
        runtime_git_commit=receipt.runtime_git_commit,
    )
    if receipt.state_route_id != binding.route_id:
        raise CensusError("receipt state-route binding drift")
    state = CensusState(resolved_state, binding, create=False)
    try:
        if receipt.tier == "smoke":
            _verify_smoke_state_matches_manifest(state, manifest_entries)
        rebuilt = build_receipt(
            loaded,
            receipt.tier,
            state,
            input_manifest_path=receipt.input_manifest_path,
            input_manifest_sha256=manifest_hash,
            journal_path=str(resolved_journal),
            journal_final_chain_hash=receipt.journal_final_chain_hash,
        )
    finally:
        state.close()
    if rebuilt.state_evidence_hash != receipt.state_evidence_hash:
        raise CensusError("SQLite state evidence binding drift")
    if rebuilt != receipt:
        raise CensusError("receipt does not exactly replay from bound SQLite state")
    raw_marker = json.loads(resolved_marker.read_text(encoding="utf-8"))
    if not isinstance(raw_marker, dict):
        raise CensusError("terminal marker must be a mapping")
    expected_marker = {
        "schema_version": 1,
        "status": "complete",
        "tier": receipt.tier,
        "receipt_path": str(resolved_receipt),
        "receipt_sha256": hash_file(resolved_receipt),
        "config_file_sha256": loaded.config_file_sha256,
        "config_semantic_hash": loaded.config_hash,
        "implementation_source_sha256": binding.implementation_source_sha256,
        "runtime_git_commit": binding.runtime_git_commit,
        "state_db_path": str(resolved_state),
        "state_route_id": binding.route_id,
        "state_evidence_hash": receipt.state_evidence_hash,
        "journal_path": str(resolved_journal),
        "journal_final_chain_hash": receipt.journal_final_chain_hash,
        "evaluation_blocklist_file_sha256": binding.evaluation_blocklist_file_sha256,
        "evaluation_blocklist_procedure_id": binding.evaluation_blocklist_procedure_id,
        "lean_invoked": False,
    }
    if raw_marker != expected_marker:
        raise CensusError("terminal marker does not bind the exact receipt")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument(
        "--tier",
        choices=("smoke", "selected_wave", "full_cross_source"),
        required=True,
    )
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--journal", type=Path, required=True)
    build.add_argument("--terminal-marker", type=Path, required=True)
    build.add_argument("--state-db", type=Path, required=True)
    build.add_argument("--input-manifest", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--journal", type=Path, required=True)
    verify.add_argument("--terminal-marker", type=Path, required=True)
    verify.add_argument("--state-db", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_wave1_census_config(path=args.config)
        if args.command == "build":
            run_build(
                loaded,
                cast(Tier, args.tier),
                output=args.output,
                journal=args.journal,
                terminal_marker=args.terminal_marker,
                state_db=args.state_db,
                input_manifest=args.input_manifest,
            )
        else:
            verify_receipt(loaded, args.receipt, args.terminal_marker, args.state_db, args.journal)
    except (CensusError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"wave1-census: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

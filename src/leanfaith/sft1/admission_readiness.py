"""Typed, fail-closed SFT1 Wave 1 gate-admission receipt.

This module records one user decision against the frozen revision 0.3.1
policy.  It is deliberately not an execution layer: it imports no Lean
backend, constructs no candidate, invokes no transform, and emits no row.

The receipt has two independent effects:

* task-owned implementation is authorized now; and
* the three bounded Wave 1 gates are admitted conditionally, but may not run
  until every recorded readiness blocker has been replaced by hash-bound
  passing evidence in a future reviewed revision.

Production admission, model-facing row emission, a 10K pilot, bulk work,
training, publication, and count commitments remain false.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, ValidationError, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.composition_policy import (
    EXPECTED_CURRENT_WAVE_OPERATION_IDS,
    EXPECTED_OPERATION_REGISTRY_HASH,
    EXPECTED_POLICY_CONFIG_HASH,
    LoadedSFT1CompositionPolicy,
    OperationSpec,
    load_sft1_composition_policy,
)
from leanfaith.sft1.n31_guard_policy import (
    EXPECTED_N31_GUARD_BANK_CONFIG_HASH,
    EXPECTED_N31_GUARD_BANK_FILE_SHA256,
    load_n31_guard_bank,
)
from leanfaith.sft1.source_census import (
    EXPECTED_CONFIG_FILE_SHA256 as EXPECTED_SOURCE_CENSUS_FILE_SHA256,
)
from leanfaith.sft1.source_census import (
    EXPECTED_CONFIG_HASH as EXPECTED_SOURCE_CENSUS_SEMANTIC_HASH,
)
from leanfaith.sft1.source_census import (
    load_wave1_source_census,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
SymbolicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", strict=True)]
IsoDate = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", strict=True),
]

_DEFAULT_RECEIPT_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_gate_admission_v0_3_2.yaml"
)

EXPECTED_RECEIPT_ID = "sft1_wave1_gate_admission_v0_3_2"
EXPECTED_RECEIPT_VERSION = "0.3.2"
EXPECTED_APPROVED_POLICY_VERSION = "0.3.1"
EXPECTED_APPROVED_COMMIT = "343ea0885e24a5ea062034559b7e4df33db408b6"
EXPECTED_APPROVED_COMMIT_TREE = "3a0b28d5704d5d83708fb8da95b0484940b22e55"
EXPECTED_BASE_POLICY_PATH = (
    "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml"
)
EXPECTED_BASE_POLICY_FILE_SHA256 = (
    "a052ecec4cc8f61db7438dd5acbc39373a624b155f8c0305bb75b7ae15d7195d"
)
EXPECTED_REVIEW_URL = "https://chatgpt.com/share/6a9450ec-3ae4-83eb-a6a8-0283a07124a2"
EXPECTED_REVIEW_ATTACHMENT_ID = "0bf713c4-145c-4bfd-a8a3-ddd346686648/pasted-text.txt"
EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256 = (
    "a2e7052c5a3d55ea36aca9e8ff25880059bdab7a9a93f4644f005aead4472b23"
)
EXPECTED_SECTION_8_RAW_SHA256 = "300e109e997e9c07f84eaaebf51593c5da341b2fa0420840e73d50945a35cd48"
EXPECTED_SECTION_8_NORMALIZED_SHA256 = (
    "0484d1b6dc231ae77aaf8ef58e0eaf5a30d206b0ff350c3e9f7787acf33787f8"
)
EXPECTED_SECTION_8_HEADING = "# 8. Exact approval wording"
EXPECTED_SECTION_8_NORMALIZATION = "lf_trim_trailing_space_collapse_blank_runs_v1"
EXPECTED_USER_ADOPTION_TEXT = (
    "I adopt the exact approval wording in Section 8 of the GPT Pro review for SFT1 "
    "revision 0.3.1 at commit `343ea0885e24a5ea062034559b7e4df33db408b6`."
)
EXPECTED_USER_ADOPTION_SHA256 = "f176417a8e0497e3faed0ed5971aa881249351302d9b3b95a2efd405beb5308d"
EXPECTED_PROJECTS: tuple[str, ...] = (
    "compiler_data",
    "cslib",
    "mathlib",
    "physlib",
)
EXPECTED_GATE_SEQUENCE: tuple[str, ...] = (
    "one_positive_one_negative_end_to_end_smoke",
    "selected_wave_operation_project_conformance_matrix",
    "approximately_100_eligible_roots_per_selected_operation",
)
EXPECTED_UNRESOLVED_BLOCKER_IDS: tuple[str, ...] = (
    "coordinator_shared_label_contract_update",
    "zero_lean_census_source_eligibility_and_source_proof_availability",
    "n31_closed_nonredundancy_checker",
    "six_selected_operation_execution_bindings",
)
EXPECTED_N31_REQUIREMENTS: tuple[str, ...] = (
    "frozen_implication_closure_for_competing_guards",
    "guard_data_variables_match_protected_target_operation",
    "guarded_value_occurs_in_relevant_target_position",
    "contradictory_or_unreachable_context_rejected",
    "unknown_nonredundancy_is_typed_not_applicable",
    "exact_de_bruijn_reindexing_verified",
)
EXPECTED_OPERATION_BINDING_COMPONENTS: tuple[str, ...] = (
    "implementation_source",
    "dispatch",
    "certificate_checker",
    "resolved_anchor",
    "applicability_bank",
    "success_fixture_bundle",
    "adversarial_fixture_bundle",
    "regression_bundle",
    "complete_binding_hash",
)
EXPECTED_CLEAN_CHECKOUT_RECEIPT_PATH = (
    "configs/transformations/sft1_value_first_v1/clean_checkout_receipt_v0_3_2.json"
)
EXPECTED_CLEAN_CHECKOUT_RECEIPT_FILE_SHA256 = (
    "4133c2df44b81b388d3cc39e499feb65d1cd410909b6843591ec6b1295ea3331"
)
EXPECTED_CLEAN_CHECKOUT_RECEIPT_SEMANTIC_HASH = (
    "90ca160b90e294170a1d88918a6aaf5cf900b8a1c89e8c7f77fcd2c8ba5b89c5"
)
EXPECTED_CENSUS_CONFIG_PATH = (
    "configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml"
)
EXPECTED_CENSUS_RECEIPT_PATH = (
    "configs/transformations/sft1_value_first_v1/wave1_source_census_receipt_v0_3_2.json"
)
EXPECTED_N31_GUARD_BANK_PATH = (
    "configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml"
)

EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH = (
    "8f50f38231e3cee7a6d5ab0d66cb708ce1f949789e91cc90b74516ef1406d409"
)
EXPECTED_ADMISSION_RECEIPT_FILE_SHA256 = (
    "c1cf07713bfca91e6b5fbedf75a5b5f6e0f841886df7a71e7f4f6c9d82c862b3"
)


class AdmissionReadinessError(ValueError):
    """Raised when the admission receipt drifts from the approved scope."""


class BindingStatus(StrEnum):
    UNRESOLVED_FAIL_CLOSED = "unresolved_fail_closed"
    RESOLVED_HASH_BOUND = "resolved_hash_bound"


class ProjectProofAvailabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE_HASH_BOUND = "available_hash_bound"
    INELIGIBLE_NO_REPRODUCIBLE_PROOF_ROUTE = "ineligible_no_reproducible_proof_route"


class ReviewSourceBinding(StrictModel):
    review_url: NonEmptyStr
    attachment_id: NonEmptyStr
    attachment_raw_sha256: Sha256
    section_heading: NonEmptyStr
    section_normalization: SymbolicId
    section8_markdown: NonEmptyStr
    section8_raw_sha256: Sha256
    section8_normalized_sha256: Sha256


class UserAdoptionBinding(StrictModel):
    exact_user_text: NonEmptyStr
    exact_user_text_sha256: Sha256
    adopted_review_section8_normalized_sha256: Sha256
    approved_policy_version: NonEmptyStr
    approved_commit: GitCommit
    recorded_date: IsoDate
    interpretation: Literal["adopts_exact_section8_without_expansion"]


class ApprovedPolicyBinding(StrictModel):
    policy_version: NonEmptyStr
    approved_commit: GitCommit
    approved_commit_tree: GitCommit
    composition_policy_path: NonEmptyStr
    composition_policy_file_sha256: Sha256
    composition_policy_config_hash: Sha256
    operation_registry_hash: Sha256
    base_policy_was_policy_only: StrictBool


class ApprovedOperation(StrictModel):
    operation_id: OperationId
    family_id: NonEmptyStr
    evidence_class: Literal["P-DEF", "P-SCHEMA", "N-RUBRIC", "N-PROOF"]
    policy_status: Literal["implementation_candidate", "proof_of_concept"]
    registered_eligible_projects: tuple[ProjectId, ...]
    gate_admitted: StrictBool
    production_admitted: StrictBool


class NegativeDimensionAdmission(StrictModel):
    admission_id: SymbolicId
    family_id: Literal["N31"]
    rubric_dimension: Literal["required_domain_guard"]
    track: Literal["natural"]
    operation_ids: tuple[OperationId, ...]
    gate_admitted: StrictBool
    proof_of_concept_gate_only: StrictBool
    production_admitted: StrictBool


class GateBounds(StrictModel):
    authorized_gate_sequence: tuple[SymbolicId, ...]
    smoke_actual_serialized_artifact_count: int = Field(strict=True, ge=0)
    operation_project_combination_count: int = Field(strict=True, ge=0)
    success_and_rejection_fixture_count: int = Field(strict=True, ge=0)
    approximate_total_eligible_roots: int = Field(strict=True, ge=0)
    retained_certificate_replay_fraction: float = Field(strict=True, ge=0.0, le=1.0)
    bounded_artifacts_are_model_facing_training_rows: StrictBool


class CurrentSessionHold(StrictModel):
    policy_loader_and_lean_free_tests_allowed: StrictBool
    task_owned_implementation_allowed: StrictBool
    lean_execution_prohibited: StrictBool
    transform_execution_prohibited: StrictBool
    row_generation_prohibited: StrictBool


class AuthorizationState(StrictModel):
    gate_admission_recorded: StrictBool
    task_owned_implementation_authorized_now: StrictBool
    bounded_gate_execution_conditionally_authorized: StrictBool
    bounded_gate_execution_may_start_now: StrictBool
    all_readiness_blockers_must_resolve_before_gate_execution: StrictBool
    implementation_readiness: StrictBool
    current_session_hold: CurrentSessionHold
    bounds: GateBounds


class ProhibitedAuthorizationState(StrictModel):
    production_admission: StrictBool
    model_facing_row_emission: StrictBool
    ten_k_pilot: StrictBool
    bulk_generation: StrictBool
    scale: StrictBool
    training: StrictBool
    publication: StrictBool
    source_root_count_commitment: StrictBool
    row_count_commitment: StrictBool
    bounded_gate_pass_auto_promotes_operation: StrictBool
    bounded_gate_pass_auto_authorizes_rows: StrictBool


class SharedContractBlocker(StrictModel):
    blocker_id: Literal["coordinator_shared_label_contract_update"]
    status: Literal["pending_coordinator_merge"]
    satisfied: StrictBool
    contract_path: Literal["plans/00_shared_contracts.md"]
    required_additive_rule: Literal[
        "exact_row_local_evidence_plus_production_operation_and_family_dimension_admission_plus_row_emission_authorization_creates_sft1_label"
    ]
    merged_commit: GitCommit | None
    merged_contract_sha256: Sha256 | None


class CleanCheckoutFacts(StrictModel):
    mode: Literal["detached_exact_commit"]
    path_provenance_only: NonEmptyStr
    git_clean_before: StrictBool
    git_clean_after: StrictBool
    replay_dependencies: Literal["git_relative_only"]


class CleanEnvironment(StrictModel):
    system_python: NonEmptyStr
    uv_python: NonEmptyStr
    uv: NonEmptyStr
    pytest: NonEmptyStr
    ruff: NonEmptyStr
    mypy: NonEmptyStr
    platform: NonEmptyStr


class CleanCheck(StrictModel):
    check_id: NonEmptyStr
    command: NonEmptyStr
    exit_code: StrictInt
    elapsed_seconds: StrictFloat
    passed: StrictInt | None
    breakdown: dict[str, StrictInt] | None


class CleanLoadedState(StrictModel):
    operation_count: StrictInt
    starter_bank_count: StrictInt
    starter_bank_entry_count: StrictInt
    repr_gate_status: Literal["passed"]
    gate_admitted_operation_count: StrictInt
    production_admitted_operation_count: StrictInt
    production_negative_count: StrictInt
    row_emission_authorized: StrictBool
    ten_k_pilot_authorized: StrictBool


class CleanReplayHashes(StrictModel):
    policy_file_sha256: Sha256
    policy_semantic_hash: Sha256
    operation_registry_hash: Sha256
    starter_bank_file_sha256: Sha256
    starter_bank_semantic_hash: Sha256
    repr_gate_file_sha256: Sha256
    repr_gate_semantic_hash: Sha256
    attempt_009_evidence_manifest_sha256: Sha256
    attempt_009_receipt_file_sha256: Sha256
    attempt_009_receipt_semantic_hash: Sha256


class CleanAttempt009(StrictModel):
    case_count: StrictInt
    endpoint_count: StrictInt
    recorded_lean_elapsed_ms: StrictInt
    complete_sidecar_bytes: StrictInt
    successful_claim: Literal["no_forbidden_rendered_residue_survived"]
    live_forbidden_string_probes: StrictBool


class CleanScopeConfirmation(StrictModel):
    lean_or_lake_invoked: StrictBool
    transforms_executed: StrictBool
    rows_generated: StrictBool
    repository_files_edited_by_replay: StrictBool
    storage_path_read: StrictBool


class CleanCheckoutReceipt(StrictModel):
    receipt_version: Literal["0.3.2"]
    receipt_id: Literal["sft1_policy_clean_checkout_343ea08_v1"]
    status: Literal["passed"]
    completed_utc: Annotated[
        str,
        Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", strict=True),
    ]
    approved_review_commit: GitCommit
    checkout: CleanCheckoutFacts
    environment: CleanEnvironment
    checks: tuple[CleanCheck, ...]
    loaded_state: CleanLoadedState
    hashes: CleanReplayHashes
    attempt_009: CleanAttempt009
    scope_confirmation: CleanScopeConfirmation


class CleanCheckoutBlocker(StrictModel):
    blocker_id: Literal["clean_checkout_policy_and_evidence_replay"]
    status: Literal["passed_hash_bound"]
    satisfied: StrictBool
    approved_commit_to_replay: GitCommit
    receipt_path: NonEmptyStr
    receipt_file_sha256: Sha256 | None
    receipt_semantic_hash: Sha256 | None
    passed: StrictBool


class ProjectProofAvailability(StrictModel):
    project_id: ProjectId
    status: ProjectProofAvailabilityStatus
    proof_route_id: SymbolicId | None
    evidence_sha256: Sha256 | None

    @model_validator(mode="after")
    def _validate_status_payload(self) -> ProjectProofAvailability:
        if self.status is ProjectProofAvailabilityStatus.UNKNOWN:
            if self.proof_route_id is not None or self.evidence_sha256 is not None:
                raise ValueError("unknown source-proof availability cannot carry evidence")
        elif self.proof_route_id is None or self.evidence_sha256 is None:
            raise ValueError("measured source-proof availability requires route and evidence")
        return self


class ZeroLeanCensusBlocker(StrictModel):
    blocker_id: Literal["zero_lean_census_source_eligibility_and_source_proof_availability"]
    status: Literal["pending_zero_lean_census"]
    satisfied: StrictBool
    census_config_path: NonEmptyStr
    census_receipt_path: NonEmptyStr
    census_config_file_sha256: Sha256
    census_config_semantic_hash: Sha256
    census_receipt_sha256: Sha256 | None
    source_eligibility_matrix_passed: StrictBool
    lean_invoked: StrictBool
    source_proof_availability_by_project: tuple[ProjectProofAvailability, ...]


class N31CheckerBlocker(StrictModel):
    blocker_id: Literal["n31_closed_nonredundancy_checker"]
    status: Literal["unresolved_fail_closed"]
    satisfied: StrictBool
    guard_bank_config_path: NonEmptyStr
    guard_bank_file_sha256: Sha256
    guard_bank_semantic_hash: Sha256
    target_head_bank_sha256: Sha256 | None
    checker_source_path: NonEmptyStr | None
    checker_source_sha256: Sha256 | None
    checker_symbol: NonEmptyStr | None
    checker_semantic_hash: Sha256 | None
    required_capabilities: tuple[SymbolicId, ...]
    unknown_nonredundancy_disposition: Literal["typed_not_applicable"]


class OperationExecutionBinding(StrictModel):
    operation_id: OperationId
    status: BindingStatus
    ready: StrictBool
    implementation_source_path: NonEmptyStr | None
    implementation_source_sha256: Sha256 | None
    dispatch_symbol: NonEmptyStr | None
    dispatch_binding_sha256: Sha256 | None
    certificate_checker_source_path: NonEmptyStr | None
    certificate_checker_source_sha256: Sha256 | None
    certificate_checker_symbol: NonEmptyStr | None
    certificate_checker_semantic_hash: Sha256 | None
    resolved_anchor_id: NonEmptyStr | None
    resolved_anchor_sha256: Sha256 | None
    applicability_bank_id: NonEmptyStr | None
    applicability_bank_sha256: Sha256 | None
    success_fixture_bundle_path: NonEmptyStr | None
    success_fixture_bundle_sha256: Sha256 | None
    adversarial_fixture_bundle_path: NonEmptyStr | None
    adversarial_fixture_bundle_sha256: Sha256 | None
    regression_bundle_path: NonEmptyStr | None
    regression_bundle_sha256: Sha256 | None
    complete_binding_hash: Sha256 | None

    @model_validator(mode="after")
    def _validate_resolution_atomicity(self) -> OperationExecutionBinding:
        payload = (
            self.implementation_source_path,
            self.implementation_source_sha256,
            self.dispatch_symbol,
            self.dispatch_binding_sha256,
            self.certificate_checker_source_path,
            self.certificate_checker_source_sha256,
            self.certificate_checker_symbol,
            self.certificate_checker_semantic_hash,
            self.resolved_anchor_id,
            self.resolved_anchor_sha256,
            self.applicability_bank_id,
            self.applicability_bank_sha256,
            self.success_fixture_bundle_path,
            self.success_fixture_bundle_sha256,
            self.adversarial_fixture_bundle_path,
            self.adversarial_fixture_bundle_sha256,
            self.regression_bundle_path,
            self.regression_bundle_sha256,
            self.complete_binding_hash,
        )
        if self.status is BindingStatus.UNRESOLVED_FAIL_CLOSED:
            if self.ready or any(value is not None for value in payload):
                raise ValueError("an unresolved operation binding must be empty and not ready")
        elif not self.ready or any(value is None for value in payload):
            raise ValueError("a resolved operation binding must be complete and ready")
        return self


class OperationBindingsBlocker(StrictModel):
    blocker_id: Literal["six_selected_operation_execution_bindings"]
    status: Literal["unresolved_fail_closed"]
    satisfied: StrictBool
    required_binding_components: tuple[SymbolicId, ...]
    operations: tuple[OperationExecutionBinding, ...]


class ReadinessState(StrictModel):
    status: Literal["blocked_fail_closed"]
    all_blockers_satisfied: StrictBool
    unresolved_blocker_ids: tuple[SymbolicId, ...]
    shared_contract: SharedContractBlocker
    clean_checkout: CleanCheckoutBlocker
    zero_lean_census: ZeroLeanCensusBlocker
    n31_checker: N31CheckerBlocker
    operation_bindings: OperationBindingsBlocker


class Wave1GateAdmissionReceipt(StrictModel):
    schema_version: Literal[1]
    receipt_id: SymbolicId
    receipt_version: NonEmptyStr
    status: Literal["gate_admitted_readiness_blocked"]
    review_source: ReviewSourceBinding
    user_adoption: UserAdoptionBinding
    approved_policy: ApprovedPolicyBinding
    approved_operations: tuple[ApprovedOperation, ...]
    negative_dimension_admission: NegativeDimensionAdmission
    authorization: AuthorizationState
    prohibited_authorizations: ProhibitedAuthorizationState
    readiness: ReadinessState

    @model_validator(mode="after")
    def _validate_internal_authority_separation(self) -> Wave1GateAdmissionReceipt:
        if not self.authorization.gate_admission_recorded:
            raise ValueError("the Wave 1 user gate admission must be recorded")
        if not self.authorization.task_owned_implementation_authorized_now:
            raise ValueError("the adopted wording authorizes task-owned implementation")
        if not self.authorization.bounded_gate_execution_conditionally_authorized:
            raise ValueError("the three bounded gates must retain their conditional admission")
        if (
            self.authorization.bounded_gate_execution_may_start_now
            or self.authorization.implementation_readiness
        ):
            raise ValueError("bounded gate execution must remain readiness-blocked")
        if not self.authorization.all_readiness_blockers_must_resolve_before_gate_execution:
            raise ValueError("readiness must be conjunctive before bounded gate execution")
        hold = self.authorization.current_session_hold
        if not (
            hold.policy_loader_and_lean_free_tests_allowed
            and hold.task_owned_implementation_allowed
            and hold.lean_execution_prohibited
            and hold.transform_execution_prohibited
            and hold.row_generation_prohibited
        ):
            raise ValueError("the current no-execution hold or implementation scope drifted")
        prohibited = self.prohibited_authorizations.model_dump(mode="python")
        if any(prohibited.values()):
            raise ValueError(
                "no production, row, scale, training, publication, or count authority exists"
            )
        return self


@dataclass(frozen=True, slots=True)
class LoadedWave1GateAdmission:
    loaded_receipt: LoadedConfig[Wave1GateAdmissionReceipt]
    loaded_base_policy: LoadedSFT1CompositionPolicy

    @property
    def config(self) -> Wave1GateAdmissionReceipt:
        return self.loaded_receipt.config

    @property
    def path(self) -> Path:
        return self.loaded_receipt.path

    @property
    def config_hash(self) -> str:
        return self.loaded_receipt.config_hash


def normalize_section8_markdown(markdown: str) -> str:
    """Normalize line endings/trailing space without changing approval words."""

    lines = [
        line.rstrip(" \t")
        for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    collapsed: list[str] = []
    for line in lines:
        if line or not collapsed or collapsed[-1]:
            collapsed.append(line)
    if not collapsed or collapsed[0] != EXPECTED_SECTION_8_HEADING:
        raise AdmissionReadinessError("approval Section 8 heading is missing or displaced")
    if sum(line == EXPECTED_SECTION_8_HEADING for line in collapsed) != 1:
        raise AdmissionReadinessError("approval Section 8 heading must occur exactly once")
    return "\n".join(collapsed) + "\n"


def _operation_map(
    policy: LoadedSFT1CompositionPolicy,
) -> dict[str, OperationSpec]:
    operations = (
        *policy.config.operations,
        *policy.config.synthetic_track.operations,
    )
    return {operation.operation_id: operation for operation in operations}


def validate_wave1_gate_admission(
    receipt: Wave1GateAdmissionReceipt,
    base_policy: LoadedSFT1CompositionPolicy,
) -> None:
    """Validate the receipt against the exact approved review and base policy."""

    if (
        receipt.receipt_id != EXPECTED_RECEIPT_ID
        or receipt.receipt_version != EXPECTED_RECEIPT_VERSION
    ):
        raise AdmissionReadinessError("Wave 1 admission receipt identity/version drift")

    review = receipt.review_source
    normalized_section = normalize_section8_markdown(review.section8_markdown)
    observed_section_raw_sha = sha256_hex(review.section8_markdown.encode("utf-8"))
    observed_section_normalized_sha = sha256_hex(normalized_section.encode("utf-8"))
    expected_review_values = (
        (review.review_url, EXPECTED_REVIEW_URL, "review URL"),
        (review.attachment_id, EXPECTED_REVIEW_ATTACHMENT_ID, "review attachment ID"),
        (
            review.attachment_raw_sha256,
            EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256,
            "review attachment raw SHA",
        ),
        (review.section_heading, EXPECTED_SECTION_8_HEADING, "Section 8 heading"),
        (
            review.section_normalization,
            EXPECTED_SECTION_8_NORMALIZATION,
            "Section 8 normalization",
        ),
        (review.section8_raw_sha256, EXPECTED_SECTION_8_RAW_SHA256, "Section 8 raw SHA"),
        (
            review.section8_normalized_sha256,
            EXPECTED_SECTION_8_NORMALIZED_SHA256,
            "Section 8 normalized SHA",
        ),
        (observed_section_raw_sha, EXPECTED_SECTION_8_RAW_SHA256, "embedded Section 8 raw SHA"),
        (
            observed_section_normalized_sha,
            EXPECTED_SECTION_8_NORMALIZED_SHA256,
            "embedded Section 8 normalized SHA",
        ),
    )
    for observed, expected, label in expected_review_values:
        if observed != expected:
            raise AdmissionReadinessError(f"{label} drift")

    adoption = receipt.user_adoption
    if adoption.exact_user_text != EXPECTED_USER_ADOPTION_TEXT:
        raise AdmissionReadinessError("exact user adoption text drift")
    observed_adoption_sha = hashlib.sha256(adoption.exact_user_text.encode("utf-8")).hexdigest()
    if (
        adoption.exact_user_text_sha256 != EXPECTED_USER_ADOPTION_SHA256
        or observed_adoption_sha != EXPECTED_USER_ADOPTION_SHA256
    ):
        raise AdmissionReadinessError("exact user adoption text hash drift")
    if adoption.adopted_review_section8_normalized_sha256 != EXPECTED_SECTION_8_NORMALIZED_SHA256:
        raise AdmissionReadinessError("user adoption does not bind the approved Section 8 hash")
    if (
        adoption.approved_policy_version != EXPECTED_APPROVED_POLICY_VERSION
        or adoption.approved_commit != EXPECTED_APPROVED_COMMIT
    ):
        raise AdmissionReadinessError("user adoption policy version/commit drift")

    policy_binding = receipt.approved_policy
    expected_policy_values = (
        (policy_binding.policy_version, EXPECTED_APPROVED_POLICY_VERSION),
        (policy_binding.approved_commit, EXPECTED_APPROVED_COMMIT),
        (policy_binding.approved_commit_tree, EXPECTED_APPROVED_COMMIT_TREE),
        (policy_binding.composition_policy_path, EXPECTED_BASE_POLICY_PATH),
        (policy_binding.composition_policy_file_sha256, EXPECTED_BASE_POLICY_FILE_SHA256),
        (policy_binding.composition_policy_config_hash, EXPECTED_POLICY_CONFIG_HASH),
        (policy_binding.operation_registry_hash, EXPECTED_OPERATION_REGISTRY_HASH),
        (policy_binding.base_policy_was_policy_only, True),
    )
    if any(observed != expected for observed, expected in expected_policy_values):
        raise AdmissionReadinessError("approved policy binding drift")
    if base_policy.config_hash != EXPECTED_POLICY_CONFIG_HASH:
        raise AdmissionReadinessError("loaded base policy canonical hash drift")

    operation_ids = tuple(operation.operation_id for operation in receipt.approved_operations)
    if operation_ids != EXPECTED_CURRENT_WAVE_OPERATION_IDS:
        raise AdmissionReadinessError("approved Wave 1 operation IDs/order drift")
    base_operations = _operation_map(base_policy)
    for approved in receipt.approved_operations:
        registered = base_operations.get(approved.operation_id)
        if registered is None:
            raise AdmissionReadinessError(
                f"approved operation is not registered: {approved.operation_id}"
            )
        if tuple(approved.registered_eligible_projects) != EXPECTED_PROJECTS:
            raise AdmissionReadinessError("approved operation project scope drift")
        if tuple(registered.eligible_projects) != tuple(approved.registered_eligible_projects):
            raise AdmissionReadinessError("approved projects differ from the frozen registry")
        if (
            approved.family_id != registered.family_id
            or approved.evidence_class != registered.evidence_class.value
            or approved.policy_status != registered.status.value
        ):
            raise AdmissionReadinessError(
                "approved operation metadata differs from frozen registry"
            )
        if not approved.gate_admitted or approved.production_admitted:
            raise AdmissionReadinessError("operation gate/production authority drift")

    dimension = receipt.negative_dimension_admission
    expected_negative_ops = EXPECTED_CURRENT_WAVE_OPERATION_IDS[-2:]
    if (
        dimension.admission_id != "n31_required_domain_guard_natural_v1"
        or tuple(dimension.operation_ids) != expected_negative_ops
        or not dimension.gate_admitted
        or not dimension.proof_of_concept_gate_only
        or dimension.production_admitted
    ):
        raise AdmissionReadinessError("N31 family/dimension admission drift")
    base_dimension = next(
        (
            item
            for item in base_policy.config.negative_family_dimension_admissions
            if item.admission_id == dimension.admission_id
        ),
        None,
    )
    if (
        base_dimension is None
        or base_dimension.family_id != dimension.family_id
        or base_dimension.rubric_dimension != dimension.rubric_dimension
        or base_dimension.track.value != dimension.track
        or tuple(base_dimension.operation_ids) != tuple(dimension.operation_ids)
    ):
        raise AdmissionReadinessError("N31 admission differs from the frozen registry dimension")

    bounds = receipt.authorization.bounds
    if (
        tuple(bounds.authorized_gate_sequence) != EXPECTED_GATE_SEQUENCE
        or bounds.smoke_actual_serialized_artifact_count != 2
        or bounds.operation_project_combination_count != 24
        or bounds.success_and_rejection_fixture_count != 48
        or bounds.approximate_total_eligible_roots != 600
        or bounds.retained_certificate_replay_fraction != 1.0
        or bounds.bounded_artifacts_are_model_facing_training_rows
    ):
        raise AdmissionReadinessError("bounded Wave 1 gate scope/cost drift")

    readiness = receipt.readiness
    if readiness.all_blockers_satisfied:
        raise AdmissionReadinessError("the current receipt cannot claim implementation readiness")
    if tuple(readiness.unresolved_blocker_ids) != EXPECTED_UNRESOLVED_BLOCKER_IDS:
        raise AdmissionReadinessError("unresolved readiness blocker inventory drift")
    if (
        readiness.shared_contract.satisfied
        or readiness.shared_contract.merged_commit is not None
        or readiness.shared_contract.merged_contract_sha256 is not None
    ):
        raise AdmissionReadinessError("shared-contract blocker must remain open")
    clean = readiness.clean_checkout
    if (
        not clean.satisfied
        or not clean.passed
        or clean.approved_commit_to_replay != EXPECTED_APPROVED_COMMIT
        or clean.receipt_path != EXPECTED_CLEAN_CHECKOUT_RECEIPT_PATH
        or clean.receipt_file_sha256 != EXPECTED_CLEAN_CHECKOUT_RECEIPT_FILE_SHA256
        or clean.receipt_semantic_hash != EXPECTED_CLEAN_CHECKOUT_RECEIPT_SEMANTIC_HASH
    ):
        raise AdmissionReadinessError("clean-checkout receipt binding drift")
    census = readiness.zero_lean_census
    if (
        census.satisfied
        or census.source_eligibility_matrix_passed
        or census.lean_invoked
        or census.census_config_path != EXPECTED_CENSUS_CONFIG_PATH
        or census.census_receipt_path != EXPECTED_CENSUS_RECEIPT_PATH
        or census.census_config_file_sha256 != EXPECTED_SOURCE_CENSUS_FILE_SHA256
        or census.census_config_semantic_hash != EXPECTED_SOURCE_CENSUS_SEMANTIC_HASH
        or census.census_receipt_sha256 is not None
        or tuple(item.project_id for item in census.source_proof_availability_by_project)
        != EXPECTED_PROJECTS
        or any(
            item.status is not ProjectProofAvailabilityStatus.UNKNOWN
            for item in census.source_proof_availability_by_project
        )
    ):
        raise AdmissionReadinessError("zero-Lean census/source-proof blocker drift")
    n31 = readiness.n31_checker
    if (
        n31.satisfied
        or n31.guard_bank_config_path != EXPECTED_N31_GUARD_BANK_PATH
        or n31.guard_bank_file_sha256 != EXPECTED_N31_GUARD_BANK_FILE_SHA256
        or n31.guard_bank_semantic_hash != EXPECTED_N31_GUARD_BANK_CONFIG_HASH
        or n31.target_head_bank_sha256 is not None
        or n31.checker_source_path is not None
        or n31.checker_source_sha256 is not None
        or n31.checker_symbol is not None
        or n31.checker_semantic_hash is not None
        or tuple(n31.required_capabilities) != EXPECTED_N31_REQUIREMENTS
    ):
        raise AdmissionReadinessError("closed N31 checker blocker drift")
    bindings = readiness.operation_bindings
    if (
        bindings.satisfied
        or tuple(bindings.required_binding_components) != EXPECTED_OPERATION_BINDING_COMPONENTS
        or tuple(binding.operation_id for binding in bindings.operations)
        != EXPECTED_CURRENT_WAVE_OPERATION_IDS
        or any(
            binding.status is not BindingStatus.UNRESOLVED_FAIL_CLOSED or binding.ready
            for binding in bindings.operations
        )
    ):
        raise AdmissionReadinessError("six-operation execution bindings must remain fail-closed")


def _verify_clean_checkout_receipt(root: Path, binding: CleanCheckoutBlocker) -> None:
    receipt_path = (root / binding.receipt_path).resolve()
    if not receipt_path.is_relative_to(root.resolve()):
        raise AdmissionReadinessError("clean-checkout receipt path escapes the repository")
    if hash_file(receipt_path) != EXPECTED_CLEAN_CHECKOUT_RECEIPT_FILE_SHA256:
        raise AdmissionReadinessError("clean-checkout receipt raw-file hash drift")
    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionReadinessError("clean-checkout receipt is unreadable") from exc
    if hash_canonical(raw) != EXPECTED_CLEAN_CHECKOUT_RECEIPT_SEMANTIC_HASH:
        raise AdmissionReadinessError("clean-checkout receipt semantic hash drift")
    try:
        receipt = CleanCheckoutReceipt.model_validate(raw)
    except ValidationError as exc:
        raise AdmissionReadinessError("clean-checkout receipt typed schema drift") from exc
    checkout = receipt.checkout
    loaded_state = receipt.loaded_state
    scope = receipt.scope_confirmation
    checks = receipt.checks
    if (
        receipt.approved_review_commit != EXPECTED_APPROVED_COMMIT
        or not checkout.git_clean_before
        or not checkout.git_clean_after
        or loaded_state.gate_admitted_operation_count != 0
        or loaded_state.production_admitted_operation_count != 0
        or loaded_state.production_negative_count != 0
        or loaded_state.row_emission_authorized
        or loaded_state.ten_k_pilot_authorized
        or scope.lean_or_lake_invoked
        or scope.transforms_executed
        or scope.rows_generated
        or scope.repository_files_edited_by_replay
        or scope.storage_path_read
        or len(checks) != 6
        or any(check.exit_code != 0 for check in checks)
        or checks[0].check_id != "focused_tests"
        or checks[0].passed != 127
    ):
        raise AdmissionReadinessError("clean-checkout receipt evidence/scope drift")


def _verify_incomplete_design_bindings(root: Path, readiness: ReadinessState) -> None:
    try:
        census = load_wave1_source_census(root)
    except (OSError, ValueError) as exc:
        raise AdmissionReadinessError("zero-Lean census design replay failed") from exc
    if (
        census.config_file_sha256 != readiness.zero_lean_census.census_config_file_sha256
        or census.config_hash != readiness.zero_lean_census.census_config_semantic_hash
        or census.config.status != "design_only_incomplete"
        or census.config.completion.census_passed
        or census.config.completion.wave1_source_eligibility_complete
        or any(
            source.n31_source_proof.status != "unknown"
            or source.n31_source_proof.n31_n_proof_eligible
            for source in census.config.sources
        )
    ):
        raise AdmissionReadinessError("zero-Lean census incomplete-state binding drift")

    try:
        n31_bank = load_n31_guard_bank(root)
    except (OSError, ValueError) as exc:
        raise AdmissionReadinessError("N31 guard design replay failed") from exc
    n31_config = n31_bank.config
    target_binding = n31_config.target_head_bank_binding
    if (
        n31_bank.config_hash != readiness.n31_checker.guard_bank_semantic_hash
        or n31_config.status != "design_frozen_implementation_unresolved"
        or n31_config.implementation_resolved
        or n31_config.execution_ready
        or target_binding.status != "unresolved"
        or target_binding.bank_hash is not None
        or target_binding.checker_symbol is not None
        or target_binding.checker_file_sha256 is not None
    ):
        raise AdmissionReadinessError("N31 guard design incomplete-state binding drift")


def load_wave1_gate_admission(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedWave1GateAdmission:
    """Load the frozen decision without starting Lean or executing a transform."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    resolved_receipt = (path or root / _DEFAULT_RECEIPT_PATH).resolve()
    if not resolved_receipt.is_relative_to(resolved_root):
        raise AdmissionReadinessError("Wave 1 admission receipt path escapes the repository")
    if hash_file(resolved_receipt) != EXPECTED_ADMISSION_RECEIPT_FILE_SHA256:
        raise AdmissionReadinessError("Wave 1 admission receipt raw-file hash drift")
    loaded_receipt = load_config(resolved_receipt, Wave1GateAdmissionReceipt)
    if loaded_receipt.config_hash != EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH:
        raise AdmissionReadinessError("Wave 1 admission receipt canonical hash drift")

    base_policy_path = root / EXPECTED_BASE_POLICY_PATH
    if hash_file(base_policy_path) != EXPECTED_BASE_POLICY_FILE_SHA256:
        raise AdmissionReadinessError("approved revision 0.3.1 policy raw-file hash drift")
    loaded_base_policy = load_sft1_composition_policy(root, path=base_policy_path)
    validate_wave1_gate_admission(loaded_receipt.config, loaded_base_policy)
    _verify_clean_checkout_receipt(root, loaded_receipt.config.readiness.clean_checkout)
    _verify_incomplete_design_bindings(root, loaded_receipt.config.readiness)
    return LoadedWave1GateAdmission(
        loaded_receipt=loaded_receipt,
        loaded_base_policy=loaded_base_policy,
    )


__all__ = [
    "EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH",
    "EXPECTED_ADMISSION_RECEIPT_FILE_SHA256",
    "EXPECTED_APPROVED_COMMIT",
    "EXPECTED_CURRENT_WAVE_OPERATION_IDS",
    "EXPECTED_PROJECTS",
    "EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256",
    "EXPECTED_SECTION_8_NORMALIZED_SHA256",
    "EXPECTED_UNRESOLVED_BLOCKER_IDS",
    "EXPECTED_USER_ADOPTION_TEXT",
    "AdmissionReadinessError",
    "BindingStatus",
    "LoadedWave1GateAdmission",
    "Wave1GateAdmissionReceipt",
    "load_wave1_gate_admission",
    "normalize_section8_markdown",
    "validate_wave1_gate_admission",
]

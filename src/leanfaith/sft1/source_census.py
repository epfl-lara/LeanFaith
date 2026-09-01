"""Strict zero-Lean loader for the incomplete SFT1 Wave 1 source census.

This module is a fail-closed policy boundary.  It reads YAML and hashes small
repository-owned inputs; it does not import a Lean backend, inspect an
environment, execute a transform, count roots, or emit a census receipt/row.
External artifacts are recorded as provenance but are deliberately not needed
for a clean-checkout policy replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$")]

DEFAULT_CENSUS_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml"
)

EXPECTED_CONFIG_FILE_SHA256 = "a8c6c3616a543ff9e1f5d4700a3b5a86da2442f70475737caf23bd264ebd2aaa"
EXPECTED_CONFIG_HASH = "daf4b26b782d096f77b9677e0a7cef5670103771942c415dc3420b3031eda44e"
EXPECTED_POLICY_COMMIT = "343ea0885e24a5ea062034559b7e4df33db408b6"
EXPECTED_POLICY_FILE_SHA256 = "a052ecec4cc8f61db7438dd5acbc39373a624b155f8c0305bb75b7ae15d7195d"
EXPECTED_POLICY_HASH = "08a6d1b2ea03f3674d06cdac44478377084af24ba5cd4af7cab57303f4e7a917"

EXPECTED_OPERATIONS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
)
EXPECTED_PROJECTS: tuple[str, ...] = ("compiler_data", "cslib", "mathlib", "physlib")
EXPECTED_REQUIRED_METRICS: tuple[str, ...] = (
    "raw_theorem_or_lemma_count",
    "license_and_revision_eligible_count",
    "exact_signature_duplicate_cluster_count",
    "exact_signature_duplicate_member_count",
    "alpha_or_structure_near_duplicate_cluster_count",
    "alpha_or_structure_near_duplicate_member_count",
    "source_domain_signature_strata",
    "per_root_compile_context_availability",
    "closed_expr_route_availability",
    "n31_source_proof_availability",
)
EXPECTED_REPO_BINDINGS: dict[str, str] = {
    "configs/data/cpt2/cpt2_v1.yaml": (
        "917fe1061cc1e2b96b12e6e42f47c64739df25172aacb5528a37e579449c00c6"
    ),
    "configs/data_reuse/inventory_v1.json": (
        "025ef81d3526c518263abb2a9af42dc67ee0e1be50e4c7272c06777bc27a4f87"
    ),
    "configs/projects/cslib.yaml": (
        "1cd35e901ca9bed56454ee534e4dc48838fbc9012cb7fbfe7901c4e7ea31e4d5"
    ),
    "configs/projects/mathlib.yaml": (
        "dfaccff566a513c17aad970c7204b1e86bc65a15ea2e234e749301b9be5cb940"
    ),
    "configs/projects/physlib.yaml": (
        "feab69e9e39792068d398cf1b7f17d787b219b2bd819ffb3239e648ef228edc1"
    ),
    "configs/sources/cslib.yaml": (
        "4e92dbf7857d1f0a061f5c54245e29f3805e06986b5c3a2f1a533edac32c7593"
    ),
    "configs/sources/mathlib.yaml": (
        "97d74a0decda12a099975b83ac1dc85fd44e7c0f347eecdce4d10d543d377957"
    ),
    "configs/sources/physlib.yaml": (
        "82c7f17e26a0c1cc2cc5bae1a97d582134d77b179acfca93eb0d2d75956f519e"
    ),
}


class SourceCensusError(ValueError):
    """Raised when the Wave 1 census contract or a pinned repo input drifts."""


class PolicyBinding(StrictModel):
    approved_policy_commit: GitCommit
    composition_policy_path: Literal[
        "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml"
    ]
    composition_policy_file_sha256: Sha256
    composition_policy_semantic_hash: Sha256
    selected_operation_ids: tuple[OperationId, ...]
    registered_project_ids: tuple[Literal["compiler_data", "cslib", "mathlib", "physlib"], ...]

    @model_validator(mode="after")
    def _exact_policy(self) -> PolicyBinding:
        if self.approved_policy_commit != EXPECTED_POLICY_COMMIT:
            raise ValueError("source census must bind the approved revision 0.3.1 commit")
        if self.composition_policy_file_sha256 != EXPECTED_POLICY_FILE_SHA256:
            raise ValueError("composition-policy file hash differs from approved revision")
        if self.composition_policy_semantic_hash != EXPECTED_POLICY_HASH:
            raise ValueError("composition-policy semantic hash differs from approved revision")
        if self.selected_operation_ids != EXPECTED_OPERATIONS:
            raise ValueError("source census must cover the exact selected Wave 1 operations")
        if self.registered_project_ids != EXPECTED_PROJECTS:
            raise ValueError("source census must cover the exact canonical projects")
        return self


class Authorization(StrictModel):
    executes_lean: Literal[False]
    may_start_lean: Literal[False]
    may_execute_transforms: Literal[False]
    may_generate_rows: Literal[False]
    may_write_passed_receipt: Literal[False]
    row_commitment_authorized: Literal[False]
    one_example_gate_authorized_by_this_file: Literal[False]
    hundred_root_gate_authorized_by_this_file: Literal[False]
    ten_k_authorized: Literal[False]
    scale_authorized: Literal[False]
    publication_authorized: Literal[False]


class Completion(StrictModel):
    census_passed: Literal[False]
    receipt_path: None
    measured_census_counts_present: Literal[False]
    all_source_identities_pinned: Literal[True]
    all_license_revision_eligibility_resolved: Literal[False]
    all_signature_inventories_complete: Literal[False]
    all_compile_contexts_resolved: Literal[False]
    all_exact_cluster_inputs_complete: Literal[False]
    all_near_duplicate_cluster_inputs_complete: Literal[False]
    all_n31_source_proofs_resolved: Literal[False]
    wave1_source_eligibility_complete: Literal[False]


class ClusterContract(StrictModel):
    exact_cluster_key: Literal["sha256_utf8_source_faithful_complete_signature_v1"]
    near_duplicate_keys: tuple[
        Literal["alpha_identity_fingerprint_v1", "signature_structure_fingerprint_v1"], ...
    ]
    preserve_exact_clusters_intact: Literal[True]
    preserve_near_duplicate_clusters_intact: Literal[True]
    cross_source_cluster_union_required: Literal[True]
    precursor_group_counts_are_census_counts: Literal[False]
    missing_cluster_input_is_eligible: Literal[False]

    @model_validator(mode="after")
    def _exact_near_keys(self) -> ClusterContract:
        if self.near_duplicate_keys != (
            "alpha_identity_fingerprint_v1",
            "signature_structure_fingerprint_v1",
        ):
            raise ValueError("near-duplicate methods must be exact and ordered")
        return self


class RepoBinding(StrictModel):
    path: NonEmptyStr
    sha256: Sha256


class ExternalPrecursor(StrictModel):
    path: NonEmptyStr
    sha256: Sha256
    reported_rows: PositiveInt
    coverage_scope: NonEmptyStr
    reported_rows_are_sft1_census_counts: Literal[False]


class SourceIdentity(StrictModel):
    repository_kind: Literal["git", "huggingface_dataset"]
    repository: NonEmptyStr
    revision: GitCommit
    source_glob_or_file: NonEmptyStr
    source_artifact_sha256: Sha256 | None
    revision_binding_status: Literal[
        "pinned_and_replayed_upstream", "pinned_clean_checkout_observed"
    ]
    repo_bindings: tuple[RepoBinding, ...] = Field(min_length=1)
    external_precursors: tuple[ExternalPrecursor, ...]

    @model_validator(mode="after")
    def _identity_coherence(self) -> SourceIdentity:
        paths = tuple(item.path for item in self.repo_bindings)
        if len(paths) != len(set(paths)):
            raise ValueError("source repo bindings must be unique")
        if self.repository_kind == "huggingface_dataset":
            if self.source_artifact_sha256 is None:
                raise ValueError("dataset identity requires the pinned source artifact hash")
        elif self.source_artifact_sha256 is not None:
            raise ValueError(
                "git source identity uses revision plus config bindings, not a file hash"
            )
        return self


class LicenseBinding(StrictModel):
    status: Literal["unknown", "identified_unreviewed"]
    spdx_id: Literal["Apache-2.0"] | None
    evidence_path: NonEmptyStr | None
    evidence_sha256: Sha256 | None
    source_eligible: Literal[False]
    blocking_reason: NonEmptyStr

    @model_validator(mode="after")
    def _evidence_coherence(self) -> LicenseBinding:
        values = (self.spdx_id, self.evidence_path, self.evidence_sha256)
        if self.status == "unknown" and any(value is not None for value in values):
            raise ValueError("unknown license cannot carry inferred evidence")
        if self.status == "identified_unreviewed" and any(value is None for value in values):
            raise ValueError("identified license requires its SPDX ID and hash-bound file")
        return self


class SignatureInventory(StrictModel):
    status: Literal["absent", "partial_precursor"]
    artifact_path: NonEmptyStr | None
    artifact_sha256: Sha256 | None
    completeness: Literal["none", "partial"]
    raw_theorem_or_lemma_count: None
    blocking_reason: NonEmptyStr

    @model_validator(mode="after")
    def _partial_evidence(self) -> SignatureInventory:
        if self.status == "absent":
            if (
                self.artifact_path is not None
                or self.artifact_sha256 is not None
                or self.completeness != "none"
            ):
                raise ValueError("absent inventory cannot carry an artifact or partial status")
        elif (
            self.artifact_path is None
            or self.artifact_sha256 is None
            or self.completeness != "partial"
        ):
            raise ValueError("partial inventory requires a hash-bound partial artifact")
        return self


class CompileContext(StrictModel):
    status: Literal["unresolved", "base_project_pinned_per_root_unresolved"]
    expected_toolchain: NonEmptyStr | None
    root_module: NonEmptyStr | None
    per_root_imports_bound: Literal[False]
    per_root_namespaces_bound: Literal[False]
    per_root_notation_scopes_bound: Literal[False]
    per_root_options_bound: Literal[False]
    blocking_reason: NonEmptyStr

    @model_validator(mode="after")
    def _base_context_coherence(self) -> CompileContext:
        if self.status == "unresolved":
            if self.expected_toolchain is not None or self.root_module is not None:
                raise ValueError("fully unresolved context cannot imply a toolchain/root module")
        elif self.expected_toolchain is None or self.root_module is None:
            raise ValueError("pinned project base requires toolchain and root module")
        return self


class ClosedExprRoute(StrictModel):
    route: Literal[
        "persistent_term_elab_of_signature_and_complete_telescope_without_declaration",
        "constant_info_type_with_canonical_universe_instantiation",
    ]
    input_kind: Literal["source_faithful_signature_text", "imported_constant_name"]
    declaration_insertion_allowed: Literal[False]
    pretty_print_reelaboration_allowed: Literal[False]
    route_ready: Literal[False]
    blocking_reason: NonEmptyStr


class ClusterInputs(StrictModel):
    exact_status: Literal["absent", "precursor_only", "partial_precursor"]
    exact_artifact_path: NonEmptyStr | None
    exact_artifact_sha256: Sha256 | None
    exact_key_available: NonEmptyStr | None
    near_status: Literal["absent", "partial_precursor"]
    near_artifact_path: NonEmptyStr | None
    near_artifact_sha256: Sha256 | None
    near_key_available: NonEmptyStr | None
    sft1_exact_cluster_count: None
    sft1_near_duplicate_cluster_count: None
    blocking_reason: NonEmptyStr

    @model_validator(mode="after")
    def _artifact_triplets(self) -> ClusterInputs:
        exact_values = (
            self.exact_artifact_path,
            self.exact_artifact_sha256,
            self.exact_key_available,
        )
        near_values = (
            self.near_artifact_path,
            self.near_artifact_sha256,
            self.near_key_available,
        )
        if (self.exact_status == "absent") != all(value is None for value in exact_values):
            raise ValueError("exact cluster status and artifact triplet disagree")
        if (self.near_status == "absent") != all(value is None for value in near_values):
            raise ValueError("near cluster status and artifact triplet disagree")
        return self


class N31SourceProof(StrictModel):
    status: Literal["unknown", "missing", "available_reproducible"]
    upstream_material: NonEmptyStr
    proof_inventory_path: NonEmptyStr | None
    proof_inventory_sha256: Sha256 | None
    proof_extractor_or_resolver_hash: Sha256 | None
    compile_context_bound: StrictBool
    exact_proof_payload_bound: StrictBool
    reproducible_replay_receipt_bound: StrictBool
    n31_n_proof_eligible: StrictBool
    blocking_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _proof_evidence_coherence(self) -> N31SourceProof:
        evidence = (
            self.proof_inventory_path,
            self.proof_inventory_sha256,
            self.proof_extractor_or_resolver_hash,
        )
        flags = (
            self.compile_context_bound,
            self.exact_proof_payload_bound,
            self.reproducible_replay_receipt_bound,
        )
        if self.status == "available_reproducible":
            if any(value is None for value in evidence) or not all(flags):
                raise ValueError("reproducible source proof requires all exact evidence bindings")
            if not self.n31_n_proof_eligible or self.blocking_reason is not None:
                raise ValueError("complete proof evidence must be eligible without a blocker")
        elif (
            any(value is not None for value in evidence)
            or any(flags)
            or self.n31_n_proof_eligible
            or self.blocking_reason is None
        ):
            raise ValueError("unknown/missing source proof must remain wholly fail-closed")
        return self


class Strata(StrictModel):
    status: Literal["unresolved"]
    artifact_path: None
    artifact_sha256: None
    blocking_reason: NonEmptyStr


class CensusMeasurements(StrictModel):
    raw_theorem_or_lemma_count: NonNegativeInt | None
    license_and_revision_eligible_count: NonNegativeInt | None
    exact_signature_duplicate_cluster_count: NonNegativeInt | None
    exact_signature_duplicate_member_count: NonNegativeInt | None
    near_duplicate_cluster_count: NonNegativeInt | None
    near_duplicate_member_count: NonNegativeInt | None

    def populated(self) -> bool:
        return any(value is not None for value in self.model_dump().values())


class OperationEligibility(StrictModel):
    operation_id: OperationId
    eligible: StrictBool
    reason: Literal["census_incomplete", "source_proof_unknown", "source_proof_missing"]


class SourceEntry(StrictModel):
    source_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    source_kind: Literal["extracted_signature", "imported_constant"]
    identity: SourceIdentity
    license: LicenseBinding
    signature_inventory: SignatureInventory
    compile_context: CompileContext
    closed_expr: ClosedExprRoute
    cluster_inputs: ClusterInputs
    n31_source_proof: N31SourceProof
    strata: Strata
    census_measurements: CensusMeasurements
    operation_eligibility: tuple[OperationEligibility, ...]

    @model_validator(mode="after")
    def _source_fail_closed(self) -> SourceEntry:
        if tuple(item.operation_id for item in self.operation_eligibility) != EXPECTED_OPERATIONS:
            raise ValueError("source operation eligibility must cover exact Wave 1 order")
        if any(item.eligible for item in self.operation_eligibility):
            raise ValueError("incomplete design census cannot make an operation source-eligible")
        if self.census_measurements.populated():
            raise ValueError("design-only census cannot claim measured census counts")

        n31_proof = self.operation_eligibility[-1]
        if self.n31_source_proof.status in {"unknown", "missing"}:
            expected_reason = f"source_proof_{self.n31_source_proof.status}"
            if n31_proof.eligible or n31_proof.reason != expected_reason:
                raise ValueError("unknown/missing proof must remove N31 N-PROOF eligibility")

        if self.source_id == "compiler_data":
            if self.source_kind != "extracted_signature":
                raise ValueError("compiler_data must be signature-elaborated")
            if self.identity.repository_kind != "huggingface_dataset":
                raise ValueError("compiler_data must bind its Hugging Face dataset identity")
            if self.closed_expr.route != (
                "persistent_term_elab_of_signature_and_complete_telescope_without_declaration"
            ):
                raise ValueError("compiler_data must directly elaborate its complete signature")
            if self.closed_expr.input_kind != "source_faithful_signature_text":
                raise ValueError("compiler_data closed Expr input must be source-faithful text")
        else:
            if self.source_kind != "imported_constant":
                raise ValueError("library source must use imported constants")
            if self.identity.repository_kind != "git":
                raise ValueError("library source must bind a git identity")
            if self.closed_expr.route != "constant_info_type_with_canonical_universe_instantiation":
                raise ValueError("library source must close ConstantInfo.type")
            if self.closed_expr.input_kind != "imported_constant_name":
                raise ValueError("library closed Expr input must be a constant name")
        return self


class Wave1SourceCensus(StrictModel):
    schema_version: Literal[1]
    census_id: Literal["sft1_wave1_zero_lean_source_census_v0_3_2"]
    revision: Literal["0.3.2"]
    status: Literal["design_only_incomplete"]
    policy_binding: PolicyBinding
    authorization: Authorization
    completion: Completion
    required_metrics: tuple[NonEmptyStr, ...]
    cluster_contract: ClusterContract
    sources: tuple[SourceEntry, ...]
    unresolved_global_fields: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_incomplete_design(self) -> Wave1SourceCensus:
        if self.required_metrics != EXPECTED_REQUIRED_METRICS:
            raise ValueError("source census required metrics differ from Wave 1 contract")
        if tuple(source.source_id for source in self.sources) != EXPECTED_PROJECTS:
            raise ValueError("source entries must cover canonical projects in canonical order")
        if len(self.unresolved_global_fields) != len(set(self.unresolved_global_fields)):
            raise ValueError("unresolved global fields must be unique")

        bindings = {
            binding.path: binding.sha256
            for source in self.sources
            for binding in source.identity.repo_bindings
        }
        binding_count = sum(len(source.identity.repo_bindings) for source in self.sources)
        if binding_count != len(bindings) or bindings != EXPECTED_REPO_BINDINGS:
            raise ValueError("repo input bindings differ from the exact census design")
        if any(source.n31_source_proof.n31_n_proof_eligible for source in self.sources):
            raise ValueError("current census permits zero N31 N-PROOF project eligibilities")
        return self


@dataclass(frozen=True, slots=True)
class LoadedWave1SourceCensus:
    config: Wave1SourceCensus
    path: Path
    config_hash: str
    config_file_sha256: str


def n31_n_proof_project_eligibility(census: Wave1SourceCensus) -> dict[str, bool]:
    """Return the exact fail-closed N31 N-PROOF project matrix."""

    return {
        source.source_id: source.n31_source_proof.n31_n_proof_eligible for source in census.sources
    }


def _repo_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise SourceCensusError(f"source-census repo binding escapes repository: {relative}")
    return path


def _verify_repo_bindings(root: Path, census: Wave1SourceCensus) -> None:
    expected = {
        census.policy_binding.composition_policy_path: (
            census.policy_binding.composition_policy_file_sha256
        ),
        **EXPECTED_REPO_BINDINGS,
    }
    for relative, expected_hash in expected.items():
        path = _repo_path(root, relative)
        observed = hash_file(path)
        if observed != expected_hash:
            raise SourceCensusError(
                f"source-census repo input drift at {relative}: {observed} != {expected_hash}"
            )


def load_wave1_source_census(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedWave1SourceCensus:
    """Load the design-only census without touching Lean or external artifacts."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    config_path = (path or root / DEFAULT_CENSUS_PATH).resolve()
    if not config_path.is_relative_to(resolved_root):
        raise SourceCensusError("source-census config path escapes repository")
    file_hash = hash_file(config_path)
    if file_hash != EXPECTED_CONFIG_FILE_SHA256:
        raise SourceCensusError("source-census YAML file differs from revision 0.3.2")
    loaded: LoadedConfig[Wave1SourceCensus] = load_config(config_path, Wave1SourceCensus)
    if loaded.config_hash != EXPECTED_CONFIG_HASH:
        raise SourceCensusError("source-census semantic hash differs from revision 0.3.2")
    _verify_repo_bindings(root, loaded.config)
    return LoadedWave1SourceCensus(
        config=loaded.config,
        path=config_path,
        config_hash=loaded.config_hash,
        config_file_sha256=file_hash,
    )

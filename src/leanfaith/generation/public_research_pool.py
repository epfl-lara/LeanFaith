"""Model-free LF-021 public research problem-pool preflight.

This module turns a tiny, curated set of public mathlib docstring/theorem
pairs into immutable :class:`ProblemPoolRecord` artifacts.  It performs no
model execution and creates no semantic label.  The one-example profile must
pass before the full three-record profile is allowed to run.

The source and execution environments are intentionally distinct:

* source provenance is checked against exact public Git objects in a later
  mathlib snapshot;
* stand-alone reference statements are cross-elaborated through LeanInteract
  in LeanFaith's pinned, supported mathlib execution environment.

That cross-elaboration is not represented as a kernel proof that the expanded
reference is equivalent to the later source declaration.  The limitation is
persisted on every record and in the report.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.config import (
    ProblemPoolConfig,
    SourceAuthorizationConfig,
    load_problem_pool_config,
)
from leanfaith.generation.problem_pool import (
    ProblemPoolCandidate,
    ProblemPoolDenylistBinding,
    build_problem_pool,
)
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.lean.extraction import (
    ExtractedDeclaration,
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import (
    BackendSettings,
    LeanInteractBackend,
)
from leanfaith.lean.project_registry import (
    ContextPayload,
    build_context_record,
    check_project_revision,
    check_project_toolchain,
    load_environment_lock,
    load_project_registry,
)
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas.enums import NLTrust, ValidationStatus, ViewStatus
from leanfaith.schemas.theorem import (
    ContextRecord,
    RepresentationRecord,
    TheoremRecord,
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_REFERENCE_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "semantic_atoms",
    "operator_tree",
)

SOURCE_CONFIG = Path("configs/sources/mathlib_post_formalrx_docstrings_v1.yaml")
SOURCE_MANIFEST = Path(
    "data/raw/real_outputs/public_research_v1/mathlib_post_formalrx_docstrings_v1.json"
)
POOL_CONFIG = Path("configs/generation/problem_pool_public_research_v1.yaml")
SOURCE_MATRIX = Path("configs/generation/local_research_source_matrix_v1.yaml")
FORMALRX_LINEAGES = Path("configs/benchmarks/formalrx_lineages.yaml")
PUBLIC_PROFILE = Path("configs/sources/public_research_replication_v1.yaml")

ONE_EXAMPLE_OUTPUT = Path("data/parsed/real_outputs/public_research_v1/one_example_preflight_v1")
ONE_EXAMPLE_REPORT = Path("reports/generation/lf021_public_research_one_example_preflight_v1.json")


class PublicResearchPoolError(RuntimeError):
    """The public pool cannot be admitted under the frozen preflight."""


def _relative_path(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field} must be a nonempty repository-relative path")
    return value


class PublicResearchSourceRecord(StrictModel):
    problem_id: str = Field(min_length=1)
    original_declaration_name: str = Field(min_length=1)
    generated_declaration_name: str = Field(min_length=1)
    introduction_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    introduction_commit_date: datetime.datetime
    introduction_author: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_blob_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    formalrx_source_lineage_tag: Literal["mathlib_docstring_theorem_pairs"]
    temporal_provenance: Literal["post_submission_commit"]
    pretraining_contamination_status: Literal["unknown"]
    docstring_block: str = Field(min_length=1)
    nl_claim_span: str = Field(min_length=1)
    nl_claim_selection: Literal["exact_contiguous_claim_span_from_contributor_docstring"]
    nl_statement_normalization: Literal["markdown_delimiters_removed_and_whitespace_collapsed_v1"]
    nl_statement: str = Field(min_length=1)
    source_signature_anchor: str = Field(min_length=1)
    reference_statement: str = Field(min_length=1)
    reference_derivation: Literal["manual_namespace_and_section_expansion_from_source_signature"]
    reference_equivalence_to_source_status: Literal[
        "textually_derived_cross_elaborated_not_kernel_compared_across_snapshot"
    ]
    domain: str = Field(min_length=1)
    near_duplicate_group_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _checks(self) -> PublicResearchSourceRecord:
        _relative_path(self.source_path, field="source_path")
        if self.introduction_commit_date.tzinfo is None:
            raise ValueError("introduction_commit_date must be timezone-aware")
        if list(self.near_duplicate_group_ids) != sorted(set(self.near_duplicate_group_ids)):
            raise ValueError("near_duplicate_group_ids must be sorted and unique")
        if self.nl_claim_span not in self.docstring_block:
            raise ValueError("nl_claim_span must be an exact contiguous docstring span")
        normalized_claim = " ".join(self.nl_claim_span.replace("**", "").replace("`", "").split())
        if normalized_claim != self.nl_statement:
            raise ValueError(
                "nl_statement must be the frozen lossless-markdown normalization of nl_claim_span"
            )
        expected = f"theorem {self.generated_declaration_name}"
        if expected not in self.reference_statement:
            raise ValueError("reference_statement must declare generated_declaration_name")
        source_name_match = re.match(
            r"^theorem\s+([^\s{(]+)",
            self.source_signature_anchor,
        )
        allowed_source_names = {
            self.original_declaration_name,
            self.original_declaration_name.rsplit(".", 1)[-1],
        }
        if source_name_match is None or source_name_match.group(1) not in allowed_source_names:
            raise ValueError("source_signature_anchor must name original_declaration_name")
        return self


class PublicResearchSourceManifest(StrictModel):
    schema_version: Literal[1] = 1
    source: Literal["mathlib_post_formalrx_docstrings_v1"]
    repository: Literal["https://github.com/leanprover-community/mathlib4"]
    source_snapshot_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    formalrx_v1_submission_cutoff: datetime.datetime
    frozen_at: datetime.datetime
    records: tuple[PublicResearchSourceRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _checks(self) -> PublicResearchSourceManifest:
        if self.formalrx_v1_submission_cutoff.tzinfo is None or self.frozen_at.tzinfo is None:
            raise ValueError("source-manifest timestamps must be timezone-aware")
        problem_ids = [record.problem_id for record in self.records]
        if len(problem_ids) != len(set(problem_ids)):
            raise ValueError("source-manifest problem IDs must be unique")
        names = [record.generated_declaration_name for record in self.records]
        if len(names) != len(set(names)):
            raise ValueError("generated declaration names must be unique")
        return self


class LocalResearchFamily(StrictModel):
    family_id: str
    model: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    role: Literal["supervision_candidate"]
    transport: Literal["local"]
    pool_compatible: Literal[True]
    scientific_activation: str

    @model_validator(mode="after")
    def _disabled(self) -> LocalResearchFamily:
        if not self.scientific_activation.startswith("disabled_"):
            raise ValueError("public pool source matrix cannot activate a model")
        return self


class HeldoutResearchFamily(StrictModel):
    family_id: Literal["reform_8b"]
    model: Literal["GuoxinChen/ReForm-8B"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    supervision_eligible: Literal[False]


class LocalResearchSourceMatrix(StrictModel):
    schema_version: Literal[1] = 1
    matrix_id: Literal["local_research_source_matrix_v1"]
    status: Literal["pool_compatible_generation_disabled"]
    source: Literal["mathlib_post_formalrx_docstrings_v1"]
    private_source_content: Literal[False]
    external_transmission_required: Literal[False]
    semantic_labels_created: Literal[False]
    gate_5g_credit_authorized: Literal[False]
    families: tuple[LocalResearchFamily, ...]
    heldout: HeldoutResearchFamily
    rules: tuple[str, ...]

    @model_validator(mode="after")
    def _three_distinct_disabled_families(self) -> LocalResearchSourceMatrix:
        if len(self.families) != 3:
            raise ValueError("public research slice requires exactly three local candidates")
        ids = [family.family_id for family in self.families]
        models = [family.model for family in self.families]
        if len(ids) != len(set(ids)) or len(models) != len(set(models)):
            raise ValueError("local candidate family IDs and model IDs must be unique")
        return self


class SourceProvenanceAudit(StrictModel):
    problem_id: str
    snapshot_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    introduction_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    introduction_is_snapshot_ancestor: Literal[True] = True
    introduction_is_first_text_occurrence: Literal[True] = True
    introduction_commit_date_matches: Literal[True] = True
    introduction_author_matches: Literal[True] = True
    introduction_after_formalrx_cutoff: Literal[True] = True
    introduction_not_after_snapshot: Literal[True] = True
    source_blob_sha1_matches: Literal[True] = True
    docstring_exact_in_introduction_blob: Literal[True] = True
    signature_exact_in_introduction_blob: Literal[True] = True
    docstring_immediately_precedes_signature: Literal[True] = True
    nl_claim_span_exact_in_docstring: Literal[True] = True
    nl_claim_normalization_verified: Literal[True] = True
    pair_present_in_snapshot: Literal[True] = True
    pair_adjacent_in_snapshot: Literal[True] = True
    source_blob_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActiveRegistryScreens(StrictModel):
    """The three independent active-registry admission surfaces."""

    problem_identity_and_nl_hits: tuple[str, ...]
    reference_lean_text_hits: tuple[str, ...]
    reference_representation_hits: tuple[str, ...]
    all_three_screens_executed: Literal[True] = True
    all_three_screens_clear: bool
    registry_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _consistent(self) -> ActiveRegistryScreens:
        clear = not (
            self.problem_identity_and_nl_hits
            or self.reference_lean_text_hits
            or self.reference_representation_hits
        )
        if self.all_three_screens_clear != clear:
            raise ValueError("all_three_screens_clear does not match hit sets")
        return self


class PublicResearchRecordAudit(StrictModel):
    problem_id: str
    source_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_provenance: SourceProvenanceAudit
    problem_record_id: str
    problem_eligibility: Literal["eligible"]
    reference_theorem_id: str
    reference_representation_id: str
    reference_statement_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_representation_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_alpha_identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    nl_claim_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nl_statement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nl_claim_normalization_verified: Literal[True] = True
    active_registry_screens: ActiveRegistryScreens
    formalrx_source_lineage_tag: Literal["mathlib_docstring_theorem_pairs"]
    temporal_provenance: Literal["post_submission_commit"]
    pretraining_contamination_status: Literal["unknown"]
    source_independent_claim_eligible: Literal[False] = False
    heldout_generator_claim_eligible: Literal[False] = False
    reference_equivalence_to_source_status: Literal[
        "textually_derived_cross_elaborated_not_kernel_compared_across_snapshot"
    ]


class PublicResearchPoolManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf021_public_research_problem_pool"]
    profile: Literal["one_example_preflight_v1", "three_record_slice_v1"]
    frozen_at: datetime.datetime
    source_record_count: int = Field(ge=1)
    eligible_problem_count: int = Field(ge=1)
    reference_theorem_count: int = Field(ge=1)
    reference_representation_count: int = Field(ge=1)
    context_id: str
    source_snapshot_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_project_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_lean_version_guard_passed: Literal[True] = True
    runtime_lean_version_guard_request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hashes: dict[str, str]
    output_hashes: dict[str, str]
    record_ids: tuple[str, ...]
    theorem_ids: tuple[str, ...]
    representation_ids: tuple[str, ...]
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_hashes(self) -> PublicResearchPoolManifest:
        counts = (
            self.source_record_count,
            self.eligible_problem_count,
            self.reference_theorem_count,
            self.reference_representation_count,
        )
        if len(set(counts)) != 1:
            raise ValueError("public pool manifest counts must reconcile")
        for mapping_name in ("input_hashes", "output_hashes"):
            mapping = getattr(self, mapping_name)
            if not mapping:
                raise ValueError(f"{mapping_name} must be nonempty")
            if any(_HEX64.fullmatch(value) is None for value in mapping.values()):
                raise ValueError(f"{mapping_name} values must be SHA-256 digests")
        return self


class PublicResearchPoolReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_public_research_problem_pool_preflight"]
    profile: Literal["one_example_preflight_v1", "three_record_slice_v1"]
    passed: bool
    one_example_preflight_required: Literal[True] = True
    one_example_preflight_passed_first: bool
    one_example_preflight_artifact: str | None
    one_example_preflight_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pool_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_matrix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formalrx_lineages_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_artifact: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_id: str
    runtime_lean_version_guard_passed: Literal[True] = True
    runtime_lean_version_guard_request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_count: int = Field(ge=1)
    eligible_problem_count: int = Field(ge=0)
    complete_three_screen_count: int = Field(ge=0)
    clear_three_screen_count: int = Field(ge=0)
    record_audits: tuple[PublicResearchRecordAudit, ...]
    local_candidate_family_count: Literal[3] = 3
    local_candidate_generation_enabled: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _consistent(self) -> PublicResearchPoolReport:
        expected_pass = (
            self.one_example_preflight_passed_first
            and self.source_record_count == self.eligible_problem_count
            and self.source_record_count == self.complete_three_screen_count
            and self.source_record_count == self.clear_three_screen_count
            and len(self.record_audits) == self.source_record_count
        )
        if self.passed != expected_pass:
            raise ValueError("report passed flag does not match preflight accounting")
        if self.profile == "three_record_slice_v1" and (
            self.one_example_preflight_artifact is None or self.one_example_preflight_sha256 is None
        ):
            raise ValueError("full slice must bind the prior one-example report")
        return self


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    source: PublicResearchSourceRecord
    provenance: SourceProvenanceAudit
    source_record_id: str
    source_record_content_hash: str
    theorem: TheoremRecord
    representation: RepresentationRecord


@dataclass(frozen=True, slots=True)
class PublicResearchPoolRun:
    report_path: Path
    manifest_path: Path
    report: PublicResearchPoolReport
    manifest: PublicResearchPoolManifest


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublicResearchPoolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_source_manifest(path: Path) -> PublicResearchSourceManifest:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        return PublicResearchSourceManifest.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicResearchPoolError(f"invalid public source manifest {path}: {exc}") from exc


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=check,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicResearchPoolError(
            f"public source Git provenance check failed: git {' '.join(args)}: {exc}"
        ) from exc


def _git_blob(repo: Path, revision: str, source_path: str) -> str:
    return _git(repo, "show", f"{revision}:{source_path}").stdout


def _verify_source_provenance(
    *,
    repo: Path,
    manifest: PublicResearchSourceManifest,
    record: PublicResearchSourceRecord,
    source_config: dict[str, object],
) -> SourceProvenanceAudit:
    snapshot = manifest.source_snapshot_revision
    _git(repo, "cat-file", "-e", f"{snapshot}^{{commit}}")
    _git(repo, "cat-file", "-e", f"{record.introduction_commit}^{{commit}}")
    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        record.introduction_commit,
        snapshot,
        check=False,
    )
    if ancestor.returncode != 0:
        raise PublicResearchPoolError(
            f"{record.problem_id}: introduction commit is not an ancestor of snapshot"
        )

    commit_lines = _git(
        repo,
        "show",
        "-s",
        "--format=%cI%n%an",
        record.introduction_commit,
    ).stdout.splitlines()
    if len(commit_lines) < 2:
        raise PublicResearchPoolError(
            f"{record.problem_id}: cannot parse introduction commit metadata"
        )
    observed_date = datetime.datetime.fromisoformat(commit_lines[0])
    observed_author = "\n".join(commit_lines[1:])
    if observed_date != record.introduction_commit_date:
        raise PublicResearchPoolError(f"{record.problem_id}: introduction commit date mismatch")
    if observed_author != record.introduction_author:
        raise PublicResearchPoolError(f"{record.problem_id}: introduction author mismatch")
    if not observed_date > manifest.formalrx_v1_submission_cutoff:
        raise PublicResearchPoolError(
            f"{record.problem_id}: introduction is not after the frozen FormalRx cutoff"
        )

    snapshot_date_raw = source_config.get("source_snapshot_date")
    if not isinstance(snapshot_date_raw, str):
        raise PublicResearchPoolError("source config lacks source_snapshot_date")
    snapshot_date = datetime.datetime.fromisoformat(snapshot_date_raw)
    if observed_date > snapshot_date:
        raise PublicResearchPoolError(
            f"{record.problem_id}: introduction is after source snapshot date"
        )

    source_decl_match = re.match(
        r"^theorem\s+([^\s{(]+)",
        record.source_signature_anchor,
    )
    assert source_decl_match is not None
    first_occurrences = _git(
        repo,
        "log",
        "--reverse",
        "--format=%H",
        f"-Stheorem {source_decl_match.group(1)}",
        snapshot,
        "--",
        record.source_path,
    ).stdout.splitlines()
    if not first_occurrences or first_occurrences[0] != record.introduction_commit:
        raise PublicResearchPoolError(
            f"{record.problem_id}: configured commit is not the first textual introduction"
        )

    observed_blob = _git(
        repo, "rev-parse", f"{record.introduction_commit}:{record.source_path}"
    ).stdout.strip()
    if observed_blob != record.source_blob_sha1:
        raise PublicResearchPoolError(f"{record.problem_id}: source blob SHA-1 mismatch")

    introduction_blob = _git_blob(repo, record.introduction_commit, record.source_path)
    snapshot_blob = _git_blob(repo, snapshot, record.source_path)
    doc_index = introduction_blob.find(record.docstring_block)
    signature_index = introduction_blob.find(record.source_signature_anchor)
    if doc_index < 0 or signature_index < 0:
        raise PublicResearchPoolError(
            f"{record.problem_id}: exact docstring/signature pair missing from intro blob"
        )
    between = introduction_blob[doc_index + len(record.docstring_block) : signature_index]
    if signature_index <= doc_index or between.strip():
        raise PublicResearchPoolError(
            f"{record.problem_id}: docstring does not immediately precede signature"
        )
    snapshot_doc_index = snapshot_blob.find(record.docstring_block)
    snapshot_signature_index = snapshot_blob.find(record.source_signature_anchor)
    if snapshot_doc_index < 0 or snapshot_signature_index < 0:
        raise PublicResearchPoolError(
            f"{record.problem_id}: exact pair is absent from configured source snapshot"
        )
    snapshot_between = snapshot_blob[
        snapshot_doc_index + len(record.docstring_block) : snapshot_signature_index
    ]
    if snapshot_signature_index <= snapshot_doc_index or snapshot_between.strip():
        raise PublicResearchPoolError(
            f"{record.problem_id}: docstring/signature are not adjacent in source snapshot"
        )

    return SourceProvenanceAudit(
        problem_id=record.problem_id,
        snapshot_revision=snapshot,
        introduction_commit=record.introduction_commit,
        source_blob_sha1=record.source_blob_sha1,
        source_blob_sha256=sha256_hex(introduction_blob.encode("utf-8")),
    )


def _build_execution_context(
    *,
    paths: RepoPaths,
    project_dir: Path,
    header_text: str,
    project_registry_key: str,
) -> ContextRecord:
    registry = load_project_registry(paths)
    spec = registry.get(project_registry_key)
    if spec is None:
        raise PublicResearchPoolError(f"project registry has no {project_registry_key!r} entry")
    try:
        project_revision = check_project_revision(spec, project_dir)
        lock = load_environment_lock(paths)
        lean_version = check_project_toolchain(spec, project_dir, lock.toolchain_lock)
    except Exception as exc:
        raise PublicResearchPoolError(
            f"pinned Lean execution context preflight failed: {exc}"
        ) from exc
    imports = tuple(
        module
        for line in header_text.splitlines()
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
        project_uri=spec.uri,
        project_revision=project_revision,
        imports=imports,
        header_text=header_text,
    )
    return build_context_record(
        payload,
        project_kind=spec.kind.value,
        project_registry_key=spec.registry_key,
    )


def _source_record_identity(
    manifest: PublicResearchSourceManifest,
    record: PublicResearchSourceRecord,
) -> tuple[str, str]:
    source_record_id = hash_canonical(
        {
            "schema": "mathlib_post_formalrx_docstring_locator_v1",
            "repository": manifest.repository,
            "snapshot_revision": manifest.source_snapshot_revision,
            "introduction_commit": record.introduction_commit,
            "source_path": record.source_path,
            "original_declaration_name": record.original_declaration_name,
        }
    )
    source_record_content_hash = hash_canonical(
        {
            "schema": "mathlib_post_formalrx_docstring_content_v1",
            "record": record.model_dump(mode="json"),
        }
    )
    return source_record_id, source_record_content_hash


def _materialize_reference(
    *,
    backend: LeanInteractBackend,
    context: ContextRecord,
    header_text: str,
    manifest: PublicResearchSourceManifest,
    record: PublicResearchSourceRecord,
    provenance: SourceProvenanceAudit,
    created_at: datetime.datetime,
) -> _PreparedRecord:
    source_record_id, source_content_hash = _source_record_identity(manifest, record)
    source = header_text + record.reference_statement.rstrip() + "\n"
    request = LeanRequest(
        request_id=f"lf021-public-reference-{source_record_id[:16]}",
        context_id=context.context_id,
        code=source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={"problem_id": record.problem_id},
    )
    result = backend.run(request)
    if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
        raise PublicResearchPoolError(
            f"{record.problem_id}: reference does not elaborate: {result.status.value}"
        )
    declarations = tuple(result.declarations)
    if len(declarations) != 1:
        raise PublicResearchPoolError(
            f"{record.problem_id}: expected one reference declaration, got {len(declarations)}"
        )
    declaration = declarations[0]
    observed_name = str(declaration.get("name") or "")
    if observed_name != record.generated_declaration_name:
        raise PublicResearchPoolError(
            f"{record.problem_id}: expected declaration {record.generated_declaration_name!r}, "
            f"got {observed_name!r}"
        )

    nl_source_link = f"{manifest.repository}/blob/{record.introduction_commit}/{record.source_path}"
    extraction = extract_from_declarations(
        SourceIdentity(
            source=manifest.source,
            source_revision=manifest.source_snapshot_revision,
            source_record=(
                f"{record.introduction_commit}:{record.source_path}:"
                f"{record.original_declaration_name}"
            ),
            source_record_id=source_record_id,
            context_id=context.context_id,
            extraction_route="public_git_docstring_reference_v1",
            nl_pair_eligibility="trusted_public_docstring",
            source_split="curated_post_formalrx_v1",
            source_file=record.source_path,
            nl_source_link=nl_source_link,
            nl_trust=NLTrust.TRUSTED,
        ),
        source,
        list(declarations),
        created_at=created_at,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        lean_result_id=result.request_hash,
    )
    if extraction.failures or len(extraction.accepted) != 1:
        details = "; ".join(
            f"{failure.code.value}:{failure.detail}" for failure in extraction.failures
        )
        raise PublicResearchPoolError(
            f"{record.problem_id}: reference extraction failed: "
            f"{details or len(extraction.accepted)} accepted"
        )
    extracted: ExtractedDeclaration = extraction.accepted[0]
    inline_source = reconstruct_for_revalidation(
        source,
        extracted.declaration,
        extracted.proof_stripped,
    )
    theorem = TheoremRecord.model_validate(
        {
            **extracted.theorem.model_dump(mode="python"),
            "inline_elaboration_source": inline_source,
            "metadata": {
                **extracted.theorem.metadata,
                "problem_id": record.problem_id,
                "original_declaration_name": record.original_declaration_name,
                "source_introduction_commit": record.introduction_commit,
                "formalrx_source_lineage_tag": record.formalrx_source_lineage_tag,
                "temporal_provenance": record.temporal_provenance,
                "pretraining_contamination_status": (record.pretraining_contamination_status),
                "reference_derivation": record.reference_derivation,
                "reference_equivalence_to_source_status": (
                    record.reference_equivalence_to_source_status
                ),
                "resolved_semantic_label": False,
            },
        }
    )
    representation_result = build_representation_batch(
        backend,
        RepresentationBatch(
            context_id=context.context_id,
            import_header="",
            ordered_theorem_inputs=(
                TheoremForRepresentation(
                    theorem_id=theorem.theorem_id,
                    full_name=record.generated_declaration_name,
                    proof_stripped=theorem.proof_stripped_declaration,
                    context_id=context.context_id,
                    source_signature=(
                        str((declaration.get("signature") or {}).get("pp", "")).strip() or None
                    ),
                    inline_declaration=True,
                    inline_source=inline_source,
                ),
            ),
        ),
        created_at=created_at,
    )
    if representation_result.per_theorem_failures:
        details = "; ".join(
            f"{failure.view}:{failure.detail}"
            for failure in representation_result.per_theorem_failures
        )
        raise PublicResearchPoolError(
            f"{record.problem_id}: reference representation failed: {details}"
        )
    if len(representation_result.ordered_representation_records) != 1:
        raise PublicResearchPoolError(f"{record.problem_id}: representation count is not one")
    representation = representation_result.ordered_representation_records[0]
    missing = tuple(
        view
        for view in _REQUIRED_REFERENCE_VIEWS
        if representation.view_status[view] is not ViewStatus.OK
    )
    if missing or representation.alpha_identity_fingerprint is None:
        raise PublicResearchPoolError(
            f"{record.problem_id}: reference lacks required views: "
            + ", ".join((*missing, "alpha_identity_fingerprint"))
        )
    return _PreparedRecord(
        source=record,
        provenance=provenance,
        source_record_id=source_record_id,
        source_record_content_hash=source_content_hash,
        theorem=theorem,
        representation=representation,
    )


def _validate_ready_pool_source(
    *,
    paths: RepoPaths,
    config: ProblemPoolConfig,
) -> None:
    enabled = tuple(source for source in config.sources if source.enabled)
    if len(enabled) != 1:
        raise PublicResearchPoolError("public research pool must enable exactly one source")
    source = enabled[0]
    source_path = paths.root / source.source_config
    if source.source_config_sha256 != hash_file(source_path):
        raise PublicResearchPoolError("public source config hash mismatch")
    raw = load_yaml_mapping(source_path)
    authorization = raw.get("lf021_authorization")
    if not isinstance(authorization, dict):
        raise PublicResearchPoolError("public source lacks lf021_authorization")
    observed = SourceAuthorizationConfig.model_validate(authorization)
    if source.authorization != observed:
        raise PublicResearchPoolError("pool/source authorization mismatch")
    if observed.private_source or not observed.external_transmission:
        raise PublicResearchPoolError("public research source authorization is not public")
    probe = raw.get("probe")
    if not isinstance(probe, dict) or (probe.get("resolved_revision") != observed.source_revision):
        raise PublicResearchPoolError("public source probe revision mismatch")
    profile = paths.root / config.public_replication_profile
    if not profile.is_file() or profile != paths.root / PUBLIC_PROFILE:
        raise PublicResearchPoolError("unexpected public replication profile")


def _problem_candidate(
    *,
    prepared: _PreparedRecord,
    manifest: PublicResearchSourceManifest,
    context: ContextRecord,
    header_path: Path,
    header_hash: str,
    paths: RepoPaths,
) -> ProblemPoolCandidate:
    record = prepared.source
    problem_group = "nl-problem:" + hash_canonical(
        {
            "schema": "public_mathlib_nl_problem_group_v1",
            "source_record_id": prepared.source_record_id,
        }
    )
    nl_source_link = f"{manifest.repository}/blob/{record.introduction_commit}/{record.source_path}"
    return ProblemPoolCandidate(
        problem_id=record.problem_id,
        problem_group=problem_group,
        source=manifest.source,
        source_revision=manifest.source_snapshot_revision,
        source_split="curated_post_formalrx_v1",
        source_record_id=prepared.source_record_id,
        source_record_content_hash=prepared.source_record_content_hash,
        nl_statement=record.nl_statement,
        nl_trust=NLTrust.TRUSTED,
        nl_source_link=nl_source_link,
        context_id=context.context_id,
        import_header_artifact=str(header_path.relative_to(paths.root)),
        import_header_hash=header_hash,
        reference_theorem_ids=(prepared.theorem.theorem_id,),
        source_license="Apache-2.0",
        private_source_content=False,
        release_eligible=True,
        near_duplicate_group_ids=record.near_duplicate_group_ids,
        overlap_tags=(
            "formalrx_lineage:mathlib_docstring_theorem_pairs",
            "pretraining_contamination:unknown",
            "temporal_provenance:post_submission_commit",
        ),
        metadata={
            "domain": record.domain,
            "original_declaration_name": record.original_declaration_name,
            "introduction_commit": record.introduction_commit,
            "source_blob_sha1": record.source_blob_sha1,
            "reference_equivalence_to_source_status": (
                record.reference_equivalence_to_source_status
            ),
            "source_independent_claim_eligible": False,
            "heldout_generator_claim_eligible": False,
            "semantic_label_present": False,
        },
    )


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        observed = path.read_bytes()
        if observed != payload:
            raise PublicResearchPoolError(
                f"immutable output differs from existing artifact: {path}"
            )
        return
    path.write_bytes(payload)


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(values: tuple[object, ...]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _hash_output_files(files: dict[str, Path]) -> dict[str, str]:
    return {name: hash_file(path) for name, path in sorted(files.items())}


def _validate_one_example_report(
    *,
    paths: RepoPaths,
    source_manifest_hash: str,
    first_problem_id: str,
) -> tuple[str, str]:
    path = paths.root / ONE_EXAMPLE_REPORT
    if not path.is_file():
        raise PublicResearchPoolError(
            "full slice requires the one-example preflight artifact first"
        )
    try:
        report = PublicResearchPoolReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PublicResearchPoolError(f"invalid one-example preflight report: {exc}") from exc
    if (
        not report.passed
        or report.profile != "one_example_preflight_v1"
        or report.source_manifest_sha256 != source_manifest_hash
        or len(report.record_audits) != 1
        or report.record_audits[0].problem_id != first_problem_id
    ):
        raise PublicResearchPoolError(
            "one-example preflight does not bind the current first source record"
        )
    return str(ONE_EXAMPLE_REPORT), hash_file(path)


def run_public_research_pool(
    *,
    paths: RepoPaths,
    public_source_repo: Path,
    execution_project_dir: Path,
    profile: Literal["one_example_preflight_v1", "three_record_slice_v1"],
) -> PublicResearchPoolRun:
    """Run the model-free public pool preflight and persist immutable outputs."""

    root = paths.root.resolve()
    source_manifest_path = root / SOURCE_MANIFEST
    source_config_path = root / SOURCE_CONFIG
    pool_config_path = root / POOL_CONFIG
    source_matrix_path = root / SOURCE_MATRIX
    formalrx_lineages_path = root / FORMALRX_LINEAGES
    public_profile_path = root / PUBLIC_PROFILE

    source_manifest = _load_source_manifest(source_manifest_path)
    source_config = load_yaml_mapping(source_config_path)
    if source_config.get("source_snapshot_revision") != (source_manifest.source_snapshot_revision):
        raise PublicResearchPoolError("source config/manifest snapshot mismatch")
    if source_config.get("repo_url") != source_manifest.repository:
        raise PublicResearchPoolError("source config/manifest repository mismatch")
    configured_snapshot_date = source_config.get("source_snapshot_date")
    if not isinstance(configured_snapshot_date, str):
        raise PublicResearchPoolError("source config lacks source_snapshot_date")
    observed_snapshot_date = _git(
        public_source_repo,
        "show",
        "-s",
        "--format=%cI",
        source_manifest.source_snapshot_revision,
    ).stdout.strip()
    if observed_snapshot_date != configured_snapshot_date:
        raise PublicResearchPoolError("source snapshot commit date mismatch")
    license_blob = _git(
        public_source_repo,
        "rev-parse",
        f"{source_manifest.source_snapshot_revision}:LICENSE",
    ).stdout.strip()
    if license_blob != source_config.get("license_blob_sha1"):
        raise PublicResearchPoolError("source snapshot LICENSE blob mismatch")

    loaded_pool = load_problem_pool_config(pool_config_path)
    _validate_ready_pool_source(paths=paths, config=loaded_pool.config)
    source_matrix = load_config(source_matrix_path, LocalResearchSourceMatrix).config
    if source_matrix.source != source_manifest.source:
        raise PublicResearchPoolError("local source matrix names the wrong source")

    formalrx = load_yaml_mapping(formalrx_lineages_path)
    lineages = formalrx.get("source_lineages_to_tag")
    if not isinstance(lineages, list) or ("mathlib_docstring_theorem_pairs" not in lineages):
        raise PublicResearchPoolError(
            "FormalRx lineage registry lacks mathlib_docstring_theorem_pairs"
        )

    header_path = root / str(source_config["execution_environment"]["import_header_artifact"])
    if header_path.is_symlink() or not header_path.is_file():
        raise PublicResearchPoolError("public research import header is not a regular file")
    header_text = header_path.read_text(encoding="utf-8")
    header_hash = hash_file(header_path)
    context = _build_execution_context(
        paths=paths,
        project_dir=execution_project_dir,
        header_text=header_text,
        project_registry_key=str(source_config["execution_environment"]["project_registry_key"]),
    )
    expected_execution_revision = str(source_config["execution_environment"]["project_revision"])
    if context.project_revision != expected_execution_revision:
        raise PublicResearchPoolError("execution context revision differs from source config")

    active = load_active_benchmark_registry(repo_root=root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(active, repo_root=root)
    if loaded_pool.config.active_benchmark_registry_manifest_sha256 != denylist.manifest_sha256:
        raise PublicResearchPoolError("pool benchmark binding mismatch")

    all_records = source_manifest.records
    selected = all_records[:1] if profile == "one_example_preflight_v1" else all_records
    if profile == "three_record_slice_v1" and len(selected) != 3:
        raise PublicResearchPoolError("full public research slice must contain exactly 3 records")

    prior_preflight_artifact: str | None = None
    prior_preflight_hash: str | None = None
    if profile == "three_record_slice_v1":
        prior_preflight_artifact, prior_preflight_hash = _validate_one_example_report(
            paths=paths,
            source_manifest_hash=hash_file(source_manifest_path),
            first_problem_id=selected[0].problem_id,
        )

    output_dir = (
        root / ONE_EXAMPLE_OUTPUT
        if profile == "one_example_preflight_v1"
        else (root / loaded_pool.config.outputs.records).parent
    )
    raw_response_dir = output_dir / "lean_raw"
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=execution_project_dir,
            context_fingerprint=context.context_fingerprint,
            environment_schema_version=context.environment_schema_version,
            raw_response_dir=raw_response_dir,
        )
    )
    prepared: list[_PreparedRecord] = []
    try:
        expected_runtime_version = context.lean_version.removeprefix("v")
        runtime_guard = backend.run(
            LeanRequest(
                request_id=f"lf021-public-runtime-version-{expected_runtime_version}",
                context_id=context.context_id,
                code=(f'import Lean\n#guard Lean.versionString == "{expected_runtime_version}"\n'),
                timeout_seconds=60.0,
            )
        )
        if runtime_guard.status is not LeanStatus.VALID:
            raise PublicResearchPoolError(
                "LeanInteract runtime version does not match the registered context"
            )
        for record in selected:
            provenance = _verify_source_provenance(
                repo=public_source_repo,
                manifest=source_manifest,
                record=record,
                source_config=source_config,
            )
            prepared.append(
                _materialize_reference(
                    backend=backend,
                    context=context,
                    header_text=header_text,
                    manifest=source_manifest,
                    record=record,
                    provenance=provenance,
                    created_at=source_manifest.frozen_at,
                )
            )
    finally:
        backend.close()

    candidates = tuple(
        _problem_candidate(
            prepared=item,
            manifest=source_manifest,
            context=context,
            header_path=header_path,
            header_hash=header_hash,
            paths=paths,
        )
        for item in prepared
    )
    pool = build_problem_pool(
        config=loaded_pool.config,
        denylist=denylist,
        candidates=candidates,
    )
    pool_by_problem = {record.problem_id: record for record in pool.records}
    audits: list[PublicResearchRecordAudit] = []
    for item in prepared:
        problem = pool_by_problem[item.source.problem_id]
        if problem.eligibility != "eligible":
            raise PublicResearchPoolError(
                f"{item.source.problem_id}: pool exclusion: " + ", ".join(problem.exclusion_reasons)
            )
        reference_hits = candidate_benchmark_hits(
            denylist_index=denylist.index,
            theorem=item.theorem,
            representation=item.representation,
        )
        lean_hits = tuple(hit for hit in reference_hits if hit.startswith("lean:"))
        representation_hits = tuple(
            hit for hit in reference_hits if hit.startswith("representation:")
        )
        unexpected = tuple(
            hit
            for hit in reference_hits
            if not (hit.startswith("lean:") or hit.startswith("representation:"))
        )
        if unexpected:
            raise PublicResearchPoolError(
                f"{item.source.problem_id}: unclassified benchmark hits: {unexpected}"
            )
        screens = ActiveRegistryScreens(
            problem_identity_and_nl_hits=problem.denylist_hits,
            reference_lean_text_hits=lean_hits,
            reference_representation_hits=representation_hits,
            all_three_screens_clear=not (problem.denylist_hits or lean_hits or representation_hits),
            registry_manifest_sha256=denylist.manifest_sha256,
            active_registry_sha256=denylist.active_registry_sha256,
            registry_content_hash=denylist.registry_content_hash,
        )
        if not screens.all_three_screens_clear:
            raise PublicResearchPoolError(
                f"{item.source.problem_id}: active benchmark registry hit"
            )
        assert item.representation.alpha_identity_fingerprint is not None
        audits.append(
            PublicResearchRecordAudit(
                problem_id=item.source.problem_id,
                source_record_id=item.source_record_id,
                source_record_content_hash=item.source_record_content_hash,
                source_provenance=item.provenance,
                problem_record_id=problem.problem_record_id,
                problem_eligibility="eligible",
                reference_theorem_id=item.theorem.theorem_id,
                reference_representation_id=item.representation.representation_id,
                reference_statement_content_hash=item.theorem.statement_content_hash,
                reference_representation_content_hash=item.representation.content_hash,
                reference_alpha_identity_fingerprint=(
                    item.representation.alpha_identity_fingerprint
                ),
                nl_claim_span_sha256=sha256_hex(item.source.nl_claim_span.encode("utf-8")),
                nl_statement_sha256=sha256_hex(item.source.nl_statement.encode("utf-8")),
                active_registry_screens=screens,
                formalrx_source_lineage_tag=item.source.formalrx_source_lineage_tag,
                temporal_provenance=item.source.temporal_provenance,
                pretraining_contamination_status=(item.source.pretraining_contamination_status),
                reference_equivalence_to_source_status=(
                    item.source.reference_equivalence_to_source_status
                ),
            )
        )

    records_path = (
        output_dir / "problem_pool_records.jsonl"
        if profile == "one_example_preflight_v1"
        else root / loaded_pool.config.outputs.records
    )
    failures_path = (
        output_dir / "problem_pool_failures.jsonl"
        if profile == "one_example_preflight_v1"
        else root / loaded_pool.config.outputs.failures
    )
    manifest_path = (
        output_dir / "problem_pool_manifest.json"
        if profile == "one_example_preflight_v1"
        else root / loaded_pool.config.outputs.manifest
    )
    context_path = output_dir / "context.json"
    theorem_path = output_dir / "reference_theorems.jsonl"
    representation_path = output_dir / "reference_representations.jsonl"
    trusted_path = output_dir / "public_trusted_problems.jsonl"
    audit_path = output_dir / "record_audits.jsonl"

    ordered_problem_records = tuple(record.model_dump(mode="json") for record in pool.records)
    ordered_theorems = tuple(
        item.theorem.model_dump(mode="json")
        for item in sorted(prepared, key=lambda value: value.theorem.theorem_id)
    )
    ordered_representations = tuple(
        item.representation.model_dump(mode="json")
        for item in sorted(prepared, key=lambda value: value.representation.representation_id)
    )
    ordered_trusted = tuple(asdict(problem) for problem in pool.public_trusted_problems)
    ordered_audits = tuple(
        audit.model_dump(mode="json") for audit in sorted(audits, key=lambda x: x.problem_id)
    )
    _write_exact(records_path, _jsonl_bytes(ordered_problem_records))
    _write_exact(failures_path, b"")
    _write_exact(context_path, _json_bytes(context.model_dump(mode="json")))
    _write_exact(theorem_path, _jsonl_bytes(ordered_theorems))
    _write_exact(representation_path, _jsonl_bytes(ordered_representations))
    _write_exact(trusted_path, _jsonl_bytes(ordered_trusted))
    _write_exact(audit_path, _jsonl_bytes(ordered_audits))

    raw_files = {
        f"lean_raw/{path.name}": path
        for path in sorted(raw_response_dir.glob("*.json"))
        if path.is_file()
    }
    output_files = {
        "problem_pool_records": records_path,
        "problem_pool_failures": failures_path,
        "context": context_path,
        "reference_theorems": theorem_path,
        "reference_representations": representation_path,
        "public_trusted_problems": trusted_path,
        "record_audits": audit_path,
        **raw_files,
    }
    input_hashes = {
        "source_manifest": hash_file(source_manifest_path),
        "source_config": hash_file(source_config_path),
        "pool_config": hash_file(pool_config_path),
        "source_matrix": hash_file(source_matrix_path),
        "formalrx_lineages": hash_file(formalrx_lineages_path),
        "public_replication_profile": hash_file(public_profile_path),
        "import_header": header_hash,
        "active_benchmark_manifest": denylist.manifest_sha256,
        "active_benchmark_registry": denylist.active_registry_sha256,
        "implementation": hash_file(Path(__file__)),
    }
    manifest = PublicResearchPoolManifest(
        artifact_kind="lf021_public_research_problem_pool",
        profile=profile,
        frozen_at=source_manifest.frozen_at,
        source_record_count=len(selected),
        eligible_problem_count=len(pool.public_trusted_problems),
        reference_theorem_count=len(prepared),
        reference_representation_count=len(prepared),
        context_id=context.context_id,
        source_snapshot_revision=source_manifest.source_snapshot_revision,
        execution_project_revision=context.project_revision,
        runtime_lean_version_guard_request_hash=runtime_guard.request_hash,
        input_hashes=input_hashes,
        output_hashes=_hash_output_files(output_files),
        record_ids=tuple(record.problem_record_id for record in pool.records),
        theorem_ids=tuple(sorted(item.theorem.theorem_id for item in prepared)),
        representation_ids=tuple(
            sorted(item.representation.representation_id for item in prepared)
        ),
    )
    _write_exact(manifest_path, _json_bytes(manifest.model_dump(mode="json")))

    report_path = (
        root / ONE_EXAMPLE_REPORT
        if profile == "one_example_preflight_v1"
        else root / loaded_pool.config.outputs.coverage_report
    )
    blockers = (
        "No generator was executed and no semantic label was created.",
        (
            "This three-record source slice is not a prevalence frame and cannot "
            "close Gate 5 or Gate 5G."
        ),
        (
            "All records retain FormalRx mathlib-docstring lineage; only temporal "
            "provenance is established."
        ),
        "Pretraining contamination is unknown for every candidate generator family.",
        (
            "Reference statements are textually derived and cross-elaborated, not "
            "kernel-compared across source snapshots."
        ),
        (
            "Unresolved evaluation registries remain protected by name but cannot "
            "be exact-hash screened until artifacts are pinned."
        ),
        (
            "Each generator family still requires a frozen overlap record and "
            "scientific run configuration before execution."
        ),
    )
    report = PublicResearchPoolReport(
        report_kind="lf021_public_research_problem_pool_preflight",
        profile=profile,
        passed=True,
        one_example_preflight_passed_first=True,
        one_example_preflight_artifact=prior_preflight_artifact,
        one_example_preflight_sha256=prior_preflight_hash,
        source_manifest_sha256=input_hashes["source_manifest"],
        source_config_sha256=input_hashes["source_config"],
        pool_config_sha256=input_hashes["pool_config"],
        source_matrix_sha256=input_hashes["source_matrix"],
        formalrx_lineages_sha256=input_hashes["formalrx_lineages"],
        manifest_artifact=str(manifest_path.relative_to(root)),
        manifest_sha256=hash_file(manifest_path),
        context_id=context.context_id,
        runtime_lean_version_guard_request_hash=runtime_guard.request_hash,
        source_record_count=len(selected),
        eligible_problem_count=len(pool.public_trusted_problems),
        complete_three_screen_count=len(audits),
        clear_three_screen_count=sum(
            audit.active_registry_screens.all_three_screens_clear for audit in audits
        ),
        record_audits=tuple(sorted(audits, key=lambda audit: audit.problem_id)),
        blockers=blockers,
    )
    _write_exact(report_path, _json_bytes(report.model_dump(mode="json")))
    return PublicResearchPoolRun(
        report_path=report_path,
        manifest_path=manifest_path,
        report=report,
        manifest=manifest,
    )


__all__ = [
    "ActiveRegistryScreens",
    "LocalResearchSourceMatrix",
    "PublicResearchPoolError",
    "PublicResearchPoolManifest",
    "PublicResearchPoolReport",
    "PublicResearchPoolRun",
    "PublicResearchRecordAudit",
    "PublicResearchSourceManifest",
    "PublicResearchSourceRecord",
    "run_public_research_pool",
]

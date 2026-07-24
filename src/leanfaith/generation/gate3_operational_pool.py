"""Fail-closed LF-021 pool materialization for 40 curated Gate-3 docstrings.

This stage consumes the immutable output of
``gate3_docstring_curation`` and converts exactly the 40 operationally
standalone records into canonical ``ProblemPoolRecord``, ``TheoremRecord``,
``RepresentationRecord``, and ``ContextRecord`` artifacts.

The stage deliberately does *not* create a generator collection plan.  The
curation is Codex/LLM-assisted operational adequacy, not human review,
semantic gold, or Gate evidence.  Reference theorem material is retained for
later scoring and is never projected into a generator input.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.config import (
    ProblemPoolConfig,
    SourceAuthorizationConfig,
    load_problem_pool_config,
)
from leanfaith.generation.gate3_docstring_curation import (
    CurationArtifactManifest,
    CurationDecision,
    CurationReport,
    Gate3DocstringCurationConfig,
    OperationalCurationRecord,
)
from leanfaith.generation.gate3_docstring_pool import _exact_pair_present
from leanfaith.generation.problem_pool import (
    ProblemPoolCandidate,
    ProblemPoolDenylistBinding,
    build_problem_pool,
)
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

SOURCE_CONFIG = Path("configs/sources/mathlib_gate3_docstrings_operational_v1.yaml")
POOL_CONFIG = Path("configs/generation/problem_pool_gate3_docstrings_operational_v1.yaml")
REPORT_PATH = Path("reports/generation/lf021_gate3_docstrings_operational_pool_v1.json")
ADEQUACY_REPORT_PATH = Path(
    "reports/generation/lf021_gate3_docstrings_operational_source_adequacy_v1.json"
)
ADEQUACY_MARKDOWN_PATH = Path(
    "reports/generation/lf021_gate3_docstrings_operational_source_adequacy_v1.md"
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_SOURCE_NAME: Literal["mathlib_gate3_docstrings_operational_v1"] = (
    "mathlib_gate3_docstrings_operational_v1"
)
_SOURCE_SPLIT = "gate3_operational_curation_v1"


class Gate3OperationalPoolError(RuntimeError):
    """The 40-record operational pool cannot be reproduced safely."""


class ArtifactBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class OperationalCurationBinding(StrictModel):
    curation_config: ArtifactBinding
    curation_report: ArtifactBinding
    curation_manifest: ArtifactBinding
    admitted_records: ArtifactBinding
    no_sorry_reference_checks: ArtifactBinding
    expected_reviewed: Literal[57]
    expected_admitted: Literal[40]
    reviewer_type: Literal["codex_agent"]
    review_method: Literal["llm_assisted_operational_curation_v1"]
    human_reviewed: Literal[False]
    semantic_gold_created: Literal[False]


class CanonicalReferenceBinding(StrictModel):
    theorem_records: ArtifactBinding
    representation_records: ArtifactBinding
    context: ArtifactBinding
    import_header: ArtifactBinding


class ScreeningBinding(StrictModel):
    active_benchmark_registry_manifest: ArtifactBinding
    rerun_problem_identity_and_nl: Literal[True]
    rerun_reference_lean: Literal[True]
    rerun_reference_representation: Literal[True]
    rerun_exact_normalized_nl_deduplication: Literal[True]
    rerun_supplied_ancestry_deduplication: Literal[True]


class OperationalPoolPolicy(StrictModel):
    expected_admitted_problem_records: Literal[40]
    nl_trust: Literal["trusted"]
    nl_trust_semantics: Literal["human_authored_provenance_only"]
    operational_adequacy_status: Literal["codex_llm_assisted_standalone_sufficient"]
    domain: Literal["Algebra"]
    domain_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    subdomain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_diversity_established: Literal[False]
    model_collection_authorized: Literal[True]
    model_collection_scope: Literal["local_models_only"]
    external_provider_collection_authorized: Literal[False]
    reference_visible_to_generator: Literal[False]
    human_review_claimed: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]
    model_execution_performed: Literal[False]
    generator_collection_plan_created: Literal[False]
    recovery_parser_binding_status: Literal["unresolved"]


class OperationalSourceConfig(StrictModel):
    source_config_version: Literal[1]
    source: Literal["mathlib_gate3_docstrings_operational_v1"]
    kind: Literal["git_curated_docstrings"]
    repo_url: Literal["https://github.com/leanprover-community/mathlib4"]
    revision: str = Field(pattern=_HEX40)
    tag: Literal["v4.31.0-rc1"]
    license: Literal["Apache-2.0"]
    license_blob_sha1: str = Field(pattern=_HEX40)
    lf021_authorization: SourceAuthorizationConfig
    operational_curation: OperationalCurationBinding
    canonical_references: CanonicalReferenceBinding
    screening: ScreeningBinding
    policy: OperationalPoolPolicy

    @model_validator(mode="after")
    def _consistent(self) -> OperationalSourceConfig:
        if self.lf021_authorization.source_revision != self.revision:
            raise ValueError("source authorization revision differs from source revision")
        if self.lf021_authorization.license_id != self.license:
            raise ValueError("source authorization license differs from source license")
        if self.lf021_authorization.private_source:
            raise ValueError("operational mathlib source must remain public")
        return self


class ReferenceCheckBinding(StrictModel):
    candidate_id: str = Field(pattern=r"^gate3_docstring_candidate:[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^gate3_docstring_curation:[0-9a-f]{64}$")
    lean_request_hash: str = Field(pattern=_HEX64)
    raw_response_artifact: ArtifactBinding


class NoSorryReferenceChecks(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["gate3_docstring_reference_checks_v1"]
    count: Literal[40]
    allow_sorry: Literal[False]
    all_valid: Literal[True]
    checks: tuple[ReferenceCheckBinding, ...]

    @model_validator(mode="after")
    def _unique(self) -> NoSorryReferenceChecks:
        candidate_ids = [item.candidate_id for item in self.checks]
        decision_ids = [item.decision_id for item in self.checks]
        if len(self.checks) != self.count:
            raise ValueError("reference-check count differs from check records")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("reference-check candidate IDs are not unique")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("reference-check decision IDs are not unique")
        return self


class RerunRegistryScreens(StrictModel):
    problem_identity_and_nl_hits: tuple[str, ...]
    reference_lean_hits: tuple[str, ...]
    reference_representation_hits: tuple[str, ...]
    all_three_screens_clear: Literal[True]
    registry_manifest_sha256: str = Field(pattern=_HEX64)
    active_registry_sha256: str = Field(pattern=_HEX64)
    registry_content_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _clear(self) -> RerunRegistryScreens:
        if (
            self.problem_identity_and_nl_hits
            or self.reference_lean_hits
            or self.reference_representation_hits
        ):
            raise ValueError("clear registry screens cannot contain hits")
        return self


class OperationalPoolRecordAudit(StrictModel):
    schema_version: Literal[1] = 1
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    problem_id: str = Field(min_length=1)
    source_record_id: str = Field(pattern=_HEX64)
    source_record_content_hash: str = Field(pattern=_HEX64)
    candidate_id: str = Field(pattern=r"^gate3_docstring_candidate:[0-9a-f]{64}$")
    curation_decision_id: str = Field(pattern=r"^gate3_docstring_curation:[0-9a-f]{64}$")
    curation_record_sha256: str = Field(pattern=_HEX64)
    curation_decision: Literal["standalone_sufficient"]
    curation_reviewer_type: Literal["codex_agent"]
    curation_review_method: Literal["llm_assisted_operational_curation_v1"]
    operational_adequacy_status: Literal["codex_llm_assisted_standalone_sufficient"]
    model_collection_authorized: Literal[True]
    model_collection_scope: Literal["local_models_only"]
    external_provider_collection_authorized: Literal[False]
    reference_visible_to_generator: Literal[False]
    human_reviewed: Literal[False]
    semantic_gold_created: Literal[False]
    gate_claimed: Literal[False]
    source_revision: str = Field(pattern=_HEX40)
    source_file: str = Field(min_length=1)
    source_file_sha256: str = Field(pattern=_HEX64)
    source_blob_sha1: str = Field(pattern=_HEX40)
    source_pair_present: Literal[True]
    temporal_introduction_commit: str = Field(pattern=_HEX40)
    temporal_introduction_created_at: datetime.datetime
    temporal_introduction_is_ancestor: Literal[True]
    temporal_exact_pair_present: Literal[True]
    temporal_strictly_postdates_latest_checkpoint: Literal[True]
    reference_theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    reference_representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    reference_statement_content_hash: str = Field(pattern=_HEX64)
    reference_representation_content_hash: str = Field(pattern=_HEX64)
    reference_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    no_sorry_alias_request_hash: str = Field(pattern=_HEX64)
    no_sorry_alias_raw_response_sha256: str = Field(pattern=_HEX64)
    no_sorry_alias_check_valid: Literal[True]
    registry_screens: RerunRegistryScreens

    @model_validator(mode="after")
    def _utc(self) -> OperationalPoolRecordAudit:
        require_utc(self.temporal_introduction_created_at)
        return self


class OperationalPoolManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf021_gate3_docstrings_operational_problem_pool_v1"]
    frozen_at: datetime.datetime
    source: Literal["mathlib_gate3_docstrings_operational_v1"]
    source_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    problem_count: Literal[40]
    curation_reviewed_count: Literal[57]
    curation_admitted_count: Literal[40]
    problem_record_count: Literal[40]
    eligible_problem_count: Literal[40]
    reference_theorem_count: Literal[40]
    reference_representation_count: Literal[40]
    no_sorry_alias_check_count: Literal[40]
    rerun_three_screen_clear_count: Literal[40]
    input_hashes: dict[str, str]
    output_hashes: dict[str, str]
    source_config_artifact: ArtifactBinding
    curation_config_artifact: ArtifactBinding
    curation_report_artifact: ArtifactBinding
    curation_manifest_artifact: ArtifactBinding
    curation_admitted_artifact: ArtifactBinding
    no_sorry_reference_checks_artifact: ArtifactBinding
    problem_records_artifact: ArtifactBinding
    context_artifact: ArtifactBinding
    reference_theorems_artifact: ArtifactBinding
    reference_representations_artifact: ArtifactBinding
    record_audits_artifact: ArtifactBinding
    import_header_artifact: ArtifactBinding
    active_benchmark_manifest_artifact: ArtifactBinding
    active_benchmark_registry_sha256: str = Field(pattern=_HEX64)
    active_benchmark_registry_content_hash: str = Field(pattern=_HEX64)
    domain: Literal["Algebra"]
    domain_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    domain_proxy_counts: dict[str, int]
    subdomain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    subdomain_proxy_counts: dict[str, int]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_diversity_established: Literal[False]
    problem_record_ids: tuple[str, ...]
    problem_groups: tuple[str, ...]
    declaration_full_names: tuple[str, ...]
    theorem_ids: tuple[str, ...]
    representation_ids: tuple[str, ...]
    model_collection_authorized_count: Literal[40]
    reference_visible_to_generator: Literal[False] = False
    human_reviewed: Literal[False] = False
    semantic_gold_created: Literal[False] = False
    gate_claimed: Literal[False] = False
    model_execution_performed: Literal[False] = False
    generator_collection_plan_created: Literal[False] = False
    recovery_parser_binding_status: Literal["unresolved"]

    @model_validator(mode="after")
    def _consistent(self) -> OperationalPoolManifest:
        require_utc(self.frozen_at)
        counts = (
            len(self.problem_record_ids),
            len(self.problem_groups),
            len(self.declaration_full_names),
            len(self.theorem_ids),
            len(self.representation_ids),
        )
        if counts != (40, 40, 40, 40, 40):
            raise ValueError("manifest ID counts do not reconcile 40 admitted records")
        if any(
            len(values) != len(set(values))
            for values in (
                self.problem_record_ids,
                self.problem_groups,
                self.declaration_full_names,
                self.theorem_ids,
                self.representation_ids,
            )
        ):
            raise ValueError("manifest IDs must be unique")
        for mapping_name in ("input_hashes", "output_hashes"):
            mapping = getattr(self, mapping_name)
            if not mapping:
                raise ValueError(f"{mapping_name} must be nonempty")
            if any(len(value) != 64 for value in mapping.values()):
                raise ValueError(f"{mapping_name} contains a non-SHA-256 value")
        if (
            not self.domain_proxy_counts
            or sum(self.domain_proxy_counts.values()) != self.problem_count
            or any(not key or value <= 0 for key, value in self.domain_proxy_counts.items())
        ):
            raise ValueError("domain-proxy counts do not reconcile problem_count")
        if (
            not self.subdomain_proxy_counts
            or sum(self.subdomain_proxy_counts.values()) != self.problem_count
            or any(not key or value <= 0 for key, value in self.subdomain_proxy_counts.items())
        ):
            raise ValueError("subdomain-proxy counts do not reconcile problem_count")
        return self


class OperationalSourceAdequacyReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_gate3_docstrings_operational_source_adequacy_v1"]
    passed: Literal[True]
    candidate_records_reviewed: Literal[57]
    operationally_standalone_admitted: Literal[40]
    operationally_excluded: Literal[17]
    final_problem_records: Literal[40]
    exact_git_source_verified: Literal[40]
    strict_temporal_nonoverlap_verified: Literal[40]
    no_sorry_alias_checks_verified: Literal[40]
    active_registry_three_screen_clear: Literal[40]
    exact_and_ancestry_dedup_survivors: Literal[40]
    canonical_reference_theorems: Literal[40]
    canonical_reference_representations: Literal[40]
    distinct_ancestry_groups: Literal[40]
    domain: Literal["Algebra"]
    domain_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    domain_proxy_counts: dict[str, int]
    subdomain_proxy_method: Literal["mathlib_algebra_second_segment_bucket_v1"]
    subdomain_proxy_counts: dict[str, int]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_diversity_established: Literal[False]
    curation_type: Literal["codex_llm_assisted_operational_adequacy"]
    human_reviewed: Literal[False]
    semantic_gold_created: Literal[False]
    model_collection_authorized: Literal[True]
    model_collection_scope: Literal["local_models_only"]
    reference_visible_to_generator: Literal[False]
    generator_collection_plan_created: Literal[False]
    recovery_parser_binding_status: Literal["unresolved"]
    limitations: tuple[str, ...]


class OperationalPoolReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_gate3_docstrings_operational_pool_preflight_v1"]
    passed: Literal[True]
    manifest_artifact: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_HEX64)
    adequacy_report_artifact: str = Field(min_length=1)
    adequacy_report_sha256: str = Field(pattern=_HEX64)
    source_config_sha256: str = Field(pattern=_HEX64)
    pool_config_sha256: str = Field(pattern=_HEX64)
    curation_manifest_sha256: str = Field(pattern=_HEX64)
    admitted_records_sha256: str = Field(pattern=_HEX64)
    no_sorry_reference_checks_sha256: str = Field(pattern=_HEX64)
    problem_record_count: Literal[40]
    eligible_problem_count: Literal[40]
    source_adequacy_passed: Literal[True]
    model_collection_authorized_count: Literal[40]
    reference_visible_to_generator: Literal[False] = False
    human_reviewed: Literal[False] = False
    semantic_gold_created: Literal[False] = False
    gate_claimed: Literal[False] = False
    model_execution_performed: Literal[False] = False
    generator_collection_plan_created: Literal[False] = False
    recovery_parser_binding_status: Literal["unresolved"]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationalPoolRun:
    report_path: Path
    adequacy_report_path: Path
    manifest_path: Path
    report: OperationalPoolReport
    adequacy_report: OperationalSourceAdequacyReport
    manifest: OperationalPoolManifest


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate3OperationalPoolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Gate3OperationalPoolError(f"cannot load JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gate3OperationalPoolError(f"{path}: expected a JSON object")
    return value


def _iter_jsonl(path: Path) -> Iterator[dict[str, object]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise Gate3OperationalPoolError(
                        f"{path}:{line_number}: invalid JSON: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise Gate3OperationalPoolError(f"{path}:{line_number}: expected a JSON object")
                yield value
    except OSError as exc:
        raise Gate3OperationalPoolError(f"cannot read JSONL {path}: {exc}") from exc


def _resolve(root: Path, binding: ArtifactBinding) -> Path:
    path = Path(binding.path)
    return path if path.is_absolute() else root / path


def _verify(root: Path, binding: ArtifactBinding) -> Path:
    path = _resolve(root, binding)
    if not path.is_file() or path.is_symlink():
        raise Gate3OperationalPoolError(f"required artifact is absent or a symlink: {path}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise Gate3OperationalPoolError(
            f"artifact hash mismatch for {path}: expected {binding.sha256}, got {observed}"
        )
    return path


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
        raise Gate3OperationalPoolError(
            f"Git provenance check failed: git {' '.join(args)}: {exc}"
        ) from exc


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Gate3OperationalPoolError(
                f"immutable output differs from existing artifact: {path}"
            )
        return
    path.write_bytes(payload)


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(values: tuple[object, ...]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _load_reference_records(
    *,
    theorem_path: Path,
    representation_path: Path,
    admitted: tuple[OperationalCurationRecord, ...],
) -> tuple[dict[str, TheoremRecord], dict[str, RepresentationRecord]]:
    theorem_ids = {item.source_candidate.theorem_id for item in admitted}
    representation_ids = {item.source_candidate.representation_id for item in admitted}

    theorems: dict[str, TheoremRecord] = {}
    for wrapper in _iter_jsonl(theorem_path):
        raw = wrapper.get("theorem")
        if not isinstance(raw, dict):
            raise Gate3OperationalPoolError("Gate-3 theorem wrapper is malformed")
        theorem_id = raw.get("theorem_id")
        if theorem_id in theorem_ids:
            theorem = TheoremRecord.model_validate(raw)
            if theorem.theorem_id in theorems:
                raise Gate3OperationalPoolError(f"duplicate reference theorem {theorem.theorem_id}")
            theorems[theorem.theorem_id] = theorem

    representations: dict[str, RepresentationRecord] = {}
    for raw in _iter_jsonl(representation_path):
        representation_id = raw.get("representation_id")
        if representation_id in representation_ids:
            representation = RepresentationRecord.model_validate(raw)
            if representation.representation_id in representations:
                raise Gate3OperationalPoolError(
                    f"duplicate reference representation {representation.representation_id}"
                )
            representations[representation.representation_id] = representation

    if set(theorems) != theorem_ids:
        raise Gate3OperationalPoolError("canonical theorem partition is incomplete")
    if set(representations) != representation_ids:
        raise Gate3OperationalPoolError("canonical representation partition is incomplete")
    return theorems, representations


def _verify_curation_inputs(
    *,
    root: Path,
    source: OperationalSourceConfig,
) -> tuple[
    Gate3DocstringCurationConfig,
    CurationReport,
    CurationArtifactManifest,
    tuple[OperationalCurationRecord, ...],
    NoSorryReferenceChecks,
]:
    binding = source.operational_curation
    curation_config_path = _verify(root, binding.curation_config)
    curation_report_path = _verify(root, binding.curation_report)
    curation_manifest_path = _verify(root, binding.curation_manifest)
    admitted_path = _verify(root, binding.admitted_records)
    checks_path = _verify(root, binding.no_sorry_reference_checks)

    curation_config = load_config(
        curation_config_path,
        Gate3DocstringCurationConfig,
    ).config
    report = CurationReport.model_validate(_load_json(curation_report_path))
    manifest = CurationArtifactManifest.model_validate(_load_json(curation_manifest_path))
    admitted = tuple(
        OperationalCurationRecord.model_validate(raw) for raw in _iter_jsonl(admitted_path)
    )
    checks = NoSorryReferenceChecks.model_validate(_load_json(checks_path))

    if (
        not report.passed
        or report.reviewed_count != binding.expected_reviewed
        or report.admitted_count != binding.expected_admitted
        or report.manifest.sha256 != binding.curation_manifest.sha256
        or report.admitted.sha256 != binding.admitted_records.sha256
        or report.reference_checks.sha256 != binding.no_sorry_reference_checks.sha256
    ):
        raise Gate3OperationalPoolError("curation report binding or accounting drifted")
    if (
        manifest.reviewed_count != binding.expected_reviewed
        or manifest.admitted_count != binding.expected_admitted
        or manifest.reviewer_type != binding.reviewer_type
        or manifest.review_method != binding.review_method
        or len(admitted) != binding.expected_admitted
    ):
        raise Gate3OperationalPoolError("curation manifest binding or accounting drifted")

    admitted_ids = tuple(item.source_candidate.candidate_id for item in admitted)
    if tuple(sorted(admitted_ids)) != tuple(sorted(manifest.admitted_candidate_ids)):
        raise Gate3OperationalPoolError("curation admitted candidate IDs drifted")
    if len(admitted_ids) != len(set(admitted_ids)):
        raise Gate3OperationalPoolError("curation admitted records are not unique")
    for item in admitted:
        if (
            item.review.decision is not CurationDecision.STANDALONE_SUFFICIENT
            or not item.review.model_collection_authorized
            or item.review.authorization_scope != "local_models_only"
            or item.review.reference_visible_to_generator
            or item.reference is None
            or item.reference_context is None
            or item.human_review_claimed
            or item.semantic_labels_created
            or item.gate_claimed
        ):
            raise Gate3OperationalPoolError(
                f"{item.decision_id}: admitted curation policy boundary changed"
            )
    if curation_config.expected_counts.admitted != 40:
        raise Gate3OperationalPoolError("curation config admitted count drifted")
    return curation_config, report, manifest, admitted, checks


def _verify_no_sorry_check(
    *,
    root: Path,
    item: OperationalCurationRecord,
    binding: ReferenceCheckBinding,
    import_header_text: str,
) -> None:
    if item.reference is None or item.reference_context is None:
        raise Gate3OperationalPoolError("admitted curation record lacks reference binding")
    if (
        binding.candidate_id != item.source_candidate.candidate_id
        or binding.decision_id != item.decision_id
        or binding.lean_request_hash != item.reference.lean_request_hash
        or binding.raw_response_artifact.model_dump(mode="json")
        != item.reference.raw_response_artifact.model_dump(mode="json")
    ):
        raise Gate3OperationalPoolError(
            f"{item.decision_id}: no-sorry reference-check binding drifted"
        )
    raw_path = _verify(root, binding.raw_response_artifact)
    raw = _load_json(raw_path)
    request = raw.get("request")
    response = raw.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise Gate3OperationalPoolError(f"{item.decision_id}: malformed LeanInteract raw response")
    expected_code = import_header_text + item.reference.reference_lean_statement
    if (
        raw.get("request_hash") != binding.lean_request_hash
        or raw.get("error") is not None
        or request.get("allow_sorry") is not False
        or request.get("context_id") != item.reference_context.context_id
        or request.get("code") != expected_code
        or response.get("sorries") not in ([], None)
    ):
        raise Gate3OperationalPoolError(
            f"{item.decision_id}: no-sorry Lean request payload drifted"
        )
    messages = response.get("messages")
    if not isinstance(messages, list) or any(
        isinstance(message, dict) and message.get("severity") == "error" for message in messages
    ):
        raise Gate3OperationalPoolError(
            f"{item.decision_id}: no-sorry alias check contains an error"
        )
    declarations = response.get("declarations")
    if (
        not isinstance(declarations, list)
        or len(declarations) != 1
        or not isinstance(declarations[0], dict)
        or declarations[0].get("name") != item.reference.reference_declaration_name
    ):
        raise Gate3OperationalPoolError(f"{item.decision_id}: no-sorry alias declaration drifted")
    if "sorry" in item.reference.reference_lean_statement.lower():
        raise Gate3OperationalPoolError(
            f"{item.decision_id}: reference alias unexpectedly contains sorry"
        )


def _verify_source_and_temporal(
    *,
    repo: Path,
    source: OperationalSourceConfig,
    item: OperationalCurationRecord,
    source_declaration_name: str | None,
) -> None:
    candidate = item.source_candidate
    provenance = candidate.source_provenance
    temporal = candidate.temporal_introduction
    if (
        provenance.revision != source.revision
        or temporal.search_revision != source.revision
        or not temporal.strictly_postdates_latest_checkpoint
        or not candidate.shared_three_family_temporal_eligible
    ):
        raise Gate3OperationalPoolError(
            f"{candidate.candidate_id}: source or temporal binding drifted"
        )
    source_blob = _git(
        repo,
        "show",
        f"{source.revision}:{provenance.source_file}",
    ).stdout
    source_blob_sha1 = _git(
        repo,
        "rev-parse",
        f"{source.revision}:{provenance.source_file}",
    ).stdout.strip()
    if hash_canonical({"raw_utf8": source_blob}) == provenance.source_file_sha256:
        # ``source_file_sha256`` is a raw-byte SHA-256, never a canonical JSON
        # hash. This branch can only signal an accidental hashing-policy mixup.
        raise Gate3OperationalPoolError(
            f"{candidate.candidate_id}: source hash policy unexpectedly changed"
        )
    declaration_names = tuple(
        dict.fromkeys(
            name
            for name in (
                source_declaration_name,
                candidate.declaration_full_name,
                candidate.declaration_full_name.rsplit(".", 1)[-1],
            )
            if name
        )
    )
    if (
        sha256_hex(source_blob.encode("utf-8")) != provenance.source_file_sha256
        or source_blob_sha1 != provenance.git_blob_sha1
        or not any(
            _exact_pair_present(
                blob=source_blob,
                raw_docstring=candidate.docstring.raw,
                declaration_name=declaration_name,
            )
            for declaration_name in declaration_names
        )
    ):
        raise Gate3OperationalPoolError(
            f"{candidate.candidate_id}: exact source pair is absent or changed"
        )

    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        temporal.introduction_commit,
        source.revision,
        check=False,
    )
    if ancestor.returncode != 0:
        raise Gate3OperationalPoolError(
            f"{candidate.candidate_id}: introduction commit is not source ancestry"
        )
    intro_date = _git(
        repo,
        "show",
        "-s",
        "--format=%cI",
        temporal.introduction_commit,
    ).stdout.strip()
    if datetime.datetime.fromisoformat(intro_date) != temporal.introduction_created_at:
        raise Gate3OperationalPoolError(f"{candidate.candidate_id}: introduction timestamp drifted")
    intro_blob = _git(
        repo,
        "show",
        f"{temporal.introduction_commit}:{temporal.introduction_source_path}",
    ).stdout
    if not any(
        _exact_pair_present(
            blob=intro_blob,
            raw_docstring=candidate.docstring.raw,
            declaration_name=declaration_name,
        )
        for declaration_name in declaration_names
    ):
        raise Gate3OperationalPoolError(
            f"{candidate.candidate_id}: introduction blob lacks exact source pair"
        )


def _source_record_identity(item: OperationalCurationRecord) -> tuple[str, str]:
    candidate = item.source_candidate
    source_record_id = hash_canonical(
        {
            "schema": "mathlib_gate3_operational_docstring_locator_v1",
            "repository": candidate.source_provenance.repository,
            "revision": candidate.source_provenance.revision,
            "source_file": candidate.source_provenance.source_file,
            "source_range": candidate.source_provenance.source_range,
            "theorem_id": candidate.theorem_id,
        }
    )
    source_record_content_hash = hash_canonical(
        {
            "schema": "mathlib_gate3_operational_docstring_content_v1",
            "curation_record": item.model_dump(mode="json"),
        }
    )
    return source_record_id, source_record_content_hash


def _problem_candidate(
    *,
    item: OperationalCurationRecord,
    theorem: TheoremRecord,
    context: ContextRecord,
    header_artifact: str,
    header_hash: str,
) -> ProblemPoolCandidate:
    source_record_id, source_record_content_hash = _source_record_identity(item)
    candidate = item.source_candidate
    problem_id = "gate3-docstring:" + candidate.candidate_id.rsplit(":", 1)[-1]
    problem_group = "nl-problem:" + hash_canonical(
        {
            "schema": "mathlib_gate3_operational_problem_group_v1",
            "candidate_id": candidate.candidate_id,
            "ancestry_id": candidate.ancestry_id,
        }
    )
    domain, domain_proxy, subdomain_proxy = _domain_and_proxies(
        candidate.source_provenance.source_file
    )
    return ProblemPoolCandidate(
        problem_id=problem_id,
        problem_group=problem_group,
        source=_SOURCE_NAME,
        source_revision=candidate.source_provenance.revision,
        source_split=_SOURCE_SPLIT,
        source_record_id=source_record_id,
        source_record_content_hash=source_record_content_hash,
        nl_statement=candidate.docstring.normalized_nl,
        nl_trust=NLTrust.TRUSTED,
        nl_source_link=candidate.nl_source_link,
        context_id=context.context_id,
        import_header_artifact=header_artifact,
        import_header_hash=header_hash,
        reference_theorem_ids=(theorem.theorem_id,),
        source_license="Apache-2.0",
        private_source_content=False,
        release_eligible=True,
        near_duplicate_group_ids=(candidate.ancestry_id,),
        overlap_tags=(
            "formalrx_lineage:mathlib_docstring_theorem_pairs",
            "pretraining_contamination:unknown",
            "temporal_provenance:strictly_postdates_all_three_checkpoints",
        ),
        metadata={
            "candidate_id": candidate.candidate_id,
            "curation_decision_id": item.decision_id,
            "curation_type": "codex_llm_assisted_operational_adequacy",
            "human_reviewed": False,
            "semantic_gold_created": False,
            "model_collection_authorized": True,
            "model_collection_scope": "local_models_only",
            "external_provider_collection_authorized": False,
            "reference_visible_to_generator": False,
            "gate_claimed": False,
            "temporal_introduction_commit": (candidate.temporal_introduction.introduction_commit),
            "source_declaration_full_name": candidate.declaration_full_name,
            "domain": domain,
            "domain_method": "mathlib_source_path_first_segment_v1",
            "domain_proxy": domain_proxy,
            "domain_proxy_method": "mathlib_algebra_second_segment_bucket_v1",
            "subdomain_proxy": subdomain_proxy,
            "subdomain_proxy_method": "mathlib_algebra_second_segment_bucket_v1",
            "domain_proxy_is_semantic_gold": False,
            "cross_domain_diversity_established": False,
        },
    )


def _domain_and_proxies(source_file: str) -> tuple[str, str, str]:
    """Return deterministic source-path strata; none is semantic domain gold."""

    parts = PurePosixPath(source_file).parts
    if len(parts) < 3 or parts[0] != "Mathlib" or parts[1] != "Algebra":
        raise Gate3OperationalPoolError(
            f"operational tranche must be under Mathlib/Algebra/<subdomain>: {source_file!r}"
        )
    domain = parts[1]
    raw_subdomain = parts[2] if len(parts) >= 4 else None
    admitted_subdomains = {
        "AffineMonoid",
        "Algebra",
        "BigOperators",
        "Category",
        "Exact",
        "Group",
    }
    subdomain_proxy = raw_subdomain if raw_subdomain in admitted_subdomains else "other"
    domain_proxy = f"{domain}/{subdomain_proxy}"
    return domain, domain_proxy, subdomain_proxy


def _validate_pool_source(
    *,
    paths: RepoPaths,
    source: OperationalSourceConfig,
    config: ProblemPoolConfig,
) -> None:
    enabled = tuple(item for item in config.sources if item.enabled)
    if len(enabled) != 1 or enabled[0].source != source.source:
        raise Gate3OperationalPoolError("operational pool must enable exactly its source")
    pool_source = enabled[0]
    if pool_source.source_config_sha256 != hash_file(paths.root / SOURCE_CONFIG):
        raise Gate3OperationalPoolError("operational source-config binding drifted")
    if pool_source.authorization != source.lf021_authorization:
        raise Gate3OperationalPoolError("pool/source authorization binding drifted")
    if pool_source.external_provider_eligible:
        raise Gate3OperationalPoolError(
            "operational curation must not authorize external-provider collection"
        )
    profile = paths.root / config.public_replication_profile
    if not profile.is_file() or profile.is_symlink():
        raise Gate3OperationalPoolError("public replication profile is absent or unsafe")


def _adequacy_markdown(
    *,
    report: OperationalSourceAdequacyReport,
    manifest_path: Path,
    root: Path,
) -> bytes:
    lines = [
        "# LF-021 Gate-3 docstring operational source adequacy v1",
        "",
        "**Result: PASS for local-model collection preflight only.**",
        "",
        f"- Reviewed candidate records: {report.candidate_records_reviewed}",
        f"- Operationally standalone and admitted: {report.operationally_standalone_admitted}",
        f"- Conservatively excluded: {report.operationally_excluded}",
        f"- Final canonical problem records: {report.final_problem_records}",
        f"- Exact Git source and temporal checks: {report.exact_git_source_verified}/40",
        f"- `allow_sorry=false` alias checks: {report.no_sorry_alias_checks_verified}/40",
        f"- Active-registry three-screen clear: {report.active_registry_three_screen_clear}/40",
        f"- Exact/ancestry dedup survivors: {report.exact_and_ancestry_dedup_survivors}/40",
        f"- Source-path domain tranche: {report.domain}",
        "- Cross-domain diversity established: "
        f"{str(report.cross_domain_diversity_established).lower()}",
        f"- Manifest: `{manifest_path.relative_to(root)}`",
        "",
        "## Deterministic source-path proxy counts",
        "",
        f"Method: `{report.subdomain_proxy_method}`.",
        "These path components are operational strata, not semantic domain gold.",
        "",
        *(f"- `{name}`: {count}" for name, count in sorted(report.subdomain_proxy_counts.items())),
        "",
        "## Scope boundary",
        "",
        "This is Codex/LLM-assisted operational adequacy, not human review or semantic gold.",
        "The reference theorem and representation remain hidden from generators.",
        "Collection is authorized only for the pinned local model families.",
        "No generator plan is created while the recovery-parser binding is unresolved.",
        "No semantic label, Gate-5 credit, or model output is created by this stage.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def run_gate3_operational_pool(
    *,
    paths: RepoPaths,
    mathlib_checkout: Path,
) -> OperationalPoolRun:
    """Build and persist the exact 40-record operational public pool."""

    root = paths.root.resolve()
    loaded_source = load_config(
        root / SOURCE_CONFIG,
        OperationalSourceConfig,
    )
    source = loaded_source.config
    loaded_pool = load_problem_pool_config(root / POOL_CONFIG)
    config = loaded_pool.config
    _validate_pool_source(paths=paths, source=source, config=config)

    if _git(mathlib_checkout, "rev-parse", "HEAD").stdout.strip() != source.revision:
        raise Gate3OperationalPoolError("mathlib checkout does not match pinned source revision")
    if (
        _git(mathlib_checkout, "rev-parse", f"{source.revision}:LICENSE").stdout.strip()
        != source.license_blob_sha1
    ):
        raise Gate3OperationalPoolError("pinned mathlib license blob changed")

    curation_config, _, _, admitted, no_sorry_checks = _verify_curation_inputs(
        root=root,
        source=source,
    )
    references = source.canonical_references
    theorem_path = _verify(root, references.theorem_records)
    representation_path = _verify(root, references.representation_records)
    context_path = _verify(root, references.context)
    header_path = _verify(root, references.import_header)
    context = ContextRecord.model_validate(_load_json(context_path))
    header_text = header_path.read_text(encoding="utf-8")
    if (
        context.context_id != curation_config.execution_context.context_id
        or context.project_revision != source.revision
        or context.header_text != header_text
        or context.header_hash != references.import_header.sha256
    ):
        raise Gate3OperationalPoolError("canonical context or import header drifted")

    theorems, representations = _load_reference_records(
        theorem_path=theorem_path,
        representation_path=representation_path,
        admitted=admitted,
    )
    checks_by_candidate = {item.candidate_id: item for item in no_sorry_checks.checks}
    for item in admitted:
        check = checks_by_candidate.get(item.source_candidate.candidate_id)
        if check is None:
            raise Gate3OperationalPoolError(f"{item.decision_id}: no-sorry check is absent")
        _verify_no_sorry_check(
            root=root,
            item=item,
            binding=check,
            import_header_text=header_text,
        )
        _verify_source_and_temporal(
            repo=mathlib_checkout,
            source=source,
            item=item,
            source_declaration_name=theorems[item.source_candidate.theorem_id].declaration_name,
        )
        theorem = theorems[item.source_candidate.theorem_id]
        representation = representations[item.source_candidate.representation_id]
        if (
            theorem.context_id != context.context_id
            or theorem.statement_content_hash
            != item.source_candidate.theorem_statement_content_hash
            or theorem.declaration_full_name != item.source_candidate.declaration_full_name
            or representation.theorem_id != theorem.theorem_id
            or representation.context_id != context.context_id
            or representation.content_hash != item.source_candidate.representation_content_hash
            or representation.alpha_identity_fingerprint is None
        ):
            raise Gate3OperationalPoolError(
                f"{item.decision_id}: canonical reference binding drifted"
            )

    active = load_active_benchmark_registry(repo_root=root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(active, repo_root=root)
    configured_registry = source.screening.active_benchmark_registry_manifest
    if (
        configured_registry.sha256 != denylist.manifest_sha256
        or loaded_pool.config.active_benchmark_registry_manifest_sha256 != denylist.manifest_sha256
    ):
        raise Gate3OperationalPoolError("active benchmark registry binding drifted")
    for item in admitted:
        upstream = item.source_candidate.registry_screens
        if (
            upstream.registry_manifest_sha256 != denylist.manifest_sha256
            or upstream.active_registry_sha256 != denylist.active_registry_sha256
            or upstream.registry_content_hash != denylist.registry_content_hash
            or not upstream.all_three_screens_clear
        ):
            raise Gate3OperationalPoolError(
                f"{item.decision_id}: upstream registry binding drifted"
            )

    header_artifact = str(header_path.relative_to(root))
    candidates = tuple(
        _problem_candidate(
            item=item,
            theorem=theorems[item.source_candidate.theorem_id],
            context=context,
            header_artifact=header_artifact,
            header_hash=references.import_header.sha256,
        )
        for item in admitted
    )
    pool = build_problem_pool(
        config=config,
        denylist=denylist,
        candidates=candidates,
    )
    if len(pool.records) != 40:
        raise Gate3OperationalPoolError("problem-pool accounting did not retain 40 terminals")
    excluded = tuple(record for record in pool.records if record.eligibility != "eligible")
    if excluded:
        details = "; ".join(
            f"{record.problem_id}:{','.join(record.exclusion_reasons)}" for record in excluded
        )
        raise Gate3OperationalPoolError(
            f"rerun screening/dedup excluded curated records: {details}"
        )
    if pool.public_trusted_problems:
        raise Gate3OperationalPoolError(
            "local-only pool unexpectedly produced external-provider prompt records"
        )

    pool_by_candidate = {}
    for record in pool.records:
        candidate_id = record.metadata.get("candidate_id")
        if not isinstance(candidate_id, str):
            raise Gate3OperationalPoolError(
                f"{record.problem_record_id}: candidate metadata is absent"
            )
        pool_by_candidate[candidate_id] = record
    audits: list[OperationalPoolRecordAudit] = []
    for item in admitted:
        candidate = item.source_candidate
        theorem = theorems[candidate.theorem_id]
        representation = representations[candidate.representation_id]
        problem = pool_by_candidate[candidate.candidate_id]
        reference_hits = candidate_benchmark_hits(
            denylist_index=denylist.index,
            theorem=theorem,
            representation=representation,
        )
        lean_hits = tuple(sorted(hit for hit in reference_hits if hit.startswith("lean:")))
        representation_hits = tuple(
            sorted(hit for hit in reference_hits if hit.startswith("representation:"))
        )
        unexpected = tuple(
            hit
            for hit in reference_hits
            if not (hit.startswith("lean:") or hit.startswith("representation:"))
        )
        if unexpected:
            raise Gate3OperationalPoolError(
                f"{candidate.candidate_id}: unclassified benchmark hits: {unexpected}"
            )
        screens = RerunRegistryScreens(
            problem_identity_and_nl_hits=problem.denylist_hits,
            reference_lean_hits=lean_hits,
            reference_representation_hits=representation_hits,
            all_three_screens_clear=True,
            registry_manifest_sha256=denylist.manifest_sha256,
            active_registry_sha256=denylist.active_registry_sha256,
            registry_content_hash=denylist.registry_content_hash,
        )
        check = checks_by_candidate[candidate.candidate_id]
        assert representation.alpha_identity_fingerprint is not None
        audits.append(
            OperationalPoolRecordAudit(
                problem_record_id=problem.problem_record_id,
                problem_id=problem.problem_id,
                source_record_id=problem.source_record_id,
                source_record_content_hash=problem.source_record_content_hash,
                candidate_id=candidate.candidate_id,
                curation_decision_id=item.decision_id,
                curation_record_sha256=hash_canonical(
                    {
                        "schema": "gate3_operational_curation_record_binding_v1",
                        "record": item.model_dump(mode="json"),
                    }
                ),
                curation_decision="standalone_sufficient",
                curation_reviewer_type="codex_agent",
                curation_review_method="llm_assisted_operational_curation_v1",
                operational_adequacy_status=("codex_llm_assisted_standalone_sufficient"),
                model_collection_authorized=True,
                model_collection_scope="local_models_only",
                external_provider_collection_authorized=False,
                reference_visible_to_generator=False,
                human_reviewed=False,
                semantic_gold_created=False,
                gate_claimed=False,
                source_revision=candidate.source_provenance.revision,
                source_file=candidate.source_provenance.source_file,
                source_file_sha256=candidate.source_provenance.source_file_sha256,
                source_blob_sha1=candidate.source_provenance.git_blob_sha1,
                source_pair_present=True,
                temporal_introduction_commit=(candidate.temporal_introduction.introduction_commit),
                temporal_introduction_created_at=(
                    candidate.temporal_introduction.introduction_created_at
                ),
                temporal_introduction_is_ancestor=True,
                temporal_exact_pair_present=True,
                temporal_strictly_postdates_latest_checkpoint=True,
                reference_theorem_id=theorem.theorem_id,
                reference_representation_id=representation.representation_id,
                reference_statement_content_hash=theorem.statement_content_hash,
                reference_representation_content_hash=representation.content_hash,
                reference_alpha_identity_fingerprint=(representation.alpha_identity_fingerprint),
                no_sorry_alias_request_hash=check.lean_request_hash,
                no_sorry_alias_raw_response_sha256=(check.raw_response_artifact.sha256),
                no_sorry_alias_check_valid=True,
                registry_screens=screens,
            )
        )

    output_dir = (root / config.outputs.records).parent
    records_path = root / config.outputs.records
    failures_path = root / config.outputs.failures
    manifest_path = root / config.outputs.manifest
    context_output_path = output_dir / "context.json"
    theorem_output_path = output_dir / "reference_theorems.jsonl"
    representation_output_path = output_dir / "reference_representations.jsonl"
    audit_output_path = output_dir / "record_audits.jsonl"

    ordered_records = tuple(
        record.model_dump(mode="json")
        for record in sorted(pool.records, key=lambda record: record.problem_record_id)
    )
    ordered_theorems = tuple(
        theorem.model_dump(mode="json")
        for theorem in sorted(theorems.values(), key=lambda theorem: theorem.theorem_id)
    )
    ordered_representations = tuple(
        representation.model_dump(mode="json")
        for representation in sorted(
            representations.values(),
            key=lambda representation: representation.representation_id,
        )
    )
    ordered_audits = tuple(
        audit.model_dump(mode="json")
        for audit in sorted(audits, key=lambda audit: audit.problem_record_id)
    )
    _write_exact(records_path, _jsonl_bytes(ordered_records))
    _write_exact(failures_path, b"")
    _write_exact(context_output_path, _json_bytes(context.model_dump(mode="json")))
    _write_exact(theorem_output_path, _jsonl_bytes(ordered_theorems))
    _write_exact(
        representation_output_path,
        _jsonl_bytes(ordered_representations),
    )
    _write_exact(audit_output_path, _jsonl_bytes(ordered_audits))

    output_files = {
        "problem_pool_records": records_path,
        "problem_pool_failures": failures_path,
        "context": context_output_path,
        "reference_theorems": theorem_output_path,
        "reference_representations": representation_output_path,
        "record_audits": audit_output_path,
    }
    input_hashes = {
        "source_config": hash_file(root / SOURCE_CONFIG),
        "pool_config": hash_file(root / POOL_CONFIG),
        "public_replication_profile": hash_file(root / config.public_replication_profile),
        "curation_config": source.operational_curation.curation_config.sha256,
        "curation_report": source.operational_curation.curation_report.sha256,
        "curation_manifest": source.operational_curation.curation_manifest.sha256,
        "curation_admitted": source.operational_curation.admitted_records.sha256,
        "no_sorry_reference_checks": (source.operational_curation.no_sorry_reference_checks.sha256),
        "canonical_theorems": references.theorem_records.sha256,
        "canonical_representations": references.representation_records.sha256,
        "canonical_context": references.context.sha256,
        "import_header": references.import_header.sha256,
        "active_benchmark_manifest": denylist.manifest_sha256,
        "active_benchmark_registry": denylist.active_registry_sha256,
        "implementation": hash_file(Path(__file__)),
    }
    domain_proxy_counts: dict[str, int] = {}
    subdomain_proxy_counts: dict[str, int] = {}
    for record in pool.records:
        domain = record.metadata.get("domain")
        domain_proxy = record.metadata.get("domain_proxy")
        subdomain_proxy = record.metadata.get("subdomain_proxy")
        if domain != "Algebra":
            raise Gate3OperationalPoolError(
                f"{record.problem_record_id}: domain is not the Algebra tranche"
            )
        if not isinstance(domain_proxy, str) or not isinstance(subdomain_proxy, str):
            raise Gate3OperationalPoolError(
                f"{record.problem_record_id}: source-path proxy is absent"
            )
        domain_proxy_counts[domain_proxy] = domain_proxy_counts.get(domain_proxy, 0) + 1
        subdomain_proxy_counts[subdomain_proxy] = subdomain_proxy_counts.get(subdomain_proxy, 0) + 1
    domain_proxy_counts = dict(sorted(domain_proxy_counts.items()))
    subdomain_proxy_counts = dict(sorted(subdomain_proxy_counts.items()))
    manifest = OperationalPoolManifest(
        artifact_kind="lf021_gate3_docstrings_operational_problem_pool_v1",
        frozen_at=curation_config.frozen_at,
        source=_SOURCE_NAME,
        source_revision=source.revision,
        context_id=context.context_id,
        problem_count=40,
        curation_reviewed_count=57,
        curation_admitted_count=40,
        problem_record_count=40,
        eligible_problem_count=40,
        reference_theorem_count=40,
        reference_representation_count=40,
        no_sorry_alias_check_count=40,
        rerun_three_screen_clear_count=40,
        input_hashes=input_hashes,
        output_hashes={name: hash_file(path) for name, path in sorted(output_files.items())},
        source_config_artifact=ArtifactBinding(
            path=str(SOURCE_CONFIG),
            sha256=input_hashes["source_config"],
        ),
        curation_config_artifact=source.operational_curation.curation_config,
        curation_report_artifact=source.operational_curation.curation_report,
        curation_manifest_artifact=source.operational_curation.curation_manifest,
        curation_admitted_artifact=source.operational_curation.admitted_records,
        no_sorry_reference_checks_artifact=(source.operational_curation.no_sorry_reference_checks),
        problem_records_artifact=ArtifactBinding(
            path=str(records_path.relative_to(root)),
            sha256=hash_file(records_path),
        ),
        context_artifact=ArtifactBinding(
            path=str(context_output_path.relative_to(root)),
            sha256=hash_file(context_output_path),
        ),
        reference_theorems_artifact=ArtifactBinding(
            path=str(theorem_output_path.relative_to(root)),
            sha256=hash_file(theorem_output_path),
        ),
        reference_representations_artifact=ArtifactBinding(
            path=str(representation_output_path.relative_to(root)),
            sha256=hash_file(representation_output_path),
        ),
        record_audits_artifact=ArtifactBinding(
            path=str(audit_output_path.relative_to(root)),
            sha256=hash_file(audit_output_path),
        ),
        import_header_artifact=references.import_header,
        active_benchmark_manifest_artifact=ArtifactBinding(
            path=denylist.manifest_path,
            sha256=denylist.manifest_sha256,
        ),
        active_benchmark_registry_sha256=denylist.active_registry_sha256,
        active_benchmark_registry_content_hash=denylist.registry_content_hash,
        domain="Algebra",
        domain_method="mathlib_source_path_first_segment_v1",
        domain_proxy_method="mathlib_algebra_second_segment_bucket_v1",
        domain_proxy_counts=domain_proxy_counts,
        subdomain_proxy_method="mathlib_algebra_second_segment_bucket_v1",
        subdomain_proxy_counts=subdomain_proxy_counts,
        domain_proxy_is_semantic_gold=False,
        cross_domain_diversity_established=False,
        problem_record_ids=tuple(
            record.problem_record_id
            for record in sorted(pool.records, key=lambda record: record.problem_record_id)
        ),
        problem_groups=tuple(
            record.problem_group
            for record in sorted(pool.records, key=lambda record: record.problem_record_id)
        ),
        declaration_full_names=tuple(
            sorted(item.source_candidate.declaration_full_name for item in admitted)
        ),
        theorem_ids=tuple(sorted(theorems)),
        representation_ids=tuple(sorted(representations)),
        model_collection_authorized_count=40,
        recovery_parser_binding_status="unresolved",
    )
    _write_exact(manifest_path, _json_bytes(manifest.model_dump(mode="json")))

    distinct_ancestry_groups = len({item.source_candidate.ancestry_id for item in admitted})
    if distinct_ancestry_groups != 40:
        raise Gate3OperationalPoolError(
            "curated input does not contain exactly 40 distinct ancestry groups"
        )
    adequacy_report = OperationalSourceAdequacyReport(
        report_kind="lf021_gate3_docstrings_operational_source_adequacy_v1",
        passed=True,
        candidate_records_reviewed=57,
        operationally_standalone_admitted=40,
        operationally_excluded=17,
        final_problem_records=40,
        exact_git_source_verified=40,
        strict_temporal_nonoverlap_verified=40,
        no_sorry_alias_checks_verified=40,
        active_registry_three_screen_clear=40,
        exact_and_ancestry_dedup_survivors=40,
        canonical_reference_theorems=40,
        canonical_reference_representations=40,
        distinct_ancestry_groups=40,
        domain="Algebra",
        domain_method="mathlib_source_path_first_segment_v1",
        domain_proxy_method="mathlib_algebra_second_segment_bucket_v1",
        domain_proxy_counts=domain_proxy_counts,
        subdomain_proxy_method="mathlib_algebra_second_segment_bucket_v1",
        subdomain_proxy_counts=subdomain_proxy_counts,
        domain_proxy_is_semantic_gold=False,
        cross_domain_diversity_established=False,
        curation_type="codex_llm_assisted_operational_adequacy",
        human_reviewed=False,
        semantic_gold_created=False,
        model_collection_authorized=True,
        model_collection_scope="local_models_only",
        reference_visible_to_generator=False,
        generator_collection_plan_created=False,
        recovery_parser_binding_status="unresolved",
        limitations=(
            "Operational curation is Codex/LLM-assisted and is not human review.",
            "Standalone adequacy is not a same-claim or semantic-gold label.",
            "Temporal non-overlap does not prove absence from model pretraining.",
            "All records retain FormalRx mathlib-docstring source-lineage tags.",
            "All 40 records are from Mathlib/Algebra; this tranche does not "
            "establish cross-domain diversity.",
            "Domain and subdomain proxies are deterministic source-path strata, "
            "not semantic domain gold.",
            "Reference statements are retained for scoring and are not generator inputs.",
            "No generator plan is created while recovery-parser binding is unresolved.",
            "This artifact creates no model output, semantic label, or Gate-5 credit.",
        ),
    )
    adequacy_report_path = root / ADEQUACY_REPORT_PATH
    _write_exact(
        adequacy_report_path,
        _json_bytes(adequacy_report.model_dump(mode="json")),
    )
    _write_exact(
        root / ADEQUACY_MARKDOWN_PATH,
        _adequacy_markdown(
            report=adequacy_report,
            manifest_path=manifest_path,
            root=root,
        ),
    )

    report = OperationalPoolReport(
        report_kind="lf021_gate3_docstrings_operational_pool_preflight_v1",
        passed=True,
        manifest_artifact=str(manifest_path.relative_to(root)),
        manifest_sha256=hash_file(manifest_path),
        adequacy_report_artifact=str(ADEQUACY_REPORT_PATH),
        adequacy_report_sha256=hash_file(adequacy_report_path),
        source_config_sha256=input_hashes["source_config"],
        pool_config_sha256=input_hashes["pool_config"],
        curation_manifest_sha256=input_hashes["curation_manifest"],
        admitted_records_sha256=input_hashes["curation_admitted"],
        no_sorry_reference_checks_sha256=input_hashes["no_sorry_reference_checks"],
        problem_record_count=40,
        eligible_problem_count=40,
        source_adequacy_passed=True,
        model_collection_authorized_count=40,
        recovery_parser_binding_status="unresolved",
        blockers=(
            "Recovery-parser binding is unresolved, so no generator collection plan exists.",
            "Operational adequacy is not human review or semantic gold.",
            "Pretraining contamination remains unknown despite strict temporal non-overlap.",
            "This 40-record artifact alone cannot close Gate 5 or Gate 5G.",
        ),
    )
    report_path = root / REPORT_PATH
    _write_exact(report_path, _json_bytes(report.model_dump(mode="json")))
    return OperationalPoolRun(
        report_path=report_path,
        adequacy_report_path=adequacy_report_path,
        manifest_path=manifest_path,
        report=report,
        adequacy_report=adequacy_report,
        manifest=manifest,
    )


__all__ = [
    "ADEQUACY_MARKDOWN_PATH",
    "ADEQUACY_REPORT_PATH",
    "POOL_CONFIG",
    "REPORT_PATH",
    "SOURCE_CONFIG",
    "Gate3OperationalPoolError",
    "OperationalPoolManifest",
    "OperationalPoolRecordAudit",
    "OperationalPoolReport",
    "OperationalPoolRun",
    "OperationalSourceAdequacyReport",
    "OperationalSourceConfig",
    "run_gate3_operational_pool",
]

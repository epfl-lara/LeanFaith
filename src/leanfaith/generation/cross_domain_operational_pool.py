"""Model-free LF-021 operational curation of cross-domain probe candidates.

The stage consumes the immutable 24-candidate feasibility probe, applies an
explicit fail-closed standalone-claim review, rechecks provenance, benchmark
screens, reference representations, exact/ancestry deduplication, and
``allow_sorry=False`` reference aliases, then emits a local-model-only problem
pool.  It never loads a generator, creates a semantic label, or claims Gate
credit.

Once the immutable manifest exists, ``run_cross_domain_operational_pool``
switches to verification mode.  Repeated invocations therefore verify exact
artifact replay without rerunning Lean or overwriting a prior result.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.config import (
    ProblemPoolConfig,
    SourceAuthorizationConfig,
    load_problem_pool_config,
)
from leanfaith.generation.cross_domain_docstring_probe import (
    CrossDomainCandidate,
    CrossDomainProbeManifest,
    CrossDomainProbeReport,
    verify_cross_domain_probe,
)
from leanfaith.generation.gate3_docstring_pool import _exact_pair_present
from leanfaith.generation.problem_pool import (
    ProblemPoolCandidate,
    ProblemPoolDenylistBinding,
    build_problem_pool,
)
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

SOURCE_CONFIG = Path("configs/sources/mathlib_cross_domain_docstrings_operational_v1.yaml")
POOL_CONFIG = Path("configs/generation/problem_pool_cross_domain_docstrings_operational_v1.yaml")
REPORT_PATH = Path("reports/generation/lf021_cross_domain_docstrings_operational_pool_v1.json")
ADEQUACY_REPORT_PATH = Path(
    "reports/generation/lf021_cross_domain_docstrings_operational_source_adequacy_v1.json"
)
ADEQUACY_MARKDOWN_PATH = Path(
    "reports/generation/lf021_cross_domain_docstrings_operational_source_adequacy_v1.md"
)

_SOURCE_NAME: Literal["mathlib_cross_domain_docstrings_operational_v1"] = (
    "mathlib_cross_domain_docstrings_operational_v1"
)
_SOURCE_SPLIT = "cross_domain_operational_curation_v1"
_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_EXPECTED_REVIEWED = 24
_EXPECTED_ADMITTED = 20
_EXPECTED_EXCLUDED = 4
_EXPECTED_PROXIES = (
    "Analysis",
    "Combinatorics",
    "Geometry",
    "NumberTheory",
    "Probability",
    "Topology",
)


class CrossDomainOperationalPoolError(RuntimeError):
    """The cross-domain operational pool cannot be reproduced safely."""


class ArtifactBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class FeasibilityInputBinding(StrictModel):
    config: ArtifactBinding
    report: ArtifactBinding
    manifest: ArtifactBinding
    selected_candidates: ArtifactBinding
    expected_reviewed: Literal[24]
    expected_domain_proxies: tuple[str, ...]

    @model_validator(mode="after")
    def _expected_proxies(self) -> FeasibilityInputBinding:
        if self.expected_domain_proxies != _EXPECTED_PROXIES:
            raise ValueError("expected domain proxies changed")
        return self


class OperationalExclusion(StrictModel):
    candidate_id: str = Field(pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")
    declaration_full_name: str = Field(min_length=1)
    reason_code: Literal[
        "incomplete_title_like_docstring",
        "malformed_model_visible_headless_view",
        "referential_docstring",
    ]
    rationale: str = Field(min_length=20)


class OperationalCurationConfig(StrictModel):
    reviewer_type: Literal["codex_agent"]
    review_method: Literal["explicit_fail_closed_operational_curation_v1"]
    default_decision: Literal["standalone_sufficient"]
    default_reason_code: Literal["standalone_mathematical_claim"]
    exclusions: tuple[OperationalExclusion, ...]
    expected_admitted: Literal[20]
    expected_excluded: Literal[4]

    @model_validator(mode="after")
    def _exclusions(self) -> OperationalCurationConfig:
        ids = [item.candidate_id for item in self.exclusions]
        names = [item.declaration_full_name for item in self.exclusions]
        if len(ids) != self.expected_excluded or len(ids) != len(set(ids)):
            raise ValueError("curation exclusions must contain four unique candidates")
        if len(names) != len(set(names)):
            raise ValueError("curation exclusion declaration names must be unique")
        return self


class CanonicalContextBinding(StrictModel):
    context: ArtifactBinding
    import_header: ArtifactBinding
    project_registry_key: Literal["mathlib"]
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    environment_schema_version: Literal[1]


class ScreeningConfig(StrictModel):
    active_benchmark_registry_manifest: ArtifactBinding
    rerun_problem_identity_and_nl: Literal[True]
    rerun_reference_lean: Literal[True]
    rerun_reference_representation: Literal[True]
    exact_normalized_nl_deduplication: Literal[True]
    supplied_ancestry_deduplication: Literal[True]


class OperationalPolicy(StrictModel):
    nl_trust: Literal["trusted"]
    nl_trust_semantics: Literal["human_authored_provenance_only"]
    domain_proxy_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_proxy_coverage_established: Literal[True]
    semantic_domain_gold_created: Literal[False]
    model_collection_authorized: Literal[True]
    model_collection_scope: Literal["local_models_only"]
    external_provider_collection_authorized: Literal[False]
    reference_visible_to_generator: Literal[False]
    human_review_claimed: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]
    model_execution_performed: Literal[False]
    generator_collection_plan_created: Literal[False]


class OutputConfig(StrictModel):
    root: str = Field(min_length=1)
    records: str = Field(min_length=1)
    failures: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    report: str = Field(min_length=1)
    adequacy_report: str = Field(min_length=1)
    adequacy_markdown: str = Field(min_length=1)

    @model_validator(mode="after")
    def _repo_relative(self) -> OutputConfig:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be repository-relative")
        return self


class CrossDomainOperationalSourceConfig(StrictModel):
    source_config_version: Literal[1]
    source: Literal["mathlib_cross_domain_docstrings_operational_v1"]
    kind: Literal["git_curated_docstrings"]
    repo_url: Literal["https://github.com/leanprover-community/mathlib4"]
    revision: str = Field(pattern=_HEX40)
    tag: Literal["v4.31.0-rc1"]
    license: Literal["Apache-2.0"]
    license_blob_sha1: str = Field(pattern=_HEX40)
    lf021_authorization: SourceAuthorizationConfig
    feasibility_input: FeasibilityInputBinding
    operational_curation: OperationalCurationConfig
    canonical_context: CanonicalContextBinding
    screening: ScreeningConfig
    policy: OperationalPolicy
    outputs: OutputConfig

    @model_validator(mode="after")
    def _consistent(self) -> CrossDomainOperationalSourceConfig:
        if self.lf021_authorization.source_revision != self.revision:
            raise ValueError("authorization/source revision mismatch")
        if self.lf021_authorization.license_id != self.license:
            raise ValueError("authorization/source license mismatch")
        if self.lf021_authorization.private_source:
            raise ValueError("mathlib operational source cannot be private")
        if self.outputs.report != str(REPORT_PATH):
            raise ValueError("source report path differs from canonical report path")
        return self


class CurationDecisionRecord(StrictModel):
    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=r"^cross_domain_curation:[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")
    declaration_full_name: str = Field(min_length=1)
    domain_proxy: str = Field(min_length=1)
    decision: Literal["standalone_sufficient", "excluded"]
    reason_code: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    model_collection_authorized: bool
    authorization_scope: Literal["local_models_only", "none"]
    reference_visible_to_generator: Literal[False] = False
    human_reviewed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _policy(self) -> CurationDecisionRecord:
        admitted = self.decision == "standalone_sufficient"
        if self.model_collection_authorized != admitted:
            raise ValueError("authorization must match curation decision")
        if self.authorization_scope != ("local_models_only" if admitted else "none"):
            raise ValueError("authorization scope must match curation decision")
        return self


class ReferenceCheckRecord(StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^cross_domain_curation:[0-9a-f]{64}$")
    reference_declaration_name: str = Field(min_length=1)
    lean_request_hash: str = Field(pattern=_HEX64)
    raw_response_artifact: ArtifactBinding
    allow_sorry: Literal[False] = False
    valid: Literal[True] = True


class RegistryScreens(StrictModel):
    problem_identity_and_nl_hits: tuple[str, ...]
    reference_lean_hits: tuple[str, ...]
    reference_representation_hits: tuple[str, ...]
    all_three_screens_clear: Literal[True]
    registry_manifest_sha256: str = Field(pattern=_HEX64)
    active_registry_sha256: str = Field(pattern=_HEX64)
    registry_content_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _clear(self) -> RegistryScreens:
        if (
            self.problem_identity_and_nl_hits
            or self.reference_lean_hits
            or self.reference_representation_hits
        ):
            raise ValueError("clear benchmark screens cannot contain hits")
        return self


class OperationalRecordAudit(StrictModel):
    schema_version: Literal[1] = 1
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^cross_domain_curation:[0-9a-f]{64}$")
    declaration_full_name: str = Field(min_length=1)
    domain_proxy: str = Field(min_length=1)
    source_revision: str = Field(pattern=_HEX40)
    source_file: str = Field(min_length=1)
    source_file_sha256: str = Field(pattern=_HEX64)
    source_blob_sha1: str = Field(pattern=_HEX40)
    source_pair_present: Literal[True]
    temporal_introduction_commit: str = Field(pattern=_HEX40)
    temporal_introduction_created_at: datetime.datetime
    temporal_strictly_postdates_latest_checkpoint: Literal[True]
    reference_theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    reference_representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    reference_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    representation_complete: Literal[True]
    no_sorry_alias_request_hash: str = Field(pattern=_HEX64)
    no_sorry_alias_raw_response_sha256: str = Field(pattern=_HEX64)
    no_sorry_alias_check_valid: Literal[True]
    registry_screens: RegistryScreens
    model_collection_scope: Literal["local_models_only"]
    reference_visible_to_generator: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]

    @model_validator(mode="after")
    def _utc(self) -> OperationalRecordAudit:
        require_utc(self.temporal_introduction_created_at)
        return self


class OperationalPoolManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf021_cross_domain_docstrings_operational_problem_pool_v1"]
    manifest_id: str = Field(pattern=r"^cross_domain_operational_manifest:[0-9a-f]{64}$")
    frozen_at: datetime.datetime
    source: Literal["mathlib_cross_domain_docstrings_operational_v1"]
    source_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    reviewed_count: Literal[24]
    admitted_count: Literal[20]
    excluded_count: Literal[4]
    problem_count: Literal[20]
    eligible_problem_count: Literal[20]
    reference_theorem_count: Literal[20]
    reference_representation_count: Literal[20]
    no_sorry_alias_check_count: Literal[20]
    rerun_three_screen_clear_count: Literal[20]
    domain_proxy_counts: dict[str, int]
    excluded_by_proxy: dict[str, int]
    exclusion_reason_counts: dict[str, int]
    input_artifacts: dict[str, ArtifactBinding]
    output_artifacts: dict[str, ArtifactBinding]
    problem_record_ids: tuple[str, ...]
    problem_groups: tuple[str, ...]
    theorem_ids: tuple[str, ...]
    representation_ids: tuple[str, ...]
    declaration_full_names: tuple[str, ...]
    active_benchmark_registry_sha256: str = Field(pattern=_HEX64)
    active_benchmark_registry_content_hash: str = Field(pattern=_HEX64)
    domain_proxy_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_proxy_coverage_established: Literal[True]
    semantic_domain_gold_created: Literal[False]
    model_collection_authorized_count: Literal[20]
    reference_visible_to_generator: Literal[False]
    human_reviewed: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]
    model_execution_performed: Literal[False]
    generator_collection_plan_created: Literal[False]

    @model_validator(mode="after")
    def _accounting(self) -> OperationalPoolManifest:
        require_utc(self.frozen_at)
        sequences = (
            self.problem_record_ids,
            self.problem_groups,
            self.theorem_ids,
            self.representation_ids,
            self.declaration_full_names,
        )
        if any(len(values) != self.problem_count for values in sequences):
            raise ValueError("manifest ID/name counts do not reconcile")
        if any(len(values) != len(set(values)) for values in sequences):
            raise ValueError("manifest IDs/names must be unique")
        if set(self.domain_proxy_counts) != set(_EXPECTED_PROXIES):
            raise ValueError("admitted proxy coverage changed")
        if sum(self.domain_proxy_counts.values()) != self.admitted_count:
            raise ValueError("admitted proxy counts do not reconcile")
        if sum(self.excluded_by_proxy.values()) != self.excluded_count:
            raise ValueError("excluded proxy counts do not reconcile")
        if sum(self.exclusion_reason_counts.values()) != self.excluded_count:
            raise ValueError("exclusion reason counts do not reconcile")
        if not self.input_artifacts or not self.output_artifacts:
            raise ValueError("manifest bindings cannot be empty")
        return self


class OperationalAdequacyReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_cross_domain_docstrings_operational_source_adequacy_v1"]
    passed: Literal[True]
    reviewed_count: Literal[24]
    admitted_count: Literal[20]
    excluded_count: Literal[4]
    admitted_by_proxy: dict[str, int]
    excluded_by_proxy: dict[str, int]
    exclusion_reason_counts: dict[str, int]
    active_registry_three_screen_clear: Literal[20]
    no_sorry_alias_checks_verified: Literal[20]
    exact_git_source_verified: Literal[20]
    complete_reference_representations: Literal[20]
    exact_and_ancestry_dedup_survivors: Literal[20]
    distinct_ancestry_groups: Literal[20]
    domain_proxy_method: Literal["mathlib_source_path_first_segment_v1"]
    domain_proxy_is_semantic_gold: Literal[False]
    cross_domain_proxy_coverage_established: Literal[True]
    semantic_domain_gold_created: Literal[False]
    curation_type: Literal["codex_explicit_fail_closed_operational_adequacy"]
    human_reviewed: Literal[False]
    semantic_labels_created: Literal[False]
    model_collection_authorized: Literal[True]
    model_collection_scope: Literal["local_models_only"]
    reference_visible_to_generator: Literal[False]
    generator_collection_plan_created: Literal[False]
    limitations: tuple[str, ...]


class OperationalPoolReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_cross_domain_docstrings_operational_pool_preflight_v1"]
    passed: Literal[True]
    manifest: ArtifactBinding
    adequacy_report: ArtifactBinding
    reviewed_count: Literal[24]
    admitted_count: Literal[20]
    excluded_count: Literal[4]
    admitted_by_proxy: dict[str, int]
    excluded_by_proxy: dict[str, int]
    source_adequacy_passed: Literal[True]
    model_collection_authorized_count: Literal[20]
    reference_visible_to_generator: Literal[False]
    human_reviewed: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]
    model_execution_performed: Literal[False]
    generator_collection_plan_created: Literal[False]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationalPoolRun:
    report_path: Path
    adequacy_report_path: Path
    manifest_path: Path
    report: OperationalPoolReport
    adequacy_report: OperationalAdequacyReport
    manifest: OperationalPoolManifest


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CrossDomainOperationalPoolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CrossDomainOperationalPoolError(f"cannot load JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CrossDomainOperationalPoolError(f"{path}: expected a JSON object")
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
                    raise CrossDomainOperationalPoolError(
                        f"{path}:{line_number}: invalid JSON: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise CrossDomainOperationalPoolError(
                        f"{path}:{line_number}: expected a JSON object"
                    )
                yield value
    except OSError as exc:
        raise CrossDomainOperationalPoolError(f"cannot read JSONL {path}: {exc}") from exc


def _resolve(root: Path, binding: ArtifactBinding) -> Path:
    path = Path(binding.path)
    return path if path.is_absolute() else root / path


def _verify_binding(root: Path, binding: ArtifactBinding) -> Path:
    path = _resolve(root, binding)
    if not path.is_file() or path.is_symlink():
        raise CrossDomainOperationalPoolError(f"required artifact absent or unsafe: {path}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise CrossDomainOperationalPoolError(
            f"artifact hash mismatch for {path}: expected {binding.sha256}, got {observed}"
        )
    return path


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CrossDomainOperationalPoolError(
                f"immutable output differs from existing artifact: {path}"
            )
        return
    path.write_bytes(payload)


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(values: tuple[object, ...]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


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
        raise CrossDomainOperationalPoolError(
            f"Git provenance check failed: git {' '.join(args)}: {exc}"
        ) from exc


def _decision_for(
    *,
    config: CrossDomainOperationalSourceConfig,
    candidate: CrossDomainCandidate,
) -> CurationDecisionRecord:
    exclusions = {item.candidate_id: item for item in config.operational_curation.exclusions}
    exclusion = exclusions.get(candidate.candidate_id)
    if exclusion is None:
        decision = "standalone_sufficient"
        reason_code: str = config.operational_curation.default_reason_code
        rationale = (
            "The contributor-authored docstring states a usable mathematical claim; "
            "admission remains operational and is not semantic gold."
        )
        authorized = True
    else:
        if exclusion.declaration_full_name != candidate.theorem.declaration_full_name:
            raise CrossDomainOperationalPoolError(
                f"{candidate.candidate_id}: exclusion declaration binding drifted"
            )
        decision = "excluded"
        reason_code = exclusion.reason_code
        rationale = exclusion.rationale
        authorized = False
    payload = {
        "schema": "lf021_cross_domain_operational_curation_decision_v1",
        "candidate_id": candidate.candidate_id,
        "declaration_full_name": candidate.theorem.declaration_full_name,
        "decision": decision,
        "reason_code": reason_code,
        "rationale": rationale,
    }
    return CurationDecisionRecord(
        decision_id="cross_domain_curation:" + hash_canonical(payload),
        candidate_id=candidate.candidate_id,
        declaration_full_name=candidate.theorem.declaration_full_name or "",
        domain_proxy=candidate.domain_proxy,
        decision=decision,  # type: ignore[arg-type]
        reason_code=reason_code,
        rationale=rationale,
        model_collection_authorized=authorized,
        authorization_scope="local_models_only" if authorized else "none",
    )


def _representation_complete(candidate: CrossDomainCandidate) -> None:
    representation = candidate.representation
    required = (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    )
    for view in required:
        if representation.view_status.get(view) != "ok" or getattr(representation, view) in (
            None,
            "",
            (),
            [],
            {},
        ):
            raise CrossDomainOperationalPoolError(
                f"{candidate.candidate_id}: required representation {view} is incomplete"
            )
    if representation.alpha_identity_fingerprint is None:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: alpha identity fingerprint is absent"
        )
    if not isinstance(representation.headless, str) or representation.headless.startswith(
        "(Command.declSig"
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: headless view is not a usable theorem signature"
        )
    raw = representation.raw_proof_stripped
    if not isinstance(raw, str) or "by sorry" not in raw:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: proof-stripped source contract drifted"
        )


def _nl_quality(candidate: CrossDomainCandidate) -> None:
    nl = candidate.docstring.normalized_nl.strip()
    lowered = nl.casefold()
    if len(nl) < 20 or lowered.startswith("see `") or lowered.startswith("see "):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: admitted NL fails standalone quality checks"
        )
    if nl.endswith(":") or not nl.endswith((".", "?", "!")):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: admitted NL is title-like or incomplete"
        )


def _verify_source(
    *,
    repo: Path,
    config: CrossDomainOperationalSourceConfig,
    candidate: CrossDomainCandidate,
) -> None:
    provenance = candidate.source_provenance
    temporal = candidate.exact_pair_introduction
    if (
        provenance.revision != config.revision
        or temporal.search_revision != config.revision
        or not temporal.strictly_postdates_latest_checkpoint
        or not temporal.introduction_commit_is_search_revision_ancestor
        or not temporal.exact_pair_present_in_introduction_blob
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: temporal/source provenance drifted"
        )
    source_blob = _git(repo, "show", f"{config.revision}:{provenance.source_file}").stdout
    blob_sha1 = _git(
        repo,
        "rev-parse",
        f"{config.revision}:{provenance.source_file}",
    ).stdout.strip()
    if (
        sha256_hex(source_blob.encode("utf-8")) != provenance.source_file_sha256
        or blob_sha1 != provenance.git_blob_sha1
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: pinned source bytes drifted"
        )
    names = tuple(
        dict.fromkeys(
            name
            for name in (
                candidate.theorem.declaration_full_name,
                candidate.theorem.declaration_name,
            )
            if name
        )
    )
    if not any(
        _exact_pair_present(
            blob=source_blob,
            raw_docstring=candidate.docstring.raw,
            declaration_name=name,
        )
        for name in names
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: exact docstring/declaration pair is absent"
        )
    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        temporal.introduction_commit,
        config.revision,
        check=False,
    )
    if ancestor.returncode != 0:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: introduction commit is not source ancestry"
        )
    intro_date = _git(
        repo, "show", "-s", "--format=%cI", temporal.introduction_commit
    ).stdout.strip()
    if datetime.datetime.fromisoformat(intro_date) != temporal.introduction_created_at:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: introduction timestamp drifted"
        )
    intro_blob = _git(
        repo,
        "show",
        f"{temporal.introduction_commit}:{temporal.introduction_source_path}",
    ).stdout
    if not any(
        _exact_pair_present(
            blob=intro_blob,
            raw_docstring=candidate.docstring.raw,
            declaration_name=name,
        )
        for name in names
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: introduction blob lacks exact pair"
        )


def _source_record_identity(candidate: CrossDomainCandidate) -> tuple[str, str]:
    locator = hash_canonical(
        {
            "schema": "mathlib_cross_domain_operational_docstring_locator_v1",
            "repository": candidate.source_provenance.repository,
            "revision": candidate.source_provenance.revision,
            "source_file": candidate.source_provenance.source_file,
            "source_range": candidate.source_provenance.source_range,
            "theorem_id": candidate.theorem.theorem_id,
        }
    )
    content = hash_canonical(
        {
            "schema": "mathlib_cross_domain_operational_docstring_content_v1",
            "candidate": candidate.model_dump(mode="json"),
        }
    )
    return locator, content


def _problem_candidate(
    *,
    candidate: CrossDomainCandidate,
    decision: CurationDecisionRecord,
    context: ContextRecord,
    header_artifact: str,
    header_hash: str,
) -> ProblemPoolCandidate:
    source_record_id, source_record_content_hash = _source_record_identity(candidate)
    suffix = candidate.candidate_id.rsplit(":", 1)[-1]
    problem_group = "nl-problem:" + hash_canonical(
        {
            "schema": "mathlib_cross_domain_operational_problem_group_v1",
            "candidate_id": candidate.candidate_id,
            "ancestry_id": candidate.theorem.ancestry_id,
        }
    )
    return ProblemPoolCandidate(
        problem_id=f"cross-domain-docstring:{suffix}",
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
        reference_theorem_ids=(candidate.theorem.theorem_id,),
        source_license="Apache-2.0",
        private_source_content=False,
        release_eligible=True,
        near_duplicate_group_ids=(candidate.theorem.ancestry_id,),
        overlap_tags=(
            "formalrx_lineage:mathlib_docstring_theorem_pairs",
            "pretraining_contamination:unknown",
            "temporal_provenance:strictly_postdates_all_three_checkpoints",
        ),
        metadata={
            "candidate_id": candidate.candidate_id,
            "curation_decision_id": decision.decision_id,
            "curation_type": "codex_explicit_fail_closed_operational_adequacy",
            "human_reviewed": False,
            "semantic_gold_created": False,
            "model_collection_authorized": True,
            "model_collection_scope": "local_models_only",
            "external_provider_collection_authorized": False,
            "reference_visible_to_generator": False,
            "gate_claimed": False,
            "domain_proxy": candidate.domain_proxy,
            "domain_proxy_method": "mathlib_source_path_first_segment_v1",
            "domain_proxy_is_semantic_gold": False,
            "cross_domain_proxy_coverage_established": True,
            "semantic_domain_gold_created": False,
            "source_declaration_full_name": candidate.theorem.declaration_full_name or "",
            "temporal_introduction_commit": (candidate.exact_pair_introduction.introduction_commit),
        },
    )


def _validate_reference_alias(
    *,
    backend: LeanInteractBackend,
    output_root: Path,
    header_text: str,
    context_id: str,
    candidate: CrossDomainCandidate,
    decision: CurationDecisionRecord,
) -> ReferenceCheckRecord:
    reference_name = "LeanFaithCrossDomainReference_" + candidate.candidate_id[-16:]
    statement = f"def {reference_name} := @{candidate.theorem.declaration_full_name}\n"
    result = backend.run(
        LeanRequest(
            request_id=f"lf021-cross-domain-reference-{candidate.candidate_id[-16:]}",
            context_id=context_id,
            code=header_text + statement,
            declarations=True,
            allow_sorry=False,
            timeout_seconds=300.0,
            metadata={"candidate_id": candidate.candidate_id},
        )
    )
    if result.status is not LeanStatus.VALID:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: no-sorry reference alias failed ({result.status.value})"
        )
    declarations = tuple(result.declarations)
    if len(declarations) != 1 or declarations[0].get("name") != reference_name:
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: reference declaration extraction drifted"
        )
    if result.raw_response_path is None:
        raise CrossDomainOperationalPoolError("reference alias lacks raw response")
    raw_path = Path(result.raw_response_path)
    if not raw_path.is_file() or output_root not in raw_path.parents:
        raise CrossDomainOperationalPoolError("reference raw response escaped output root")
    raw = _load_json(raw_path)
    request = raw.get("request")
    response = raw.get("response")
    if (
        not isinstance(request, dict)
        or not isinstance(response, dict)
        or request.get("allow_sorry") is not False
        or request.get("code") != header_text + statement
        or raw.get("error") is not None
        or response.get("sorries") not in ([], None)
    ):
        raise CrossDomainOperationalPoolError(
            f"{candidate.candidate_id}: persisted no-sorry request is invalid"
        )
    return ReferenceCheckRecord(
        candidate_id=candidate.candidate_id,
        decision_id=decision.decision_id,
        reference_declaration_name=reference_name,
        lean_request_hash=result.request_hash,
        raw_response_artifact=ArtifactBinding(
            path=str(raw_path.relative_to(output_root.parent.parent.parent.parent)),
            sha256=hash_file(raw_path),
        ),
    )


def _adequacy_markdown(
    *,
    report: OperationalAdequacyReport,
    manifest_path: Path,
    root: Path,
) -> bytes:
    lines = [
        "# LF-021 cross-domain docstring operational source adequacy v1",
        "",
        "**Result: PASS for pinned local-model collection preflight only.**",
        "",
        f"- Reviewed feasibility candidates: {report.reviewed_count}",
        f"- Operationally admitted: {report.admitted_count}",
        f"- Fail-closed exclusions: {report.excluded_count}",
        f"- Active-registry three-screen clear: {report.active_registry_three_screen_clear}/20",
        f"- `allow_sorry=false` alias checks: {report.no_sorry_alias_checks_verified}/20",
        f"- Exact Git source checks: {report.exact_git_source_verified}/20",
        f"- Manifest: `{manifest_path.relative_to(root)}`",
        "",
        "## Admitted top-level Mathlib path proxies",
        "",
        *(f"- `{proxy}`: {count}" for proxy, count in sorted(report.admitted_by_proxy.items())),
        "",
        "## Fail-closed exclusions by proxy",
        "",
        *(f"- `{proxy}`: {count}" for proxy, count in sorted(report.excluded_by_proxy.items())),
        "",
        "## Scope boundary",
        "",
        "Top-level Mathlib paths are deterministic proxies, not semantic-domain gold.",
        "Operational curation is Codex-assisted and is not human review or faithfulness gold.",
        "The reference theorem and representation are retained for scoring "
        "and hidden from generators.",
        "No generator was loaded, no semantic label was created, and no Gate credit is claimed.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _verify_persisted(
    *,
    paths: RepoPaths,
    source: CrossDomainOperationalSourceConfig,
) -> OperationalPoolRun:
    root = paths.root.resolve()
    manifest_path = root / source.outputs.manifest
    report_path = root / source.outputs.report
    adequacy_path = root / source.outputs.adequacy_report
    manifest = OperationalPoolManifest.model_validate(_load_json(manifest_path))
    report = OperationalPoolReport.model_validate(_load_json(report_path))
    adequacy = OperationalAdequacyReport.model_validate(_load_json(adequacy_path))
    for binding in (*manifest.input_artifacts.values(), *manifest.output_artifacts.values()):
        _verify_binding(root, binding)
    if report.manifest.sha256 != hash_file(manifest_path):
        raise CrossDomainOperationalPoolError("report manifest binding drifted")
    if report.adequacy_report.sha256 != hash_file(adequacy_path):
        raise CrossDomainOperationalPoolError("report adequacy binding drifted")
    records = tuple(
        ProblemPoolRecord.model_validate(raw) for raw in _iter_jsonl(root / source.outputs.records)
    )
    output_root = root / source.outputs.root
    decisions = tuple(
        CurationDecisionRecord.model_validate(raw)
        for raw in _iter_jsonl(output_root / "curation_decisions.jsonl")
    )
    checks = tuple(
        ReferenceCheckRecord.model_validate(raw)
        for raw in _iter_jsonl(output_root / "no_sorry_reference_checks.jsonl")
    )
    audits = tuple(
        OperationalRecordAudit.model_validate(raw)
        for raw in _iter_jsonl(output_root / "record_audits.jsonl")
    )
    if (
        len(records) != manifest.problem_count
        or any(record.eligibility != "eligible" for record in records)
        or {record.problem_record_id for record in records} != set(manifest.problem_record_ids)
    ):
        raise CrossDomainOperationalPoolError("persisted problem records drifted")
    if (
        len(decisions) != manifest.reviewed_count
        or sum(item.decision == "standalone_sufficient" for item in decisions)
        != manifest.admitted_count
        or len(checks) != manifest.no_sorry_alias_check_count
        or len(audits) != manifest.problem_count
    ):
        raise CrossDomainOperationalPoolError("persisted curation/audit accounting drifted")
    for check in checks:
        _verify_binding(root, check.raw_response_artifact)
    prohibited_reference_fields = {
        "reference_lean_statement",
        "reference_statement",
        "reference_type_pp",
        "signature_pp",
        "signature_explicit",
        "raw_proof_stripped",
    }
    if any(
        record.external_provider_eligible
        or bool(prohibited_reference_fields & record.metadata.keys())
        or record.metadata.get("reference_visible_to_generator") is not False
        or record.metadata.get("semantic_gold_created") is not False
        or record.metadata.get("gate_claimed") is not False
        for record in records
    ):
        raise CrossDomainOperationalPoolError("persisted prompt-safety boundary drifted")
    if (root / source.outputs.failures).read_bytes() != b"":
        raise CrossDomainOperationalPoolError("operational pool failure partition is not empty")
    return OperationalPoolRun(
        report_path=report_path,
        adequacy_report_path=adequacy_path,
        manifest_path=manifest_path,
        report=report,
        adequacy_report=adequacy,
        manifest=manifest,
    )


def run_cross_domain_operational_pool(
    *,
    paths: RepoPaths,
    mathlib_checkout: Path,
) -> OperationalPoolRun:
    """Materialize once, then exactly verify the cross-domain operational pool."""

    root = paths.root.resolve()
    loaded_source = load_config(
        root / SOURCE_CONFIG,
        CrossDomainOperationalSourceConfig,
    )
    source = loaded_source.config
    loaded_pool = load_problem_pool_config(root / POOL_CONFIG)
    pool_config: ProblemPoolConfig = loaded_pool.config
    manifest_path = root / source.outputs.manifest
    if manifest_path.is_file():
        return _verify_persisted(paths=paths, source=source)

    if _git(mathlib_checkout, "rev-parse", "HEAD").stdout.strip() != source.revision:
        raise CrossDomainOperationalPoolError("mathlib checkout is not at the pinned revision")
    if (
        _git(mathlib_checkout, "rev-parse", f"{source.revision}:LICENSE").stdout.strip()
        != source.license_blob_sha1
    ):
        raise CrossDomainOperationalPoolError("pinned mathlib license blob changed")

    enabled = tuple(item for item in pool_config.sources if item.enabled)
    if len(enabled) != 1 or enabled[0].source != source.source:
        raise CrossDomainOperationalPoolError("pool must enable exactly its operational source")
    if enabled[0].source_config_sha256 != hash_file(root / SOURCE_CONFIG):
        raise CrossDomainOperationalPoolError("pool source-config binding drifted")
    if enabled[0].authorization != source.lf021_authorization:
        raise CrossDomainOperationalPoolError("pool/source authorization drifted")
    if enabled[0].external_provider_eligible:
        raise CrossDomainOperationalPoolError("pool cannot authorize external providers")
    replication_path = root / pool_config.public_replication_profile
    if not replication_path.is_file() or replication_path.is_symlink():
        raise CrossDomainOperationalPoolError("public replication profile is absent or unsafe")

    feasibility = source.feasibility_input
    for binding in (
        feasibility.config,
        feasibility.report,
        feasibility.manifest,
        feasibility.selected_candidates,
        source.canonical_context.context,
        source.canonical_context.import_header,
        source.screening.active_benchmark_registry_manifest,
    ):
        _verify_binding(root, binding)
    probe = verify_cross_domain_probe(paths=paths)
    if (
        hash_file(probe.report_path) != feasibility.report.sha256
        or hash_file(probe.manifest_path) != feasibility.manifest.sha256
        or probe.manifest.selected_candidates != _EXPECTED_REVIEWED
        or probe.report.selected_count != _EXPECTED_REVIEWED
    ):
        raise CrossDomainOperationalPoolError("feasibility probe binding or count drifted")
    probe_manifest = CrossDomainProbeManifest.model_validate(_load_json(probe.manifest_path))
    probe_report = CrossDomainProbeReport.model_validate(_load_json(probe.report_path))
    if (
        not probe_manifest.passed
        or not probe_report.passed
        or probe_manifest.problem_pool_admitted
        or probe_manifest.model_collection_authorized
    ):
        raise CrossDomainOperationalPoolError("feasibility source policy boundary changed")

    selected_path = _verify_binding(root, feasibility.selected_candidates)
    selected = tuple(CrossDomainCandidate.model_validate(raw) for raw in _iter_jsonl(selected_path))
    if len(selected) != _EXPECTED_REVIEWED:
        raise CrossDomainOperationalPoolError("selected feasibility count drifted")
    if tuple(sorted({item.domain_proxy for item in selected})) != _EXPECTED_PROXIES:
        raise CrossDomainOperationalPoolError("selected feasibility proxy coverage drifted")
    if any(not item.selected for item in selected):
        raise CrossDomainOperationalPoolError("selected partition contains unselected candidate")

    decisions = tuple(_decision_for(config=source, candidate=item) for item in selected)
    if len({item.decision_id for item in decisions}) != _EXPECTED_REVIEWED:
        raise CrossDomainOperationalPoolError("curation decision IDs are not unique")
    admitted_ids = {
        decision.candidate_id
        for decision in decisions
        if decision.decision == "standalone_sufficient"
    }
    admitted = tuple(item for item in selected if item.candidate_id in admitted_ids)
    excluded = tuple(item for item in selected if item.candidate_id not in admitted_ids)
    if len(admitted) != _EXPECTED_ADMITTED or len(excluded) != _EXPECTED_EXCLUDED:
        raise CrossDomainOperationalPoolError("curation accounting drifted")

    context_path = _verify_binding(root, source.canonical_context.context)
    header_path = _verify_binding(root, source.canonical_context.import_header)
    context = ContextRecord.model_validate(_load_json(context_path))
    header_text = header_path.read_text(encoding="utf-8")
    if (
        context.context_id != source.canonical_context.context_id
        or context.project_revision != source.revision
        or context.header_text != header_text
        or context.header_hash != source.canonical_context.import_header.sha256
        or header_text != "import Mathlib\n"
    ):
        raise CrossDomainOperationalPoolError("canonical context/header drifted")

    decision_by_candidate = {item.candidate_id: item for item in decisions}
    for candidate in admitted:
        if (
            candidate.theorem.context_id != context.context_id
            or candidate.representation.context_id != context.context_id
            or candidate.representation.theorem_id != candidate.theorem.theorem_id
        ):
            raise CrossDomainOperationalPoolError(
                f"{candidate.candidate_id}: canonical reference binding drifted"
            )
        _nl_quality(candidate)
        _representation_complete(candidate)
        _verify_source(repo=mathlib_checkout, config=source, candidate=candidate)

    active = load_active_benchmark_registry(repo_root=root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(active, repo_root=root)
    configured_registry = source.screening.active_benchmark_registry_manifest
    if (
        configured_registry.sha256 != denylist.manifest_sha256
        or pool_config.active_benchmark_registry_manifest_sha256 != denylist.manifest_sha256
    ):
        raise CrossDomainOperationalPoolError("active benchmark registry binding drifted")
    for candidate in admitted:
        upstream = candidate.registry_screens
        if (
            upstream.registry_manifest_sha256 != denylist.manifest_sha256
            or upstream.active_registry_sha256 != denylist.active_registry_sha256
            or upstream.registry_content_hash != denylist.registry_content_hash
            or not upstream.all_three_screens_clear
        ):
            raise CrossDomainOperationalPoolError(
                f"{candidate.candidate_id}: upstream benchmark screens drifted"
            )

    output_root = root / source.outputs.root
    raw_dir = output_root / "raw_reference_checks"
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=mathlib_checkout,
            context_fingerprint=context.context_id.removeprefix("ctx:"),
            environment_schema_version=source.canonical_context.environment_schema_version,
            raw_response_dir=raw_dir,
        )
    )
    checks: list[ReferenceCheckRecord] = []
    try:
        for candidate in sorted(admitted, key=lambda item: item.candidate_id):
            checks.append(
                _validate_reference_alias(
                    backend=backend,
                    output_root=output_root,
                    header_text=header_text,
                    context_id=context.context_id,
                    candidate=candidate,
                    decision=decision_by_candidate[candidate.candidate_id],
                )
            )
    finally:
        backend.close()
    checks_by_candidate = {item.candidate_id: item for item in checks}
    if len(checks_by_candidate) != _EXPECTED_ADMITTED:
        raise CrossDomainOperationalPoolError("no-sorry reference checks do not reconcile")

    header_artifact = str(header_path.relative_to(root))
    problem_candidates = tuple(
        _problem_candidate(
            candidate=candidate,
            decision=decision_by_candidate[candidate.candidate_id],
            context=context,
            header_artifact=header_artifact,
            header_hash=source.canonical_context.import_header.sha256,
        )
        for candidate in admitted
    )
    pool = build_problem_pool(
        config=pool_config,
        denylist=denylist,
        candidates=problem_candidates,
    )
    if len(pool.records) != _EXPECTED_ADMITTED:
        raise CrossDomainOperationalPoolError("problem-pool terminal accounting drifted")
    ineligible = tuple(record for record in pool.records if record.eligibility != "eligible")
    if ineligible:
        details = "; ".join(
            f"{item.problem_id}:{','.join(item.exclusion_reasons)}" for item in ineligible
        )
        raise CrossDomainOperationalPoolError(
            f"rerun screening/dedup rejected admitted records: {details}"
        )
    if pool.public_trusted_problems:
        raise CrossDomainOperationalPoolError(
            "local-only operational pool exposed external-provider prompt records"
        )

    problem_by_candidate: dict[str, ProblemPoolRecord] = {}
    for record in pool.records:
        candidate_id = record.metadata.get("candidate_id")
        if not isinstance(candidate_id, str):
            raise CrossDomainOperationalPoolError("problem record lacks candidate binding")
        problem_by_candidate[candidate_id] = record

    audits: list[OperationalRecordAudit] = []
    for candidate in admitted:
        problem = problem_by_candidate[candidate.candidate_id]
        reference_hits = candidate_benchmark_hits(
            denylist_index=denylist.index,
            theorem=candidate.theorem,
            representation=candidate.representation,
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
            raise CrossDomainOperationalPoolError(
                f"{candidate.candidate_id}: unclassified benchmark hits: {unexpected}"
            )
        screens = RegistryScreens(
            problem_identity_and_nl_hits=problem.denylist_hits,
            reference_lean_hits=lean_hits,
            reference_representation_hits=representation_hits,
            all_three_screens_clear=True,
            registry_manifest_sha256=denylist.manifest_sha256,
            active_registry_sha256=denylist.active_registry_sha256,
            registry_content_hash=denylist.registry_content_hash,
        )
        check = checks_by_candidate[candidate.candidate_id]
        alpha = candidate.representation.alpha_identity_fingerprint
        assert alpha is not None
        audits.append(
            OperationalRecordAudit(
                problem_record_id=problem.problem_record_id,
                candidate_id=candidate.candidate_id,
                decision_id=decision_by_candidate[candidate.candidate_id].decision_id,
                declaration_full_name=candidate.theorem.declaration_full_name or "",
                domain_proxy=candidate.domain_proxy,
                source_revision=candidate.source_provenance.revision,
                source_file=candidate.source_provenance.source_file,
                source_file_sha256=candidate.source_provenance.source_file_sha256,
                source_blob_sha1=candidate.source_provenance.git_blob_sha1,
                source_pair_present=True,
                temporal_introduction_commit=(
                    candidate.exact_pair_introduction.introduction_commit
                ),
                temporal_introduction_created_at=(
                    candidate.exact_pair_introduction.introduction_created_at
                ),
                temporal_strictly_postdates_latest_checkpoint=True,
                reference_theorem_id=candidate.theorem.theorem_id,
                reference_representation_id=candidate.representation.representation_id,
                reference_alpha_identity_fingerprint=alpha,
                representation_complete=True,
                no_sorry_alias_request_hash=check.lean_request_hash,
                no_sorry_alias_raw_response_sha256=check.raw_response_artifact.sha256,
                no_sorry_alias_check_valid=True,
                registry_screens=screens,
                model_collection_scope="local_models_only",
                reference_visible_to_generator=False,
                semantic_labels_created=False,
                gate_claimed=False,
            )
        )

    records_path = root / source.outputs.records
    failures_path = root / source.outputs.failures
    decisions_path = output_root / "curation_decisions.jsonl"
    checks_path = output_root / "no_sorry_reference_checks.jsonl"
    context_output_path = output_root / "context.json"
    theorem_output_path = output_root / "reference_theorems.jsonl"
    representation_output_path = output_root / "reference_representations.jsonl"
    audit_output_path = output_root / "record_audits.jsonl"
    manifest_path = root / source.outputs.manifest

    ordered_records = tuple(
        item.model_dump(mode="json")
        for item in sorted(pool.records, key=lambda value: value.problem_record_id)
    )
    ordered_decisions = tuple(
        item.model_dump(mode="json")
        for item in sorted(decisions, key=lambda value: value.candidate_id)
    )
    ordered_checks = tuple(
        item.model_dump(mode="json")
        for item in sorted(checks, key=lambda value: value.candidate_id)
    )
    ordered_theorems = tuple(
        item.theorem.model_dump(mode="json")
        for item in sorted(admitted, key=lambda value: value.theorem.theorem_id)
    )
    ordered_representations = tuple(
        item.representation.model_dump(mode="json")
        for item in sorted(admitted, key=lambda value: value.representation.representation_id)
    )
    ordered_audits = tuple(
        item.model_dump(mode="json")
        for item in sorted(audits, key=lambda value: value.problem_record_id)
    )
    _write_exact(records_path, _jsonl_bytes(ordered_records))
    _write_exact(failures_path, b"")
    _write_exact(decisions_path, _jsonl_bytes(ordered_decisions))
    _write_exact(checks_path, _jsonl_bytes(ordered_checks))
    _write_exact(context_output_path, _json_bytes(context.model_dump(mode="json")))
    _write_exact(theorem_output_path, _jsonl_bytes(ordered_theorems))
    _write_exact(representation_output_path, _jsonl_bytes(ordered_representations))
    _write_exact(audit_output_path, _jsonl_bytes(ordered_audits))

    domain_counts = dict(sorted(Counter(item.domain_proxy for item in admitted).items()))
    excluded_by_proxy = dict(sorted(Counter(item.domain_proxy for item in excluded).items()))
    reason_counts = dict(
        sorted(
            Counter(item.reason_code for item in decisions if item.decision == "excluded").items()
        )
    )
    input_artifacts = {
        "source_config": ArtifactBinding(
            path=str(SOURCE_CONFIG), sha256=hash_file(root / SOURCE_CONFIG)
        ),
        "pool_config": ArtifactBinding(path=str(POOL_CONFIG), sha256=hash_file(root / POOL_CONFIG)),
        "public_replication_profile": ArtifactBinding(
            path=pool_config.public_replication_profile,
            sha256=hash_file(replication_path),
        ),
        "feasibility_config": feasibility.config,
        "feasibility_report": feasibility.report,
        "feasibility_manifest": feasibility.manifest,
        "selected_candidates": feasibility.selected_candidates,
        "canonical_context": source.canonical_context.context,
        "import_header": source.canonical_context.import_header,
        "active_benchmark_manifest": ArtifactBinding(
            path=denylist.manifest_path,
            sha256=denylist.manifest_sha256,
        ),
        "active_benchmark_registry": ArtifactBinding(
            path=str(active.active_registry_path.relative_to(root)),
            sha256=denylist.active_registry_sha256,
        ),
        "implementation": ArtifactBinding(
            path=str(Path(__file__).relative_to(root)),
            sha256=hash_file(Path(__file__)),
        ),
    }
    output_files = {
        "problem_pool_records": records_path,
        "problem_pool_failures": failures_path,
        "curation_decisions": decisions_path,
        "no_sorry_reference_checks": checks_path,
        "context": context_output_path,
        "reference_theorems": theorem_output_path,
        "reference_representations": representation_output_path,
        "record_audits": audit_output_path,
    }
    output_artifacts = {
        name: ArtifactBinding(
            path=str(path.relative_to(root)),
            sha256=hash_file(path),
        )
        for name, path in sorted(output_files.items())
    }
    for check in sorted(checks, key=lambda value: value.candidate_id):
        output_artifacts[f"raw_reference_check:{check.candidate_id}"] = check.raw_response_artifact
    manifest_core = {
        "schema": "lf021_cross_domain_operational_manifest_identity_v1",
        "input_artifacts": {
            name: value.model_dump(mode="json") for name, value in sorted(input_artifacts.items())
        },
        "output_artifacts": {
            name: value.model_dump(mode="json") for name, value in sorted(output_artifacts.items())
        },
        "problem_record_ids": [
            item.problem_record_id
            for item in sorted(pool.records, key=lambda value: value.problem_record_id)
        ],
    }
    manifest = OperationalPoolManifest(
        artifact_kind="lf021_cross_domain_docstrings_operational_problem_pool_v1",
        manifest_id="cross_domain_operational_manifest:" + hash_canonical(manifest_core),
        frozen_at=datetime.datetime(2026, 7, 24, 0, 0, tzinfo=datetime.UTC),
        source=_SOURCE_NAME,
        source_revision=source.revision,
        context_id=context.context_id,
        reviewed_count=24,
        admitted_count=20,
        excluded_count=4,
        problem_count=20,
        eligible_problem_count=20,
        reference_theorem_count=20,
        reference_representation_count=20,
        no_sorry_alias_check_count=20,
        rerun_three_screen_clear_count=20,
        domain_proxy_counts=domain_counts,
        excluded_by_proxy=excluded_by_proxy,
        exclusion_reason_counts=reason_counts,
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        problem_record_ids=tuple(
            item.problem_record_id
            for item in sorted(pool.records, key=lambda value: value.problem_record_id)
        ),
        problem_groups=tuple(
            item.problem_group
            for item in sorted(pool.records, key=lambda value: value.problem_record_id)
        ),
        theorem_ids=tuple(sorted(item.theorem.theorem_id for item in admitted)),
        representation_ids=tuple(
            sorted(item.representation.representation_id for item in admitted)
        ),
        declaration_full_names=tuple(
            sorted(item.theorem.declaration_full_name or "" for item in admitted)
        ),
        active_benchmark_registry_sha256=denylist.active_registry_sha256,
        active_benchmark_registry_content_hash=denylist.registry_content_hash,
        domain_proxy_method="mathlib_source_path_first_segment_v1",
        domain_proxy_is_semantic_gold=False,
        cross_domain_proxy_coverage_established=True,
        semantic_domain_gold_created=False,
        model_collection_authorized_count=20,
        reference_visible_to_generator=False,
        human_reviewed=False,
        semantic_labels_created=False,
        gate_claimed=False,
        model_execution_performed=False,
        generator_collection_plan_created=False,
    )
    _write_exact(manifest_path, _json_bytes(manifest.model_dump(mode="json")))

    ancestry_count = len({item.theorem.ancestry_id for item in admitted})
    if ancestry_count != _EXPECTED_ADMITTED:
        raise CrossDomainOperationalPoolError("admitted ancestry groups are not unique")
    limitations = (
        "Operational curation is Codex-assisted and is not human review.",
        "Standalone adequacy is not a same-claim or semantic-gold label.",
        "Top-level Mathlib paths are domain proxies, not adjudicated semantic domains.",
        "Temporal non-overlap does not prove absence from model pretraining.",
        "All records retain FormalRx mathlib-docstring source-lineage tags.",
        "References are retained for scoring and hidden from generators.",
        "No generator plan, model output, semantic label, or Gate credit is created.",
    )
    adequacy = OperationalAdequacyReport(
        report_kind="lf021_cross_domain_docstrings_operational_source_adequacy_v1",
        passed=True,
        reviewed_count=24,
        admitted_count=20,
        excluded_count=4,
        admitted_by_proxy=domain_counts,
        excluded_by_proxy=excluded_by_proxy,
        exclusion_reason_counts=reason_counts,
        active_registry_three_screen_clear=20,
        no_sorry_alias_checks_verified=20,
        exact_git_source_verified=20,
        complete_reference_representations=20,
        exact_and_ancestry_dedup_survivors=20,
        distinct_ancestry_groups=20,
        domain_proxy_method="mathlib_source_path_first_segment_v1",
        domain_proxy_is_semantic_gold=False,
        cross_domain_proxy_coverage_established=True,
        semantic_domain_gold_created=False,
        curation_type="codex_explicit_fail_closed_operational_adequacy",
        human_reviewed=False,
        semantic_labels_created=False,
        model_collection_authorized=True,
        model_collection_scope="local_models_only",
        reference_visible_to_generator=False,
        generator_collection_plan_created=False,
        limitations=limitations,
    )
    adequacy_path = root / source.outputs.adequacy_report
    _write_exact(adequacy_path, _json_bytes(adequacy.model_dump(mode="json")))
    _write_exact(
        root / source.outputs.adequacy_markdown,
        _adequacy_markdown(report=adequacy, manifest_path=manifest_path, root=root),
    )

    blockers = (
        "Four feasibility candidates were excluded fail-closed; quotas were not backfilled.",
        "Path proxies do not establish adjudicated semantic-domain diversity.",
        "Operational adequacy is not human review or semantic gold.",
        "Pretraining contamination remains unknown despite strict temporal provenance.",
        "This pool alone cannot close Gate 5 or Gate 5G.",
    )
    report = OperationalPoolReport(
        report_kind="lf021_cross_domain_docstrings_operational_pool_preflight_v1",
        passed=True,
        manifest=ArtifactBinding(
            path=str(manifest_path.relative_to(root)),
            sha256=hash_file(manifest_path),
        ),
        adequacy_report=ArtifactBinding(
            path=str(adequacy_path.relative_to(root)),
            sha256=hash_file(adequacy_path),
        ),
        reviewed_count=24,
        admitted_count=20,
        excluded_count=4,
        admitted_by_proxy=domain_counts,
        excluded_by_proxy=excluded_by_proxy,
        source_adequacy_passed=True,
        model_collection_authorized_count=20,
        reference_visible_to_generator=False,
        human_reviewed=False,
        semantic_labels_created=False,
        gate_claimed=False,
        model_execution_performed=False,
        generator_collection_plan_created=False,
        blockers=blockers,
    )
    report_path = root / source.outputs.report
    _write_exact(report_path, _json_bytes(report.model_dump(mode="json")))
    return OperationalPoolRun(
        report_path=report_path,
        adequacy_report_path=adequacy_path,
        manifest_path=manifest_path,
        report=report,
        adequacy_report=adequacy,
        manifest=manifest,
    )


def verify_cross_domain_operational_pool(*, paths: RepoPaths) -> OperationalPoolRun:
    """Verify the persisted pool without Lean or model execution."""

    source = load_config(
        paths.root / SOURCE_CONFIG,
        CrossDomainOperationalSourceConfig,
    ).config
    return _verify_persisted(paths=paths, source=source)


__all__ = [
    "ADEQUACY_MARKDOWN_PATH",
    "ADEQUACY_REPORT_PATH",
    "POOL_CONFIG",
    "REPORT_PATH",
    "SOURCE_CONFIG",
    "CrossDomainOperationalPoolError",
    "CrossDomainOperationalSourceConfig",
    "CurationDecisionRecord",
    "OperationalAdequacyReport",
    "OperationalPoolManifest",
    "OperationalPoolReport",
    "OperationalPoolRun",
    "OperationalRecordAudit",
    "ReferenceCheckRecord",
    "run_cross_domain_operational_pool",
    "verify_cross_domain_operational_pool",
]

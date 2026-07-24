"""Bounded, model-free LF-021 cross-domain mathlib feasibility probe.

The frozen Gate-3 mathlib sample is an Algebra-only prefix.  This module does
not reinterpret or modify it.  Instead, it checks a separately configured set
of public files from non-Algebra top-level mathlib directories.  LeanInteract
extracts theorem/lemma declarations, the representation pipeline derives the
same reference views used elsewhere, and the active benchmark registry screens
both NL and formal representations.

The result is evidence that a later cross-domain source pool can be built.  It
is deliberately *not* an admitted problem pool, a semantic label, a human
review, model execution, or Gate evidence.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
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
from leanfaith.generation.gate3_docstring_pool import (
    AdjacentDocstring,
    RegistryScreens,
    SourceProvenance,
    TemporalIntroductionEvidence,
    _blob_for_introduction_candidate,
    _git,
    _git_optional,
    _git_tree_blobs,
    _registry_screens,
    _revision_order,
    extract_adjacent_docstring,
)
from leanfaith.lean.extraction import SourceIdentity, extract_from_declarations
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.session_policy import run_with_retries
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
    declaration_environment_lookup_name,
)
from leanfaith.schemas.enums import ValidationStatus, ViewStatus
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.sources.mathlib import verify_checkout_revision

CONFIG_PATH = Path("configs/generation/problem_pool_cross_domain_mathlib_docstrings_probe_v1.yaml")
REPORT_PATH = Path("reports/generation/lf021_cross_domain_mathlib_docstring_feasibility_v1.json")
DEFAULT_OUTPUT_ROOT = Path(
    "/storage/milikic/leanfaith/lf021/cross_domain_mathlib_docstring_feasibility_v1"
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_REQUIRED_REFERENCE_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "semantic_atoms",
    "operator_tree",
)


class CrossDomainProbeError(RuntimeError):
    """The bounded feasibility probe cannot be completed safely."""


class ArtifactBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class ProbeSourceConfig(StrictModel):
    repository: Literal["https://github.com/leanprover-community/mathlib4"]
    checkout_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    environment_schema_version: Literal[1]
    import_header: ArtifactBinding
    latest_generator_checkpoint_created_at: datetime.datetime

    @model_validator(mode="after")
    def _timestamp(self) -> ProbeSourceConfig:
        require_utc(self.latest_generator_checkpoint_created_at)
        return self


class DomainFileConfig(StrictModel):
    domain_proxy: str = Field(pattern=r"^[A-Z][A-Za-z0-9]+$")
    source_file: str = Field(pattern=r"^Mathlib/[A-Za-z0-9_/]+\.lean$")
    source_file_sha256: str = Field(pattern=_HEX64)
    git_blob_sha1: str = Field(pattern=_HEX40)
    file_addition_commit: str = Field(pattern=_HEX40)
    file_addition_created_at: datetime.datetime
    screening_limit: int = Field(gt=0)
    target_selected: int = Field(gt=0)

    @model_validator(mode="after")
    def _domain_path_and_counts(self) -> DomainFileConfig:
        require_utc(self.file_addition_created_at)
        parts = PurePosixPath(self.source_file).parts
        if len(parts) < 3 or parts[:2] != ("Mathlib", self.domain_proxy):
            raise ValueError("source_file top-level directory must equal domain_proxy")
        if self.domain_proxy == "Algebra":
            raise ValueError("cross-domain feasibility probe cannot include Algebra")
        if self.screening_limit < self.target_selected:
            raise ValueError("screening_limit cannot be below target_selected")
        return self


class ProbeSelectionConfig(StrictModel):
    selection_version: Literal["lf021_cross_domain_docstring_hash_v1"]
    minimum_domain_proxies: int = Field(ge=4)
    exact_normalized_nl_deduplication: Literal[True]
    require_post_checkpoint_exact_pair_introduction: Literal[True]
    require_complete_reference_representation: Literal[True]


class ProbeScreeningConfig(StrictModel):
    active_registry_manifest: str = Field(min_length=1)
    active_registry_manifest_sha256: str = Field(pattern=_HEX64)
    require_nl_screen: Literal[True]
    require_reference_lean_screen: Literal[True]
    require_reference_representation_screen: Literal[True]

    @model_validator(mode="after")
    def _safe_path(self) -> ProbeScreeningConfig:
        path = PurePosixPath(self.active_registry_manifest)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("active_registry_manifest must be repository-relative")
        return self


class ProbePolicyConfig(StrictModel):
    source_license: Literal["Apache-2.0"]
    nl_trust: Literal["trusted"]
    nl_trust_semantics: Literal["human_authored_provenance_only_not_self_containedness"]
    self_containedness_status: Literal["unreviewed"]
    domain_semantics: Literal["top_level_mathlib_directory_proxy_only"]
    candidate_source_records_only: Literal[True]
    problem_pool_admitted: Literal[False]
    model_collection_authorized: Literal[False]
    model_execution_performed: Literal[False]
    semantic_labels_created: Literal[False]
    private_source_content_used: Literal[False]
    external_provider_transmission_performed: Literal[False]
    gate_claimed: Literal[False]


class ProbeOutputsConfig(StrictModel):
    root: str = Field(min_length=1)
    report: str = Field(min_length=1)

    @model_validator(mode="after")
    def _safe_report(self) -> ProbeOutputsConfig:
        report = PurePosixPath(self.report)
        if report.is_absolute() or ".." in report.parts:
            raise ValueError("report must be repository-relative")
        return self


class CrossDomainProbeConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal["lf021_cross_domain_mathlib_docstring_feasibility_v1"]
    frozen_at: datetime.datetime
    source: ProbeSourceConfig
    domains: tuple[DomainFileConfig, ...] = Field(min_length=4)
    selection: ProbeSelectionConfig
    screening: ProbeScreeningConfig
    policy: ProbePolicyConfig
    outputs: ProbeOutputsConfig

    @model_validator(mode="after")
    def _consistent(self) -> CrossDomainProbeConfig:
        require_utc(self.frozen_at)
        domains = [item.domain_proxy for item in self.domains]
        paths = [item.source_file for item in self.domains]
        if len(domains) != len(set(domains)):
            raise ValueError("domain_proxy values must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("source_file values must be unique")
        if len(domains) < self.selection.minimum_domain_proxies:
            raise ValueError("configured domains are below minimum_domain_proxies")
        cutoff = self.source.latest_generator_checkpoint_created_at
        if any(item.file_addition_created_at <= cutoff for item in self.domains):
            raise ValueError("every configured file addition must postdate checkpoint cutoff")
        return self


class ProbeOutcomeCode(StrEnum):
    NO_ADJACENT_DOCSTRING = "no_adjacent_docstring"
    BENCHMARK_NL_HIT = "benchmark_nl_hit"
    DUPLICATE_NORMALIZED_NL = "duplicate_normalized_nl"
    OUTSIDE_BOUNDED_SCREEN = "outside_bounded_screen"
    REPRESENTATION_INCOMPLETE = "representation_incomplete"
    BENCHMARK_REFERENCE_HIT = "benchmark_reference_hit"
    TEMPORAL_INTRODUCTION_UNPROVEN = "temporal_introduction_unproven"
    CHECKPOINT_TEMPORAL_OVERLAP = "checkpoint_temporal_overlap"
    ELIGIBLE_NOT_SELECTED = "eligible_not_selected"
    SELECTED = "selected"


class ProbeOutcome(StrictModel):
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    domain_proxy: str = Field(min_length=1)
    declaration_full_name: str = Field(min_length=1)
    outcome: ProbeOutcomeCode
    detail: str = Field(min_length=1)
    normalized_nl_sha256: str | None = Field(default=None, pattern=_HEX64)
    candidate_id: str | None = Field(default=None, pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _selected_shape(self) -> ProbeOutcome:
        if self.outcome is ProbeOutcomeCode.SELECTED and self.candidate_id is None:
            raise ValueError("selected outcome requires candidate_id")
        if self.outcome is not ProbeOutcomeCode.SELECTED and self.candidate_id is not None:
            raise ValueError("only selected outcomes may carry candidate_id")
        return self


class SourceFileIntroduction(StrictModel):
    commit: str = Field(pattern=_HEX40)
    created_at: datetime.datetime
    added_at_configured_path: Literal[True]
    commit_is_pinned_revision_ancestor: Literal[True]
    strictly_postdates_latest_checkpoint: Literal[True]

    @model_validator(mode="after")
    def _utc(self) -> SourceFileIntroduction:
        require_utc(self.created_at)
        return self


class CrossDomainCandidate(StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^cross_domain_candidate:[0-9a-f]{64}$")
    selection_hash: str = Field(pattern=_HEX64)
    domain_proxy: str = Field(min_length=1)
    theorem: TheoremRecord
    representation: RepresentationRecord
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    source_file_introduction: SourceFileIntroduction
    exact_pair_introduction: TemporalIntroductionEvidence
    registry_screens: RegistryScreens
    selected: bool
    nl_source_link: str = Field(min_length=1)
    source_license: Literal["Apache-2.0"] = "Apache-2.0"
    nl_trust: Literal["trusted"] = "trusted"
    nl_trust_semantics: Literal["human_authored_provenance_only_not_self_containedness"] = (
        "human_authored_provenance_only_not_self_containedness"
    )
    self_containedness_status: Literal["unreviewed"] = "unreviewed"
    domain_semantics: Literal["top_level_mathlib_directory_proxy_only"] = (
        "top_level_mathlib_directory_proxy_only"
    )
    candidate_source_record_only: Literal[True] = True
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_content_used: Literal[False] = False
    external_provider_transmission_performed: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _identity_and_policy(self) -> CrossDomainCandidate:
        if self.theorem.theorem_id != self.representation.theorem_id:
            raise ValueError("theorem and representation IDs are inconsistent")
        if not self.registry_screens.all_three_screens_clear:
            raise ValueError("candidate requires clear benchmark screens")
        if not self.exact_pair_introduction.strictly_postdates_latest_checkpoint:
            raise ValueError("candidate exact pair must postdate generator cutoff")
        expected = "cross_domain_candidate:" + hash_canonical(
            {
                "schema": "lf021_cross_domain_docstring_candidate_v1",
                "selection_hash": self.selection_hash,
                "theorem_id": self.theorem.theorem_id,
                "representation_id": self.representation.representation_id,
                "raw_docstring_sha256": self.docstring.raw_sha256,
                "selected": self.selected,
            }
        )
        if self.candidate_id != expected:
            raise ValueError("candidate_id does not match immutable payload")
        return self


class DomainAccounting(StrictModel):
    source_file: str = Field(min_length=1)
    declarations_seen: int = Field(ge=0)
    proposition_references: int = Field(ge=0)
    adjacent_docstrings: int = Field(ge=0)
    normalized_nl_clear: int = Field(ge=0)
    bounded_reference_screens: int = Field(ge=0)
    representation_complete: int = Field(ge=0)
    registry_clear: int = Field(ge=0)
    temporally_clean: int = Field(ge=0)
    selected: int = Field(ge=0)
    target_selected: int = Field(gt=0)
    target_met: bool

    @model_validator(mode="after")
    def _counts(self) -> DomainAccounting:
        chain = (
            self.proposition_references,
            self.adjacent_docstrings,
            self.normalized_nl_clear,
        )
        if any(right > left for left, right in pairwise(chain)):
            raise ValueError("domain accounting decreases in the wrong direction")
        if self.bounded_reference_screens > self.normalized_nl_clear:
            raise ValueError("bounded screens exceed NL-clear records")
        if self.representation_complete > self.bounded_reference_screens:
            raise ValueError("complete representations exceed bounded screens")
        if self.registry_clear > self.representation_complete:
            raise ValueError("registry-clear count exceeds complete representations")
        if self.temporally_clean > self.registry_clear:
            raise ValueError("temporal-clean count exceeds registry-clear count")
        if self.selected > self.temporally_clean:
            raise ValueError("selected count exceeds temporally-clean count")
        if self.target_met != (self.selected >= self.target_selected):
            raise ValueError("target_met does not match selected count")
        return self


class ProbeExtractionFailure(StrictModel):
    domain_proxy: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    declaration_name: str | None
    code: str = Field(min_length=1)
    detail: str
    outcome_level: str


class ArtifactHash(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class CrossDomainProbeManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf021_cross_domain_mathlib_docstring_feasibility"]
    manifest_id: str = Field(pattern=r"^cross_domain_manifest:[0-9a-f]{64}$")
    frozen_at: datetime.datetime
    config_id: Literal["lf021_cross_domain_mathlib_docstring_feasibility_v1"]
    source_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    input_artifacts: dict[str, ArtifactHash]
    output_artifacts: dict[str, ArtifactHash]
    attempted_source_files: int = Field(ge=4)
    declarations_seen: int = Field(gt=0)
    proposition_references: int = Field(gt=0)
    extraction_failures: int = Field(ge=0)
    terminal_outcomes: int = Field(gt=0)
    terminal_outcome_counts: dict[str, int]
    domain_accounting: dict[str, DomainAccounting]
    eligible_candidates: int = Field(ge=0)
    selected_candidates: int = Field(ge=0)
    selected_domain_proxies: int = Field(ge=0)
    minimum_domain_proxies: int = Field(ge=4)
    passed: bool
    blockers: tuple[str, ...]
    domain_semantics: Literal["top_level_mathlib_directory_proxy_only"]
    candidate_source_records_only: Literal[True] = True
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_content_used: Literal[False] = False
    external_provider_transmission_performed: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _accounting(self) -> CrossDomainProbeManifest:
        require_utc(self.frozen_at)
        if self.terminal_outcomes != self.proposition_references:
            raise ValueError("every proposition reference must have one terminal outcome")
        if sum(self.terminal_outcome_counts.values()) != self.terminal_outcomes:
            raise ValueError("terminal outcome counts do not reconcile")
        if self.declarations_seen != self.proposition_references + self.extraction_failures:
            raise ValueError("declaration extraction accounting does not reconcile")
        if self.eligible_candidates != sum(
            item.temporally_clean for item in self.domain_accounting.values()
        ):
            raise ValueError("eligible candidate count does not match per-domain accounting")
        if self.selected_candidates != sum(
            item.selected for item in self.domain_accounting.values()
        ):
            raise ValueError("selected candidate count does not match per-domain accounting")
        met = sum(item.target_met for item in self.domain_accounting.values())
        if self.selected_domain_proxies != met:
            raise ValueError("selected domain proxy count does not match target_met")
        expected_pass = met >= self.minimum_domain_proxies and not self.blockers
        if self.passed != expected_pass:
            raise ValueError("passed does not match domain coverage and blockers")
        return self


class CrossDomainProbeReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_cross_domain_mathlib_docstring_feasibility"]
    passed: bool
    manifest: ArtifactHash
    eligible_candidates: ArtifactHash
    selected_candidates: ArtifactHash
    outcomes: ArtifactHash
    extraction_failures: ArtifactHash
    raw_response_index: ArtifactHash
    selected_domain_proxies: tuple[str, ...]
    selected_count: int = Field(ge=0)
    blockers: tuple[str, ...]
    caveats: tuple[str, ...] = Field(min_length=1)
    domain_semantics: Literal["top_level_mathlib_directory_proxy_only"]
    candidate_source_records_only: Literal[True] = True
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_claimed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class CrossDomainProbeRun:
    report_path: Path
    manifest_path: Path
    report: CrossDomainProbeReport
    manifest: CrossDomainProbeManifest


@dataclass(frozen=True, slots=True)
class _DocDraft:
    domain: DomainFileConfig
    theorem: TheoremRecord
    extraction_representation: RepresentationRecord
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    selection_hash: str


def _artifact(path: Path) -> ArtifactHash:
    return ArtifactHash(path=str(path), sha256=hash_file(path))


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(values: tuple[object, ...]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CrossDomainProbeError(
                f"immutable artifact already exists with other bytes: {path}"
            )
        return
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _source_path(checkout: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise CrossDomainProbeError(f"unsafe source path: {relative}")
    path = (checkout / Path(*pure.parts)).resolve(strict=True)
    try:
        path.relative_to(checkout.resolve(strict=True))
    except ValueError as exc:
        raise CrossDomainProbeError(f"source path escapes checkout: {relative}") from exc
    if not path.is_file() or path.is_symlink():
        raise CrossDomainProbeError(f"source path is not a regular file: {relative}")
    return path


def _validate_domain_source(
    *,
    checkout: Path,
    revision: str,
    cutoff: datetime.datetime,
    domain: DomainFileConfig,
    git_blobs: dict[str, str],
) -> SourceFileIntroduction:
    path = _source_path(checkout, domain.source_file)
    if hash_file(path) != domain.source_file_sha256:
        raise CrossDomainProbeError(f"{domain.source_file}: source SHA-256 drift")
    if git_blobs.get(domain.source_file) != domain.git_blob_sha1:
        raise CrossDomainProbeError(f"{domain.source_file}: pinned Git blob drift")
    ancestor_code, _ = _git_optional(
        checkout, "merge-base", "--is-ancestor", domain.file_addition_commit, revision
    )
    if ancestor_code != 0:
        raise CrossDomainProbeError(
            f"{domain.source_file}: configured addition is outside pinned ancestry"
        )
    changed = set(
        _git(
            checkout,
            "diff-tree",
            "--root",
            "--diff-filter=A",
            "--no-commit-id",
            "--name-only",
            "-r",
            domain.file_addition_commit,
        ).splitlines()
    )
    if domain.source_file not in changed:
        raise CrossDomainProbeError(f"{domain.source_file}: configured commit did not add path")
    observed_timestamp = datetime.datetime.fromisoformat(
        _git(checkout, "show", "-s", "--format=%cI", domain.file_addition_commit).strip()
    )
    require_utc(observed_timestamp)
    if observed_timestamp != domain.file_addition_created_at:
        raise CrossDomainProbeError(f"{domain.source_file}: configured addition timestamp drift")
    if observed_timestamp <= cutoff:
        raise CrossDomainProbeError(
            f"{domain.source_file}: addition does not postdate checkpoint cutoff"
        )
    return SourceFileIntroduction(
        commit=domain.file_addition_commit,
        created_at=observed_timestamp,
        added_at_configured_path=True,
        commit_is_pinned_revision_ancestor=True,
        strictly_postdates_latest_checkpoint=True,
    )


def _exact_pair_introduction(
    *,
    checkout: Path,
    revision: str,
    cutoff: datetime.datetime,
    theorem: TheoremRecord,
    docstring: AdjacentDocstring,
    revision_order: dict[str, int],
) -> TemporalIntroductionEvidence | None:
    if theorem.source_file is None or theorem.declaration_name is None:
        raise CrossDomainProbeError(f"{theorem.theorem_id}: source/name missing")
    raw_log = _git(
        checkout,
        "log",
        "--follow",
        "--format=%H%x09%cI",
        "-S",
        docstring.raw,
        revision,
        "--",
        theorem.source_file,
    )
    escaped_name = re.escape(theorem.declaration_name)
    declaration_regex = rf"(theorem|lemma)[[:space:]]+{escaped_name}([^[:alnum:]_']|$)"
    declaration_log = _git(
        checkout,
        "log",
        "--follow",
        "--format=%H%x09%cI",
        "-G",
        declaration_regex,
        revision,
        "--",
        theorem.source_file,
    )
    candidate_lines = set(raw_log.splitlines()) | set(declaration_log.splitlines())
    parsed: list[tuple[int, str, str]] = []
    for line in candidate_lines:
        commit, separator, created_at_raw = line.partition("\t")
        if not separator or re.fullmatch(_HEX40[1:-1], commit) is None:
            raise CrossDomainProbeError(f"{theorem.theorem_id}: malformed Git history record")
        rank = revision_order.get(commit)
        if rank is None:
            raise CrossDomainProbeError(
                f"{theorem.theorem_id}: history record is outside pinned ancestry"
            )
        parsed.append((rank, commit, created_at_raw))
    for _, commit, created_at_raw in sorted(parsed):
        pair = _blob_for_introduction_candidate(
            repo=checkout,
            commit=commit,
            current_source_path=theorem.source_file,
            raw_docstring=docstring.raw,
            declaration_name=theorem.declaration_name,
        )
        if pair is None:
            continue
        created_at = datetime.datetime.fromisoformat(created_at_raw)
        require_utc(created_at)
        return TemporalIntroductionEvidence(
            history_method="git_log_follow_pickaxe_exact_pair_v3",
            search_revision=revision,
            exact_docstring_sha256=docstring.raw_sha256,
            introduction_commit=commit,
            introduction_created_at=created_at,
            introduction_source_path=pair[0],
            latest_checkpoint_created_at=cutoff,
            strictly_postdates_latest_checkpoint=created_at > cutoff,
        )
    return None


def _selection_hash(
    *,
    config: CrossDomainProbeConfig,
    domain: str,
    theorem: TheoremRecord,
    docstring: AdjacentDocstring,
) -> str:
    return hash_canonical(
        {
            "selection_version": config.selection.selection_version,
            "domain_proxy": domain,
            "theorem_id": theorem.theorem_id,
            "ancestry_id": theorem.ancestry_id,
            "raw_docstring_sha256": docstring.raw_sha256,
        }
    )


def _source_provenance(
    *,
    config: CrossDomainProbeConfig,
    domain: DomainFileConfig,
    theorem: TheoremRecord,
    theorem_header: str,
) -> SourceProvenance:
    if theorem.source_range is None:
        raise CrossDomainProbeError(f"{theorem.theorem_id}: source range missing")
    return SourceProvenance(
        repository=config.source.repository,
        revision=config.source.checkout_revision,
        source_file=domain.source_file,
        source_range=theorem.source_range,
        git_blob_sha1=domain.git_blob_sha1,
        source_file_sha256=domain.source_file_sha256,
        theorem_header_sha256=sha256_hex(theorem_header.encode("utf-8")),
    )


def _candidate(
    *,
    config: CrossDomainProbeConfig,
    draft: _DocDraft,
    representation: RepresentationRecord,
    source_introduction: SourceFileIntroduction,
    temporal: TemporalIntroductionEvidence,
    screens: RegistryScreens,
    selected: bool,
) -> CrossDomainCandidate:
    theorem = draft.theorem
    candidate_id = "cross_domain_candidate:" + hash_canonical(
        {
            "schema": "lf021_cross_domain_docstring_candidate_v1",
            "selection_hash": draft.selection_hash,
            "theorem_id": theorem.theorem_id,
            "representation_id": representation.representation_id,
            "raw_docstring_sha256": draft.docstring.raw_sha256,
            "selected": selected,
        }
    )
    assert theorem.source_file is not None
    start = draft.docstring.start_line
    finish = draft.docstring.finish_line
    fragment = f"#L{start}" if start == finish else f"#L{start}-L{finish}"
    return CrossDomainCandidate(
        candidate_id=candidate_id,
        selection_hash=draft.selection_hash,
        domain_proxy=draft.domain.domain_proxy,
        theorem=theorem,
        representation=representation,
        docstring=draft.docstring,
        source_provenance=draft.source_provenance,
        source_file_introduction=source_introduction,
        exact_pair_introduction=temporal,
        registry_screens=screens,
        selected=selected,
        nl_source_link=(
            f"{config.source.repository}/blob/{config.source.checkout_revision}/"
            f"{theorem.source_file}{fragment}"
        ),
    )


def _raw_response_index(raw_dir: Path) -> dict[str, object]:
    files = []
    for path in sorted(raw_dir.glob("*.json")):
        files.append(
            {
                "path": path.name,
                "sha256": hash_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return {
        "schema_version": 1,
        "artifact_kind": "lf021_cross_domain_probe_lean_raw_response_index",
        "files": files,
        "file_count": len(files),
    }


def _artifact_path(paths: RepoPaths, binding: ArtifactBinding) -> Path:
    path = Path(binding.path)
    return path if path.is_absolute() else paths.root / path


def run_cross_domain_probe(
    *,
    paths: RepoPaths,
    mathlib_checkout: Path,
    output_root: Path | None = None,
) -> CrossDomainProbeRun:
    """Execute and persist the immutable bounded feasibility probe."""

    loaded = load_config(paths.root / CONFIG_PATH, CrossDomainProbeConfig)
    config = loaded.config
    destination = output_root or Path(config.outputs.root)
    if destination.exists():
        raise CrossDomainProbeError(
            f"immutable output root already exists; use verify-only: {destination}"
        )
    report_path = paths.root / config.outputs.report
    if report_path.exists():
        raise CrossDomainProbeError(
            f"immutable report already exists; use verify-only: {report_path}"
        )
    verify_checkout_revision(mathlib_checkout, config.source.checkout_revision)
    registry_manifest = paths.root / config.screening.active_registry_manifest
    if hash_file(registry_manifest) != config.screening.active_registry_manifest_sha256:
        raise CrossDomainProbeError("active benchmark manifest hash drift")
    import_header_path = _artifact_path(paths, config.source.import_header)
    if hash_file(import_header_path) != config.source.import_header.sha256:
        raise CrossDomainProbeError("import header hash drift")
    import_header = import_header_path.read_text(encoding="utf-8")
    if import_header != "import Mathlib\n":
        raise CrossDomainProbeError("cross-domain probe requires exact `import Mathlib` header")

    git_blobs = _git_tree_blobs(mathlib_checkout, config.source.checkout_revision)
    source_introductions = {
        domain.domain_proxy: _validate_domain_source(
            checkout=mathlib_checkout,
            revision=config.source.checkout_revision,
            cutoff=config.source.latest_generator_checkpoint_created_at,
            domain=domain,
            git_blobs=git_blobs,
        )
        for domain in config.domains
    }

    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        raise CrossDomainProbeError(f"stale staging root exists: {staging}")
    raw_dir = staging / "raw_responses"
    raw_dir.mkdir(parents=True)
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=mathlib_checkout,
            context_fingerprint=config.source.context_id.removeprefix("ctx:"),
            environment_schema_version=config.source.environment_schema_version,
            raw_response_dir=raw_dir,
        )
    )
    extraction_failures: list[ProbeExtractionFailure] = []
    declarations_seen_by_domain: Counter[str] = Counter()
    extracted_by_domain: Counter[str] = Counter()
    extracted: list[tuple[DomainFileConfig, TheoremRecord, RepresentationRecord]] = []
    try:
        for ordinal, domain in enumerate(config.domains):
            result = run_with_retries(
                backend.run,
                LeanRequest(
                    request_id=f"lf021-cross-domain-file-{ordinal}-{domain.domain_proxy}",
                    context_id=config.source.context_id,
                    file_path=Path(domain.source_file),
                    declarations=True,
                    timeout_seconds=600.0,
                ),
            ).result
            if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
                raise CrossDomainProbeError(
                    f"{domain.source_file}: FileCommand ended as {result.status.value}"
                )
            source_text = _source_path(mathlib_checkout, domain.source_file).read_text(
                encoding="utf-8"
            )
            declarations_seen_by_domain[domain.domain_proxy] += len(result.declarations)
            extraction = extract_from_declarations(
                SourceIdentity(
                    source="mathlib",
                    source_revision=config.source.checkout_revision,
                    source_record=domain.source_file,
                    context_id=config.source.context_id,
                    source_file=domain.source_file,
                ),
                source_text,
                list(result.declarations),
                created_at=config.frozen_at,
                elaboration_status=ValidationStatus.ELABORATES,
                lean_result_id=result.request_hash,
            )
            extracted_by_domain[domain.domain_proxy] += len(extraction.accepted)
            extracted.extend(
                (domain, item.theorem, item.representation) for item in extraction.accepted
            )
            extraction_failures.extend(
                ProbeExtractionFailure(
                    domain_proxy=domain.domain_proxy,
                    source_file=domain.source_file,
                    declaration_name=failure.declaration_name,
                    code=failure.code.value,
                    detail=failure.detail,
                    outcome_level=failure.outcome_level,
                )
                for failure in extraction.failures
            )

        active = load_active_benchmark_registry(
            manifest_path=registry_manifest,
            repo_root=paths.root,
            expected_manifest_sha256=config.screening.active_registry_manifest_sha256,
        )
        outcomes: dict[str, ProbeOutcome] = {}
        drafts_by_domain: dict[str, list[_DocDraft]] = defaultdict(list)
        for domain, theorem, extraction_representation in extracted:
            source_text = _source_path(mathlib_checkout, domain.source_file).read_text(
                encoding="utf-8"
            )
            docstring, theorem_header = extract_adjacent_docstring(
                theorem=theorem, source_text=source_text
            )
            if docstring is None or not docstring.normalized_nl:
                outcomes[theorem.theorem_id] = ProbeOutcome(
                    theorem_id=theorem.theorem_id,
                    domain_proxy=domain.domain_proxy,
                    declaration_full_name=theorem.declaration_full_name
                    or theorem.declaration_name
                    or "",
                    outcome=ProbeOutcomeCode.NO_ADJACENT_DOCSTRING,
                    detail="no nonempty adjacent contributor theorem/lemma docstring",
                )
                continue
            if active.index.contains_nl(docstring.normalized_nl):
                outcomes[theorem.theorem_id] = ProbeOutcome(
                    theorem_id=theorem.theorem_id,
                    domain_proxy=domain.domain_proxy,
                    declaration_full_name=theorem.declaration_full_name
                    or theorem.declaration_name
                    or "",
                    outcome=ProbeOutcomeCode.BENCHMARK_NL_HIT,
                    detail="normalized contributor docstring appears in active benchmark registry",
                    normalized_nl_sha256=docstring.normalized_nl_sha256,
                )
                continue
            drafts_by_domain[domain.domain_proxy].append(
                _DocDraft(
                    domain=domain,
                    theorem=theorem,
                    extraction_representation=extraction_representation,
                    docstring=docstring,
                    source_provenance=_source_provenance(
                        config=config,
                        domain=domain,
                        theorem=theorem,
                        theorem_header=theorem_header,
                    ),
                    selection_hash=_selection_hash(
                        config=config,
                        domain=domain.domain_proxy,
                        theorem=theorem,
                        docstring=docstring,
                    ),
                )
            )

        seen_nl: set[str] = set()
        bounded: list[_DocDraft] = []
        normalized_nl_clear_by_domain: Counter[str] = Counter()
        for draft in sorted(
            (item for values in drafts_by_domain.values() for item in values),
            key=lambda item: (item.selection_hash, item.theorem.theorem_id),
        ):
            nl_digest = draft.docstring.normalized_nl_sha256
            if nl_digest in seen_nl:
                outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                    theorem_id=draft.theorem.theorem_id,
                    domain_proxy=draft.domain.domain_proxy,
                    declaration_full_name=draft.theorem.declaration_full_name or "",
                    outcome=ProbeOutcomeCode.DUPLICATE_NORMALIZED_NL,
                    detail="hash-minimal theorem already represents identical normalized NL",
                    normalized_nl_sha256=nl_digest,
                )
                continue
            seen_nl.add(nl_digest)
            normalized_nl_clear_by_domain[draft.domain.domain_proxy] += 1

        for domain in config.domains:
            unique = sorted(
                (
                    item
                    for item in drafts_by_domain[domain.domain_proxy]
                    if outcomes.get(item.theorem.theorem_id) is None
                ),
                key=lambda item: (item.selection_hash, item.theorem.theorem_id),
            )
            bounded.extend(unique[: domain.screening_limit])
            for item in unique[domain.screening_limit :]:
                outcomes[item.theorem.theorem_id] = ProbeOutcome(
                    theorem_id=item.theorem.theorem_id,
                    domain_proxy=domain.domain_proxy,
                    declaration_full_name=item.theorem.declaration_full_name or "",
                    outcome=ProbeOutcomeCode.OUTSIDE_BOUNDED_SCREEN,
                    detail=(
                        f"outside deterministic hash-minimal screening limit "
                        f"{domain.screening_limit}"
                    ),
                    normalized_nl_sha256=item.docstring.normalized_nl_sha256,
                )

        representation_result = build_representation_batch(
            backend,
            RepresentationBatch(
                context_id=config.source.context_id,
                import_header=import_header,
                ordered_theorem_inputs=tuple(
                    TheoremForRepresentation(
                        theorem_id=item.theorem.theorem_id,
                        full_name=item.theorem.declaration_full_name or "",
                        proof_stripped=item.theorem.proof_stripped_declaration,
                        context_id=item.theorem.context_id,
                        source_signature=item.extraction_representation.headless,
                        environment_lookup_name=declaration_environment_lookup_name(
                            item.theorem.declaration_full_name or "",
                            item.theorem.source_file,
                        ),
                    )
                    for item in bounded
                ),
            ),
            created_at=config.frozen_at,
        )
    finally:
        backend.close()

    representations = {
        item.theorem_id: item for item in representation_result.ordered_representation_records
    }
    if len(representations) != len(bounded):
        raise CrossDomainProbeError(
            "representation builder did not return exactly one record per bounded reference"
        )
    revision_order = _revision_order(mathlib_checkout, config.source.checkout_revision)
    clean_by_domain: dict[
        str,
        list[tuple[_DocDraft, RepresentationRecord, RegistryScreens, TemporalIntroductionEvidence]],
    ] = defaultdict(list)
    representation_complete_by_domain: Counter[str] = Counter()
    registry_clear_by_domain: Counter[str] = Counter()
    for draft in bounded:
        representation = representations[draft.theorem.theorem_id]
        missing = [
            view
            for view in _REQUIRED_REFERENCE_VIEWS
            if representation.view_status.get(view) is not ViewStatus.OK
        ]
        if representation.alpha_identity_fingerprint is None:
            missing.append("alpha_identity_fingerprint")
        if missing:
            outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                theorem_id=draft.theorem.theorem_id,
                domain_proxy=draft.domain.domain_proxy,
                declaration_full_name=draft.theorem.declaration_full_name or "",
                outcome=ProbeOutcomeCode.REPRESENTATION_INCOMPLETE,
                detail="missing reference views: " + ", ".join(sorted(missing)),
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        representation_complete_by_domain[draft.domain.domain_proxy] += 1
        screens = _registry_screens(
            active=active,
            theorem=draft.theorem,
            representation=representation,
            docstring=draft.docstring,
        )
        if not screens.all_three_screens_clear:
            outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                theorem_id=draft.theorem.theorem_id,
                domain_proxy=draft.domain.domain_proxy,
                declaration_full_name=draft.theorem.declaration_full_name or "",
                outcome=ProbeOutcomeCode.BENCHMARK_REFERENCE_HIT,
                detail="formal reference or representation appears in active benchmark registry",
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        registry_clear_by_domain[draft.domain.domain_proxy] += 1
        temporal = _exact_pair_introduction(
            checkout=mathlib_checkout,
            revision=config.source.checkout_revision,
            cutoff=config.source.latest_generator_checkpoint_created_at,
            theorem=draft.theorem,
            docstring=draft.docstring,
            revision_order=revision_order,
        )
        if temporal is None:
            outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                theorem_id=draft.theorem.theorem_id,
                domain_proxy=draft.domain.domain_proxy,
                declaration_full_name=draft.theorem.declaration_full_name or "",
                outcome=ProbeOutcomeCode.TEMPORAL_INTRODUCTION_UNPROVEN,
                detail="exact theorem/docstring introduction could not be proven",
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        if not temporal.strictly_postdates_latest_checkpoint:
            outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                theorem_id=draft.theorem.theorem_id,
                domain_proxy=draft.domain.domain_proxy,
                declaration_full_name=draft.theorem.declaration_full_name or "",
                outcome=ProbeOutcomeCode.CHECKPOINT_TEMPORAL_OVERLAP,
                detail="exact theorem/docstring pair does not postdate latest generator checkpoint",
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        clean_by_domain[draft.domain.domain_proxy].append(
            (draft, representation, screens, temporal)
        )

    eligible: list[CrossDomainCandidate] = []
    selected: list[CrossDomainCandidate] = []
    for domain in config.domains:
        clean = sorted(
            clean_by_domain[domain.domain_proxy],
            key=lambda item: (item[0].selection_hash, item[0].theorem.theorem_id),
        )
        selected_ids = {item[0].theorem.theorem_id for item in clean[: domain.target_selected]}
        for draft, representation, screens, temporal in clean:
            is_selected = draft.theorem.theorem_id in selected_ids
            candidate = _candidate(
                config=config,
                draft=draft,
                representation=representation,
                source_introduction=source_introductions[domain.domain_proxy],
                temporal=temporal,
                screens=screens,
                selected=is_selected,
            )
            eligible.append(candidate)
            if is_selected:
                selected.append(candidate)
                outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                    theorem_id=draft.theorem.theorem_id,
                    domain_proxy=domain.domain_proxy,
                    declaration_full_name=draft.theorem.declaration_full_name or "",
                    outcome=ProbeOutcomeCode.SELECTED,
                    detail="hash-minimal registry-clear post-cutoff reference selected",
                    normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
                    candidate_id=candidate.candidate_id,
                )
            else:
                outcomes[draft.theorem.theorem_id] = ProbeOutcome(
                    theorem_id=draft.theorem.theorem_id,
                    domain_proxy=domain.domain_proxy,
                    declaration_full_name=draft.theorem.declaration_full_name or "",
                    outcome=ProbeOutcomeCode.ELIGIBLE_NOT_SELECTED,
                    detail="eligible within bounded screen but beyond per-domain target",
                    normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
                )

    if len(outcomes) != len(extracted):
        missing = sorted(
            theorem.theorem_id for _, theorem, _ in extracted if theorem.theorem_id not in outcomes
        )
        raise CrossDomainProbeError(
            f"terminal outcome accounting missing {len(missing)} references: {missing[:3]}"
        )

    eligible_tuple = tuple(
        sorted(eligible, key=lambda item: (item.domain_proxy, item.selection_hash))
    )
    selected_tuple = tuple(
        sorted(selected, key=lambda item: (item.domain_proxy, item.selection_hash))
    )
    outcomes_tuple = tuple(outcomes[key] for key in sorted(outcomes))
    failures_tuple = tuple(
        sorted(
            extraction_failures,
            key=lambda item: (
                item.domain_proxy,
                item.source_file,
                item.declaration_name or "",
                item.code,
            ),
        )
    )
    eligible_path = staging / "eligible_candidates.jsonl"
    selected_path = staging / "selected_candidates.jsonl"
    outcomes_path = staging / "terminal_outcomes.jsonl"
    failures_path = staging / "extraction_failures.jsonl"
    raw_index_path = staging / "raw_response_index.json"
    _write_exact(eligible_path, _jsonl_bytes(eligible_tuple))
    _write_exact(selected_path, _jsonl_bytes(selected_tuple))
    _write_exact(outcomes_path, _jsonl_bytes(outcomes_tuple))
    _write_exact(failures_path, _jsonl_bytes(failures_tuple))
    _write_exact(raw_index_path, _json_bytes(_raw_response_index(raw_dir)))

    adjacent_by_domain: Counter[str] = Counter()
    for drafts in drafts_by_domain.values():
        for draft in drafts:
            adjacent_by_domain[draft.domain.domain_proxy] += 1
    bounded_by_domain = Counter(item.domain.domain_proxy for item in bounded)
    temporal_by_domain = Counter(item.domain_proxy for item in eligible_tuple)
    selected_by_domain = Counter(item.domain_proxy for item in selected_tuple)
    domain_accounting = {
        domain.domain_proxy: DomainAccounting(
            source_file=domain.source_file,
            declarations_seen=declarations_seen_by_domain[domain.domain_proxy],
            proposition_references=extracted_by_domain[domain.domain_proxy],
            adjacent_docstrings=adjacent_by_domain[domain.domain_proxy],
            normalized_nl_clear=normalized_nl_clear_by_domain[domain.domain_proxy],
            bounded_reference_screens=bounded_by_domain[domain.domain_proxy],
            representation_complete=representation_complete_by_domain[domain.domain_proxy],
            registry_clear=registry_clear_by_domain[domain.domain_proxy],
            temporally_clean=temporal_by_domain[domain.domain_proxy],
            selected=selected_by_domain[domain.domain_proxy],
            target_selected=domain.target_selected,
            target_met=selected_by_domain[domain.domain_proxy] >= domain.target_selected,
        )
        for domain in config.domains
    }
    met_domains = tuple(sorted(name for name, item in domain_accounting.items() if item.target_met))
    blockers = tuple(
        sorted(
            f"{name}: selected {item.selected} below target {item.target_selected}"
            for name, item in domain_accounting.items()
            if not item.target_met
        )
    )
    if len(met_domains) < config.selection.minimum_domain_proxies:
        blockers += (
            f"only {len(met_domains)} domain proxies meet target; "
            f"minimum is {config.selection.minimum_domain_proxies}",
        )

    config_artifact = _artifact(paths.root / CONFIG_PATH)
    source_artifacts = {
        f"source_file:{domain.domain_proxy}": ArtifactHash(
            path=str(mathlib_checkout / domain.source_file),
            sha256=domain.source_file_sha256,
        )
        for domain in config.domains
    }
    input_artifacts = {
        "config": config_artifact,
        "active_registry_manifest": _artifact(registry_manifest),
        "import_header": _artifact(import_header_path),
        **source_artifacts,
    }
    final_paths = {
        "eligible_candidates": destination / eligible_path.relative_to(staging),
        "selected_candidates": destination / selected_path.relative_to(staging),
        "terminal_outcomes": destination / outcomes_path.relative_to(staging),
        "extraction_failures": destination / failures_path.relative_to(staging),
        "raw_response_index": destination / raw_index_path.relative_to(staging),
    }
    output_artifacts = {
        name: ArtifactHash(path=str(final_paths[name]), sha256=hash_file(path))
        for name, path in {
            "eligible_candidates": eligible_path,
            "selected_candidates": selected_path,
            "terminal_outcomes": outcomes_path,
            "extraction_failures": failures_path,
            "raw_response_index": raw_index_path,
        }.items()
    }
    outcome_counts = Counter(item.outcome.value for item in outcomes_tuple)
    manifest_payload = {
        "artifact_kind": "lf021_cross_domain_mathlib_docstring_feasibility",
        "frozen_at": config.frozen_at,
        "config_id": config.config_id,
        "source_revision": config.source.checkout_revision,
        "context_id": config.source.context_id,
        "input_artifacts": input_artifacts,
        "output_artifacts": output_artifacts,
        "attempted_source_files": len(config.domains),
        "declarations_seen": sum(declarations_seen_by_domain.values()),
        "proposition_references": len(extracted),
        "extraction_failures": len(extraction_failures),
        "terminal_outcomes": len(outcomes_tuple),
        "terminal_outcome_counts": dict(sorted(outcome_counts.items())),
        "domain_accounting": domain_accounting,
        "eligible_candidates": len(eligible_tuple),
        "selected_candidates": len(selected_tuple),
        "selected_domain_proxies": len(met_domains),
        "minimum_domain_proxies": config.selection.minimum_domain_proxies,
        "passed": not blockers,
        "blockers": blockers,
        "domain_semantics": config.policy.domain_semantics,
    }
    manifest = CrossDomainProbeManifest.model_validate(
        {
            "manifest_id": "cross_domain_manifest:"
            + hash_canonical(
                {
                    "schema": "lf021_cross_domain_probe_manifest_v1",
                    "config_sha256": config_artifact.sha256,
                    "source_revision": config.source.checkout_revision,
                    "context_id": config.source.context_id,
                    "eligible_candidates_sha256": output_artifacts["eligible_candidates"].sha256,
                    "selected_candidates_sha256": output_artifacts["selected_candidates"].sha256,
                    "terminal_outcomes_sha256": output_artifacts["terminal_outcomes"].sha256,
                    "extraction_failures_sha256": output_artifacts["extraction_failures"].sha256,
                    "raw_response_index_sha256": output_artifacts["raw_response_index"].sha256,
                    "eligible_candidates": len(eligible_tuple),
                    "selected_candidates": len(selected_tuple),
                    "selected_domain_proxies": met_domains,
                }
            ),
            **manifest_payload,
        }
    )
    manifest_path_staging = staging / "manifest.json"
    _write_exact(manifest_path_staging, _json_bytes(manifest))
    manifest_path = destination / "manifest.json"
    manifest_artifact = ArtifactHash(
        path=str(manifest_path), sha256=hash_file(manifest_path_staging)
    )
    report = CrossDomainProbeReport(
        report_kind="lf021_cross_domain_mathlib_docstring_feasibility",
        passed=manifest.passed,
        manifest=manifest_artifact,
        eligible_candidates=output_artifacts["eligible_candidates"],
        selected_candidates=output_artifacts["selected_candidates"],
        outcomes=output_artifacts["terminal_outcomes"],
        extraction_failures=output_artifacts["extraction_failures"],
        raw_response_index=output_artifacts["raw_response_index"],
        selected_domain_proxies=met_domains,
        selected_count=len(selected_tuple),
        blockers=blockers,
        caveats=(
            "Top-level Mathlib directories are domain proxies, not adjudicated semantic labels.",
            "Contributor-authored provenance does not establish "
            "self-containedness or semantic gold.",
            "This feasibility artifact does not authorize model collection or change any Gate.",
        ),
        domain_semantics=config.policy.domain_semantics,
    )
    report_bytes = _json_bytes(report)
    try:
        os.replace(staging, destination)
        _write_exact(report_path, report_bytes)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return CrossDomainProbeRun(
        report_path=report_path,
        manifest_path=manifest_path,
        report=report,
        manifest=manifest,
    )


def verify_cross_domain_probe(
    *,
    paths: RepoPaths,
    output_root: Path | None = None,
) -> CrossDomainProbeRun:
    """Validate every persisted probe binding without Lean or model execution."""

    config = load_config(paths.root / CONFIG_PATH, CrossDomainProbeConfig).config
    destination = output_root or Path(config.outputs.root)
    report_path = paths.root / config.outputs.report
    report = CrossDomainProbeReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    manifest_path = Path(report.manifest.path)
    if manifest_path != destination / "manifest.json":
        raise CrossDomainProbeError("report manifest path does not match output root")
    if hash_file(manifest_path) != report.manifest.sha256:
        raise CrossDomainProbeError("manifest hash mismatch")
    manifest = CrossDomainProbeManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    for name, artifact in manifest.input_artifacts.items():
        path = Path(artifact.path)
        if not path.is_absolute():
            path = paths.root / path
        if hash_file(path) != artifact.sha256:
            raise CrossDomainProbeError(f"input artifact hash mismatch: {name}")
    for name, artifact in manifest.output_artifacts.items():
        if hash_file(Path(artifact.path)) != artifact.sha256:
            raise CrossDomainProbeError(f"output artifact hash mismatch: {name}")
    eligible = tuple(
        CrossDomainCandidate.model_validate_json(line)
        for line in Path(report.eligible_candidates.path).read_text(encoding="utf-8").splitlines()
        if line
    )
    selected = tuple(
        CrossDomainCandidate.model_validate_json(line)
        for line in Path(report.selected_candidates.path).read_text(encoding="utf-8").splitlines()
        if line
    )
    outcomes = tuple(
        ProbeOutcome.model_validate_json(line)
        for line in Path(report.outcomes.path).read_text(encoding="utf-8").splitlines()
        if line
    )
    if len(eligible) != manifest.eligible_candidates:
        raise CrossDomainProbeError("eligible candidate count drift")
    if len(selected) != manifest.selected_candidates:
        raise CrossDomainProbeError("selected candidate count drift")
    if len(outcomes) != manifest.terminal_outcomes:
        raise CrossDomainProbeError("terminal outcome count drift")
    if any(not candidate.selected for candidate in selected):
        raise CrossDomainProbeError("selected partition contains unselected record")
    selected_ids = {candidate.candidate_id for candidate in selected}
    if selected_ids != {
        outcome.candidate_id for outcome in outcomes if outcome.outcome is ProbeOutcomeCode.SELECTED
    }:
        raise CrossDomainProbeError("selected candidate/outcome IDs disagree")
    return CrossDomainProbeRun(
        report_path=report_path,
        manifest_path=manifest_path,
        report=report,
        manifest=manifest,
    )

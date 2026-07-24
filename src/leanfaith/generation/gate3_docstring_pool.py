"""Model-free expansion of LF-021 candidates from frozen Gate-3 mathlib records.

The expansion is deliberately separate from ``public_research_pool``.  It
does not rewrite theorem signatures, invoke a model, create semantic labels,
or make a gate claim.  Every accepted NL statement is the exact leading
contributor docstring attached to an already-frozen mathlib ``TheoremRecord``.

Integrity failures (source/context/declaration/representation drift) abort the
whole run.  Ordinary absence of a docstring and active-benchmark hits are
persisted as explicit terminal exclusions.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import stat
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
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
from leanfaith.datasets.denylist import (
    ActiveBenchmarkRegistry,
    load_active_benchmark_registry,
    nl_hash,
)
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.lean.extraction import PLACEHOLDER
from leanfaith.schemas.enums import ViewStatus
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

CONFIG_PATH = Path("configs/generation/problem_pool_gate3_mathlib_docstrings_v1.yaml")
ONE_EXAMPLE_REPORT = Path("reports/generation/lf021_gate3_mathlib_docstrings_one_example_v1.json")
FULL_REPORT = Path("reports/generation/lf021_gate3_mathlib_docstrings_v1.json")
DEFAULT_OUTPUT_ROOT = Path(
    "/storage/milikic/leanfaith/lf021/problem_pool_gate3_mathlib_docstrings_v1"
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_DECLARATION_TOKEN = re.compile(r"\b(?:theorem|lemma)\s+")
_WS = re.compile(r"\s+")
_MODEL_VISIBLE_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
)


class Gate3DocstringPoolError(RuntimeError):
    """The frozen candidate expansion cannot be completed safely."""


class FrozenArtifact(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class Gate3DocstringSourceConfig(StrictModel):
    repository: Literal["https://github.com/leanprover-community/mathlib4"]
    checkout_revision: str = Field(pattern=_HEX40)
    source_name: Literal["mathlib"]
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    expected_mathlib_records: int = Field(gt=0)
    theorem_manifest: FrozenArtifact
    theorem_records: FrozenArtifact
    representation_records: FrozenArtifact
    representation_normalization_version: str = Field(min_length=1)


class Gate3DocstringSelectionConfig(StrictModel):
    selection_version: str = Field(min_length=1)
    target_distinct_ancestry_groups: int = Field(gt=0)
    exact_normalized_nl_deduplication: Literal[True]
    selection_key_fields: tuple[
        Literal["selection_version"],
        Literal["theorem_id"],
        Literal["ancestry_id"],
        Literal["raw_docstring_sha256"],
    ]

    @model_validator(mode="after")
    def _canonical_key(self) -> Gate3DocstringSelectionConfig:
        expected = (
            "selection_version",
            "theorem_id",
            "ancestry_id",
            "raw_docstring_sha256",
        )
        if self.selection_key_fields != expected:
            raise ValueError("selection_key_fields must match the v1 canonical key")
        return self


class GeneratorCheckpointPin(StrictModel):
    family_id: str = Field(min_length=1)
    repo_id: str = Field(min_length=1)
    revision: str = Field(pattern=_HEX40)
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _utc(self) -> GeneratorCheckpointPin:
        require_utc(self.created_at)
        return self


class TemporalNonOverlapConfig(StrictModel):
    history_method: Literal["git_log_follow_pickaxe_exact_pair_v3"]
    latest_checkpoint_created_at: datetime.datetime
    require_strictly_after_latest_checkpoint: Literal[True]
    checkpoint_pins: tuple[GeneratorCheckpointPin, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _latest_is_bound(self) -> TemporalNonOverlapConfig:
        require_utc(self.latest_checkpoint_created_at)
        if len({pin.family_id for pin in self.checkpoint_pins}) != len(self.checkpoint_pins):
            raise ValueError("checkpoint family IDs must be unique")
        observed_latest = max(pin.created_at for pin in self.checkpoint_pins)
        if observed_latest != self.latest_checkpoint_created_at:
            raise ValueError("latest checkpoint cutoff must equal max checkpoint created_at")
        return self


class Gate3DocstringScreeningConfig(StrictModel):
    active_registry_manifest: str = Field(min_length=1)
    active_registry_manifest_sha256: str = Field(pattern=_HEX64)
    require_nl_screen: Literal[True]
    require_reference_lean_screen: Literal[True]
    require_reference_representation_screen: Literal[True]

    @model_validator(mode="after")
    def _safe_path(self) -> Gate3DocstringScreeningConfig:
        path = PurePosixPath(self.active_registry_manifest)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("active_registry_manifest must be repository-relative")
        return self


class Gate3DocstringPolicyConfig(StrictModel):
    source_license: Literal["Apache-2.0"]
    nl_trust: Literal["trusted"]
    nl_trust_semantics: Literal["human_authored_provenance_only_not_self_containedness"]
    self_containedness_status: Literal["unreviewed"]
    candidate_source_records_only: Literal[True]
    problem_pool_admitted: Literal[False]
    model_collection_authorized: Literal[False]
    model_execution_performed: Literal[False]
    semantic_labels_created: Literal[False]
    private_source_transmission_performed: Literal[False]
    gate_claimed: Literal[False]
    use_existing_theorem_record_as_reference: Literal[True]
    full_alpha_structural_required: Literal[False]
    graph_required: Literal[False]


class Gate3DocstringPoolConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal["gate3_mathlib_adjacent_docstrings_v1"]
    frozen_at: datetime.datetime
    source: Gate3DocstringSourceConfig
    selection: Gate3DocstringSelectionConfig
    temporal_non_overlap: TemporalNonOverlapConfig
    screening: Gate3DocstringScreeningConfig
    policy: Gate3DocstringPolicyConfig

    @model_validator(mode="after")
    def _utc(self) -> Gate3DocstringPoolConfig:
        require_utc(self.frozen_at)
        return self


class CandidateOutcomeCode(StrEnum):
    ELIGIBLE = "eligible"
    NO_ADJACENT_DOCSTRING = "no_adjacent_docstring"
    EMPTY_DOCSTRING = "empty_docstring"
    REPRESENTATION_INCOMPLETE = "representation_incomplete"
    BENCHMARK_NL_HIT = "benchmark_nl_hit"
    BENCHMARK_REFERENCE_HIT = "benchmark_reference_hit"
    TEMPORAL_INTRODUCTION_UNPROVEN = "temporal_introduction_unproven"
    CHECKPOINT_TEMPORAL_OVERLAP = "checkpoint_temporal_overlap"
    DUPLICATE_NORMALIZED_NL = "duplicate_normalized_nl"
    DUPLICATE_ANCESTRY = "duplicate_ancestry"


class AdjacentDocstring(StrictModel):
    raw: str = Field(min_length=5)
    raw_sha256: str = Field(pattern=_HEX64)
    normalized_nl: str
    normalized_nl_sha256: str = Field(pattern=_HEX64)
    start_line: int = Field(gt=0)
    finish_line: int = Field(gt=0)
    start_column: int = Field(ge=0)
    finish_column: int = Field(ge=0)
    immediately_attached_to_declaration_command: Literal[True] = True

    @model_validator(mode="after")
    def _consistent(self) -> AdjacentDocstring:
        if not self.raw.startswith("/--") or not self.raw.endswith("-/"):
            raise ValueError("raw contributor docstring must preserve /-- ... -/")
        if self.raw_sha256 != sha256_hex(self.raw.encode("utf-8")):
            raise ValueError("raw_sha256 does not match raw docstring")
        if self.normalized_nl_sha256 != nl_hash(self.normalized_nl):
            raise ValueError("normalized_nl_sha256 does not match normalized_nl")
        if self.finish_line < self.start_line:
            raise ValueError("docstring line range is reversed")
        return self


class SourceProvenance(StrictModel):
    repository: Literal["https://github.com/leanprover-community/mathlib4"]
    revision: str = Field(pattern=_HEX40)
    source_file: str = Field(min_length=1)
    source_range: tuple[int, int]
    git_blob_sha1: str = Field(pattern=_HEX40)
    source_file_sha256: str = Field(pattern=_HEX64)
    theorem_header_sha256: str = Field(pattern=_HEX64)
    theorem_header_matches_frozen_record: Literal[True] = True
    declaration_name_matches_frozen_record: Literal[True] = True


class TemporalIntroductionEvidence(StrictModel):
    history_method: Literal["git_log_follow_pickaxe_exact_pair_v3"]
    search_revision: str = Field(pattern=_HEX40)
    exact_docstring_sha256: str = Field(pattern=_HEX64)
    introduction_commit: str = Field(pattern=_HEX40)
    introduction_created_at: datetime.datetime
    introduction_source_path: str = Field(min_length=1)
    introduction_commit_is_search_revision_ancestor: Literal[True] = True
    exact_pair_present_in_introduction_blob: Literal[True] = True
    first_pickaxe_occurrence_with_exact_pair: Literal[True] = True
    latest_checkpoint_created_at: datetime.datetime
    strictly_postdates_latest_checkpoint: bool

    @model_validator(mode="after")
    def _timestamps(self) -> TemporalIntroductionEvidence:
        require_utc(self.introduction_created_at)
        require_utc(self.latest_checkpoint_created_at)
        expected = self.introduction_created_at > self.latest_checkpoint_created_at
        if self.strictly_postdates_latest_checkpoint != expected:
            raise ValueError("temporal eligibility does not match introduction/cutoff dates")
        return self


class RegistryScreens(StrictModel):
    nl_hits: tuple[str, ...]
    reference_lean_hits: tuple[str, ...]
    reference_representation_hits: tuple[str, ...]
    all_three_screens_executed: Literal[True] = True
    all_three_screens_clear: bool
    registry_manifest_sha256: str = Field(pattern=_HEX64)
    active_registry_sha256: str = Field(pattern=_HEX64)
    registry_content_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _consistent(self) -> RegistryScreens:
        expected = not (
            self.nl_hits or self.reference_lean_hits or self.reference_representation_hits
        )
        if self.all_three_screens_clear != expected:
            raise ValueError("all_three_screens_clear does not match the screen hit sets")
        for field_name in (
            "nl_hits",
            "reference_lean_hits",
            "reference_representation_hits",
        ):
            values = getattr(self, field_name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{field_name} must be sorted and unique")
        return self


class Gate3MathlibDocstringCandidate(StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^gate3_docstring_candidate:[0-9a-f]{64}$")
    selection_hash: str = Field(pattern=_HEX64)
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    ancestry_id: str = Field(pattern=r"^anc:[0-9a-f]{64}$")
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    declaration_full_name: str = Field(min_length=1)
    theorem_statement_content_hash: str = Field(pattern=_HEX64)
    representation_content_hash: str = Field(pattern=_HEX64)
    reference_record_source: Literal["frozen_gate3_theorem_record"]
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    temporal_introduction: TemporalIntroductionEvidence
    registry_screens: RegistryScreens
    nl_source_link: str = Field(min_length=1)
    source_license: Literal["Apache-2.0"]
    nl_trust: Literal["trusted"]
    nl_trust_semantics: Literal["human_authored_provenance_only_not_self_containedness"]
    self_containedness_status: Literal["unreviewed"]
    candidate_source_record_only: Literal[True] = True
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    near_miss: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    gate_claimed: Literal[False] = False
    shared_three_family_temporal_eligible: Literal[True] = True

    @model_validator(mode="after")
    def _identity(self) -> Gate3MathlibDocstringCandidate:
        if not self.registry_screens.all_three_screens_clear:
            raise ValueError("eligible candidate must have all registry screens clear")
        if not self.temporal_introduction.strictly_postdates_latest_checkpoint:
            raise ValueError("shared candidate must strictly postdate latest checkpoint")
        expected = "gate3_docstring_candidate:" + hash_canonical(
            {
                "schema": "gate3_mathlib_adjacent_docstring_candidate_v1",
                "selection_hash": self.selection_hash,
                "theorem_id": self.theorem_id,
                "representation_id": self.representation_id,
                "raw_docstring_sha256": self.docstring.raw_sha256,
            }
        )
        if self.candidate_id != expected:
            raise ValueError("candidate_id does not match immutable candidate payload")
        return self


class ScreenedDocstringRecord(StrictModel):
    """A registry-clean pair before the strict checkpoint-date admission."""

    schema_version: Literal[1] = 1
    screened_record_id: str = Field(pattern=r"^gate3_docstring_screened:[0-9a-f]{64}$")
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    ancestry_id: str = Field(pattern=r"^anc:[0-9a-f]{64}$")
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    registry_screens: RegistryScreens
    temporal_introduction: TemporalIntroductionEvidence | None
    temporal_status: Literal[
        "strictly_postdates_latest_checkpoint",
        "checkpoint_temporal_overlap",
        "introduction_unproven",
    ]
    shared_three_family_temporal_eligible: bool
    nl_trust_semantics: Literal["human_authored_provenance_only_not_self_containedness"] = (
        "human_authored_provenance_only_not_self_containedness"
    )
    self_containedness_status: Literal["unreviewed"] = "unreviewed"
    candidate_source_record_only: Literal[True] = True
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _status(self) -> ScreenedDocstringRecord:
        if not self.registry_screens.all_three_screens_clear:
            raise ValueError("screened record must have all registry screens clear")
        if self.temporal_introduction is None:
            expected = "introduction_unproven"
            eligible = False
        elif self.temporal_introduction.strictly_postdates_latest_checkpoint:
            expected = "strictly_postdates_latest_checkpoint"
            eligible = True
        else:
            expected = "checkpoint_temporal_overlap"
            eligible = False
        if self.temporal_status != expected:
            raise ValueError("temporal_status does not match temporal evidence")
        if self.shared_three_family_temporal_eligible != eligible:
            raise ValueError("temporal eligibility does not match temporal evidence")
        expected_id = "gate3_docstring_screened:" + hash_canonical(
            {
                "schema": "gate3_mathlib_screened_docstring_v1",
                "theorem_id": self.theorem_id,
                "representation_id": self.representation_id,
                "raw_docstring_sha256": self.docstring.raw_sha256,
                "temporal_status": self.temporal_status,
                "introduction_commit": (
                    self.temporal_introduction.introduction_commit
                    if self.temporal_introduction is not None
                    else None
                ),
            }
        )
        if self.screened_record_id != expected_id:
            raise ValueError("screened_record_id does not match payload")
        return self


class Gate3DocstringOutcome(StrictModel):
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    ancestry_id: str = Field(pattern=r"^anc:[0-9a-f]{64}$")
    outcome: CandidateOutcomeCode
    detail: str = Field(min_length=1)
    candidate_id: str | None = Field(
        default=None, pattern=r"^gate3_docstring_candidate:[0-9a-f]{64}$"
    )
    normalized_nl_sha256: str | None = Field(default=None, pattern=_HEX64)
    registry_hits: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _terminal_shape(self) -> Gate3DocstringOutcome:
        if self.outcome is CandidateOutcomeCode.ELIGIBLE:
            if self.candidate_id is None or self.normalized_nl_sha256 is None:
                raise ValueError("eligible outcome requires candidate and normalized NL IDs")
        elif self.candidate_id is not None:
            raise ValueError("excluded outcome cannot retain an eligible candidate ID")
        return self


class ArtifactHash(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class Gate3DocstringPoolManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["gate3_mathlib_adjacent_docstring_candidate_manifest"]
    profile: Literal["one_example", "full"]
    frozen_at: datetime.datetime
    config_id: Literal["gate3_mathlib_adjacent_docstrings_v1"]
    source_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    input_artifacts: dict[str, ArtifactHash]
    output_artifacts: dict[str, ArtifactHash]
    attempted_mathlib_records: int = Field(gt=0)
    adjacent_docstring_records: int = Field(ge=0)
    screen_clear_records: int = Field(ge=0)
    temporally_clean_records_before_dedup: int = Field(ge=0)
    eligible_distinct_ancestry_groups: int = Field(ge=0)
    target_distinct_ancestry_groups: int = Field(gt=0)
    selected_distinct_ancestry_groups: int = Field(ge=0)
    shortfall: int = Field(ge=0)
    terminal_outcome_counts: dict[str, int]
    first_candidate_id: str | None
    selected_candidate_ids: tuple[str, ...]
    one_example_report: ArtifactHash | None = None
    candidate_source_records_only: Literal[True] = True
    self_containedness_status: Literal["unreviewed"] = "unreviewed"
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _accounting(self) -> Gate3DocstringPoolManifest:
        require_utc(self.frozen_at)
        if sum(self.terminal_outcome_counts.values()) != self.attempted_mathlib_records:
            raise ValueError("terminal outcome counts do not reconcile attempted records")
        if self.selected_distinct_ancestry_groups != len(self.selected_candidate_ids):
            raise ValueError("selected count does not match selected_candidate_ids")
        expected_shortfall = max(
            0,
            self.target_distinct_ancestry_groups - self.eligible_distinct_ancestry_groups,
        )
        if self.shortfall != expected_shortfall:
            raise ValueError("shortfall does not match target and eligible group count")
        if self.profile == "one_example":
            if self.selected_distinct_ancestry_groups != 1 or self.one_example_report is not None:
                raise ValueError("one-example profile must select exactly one without prior report")
        elif self.one_example_report is None:
            raise ValueError("full profile must bind the prior one-example report")
        if (
            self.selected_candidate_ids
            and self.first_candidate_id != self.selected_candidate_ids[0]
        ):
            raise ValueError("first_candidate_id must match selected ordering")
        return self


class Gate3DocstringPoolReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_gate3_mathlib_docstring_pool_preflight"]
    profile: Literal["one_example", "full"]
    passed: bool
    manifest: ArtifactHash
    selected_candidates: ArtifactHash
    screened_candidates: ArtifactHash
    eligible_candidates: ArtifactHash
    outcomes: ArtifactHash
    shortfall_report: ArtifactHash
    selected_count: int = Field(ge=0)
    eligible_distinct_ancestry_groups: int = Field(ge=0)
    target_distinct_ancestry_groups: int = Field(gt=0)
    shortfall: int = Field(ge=0)
    blockers: tuple[str, ...]
    candidate_source_records_only: Literal[True] = True
    self_containedness_status: Literal["unreviewed"] = "unreviewed"
    problem_pool_admitted: Literal[False] = False
    model_collection_authorized: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    gate_claimed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class Gate3DocstringPoolRun:
    report_path: Path
    manifest_path: Path
    report: Gate3DocstringPoolReport
    manifest: Gate3DocstringPoolManifest


@dataclass(frozen=True, slots=True)
class _DocstringCandidateDraft:
    theorem: TheoremRecord
    representation: RepresentationRecord
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    temporal_introduction: TemporalIntroductionEvidence
    registry_screens: RegistryScreens
    selection_hash: str


@dataclass(frozen=True, slots=True)
class _PreTemporalDraft:
    theorem: TheoremRecord
    representation: RepresentationRecord
    docstring: AdjacentDocstring
    source_provenance: SourceProvenance
    registry_screens: RegistryScreens


@dataclass(frozen=True, slots=True)
class _SourceFileSnapshot:
    text: str
    git_blob_sha1: str
    source_file_sha256: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate3DocstringPoolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json_loads(line: str, *, artifact: Path, line_number: int) -> dict[str, object]:
    try:
        value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise Gate3DocstringPoolError(f"{artifact}:{line_number}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Gate3DocstringPoolError(f"{artifact}:{line_number}: expected a JSON object")
    return value


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Gate3DocstringPoolError(
            f"Git provenance command failed: git {' '.join(args)}: {exc}"
        ) from exc


def _git_optional(repo: Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise Gate3DocstringPoolError(
            f"Git provenance command failed: git {' '.join(args)}: {exc}"
        ) from exc
    return result.returncode, result.stdout


def _git_blob_sha1(data: bytes) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def _git_tree_blobs(repo: Path, revision: str) -> dict[str, str]:
    raw = _git(repo, "ls-tree", "-r", "--full-tree", revision, "--", "Mathlib")
    result: dict[str, str] = {}
    for line in raw.splitlines():
        metadata, separator, path = line.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[1] != "blob":
            raise Gate3DocstringPoolError(f"cannot parse Git tree entry: {line!r}")
        result[path] = fields[2]
    if not result:
        raise Gate3DocstringPoolError("pinned Git tree contains no Mathlib blobs")
    return result


def _normalize_docstring(raw: str) -> str:
    if not raw.startswith("/--") or not raw.endswith("-/"):
        raise Gate3DocstringPoolError("contributor docstring delimiters are malformed")
    return _WS.sub(" ", raw[3:-2]).strip()


def _matching_comment_finish(text: str, start: int) -> int:
    if not text.startswith("/--", start):
        raise Gate3DocstringPoolError("docstring parser did not start at /--")
    depth = 1
    cursor = start + 3
    while cursor < len(text):
        if text.startswith("/-", cursor):
            depth += 1
            cursor += 2
        elif text.startswith("-/", cursor):
            depth -= 1
            cursor += 2
            if depth == 0:
                return cursor
        else:
            cursor += 1
    raise Gate3DocstringPoolError("unterminated contributor docstring")


def _line_column(text: str, offset: int, *, base_line: int) -> tuple[int, int]:
    prefix = text[:offset]
    line = base_line + prefix.count("\n")
    last_newline = prefix.rfind("\n")
    column = len(prefix) if last_newline < 0 else len(prefix) - last_newline - 1
    return line, column


def extract_adjacent_docstring(
    *,
    theorem: TheoremRecord,
    source_text: str,
) -> tuple[AdjacentDocstring | None, str]:
    """Return the exact leading command docstring and frozen theorem header.

    Lean declaration ranges include a leading command docstring when one is
    attached.  The exact frozen proof-stripped header must match the pinned
    source slice before a docstring is inspected.
    """

    if theorem.source_range is None:
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: missing source_range")
    if not theorem.proof_stripped_declaration.endswith(PLACEHOLDER):
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: frozen declaration lacks canonical placeholder"
        )
    start_line, finish_line = theorem.source_range
    lines = source_text.splitlines(keepends=True)
    if start_line < 1 or finish_line < start_line or finish_line > len(lines):
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: source_range is outside pinned file")
    source_slice = "".join(lines[start_line - 1 : finish_line])
    theorem_header = theorem.proof_stripped_declaration[: -len(PLACEHOLDER)]
    if not source_slice.startswith(theorem_header):
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: frozen theorem header differs from pinned source"
        )

    leading_count = len(theorem_header) - len(theorem_header.lstrip())
    command_text = theorem_header[leading_count:]
    if not command_text.startswith("/--"):
        return None, theorem_header
    finish = _matching_comment_finish(command_text, 0)
    raw = command_text[:finish]
    normalized = _normalize_docstring(raw)
    if not normalized:
        return (
            AdjacentDocstring(
                raw=raw,
                raw_sha256=sha256_hex(raw.encode("utf-8")),
                normalized_nl="",
                normalized_nl_sha256=nl_hash(""),
                start_line=start_line,
                finish_line=start_line,
                start_column=leading_count,
                finish_column=leading_count + len(raw),
            ),
            theorem_header,
        )

    declaration_matches = tuple(_DECLARATION_TOKEN.finditer(command_text))
    expected_name = theorem.declaration_name
    if expected_name is None:
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: declaration name is absent")
    named_matches = tuple(
        match
        for match in declaration_matches
        if re.match(
            rf"{re.escape(expected_name)}(?=$|[\s{{(:])",
            command_text[match.end() :],
        )
    )
    if len(named_matches) != 1 or named_matches[0].start() <= finish:
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: docstring/declaration attachment is ambiguous"
        )
    start_position = leading_count
    finish_position = leading_count + finish
    doc_start_line, doc_start_col = _line_column(
        theorem_header, start_position, base_line=start_line
    )
    doc_finish_line, doc_finish_col = _line_column(
        theorem_header, finish_position, base_line=start_line
    )
    return (
        AdjacentDocstring(
            raw=raw,
            raw_sha256=sha256_hex(raw.encode("utf-8")),
            normalized_nl=normalized,
            normalized_nl_sha256=nl_hash(normalized),
            start_line=doc_start_line,
            finish_line=doc_finish_line,
            start_column=doc_start_col,
            finish_column=doc_finish_col,
        ),
        theorem_header,
    )


def _load_frozen_mathlib_theorems(
    config: Gate3DocstringPoolConfig,
) -> tuple[dict[str, TheoremRecord], dict[str, dict[str, object]]]:
    manifest_path = Path(config.source.theorem_manifest.path)
    records_path = Path(config.source.theorem_records.path)
    if hash_file(manifest_path) != config.source.theorem_manifest.sha256:
        raise Gate3DocstringPoolError("Gate-3 theorem manifest hash mismatch")
    if hash_file(records_path) != config.source.theorem_records.sha256:
        raise Gate3DocstringPoolError("Gate-3 theorem partition hash mismatch")

    try:
        manifest_raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Gate3DocstringPoolError(f"invalid Gate-3 theorem manifest: {exc}") from exc
    manifest_records = manifest_raw.get("records") if isinstance(manifest_raw, dict) else None
    if not isinstance(manifest_records, list):
        raise Gate3DocstringPoolError("Gate-3 theorem manifest lacks records")
    expected: dict[str, dict[str, object]] = {}
    for item in manifest_records:
        if isinstance(item, dict) and item.get("source") == config.source.source_name:
            theorem_id = item.get("theorem_id")
            if not isinstance(theorem_id, str) or theorem_id in expected:
                raise Gate3DocstringPoolError("invalid/duplicate mathlib theorem ID in manifest")
            expected[theorem_id] = item
    if len(expected) != config.source.expected_mathlib_records:
        raise Gate3DocstringPoolError(
            "Gate-3 manifest mathlib count differs from frozen configuration"
        )

    theorems: dict[str, TheoremRecord] = {}
    with records_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = _json_loads(line, artifact=records_path, line_number=line_number)
            theorem_raw = raw.get("theorem")
            if not isinstance(theorem_raw, dict) or theorem_raw.get("source") != "mathlib":
                continue
            theorem = TheoremRecord.model_validate(theorem_raw)
            if theorem.theorem_id not in expected:
                raise Gate3DocstringPoolError(
                    f"theorem partition contains unexpected mathlib ID {theorem.theorem_id}"
                )
            if theorem.theorem_id in theorems:
                raise Gate3DocstringPoolError(f"duplicate theorem record {theorem.theorem_id}")
            manifest_item = expected[theorem.theorem_id]
            if theorem.context_id != manifest_item.get(
                "context_id"
            ) or theorem.statement_content_hash != manifest_item.get("statement_content_hash"):
                raise Gate3DocstringPoolError(
                    f"{theorem.theorem_id}: theorem/manifest identity drift"
                )
            theorems[theorem.theorem_id] = theorem
    if set(theorems) != set(expected):
        raise Gate3DocstringPoolError("theorem partition does not exactly cover mathlib manifest")
    return theorems, expected


def _load_representations(
    config: Gate3DocstringPoolConfig,
    theorem_ids: frozenset[str],
) -> dict[str, RepresentationRecord]:
    path = Path(config.source.representation_records.path)
    if hash_file(path) != config.source.representation_records.sha256:
        raise Gate3DocstringPoolError("Gate-3 representation partition hash mismatch")
    records: dict[str, RepresentationRecord] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = _json_loads(line, artifact=path, line_number=line_number)
            theorem_id = raw.get("theorem_id")
            if theorem_id not in theorem_ids:
                continue
            representation = RepresentationRecord.model_validate(raw)
            if theorem_id in records:
                raise Gate3DocstringPoolError(f"duplicate Gate-3 representation for {theorem_id}")
            records[str(theorem_id)] = representation
    if set(records) != set(theorem_ids):
        missing = sorted(set(theorem_ids) - set(records))
        raise Gate3DocstringPoolError(
            f"Gate-3 representation partition lacks {len(missing)} frozen mathlib records"
        )
    return records


def _validate_theorem_and_representation(
    *,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    config: Gate3DocstringPoolConfig,
) -> None:
    if (
        theorem.source != config.source.source_name
        or theorem.source_revision != config.source.checkout_revision
        or theorem.context_id != config.source.context_id
        or not theorem.is_proposition
        or theorem.metadata.get("transform_source_eligible") is not True
    ):
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: theorem source/context/eligibility drift"
        )
    if (
        representation.theorem_id != theorem.theorem_id
        or representation.context_id != theorem.context_id
        or representation.normalization_version
        != config.source.representation_normalization_version
        or representation.raw_proof_stripped != theorem.proof_stripped_declaration
    ):
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: representation identity/content drift"
        )


def _missing_screening_views(
    representation: RepresentationRecord,
) -> tuple[str, ...]:
    missing = [
        view
        for view in _MODEL_VISIBLE_VIEWS
        if representation.view_status.get(view) is not ViewStatus.OK
    ]
    if representation.alpha_identity_fingerprint is None:
        missing.append("alpha_identity_fingerprint")
    return tuple(missing)


def _source_file(
    *,
    checkout: Path,
    theorem: TheoremRecord,
    git_blobs: dict[str, str],
) -> _SourceFileSnapshot:
    if theorem.source_file is None or theorem.source_range is None:
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: source file/range is missing")
    relative = PurePosixPath(theorem.source_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: unsafe source file path")
    path = checkout / Path(*relative.parts)
    try:
        mode = path.lstat().st_mode
        resolved = path.resolve(strict=True)
        resolved.relative_to(checkout)
    except (OSError, ValueError) as exc:
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: source file is absent or escapes checkout"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: source path is not a regular non-symlink file"
        )
    data = path.read_bytes()
    observed_blob = _git_blob_sha1(data)
    expected_blob = git_blobs.get(theorem.source_file)
    if expected_blob is None or observed_blob != expected_blob:
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: working source differs from pinned Git blob"
        )
    try:
        source_text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Gate3DocstringPoolError(f"{theorem.theorem_id}: source file is not UTF-8") from exc
    return _SourceFileSnapshot(
        text=source_text,
        git_blob_sha1=observed_blob,
        source_file_sha256=sha256_hex(data),
    )


def _provenance_for_theorem(
    *,
    theorem: TheoremRecord,
    snapshot: _SourceFileSnapshot,
    theorem_header: str,
) -> SourceProvenance:
    assert theorem.source_file is not None
    assert theorem.source_range is not None
    return SourceProvenance(
        repository="https://github.com/leanprover-community/mathlib4",
        revision=theorem.source_revision,
        source_file=theorem.source_file,
        source_range=theorem.source_range,
        git_blob_sha1=snapshot.git_blob_sha1,
        source_file_sha256=snapshot.source_file_sha256,
        theorem_header_sha256=sha256_hex(theorem_header.encode("utf-8")),
    )


def _exact_pair_present(
    *,
    blob: str,
    raw_docstring: str,
    declaration_name: str,
) -> bool:
    """Whether one exact docstring occurrence attaches to the named command."""

    offset = 0
    while True:
        found = blob.find(raw_docstring, offset)
        if found < 0:
            return False
        remainder = blob[found + len(raw_docstring) :]
        next_decl = _DECLARATION_TOKEN.search(remainder)
        if next_decl is not None and re.match(
            rf"{re.escape(declaration_name)}(?=$|[\s{{(:])",
            remainder[next_decl.end() :],
        ):
            return True
        offset = found + 1


def _blob_for_introduction_candidate(
    *,
    repo: Path,
    commit: str,
    current_source_path: str,
    raw_docstring: str,
    declaration_name: str,
) -> tuple[str, str] | None:
    """Find the changed Lean blob containing the exact pair at one commit."""

    returncode, blob = _git_optional(repo, "show", f"{commit}:{current_source_path}")
    if returncode == 0 and _exact_pair_present(
        blob=blob,
        raw_docstring=raw_docstring,
        declaration_name=declaration_name,
    ):
        return current_source_path, blob

    changed = _git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit)
    for path in sorted(
        item
        for item in changed.splitlines()
        if item.startswith("Mathlib/") and item.endswith(".lean")
    ):
        returncode, candidate_blob = _git_optional(repo, "show", f"{commit}:{path}")
        if returncode == 0 and _exact_pair_present(
            blob=candidate_blob,
            raw_docstring=raw_docstring,
            declaration_name=declaration_name,
        ):
            return path, candidate_blob
    return None


def _first_introduction(
    *,
    repo: Path,
    config: Gate3DocstringPoolConfig,
    draft: _PreTemporalDraft,
    revision_order: dict[str, int],
) -> TemporalIntroductionEvidence | None:
    theorem = draft.theorem
    assert theorem.source_file is not None
    assert theorem.declaration_name is not None
    raw_log = _git(
        repo,
        "log",
        "--follow",
        "--format=%H%x09%cI",
        "-S",
        draft.docstring.raw,
        config.source.checkout_revision,
        "--",
        theorem.source_file,
    )
    # Regex change search is token-delimited, so renaming ``foo_iff`` to
    # ``foo`` is observable even though ``-S 'lemma foo'`` is a substring of
    # both versions and would miss the transition.
    escaped_name = re.escape(theorem.declaration_name)
    declaration_regex = rf"(theorem|lemma)[[:space:]]+{escaped_name}([^[:alnum:]_']|$)"
    declaration_log = _git(
        repo,
        "log",
        "--follow",
        "--format=%H%x09%cI",
        "-G",
        declaration_regex,
        config.source.checkout_revision,
        "--",
        theorem.source_file,
    )
    candidate_lines = set(raw_log.splitlines()) | set(declaration_log.splitlines())
    parsed_lines: list[tuple[int, str, str]] = []
    for line in candidate_lines:
        commit, separator, created_at_raw = line.partition("\t")
        if not separator or re.fullmatch(_HEX40[1:-1], commit) is None:
            raise Gate3DocstringPoolError(
                f"{theorem.theorem_id}: cannot parse Git introduction history"
            )
        rank = revision_order.get(commit)
        if rank is None:
            raise Gate3DocstringPoolError(
                f"{theorem.theorem_id}: history commit is outside pinned ancestry"
            )
        parsed_lines.append((rank, commit, created_at_raw))
    for _, commit, created_at_raw in sorted(parsed_lines):
        pair_blob = _blob_for_introduction_candidate(
            repo=repo,
            commit=commit,
            current_source_path=theorem.source_file,
            raw_docstring=draft.docstring.raw,
            declaration_name=theorem.declaration_name,
        )
        if pair_blob is None:
            continue
        introduction_source_path, _ = pair_blob
        try:
            created_at = datetime.datetime.fromisoformat(created_at_raw)
        except ValueError as exc:
            raise Gate3DocstringPoolError(
                f"{theorem.theorem_id}: invalid Git commit timestamp {created_at_raw!r}"
            ) from exc
        require_utc(created_at)
        ancestor_code, _ = _git_optional(
            repo,
            "merge-base",
            "--is-ancestor",
            commit,
            config.source.checkout_revision,
        )
        if ancestor_code != 0:
            raise Gate3DocstringPoolError(
                f"{theorem.theorem_id}: introduction commit is not pinned-revision ancestry"
            )
        return TemporalIntroductionEvidence(
            history_method=config.temporal_non_overlap.history_method,
            search_revision=config.source.checkout_revision,
            exact_docstring_sha256=draft.docstring.raw_sha256,
            introduction_commit=commit,
            introduction_created_at=created_at,
            introduction_source_path=introduction_source_path,
            latest_checkpoint_created_at=(config.temporal_non_overlap.latest_checkpoint_created_at),
            strictly_postdates_latest_checkpoint=(
                created_at > config.temporal_non_overlap.latest_checkpoint_created_at
            ),
        )
    return None


def _revision_order(repo: Path, revision: str) -> dict[str, int]:
    """Topological oldest-first rank for every commit in pinned ancestry."""

    commits = _git(repo, "rev-list", "--topo-order", "--reverse", revision).splitlines()
    if not commits or commits[-1] != revision:
        raise Gate3DocstringPoolError("cannot enumerate pinned Git ancestry")
    if len(commits) != len(set(commits)):
        raise Gate3DocstringPoolError("pinned Git ancestry contains duplicate commit IDs")
    return {commit: rank for rank, commit in enumerate(commits)}


def _screened_record(
    *,
    draft: _PreTemporalDraft,
    evidence: TemporalIntroductionEvidence | None,
) -> ScreenedDocstringRecord:
    status: Literal[
        "strictly_postdates_latest_checkpoint",
        "checkpoint_temporal_overlap",
        "introduction_unproven",
    ]
    if evidence is None:
        status = "introduction_unproven"
    elif evidence.strictly_postdates_latest_checkpoint:
        status = "strictly_postdates_latest_checkpoint"
    else:
        status = "checkpoint_temporal_overlap"
    screened_id = "gate3_docstring_screened:" + hash_canonical(
        {
            "schema": "gate3_mathlib_screened_docstring_v1",
            "theorem_id": draft.theorem.theorem_id,
            "representation_id": draft.representation.representation_id,
            "raw_docstring_sha256": draft.docstring.raw_sha256,
            "temporal_status": status,
            "introduction_commit": (evidence.introduction_commit if evidence is not None else None),
        }
    )
    return ScreenedDocstringRecord(
        screened_record_id=screened_id,
        theorem_id=draft.theorem.theorem_id,
        representation_id=draft.representation.representation_id,
        ancestry_id=draft.theorem.ancestry_id,
        docstring=draft.docstring,
        source_provenance=draft.source_provenance,
        registry_screens=draft.registry_screens,
        temporal_introduction=evidence,
        temporal_status=status,
        shared_three_family_temporal_eligible=bool(
            evidence is not None and evidence.strictly_postdates_latest_checkpoint
        ),
    )


def _registry_screens(
    *,
    active: ActiveBenchmarkRegistry,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    docstring: AdjacentDocstring,
) -> RegistryScreens:
    nl_hits = (
        (f"normalized_nl:{docstring.normalized_nl_sha256}",)
        if active.index.contains_nl(docstring.normalized_nl)
        else ()
    )
    reference_hits = candidate_benchmark_hits(
        denylist_index=active.index,
        theorem=theorem,
        representation=representation,
    )
    lean_hits = tuple(hit for hit in reference_hits if hit.startswith("lean:"))
    representation_hits = tuple(hit for hit in reference_hits if hit.startswith("representation:"))
    unexpected = tuple(
        hit
        for hit in reference_hits
        if not (hit.startswith("lean:") or hit.startswith("representation:"))
    )
    if unexpected:
        raise Gate3DocstringPoolError(
            f"{theorem.theorem_id}: unclassified active-registry hits {unexpected}"
        )
    return RegistryScreens(
        nl_hits=tuple(sorted(nl_hits)),
        reference_lean_hits=tuple(sorted(lean_hits)),
        reference_representation_hits=tuple(sorted(representation_hits)),
        all_three_screens_clear=not (nl_hits or lean_hits or representation_hits),
        registry_manifest_sha256=hash_file(active.manifest_path),
        active_registry_sha256=hash_file(active.active_registry_path),
        registry_content_hash=active.index.registry_content_hash,
    )


def _selection_hash(
    *,
    config: Gate3DocstringPoolConfig,
    theorem: TheoremRecord,
    docstring: AdjacentDocstring,
) -> str:
    return hash_canonical(
        {
            "selection_version": config.selection.selection_version,
            "theorem_id": theorem.theorem_id,
            "ancestry_id": theorem.ancestry_id,
            "raw_docstring_sha256": docstring.raw_sha256,
        }
    )


def _candidate(draft: _DocstringCandidateDraft) -> Gate3MathlibDocstringCandidate:
    theorem = draft.theorem
    representation = draft.representation
    assert theorem.source_file is not None
    assert theorem.source_range is not None
    assert theorem.declaration_full_name is not None
    candidate_id = "gate3_docstring_candidate:" + hash_canonical(
        {
            "schema": "gate3_mathlib_adjacent_docstring_candidate_v1",
            "selection_hash": draft.selection_hash,
            "theorem_id": theorem.theorem_id,
            "representation_id": representation.representation_id,
            "raw_docstring_sha256": draft.docstring.raw_sha256,
        }
    )
    lines = (
        f"#L{draft.docstring.start_line}-L{draft.docstring.finish_line}"
        if draft.docstring.finish_line != draft.docstring.start_line
        else f"#L{draft.docstring.start_line}"
    )
    return Gate3MathlibDocstringCandidate(
        candidate_id=candidate_id,
        selection_hash=draft.selection_hash,
        theorem_id=theorem.theorem_id,
        representation_id=representation.representation_id,
        ancestry_id=theorem.ancestry_id,
        root_ancestry_ids=theorem.root_ancestry_ids,
        context_id=theorem.context_id,
        declaration_full_name=theorem.declaration_full_name,
        theorem_statement_content_hash=theorem.statement_content_hash,
        representation_content_hash=representation.content_hash,
        reference_record_source="frozen_gate3_theorem_record",
        docstring=draft.docstring,
        source_provenance=draft.source_provenance,
        temporal_introduction=draft.temporal_introduction,
        registry_screens=draft.registry_screens,
        nl_source_link=(
            "https://github.com/leanprover-community/mathlib4/blob/"
            f"{theorem.source_revision}/{theorem.source_file}{lines}"
        ),
        source_license="Apache-2.0",
        nl_trust="trusted",
        nl_trust_semantics="human_authored_provenance_only_not_self_containedness",
        self_containedness_status="unreviewed",
    )


def select_distinct_candidates(
    drafts: tuple[_DocstringCandidateDraft, ...],
) -> tuple[tuple[Gate3MathlibDocstringCandidate, ...], dict[str, CandidateOutcomeCode]]:
    """Select one hash-minimal record per ancestry and normalized NL hash."""

    ordered = sorted(
        drafts,
        key=lambda item: (item.selection_hash, item.theorem.theorem_id),
    )
    selected: list[Gate3MathlibDocstringCandidate] = []
    exclusions: dict[str, CandidateOutcomeCode] = {}
    seen_ancestry: set[str] = set()
    seen_nl: set[str] = set()
    for draft in ordered:
        theorem_id = draft.theorem.theorem_id
        if draft.theorem.ancestry_id in seen_ancestry:
            exclusions[theorem_id] = CandidateOutcomeCode.DUPLICATE_ANCESTRY
            continue
        if draft.docstring.normalized_nl_sha256 in seen_nl:
            exclusions[theorem_id] = CandidateOutcomeCode.DUPLICATE_NORMALIZED_NL
            continue
        selected.append(_candidate(draft))
        seen_ancestry.add(draft.theorem.ancestry_id)
        seen_nl.add(draft.docstring.normalized_nl_sha256)
    return tuple(selected), exclusions


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Gate3DocstringPoolError(
                f"immutable output differs from existing artifact: {path}"
            )
        return
    path.write_bytes(payload)


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(values: tuple[object, ...]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


def _artifact(path: Path, *, root: Path) -> ArtifactHash:
    return ArtifactHash(path=_relative_or_absolute(path, root), sha256=hash_file(path))


def _validate_one_example(
    *,
    root: Path,
    expected_first_candidate_id: str,
    config_hash: str,
) -> ArtifactHash:
    path = root / ONE_EXAMPLE_REPORT
    if not path.is_file():
        raise Gate3DocstringPoolError(
            "full expansion requires the one-example preflight report first"
        )
    try:
        report = Gate3DocstringPoolReport.model_validate_json(path.read_text(encoding="utf-8"))
        manifest_path = Path(report.manifest.path)
        if not manifest_path.is_absolute():
            manifest_path = root / manifest_path
        manifest = Gate3DocstringPoolManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise Gate3DocstringPoolError(f"invalid one-example preflight artifacts: {exc}") from exc
    config_artifact = manifest.input_artifacts.get("config")
    if (
        not report.passed
        or report.profile != "one_example"
        or manifest.first_candidate_id != expected_first_candidate_id
        or config_artifact is None
        or config_artifact.sha256 != config_hash
    ):
        raise Gate3DocstringPoolError(
            "one-example preflight does not bind current inputs/first candidate"
        )
    return _artifact(path, root=root)


def run_gate3_docstring_pool(
    *,
    paths: RepoPaths,
    mathlib_checkout: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    profile: Literal["one_example", "full"],
) -> Gate3DocstringPoolRun:
    """Run one-example or full deterministic candidate expansion."""

    root = paths.root.resolve()
    config_path = root / CONFIG_PATH
    config = load_config(config_path, Gate3DocstringPoolConfig).config
    config_hash = hash_file(config_path)
    registry_manifest_path = root / config.screening.active_registry_manifest
    if hash_file(registry_manifest_path) != (config.screening.active_registry_manifest_sha256):
        raise Gate3DocstringPoolError("active registry manifest binding mismatch")

    checkout = mathlib_checkout.resolve()
    if _git(checkout, "rev-parse", "HEAD").strip() != config.source.checkout_revision:
        raise Gate3DocstringPoolError("mathlib checkout HEAD differs from pinned revision")
    git_blobs = _git_tree_blobs(checkout, config.source.checkout_revision)
    revision_order = _revision_order(checkout, config.source.checkout_revision)
    active = load_active_benchmark_registry(repo_root=root)
    if active.manifest_path.resolve() != registry_manifest_path.resolve():
        raise Gate3DocstringPoolError("loaded active registry manifest path drift")

    theorems, _ = _load_frozen_mathlib_theorems(config)
    representations = _load_representations(config, frozenset(theorems))

    source_cache: dict[str, _SourceFileSnapshot] = {}
    pre_temporal: list[_PreTemporalDraft] = []
    outcomes: dict[str, Gate3DocstringOutcome] = {}
    adjacent_count = 0
    screen_clear_count = 0
    for theorem_id in sorted(theorems):
        theorem = theorems[theorem_id]
        representation = representations[theorem_id]
        _validate_theorem_and_representation(
            theorem=theorem,
            representation=representation,
            config=config,
        )
        assert theorem.source_file is not None
        cached = source_cache.get(theorem.source_file)
        if cached is None:
            cached = _source_file(
                checkout=checkout,
                theorem=theorem,
                git_blobs=git_blobs,
            )
            source_cache[theorem.source_file] = cached
        source_text = cached.text
        docstring, theorem_header = extract_adjacent_docstring(
            theorem=theorem,
            source_text=source_text,
        )
        if docstring is None:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=theorem.ancestry_id,
                outcome=CandidateOutcomeCode.NO_ADJACENT_DOCSTRING,
                detail="frozen declaration command has no leading contributor /-- ... -/ docstring",
            )
            continue
        adjacent_count += 1
        if not docstring.normalized_nl:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=theorem.ancestry_id,
                outcome=CandidateOutcomeCode.EMPTY_DOCSTRING,
                detail="adjacent contributor docstring is empty after whitespace normalization",
                normalized_nl_sha256=docstring.normalized_nl_sha256,
            )
            continue
        missing_views = _missing_screening_views(representation)
        if missing_views:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=theorem.ancestry_id,
                outcome=CandidateOutcomeCode.REPRESENTATION_INCOMPLETE,
                detail=(
                    "frozen reference cannot be fully screened; missing views: "
                    + ", ".join(missing_views)
                ),
                normalized_nl_sha256=docstring.normalized_nl_sha256,
            )
            continue
        provenance = _provenance_for_theorem(
            theorem=theorem,
            snapshot=cached,
            theorem_header=theorem_header,
        )
        screens = _registry_screens(
            active=active,
            theorem=theorem,
            representation=representation,
            docstring=docstring,
        )
        hit_values = tuple(
            sorted(
                (
                    *screens.nl_hits,
                    *screens.reference_lean_hits,
                    *screens.reference_representation_hits,
                )
            )
        )
        if screens.nl_hits:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=theorem.ancestry_id,
                outcome=CandidateOutcomeCode.BENCHMARK_NL_HIT,
                detail="normalized contributor docstring matches the active benchmark registry",
                normalized_nl_sha256=docstring.normalized_nl_sha256,
                registry_hits=hit_values,
            )
            continue
        if screens.reference_lean_hits or screens.reference_representation_hits:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=theorem.ancestry_id,
                outcome=CandidateOutcomeCode.BENCHMARK_REFERENCE_HIT,
                detail="reference theorem or representation matches active benchmark registry",
                normalized_nl_sha256=docstring.normalized_nl_sha256,
                registry_hits=hit_values,
            )
            continue
        screen_clear_count += 1
        pre_temporal.append(
            _PreTemporalDraft(
                theorem=theorem,
                representation=representation,
                docstring=docstring,
                source_provenance=provenance,
                registry_screens=screens,
            )
        )

    # Git operations are read-only and independent.  Preserve deterministic
    # downstream ordering by zipping evidence back to the already sorted
    # theorem traversal rather than completion order.
    with ThreadPoolExecutor(max_workers=8) as executor:
        temporal_evidence = tuple(
            executor.map(
                lambda draft: _first_introduction(
                    repo=checkout,
                    config=config,
                    draft=draft,
                    revision_order=revision_order,
                ),
                pre_temporal,
            )
        )
    screened_records = tuple(
        _screened_record(draft=draft, evidence=evidence)
        for draft, evidence in zip(pre_temporal, temporal_evidence, strict=True)
    )
    drafts: list[_DocstringCandidateDraft] = []
    for draft, evidence in zip(pre_temporal, temporal_evidence, strict=True):
        theorem_id = draft.theorem.theorem_id
        if evidence is None:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=draft.theorem.ancestry_id,
                outcome=CandidateOutcomeCode.TEMPORAL_INTRODUCTION_UNPROVEN,
                detail=(
                    "pinned Git pickaxe history did not prove a first exact "
                    "docstring+theorem pair occurrence"
                ),
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        if not evidence.strictly_postdates_latest_checkpoint:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=draft.theorem.ancestry_id,
                outcome=CandidateOutcomeCode.CHECKPOINT_TEMPORAL_OVERLAP,
                detail=(
                    f"first exact-pair introduction {evidence.introduction_created_at.isoformat()} "
                    "does not strictly postdate latest active checkpoint "
                    f"{evidence.latest_checkpoint_created_at.isoformat()}"
                ),
                normalized_nl_sha256=draft.docstring.normalized_nl_sha256,
            )
            continue
        drafts.append(
            _DocstringCandidateDraft(
                theorem=draft.theorem,
                representation=draft.representation,
                docstring=draft.docstring,
                source_provenance=draft.source_provenance,
                temporal_introduction=evidence,
                registry_screens=draft.registry_screens,
                selection_hash=_selection_hash(
                    config=config,
                    theorem=draft.theorem,
                    docstring=draft.docstring,
                ),
            )
        )

    eligible, dedup_exclusions = select_distinct_candidates(tuple(drafts))
    eligible_by_theorem = {item.theorem_id: item for item in eligible}
    for candidate_draft in drafts:
        theorem_id = candidate_draft.theorem.theorem_id
        candidate = eligible_by_theorem.get(theorem_id)
        if candidate is not None:
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=candidate_draft.theorem.ancestry_id,
                outcome=CandidateOutcomeCode.ELIGIBLE,
                detail="adjacent docstring and frozen reference passed all admission checks",
                candidate_id=candidate.candidate_id,
                normalized_nl_sha256=candidate_draft.docstring.normalized_nl_sha256,
            )
        else:
            outcome = dedup_exclusions[theorem_id]
            outcomes[theorem_id] = Gate3DocstringOutcome(
                theorem_id=theorem_id,
                ancestry_id=candidate_draft.theorem.ancestry_id,
                outcome=outcome,
                detail=(
                    "a lower hash-ranked record already represents this ancestry"
                    if outcome is CandidateOutcomeCode.DUPLICATE_ANCESTRY
                    else "a lower hash-ranked record has the same normalized contributor docstring"
                ),
                normalized_nl_sha256=candidate_draft.docstring.normalized_nl_sha256,
            )

    if set(outcomes) != set(theorems):
        raise Gate3DocstringPoolError("not every frozen mathlib theorem has a terminal outcome")
    if not eligible:
        raise Gate3DocstringPoolError("no eligible adjacent-docstring candidates remain")

    target = config.selection.target_distinct_ancestry_groups
    selected = eligible[:1] if profile == "one_example" else eligible[:target]
    shortfall = max(0, target - len(eligible))
    prior_report: ArtifactHash | None = None
    if profile == "full":
        prior_report = _validate_one_example(
            root=root,
            expected_first_candidate_id=eligible[0].candidate_id,
            config_hash=config_hash,
        )

    profile_dir = output_root.resolve() / profile
    eligible_path = profile_dir / "eligible_candidates.jsonl"
    selected_path = profile_dir / "selected_candidates.jsonl"
    screened_path = profile_dir / "screened_candidates.jsonl"
    outcomes_path = profile_dir / "terminal_outcomes.jsonl"
    shortfall_path = profile_dir / "shortfall_report.json"
    manifest_path = profile_dir / "manifest.json"

    _write_exact(
        eligible_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in eligible)),
    )
    _write_exact(
        selected_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in selected)),
    )
    _write_exact(
        screened_path,
        _jsonl_bytes(
            tuple(
                item.model_dump(mode="json")
                for item in sorted(screened_records, key=lambda value: value.theorem_id)
            )
        ),
    )
    ordered_outcomes = tuple(outcomes[key] for key in sorted(outcomes))
    _write_exact(
        outcomes_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in ordered_outcomes)),
    )
    _write_exact(
        shortfall_path,
        _json_bytes(
            {
                "schema_version": 1,
                "target_distinct_ancestry_groups": target,
                "eligible_distinct_ancestry_groups": len(eligible),
                "selected_distinct_ancestry_groups": len(selected),
                "shortfall": shortfall,
                "target_met": len(eligible) >= target,
                "candidate_source_records_only": True,
                "self_containedness_status": "unreviewed",
                "problem_pool_admitted": False,
                "model_collection_authorized": False,
                "shortfall_reason": (
                    None
                    if shortfall == 0
                    else (
                        "frozen source has fewer eligible distinct groups "
                        "after fail-closed screening"
                    )
                ),
            }
        ),
    )

    report_path = root / (ONE_EXAMPLE_REPORT if profile == "one_example" else FULL_REPORT)
    output_artifacts = {
        "eligible_candidates": _artifact(eligible_path, root=root),
        "selected_candidates": _artifact(selected_path, root=root),
        "screened_candidates": _artifact(screened_path, root=root),
        "terminal_outcomes": _artifact(outcomes_path, root=root),
        "shortfall_report": _artifact(shortfall_path, root=root),
    }
    input_artifacts = {
        "config": ArtifactHash(path=str(CONFIG_PATH), sha256=config_hash),
        "theorem_manifest": ArtifactHash(
            path=config.source.theorem_manifest.path,
            sha256=config.source.theorem_manifest.sha256,
        ),
        "theorem_records": ArtifactHash(
            path=config.source.theorem_records.path,
            sha256=config.source.theorem_records.sha256,
        ),
        "representation_records": ArtifactHash(
            path=config.source.representation_records.path,
            sha256=config.source.representation_records.sha256,
        ),
        "active_registry_manifest": _artifact(registry_manifest_path, root=root),
        "module": _artifact(Path(__file__), root=root),
    }
    counts = Counter(item.outcome.value for item in ordered_outcomes)
    manifest = Gate3DocstringPoolManifest(
        artifact_kind="gate3_mathlib_adjacent_docstring_candidate_manifest",
        profile=profile,
        frozen_at=config.frozen_at,
        config_id=config.config_id,
        source_revision=config.source.checkout_revision,
        context_id=config.source.context_id,
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        attempted_mathlib_records=len(theorems),
        adjacent_docstring_records=adjacent_count,
        screen_clear_records=screen_clear_count,
        temporally_clean_records_before_dedup=len(drafts),
        eligible_distinct_ancestry_groups=len(eligible),
        target_distinct_ancestry_groups=target,
        selected_distinct_ancestry_groups=len(selected),
        shortfall=shortfall,
        terminal_outcome_counts=dict(sorted(counts.items())),
        first_candidate_id=selected[0].candidate_id,
        selected_candidate_ids=tuple(item.candidate_id for item in selected),
        one_example_report=prior_report,
    )
    _write_exact(manifest_path, _json_bytes(manifest.model_dump(mode="json")))
    manifest_artifact = _artifact(manifest_path, root=root)
    target_met = len(eligible) >= target
    passed = bool(selected) and (profile == "one_example" or target_met)
    blockers = (
        ()
        if passed
        else (f"eligible distinct ancestry groups {len(eligible)} are below target {target}",)
    )
    report = Gate3DocstringPoolReport(
        report_kind="lf021_gate3_mathlib_docstring_pool_preflight",
        profile=profile,
        passed=passed,
        manifest=manifest_artifact,
        selected_candidates=output_artifacts["selected_candidates"],
        screened_candidates=output_artifacts["screened_candidates"],
        eligible_candidates=output_artifacts["eligible_candidates"],
        outcomes=output_artifacts["terminal_outcomes"],
        shortfall_report=output_artifacts["shortfall_report"],
        selected_count=len(selected),
        eligible_distinct_ancestry_groups=len(eligible),
        target_distinct_ancestry_groups=target,
        shortfall=shortfall,
        blockers=blockers,
    )
    _write_exact(report_path, _json_bytes(report.model_dump(mode="json")))
    return Gate3DocstringPoolRun(
        report_path=report_path,
        manifest_path=manifest_path,
        report=report,
        manifest=manifest,
    )


__all__ = [
    "CONFIG_PATH",
    "DEFAULT_OUTPUT_ROOT",
    "FULL_REPORT",
    "ONE_EXAMPLE_REPORT",
    "AdjacentDocstring",
    "CandidateOutcomeCode",
    "Gate3DocstringOutcome",
    "Gate3DocstringPoolConfig",
    "Gate3DocstringPoolError",
    "Gate3DocstringPoolManifest",
    "Gate3DocstringPoolReport",
    "Gate3DocstringPoolRun",
    "Gate3MathlibDocstringCandidate",
    "extract_adjacent_docstring",
    "run_gate3_docstring_pool",
    "select_distinct_candidates",
]

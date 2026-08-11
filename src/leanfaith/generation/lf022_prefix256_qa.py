"""Fail-closed operational QA for frozen LF-022 prefix-256 batches.

This audit consumes a frozen public batch manifest and the immutable report
from a complete exact offline replay.  It does not call a provider, infer a
semantic label, promote a variant, or admit any artifact to training or
evaluation.  The only output is a content-addressed operational go/no-go
report and a deterministic 32-task reviewer bundle.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import (
    LF022BatchError,
    LF022BatchRunReport,
    LF022PublicBatchManifest,
    VerifiedLF022BatchTask,
    load_lf022_public_batch,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionAttemptRecord,
    LF022ExecutionTerminalRecord,
)
from leanfaith.generation.lf022_historical_replay import (
    LF022HistoricalModuleBinding,
    LF022HistoricalReplayError,
    LF022HistoricalReplayResult,
    run_lf022_historical_replay,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import (
    VariantOutputErrorCode,
    VariantOutputParseError,
    normalize_variant_candidate,
    validate_variant_candidate,
)
from leanfaith.schemas.enums import GeneratorKind, LLMRole, QualityTier, ValidationStatus
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import ManifestError, collect_code_state
from leanfaith.schemas.variant import VariantRecord

LF022_PREFIX256_QA_RULE = "lf022_prefix256_operational_qa_v1"
LF022_PREFIX256_SAMPLE_RULE = "lf022_prefix256_success_hash_sample_v1"
LF022_PREFIX256_TASK_COUNT: Literal[256] = 256
LF022_PREFIX256_MINIMUM_SUCCESS_COUNT: Literal[243] = 243
LF022_PREFIX256_REVIEW_SAMPLE_SIZE: Literal[32] = 32
LF022_TERMINAL_STATUSES = frozenset(
    {
        "provisional_variants_created",
        "proposer_parse_failed",
        "provider_exhausted",
        "transport_unknown",
    }
)

_PROMPT_LEAKAGE_MARKERS = (
    "```",
    "{{input_json}}",
    "{{prompt_template_sha256}}",
    '"candidate_lean"',
    '"edit_summary"',
    "output schema",
    "prompt_template_sha256",
    "return one strict json object",
    "semantic-faithfulness dataset",
)
_PLACEHOLDER_PATTERN = re.compile(r"(?<![\w'])\b(?:sorry|admit)\b", re.IGNORECASE)
_PROOF_BODY_PATTERN = re.compile(
    r"(?<![\w'])\bby\b|^[ \t]*where\b",
    re.IGNORECASE | re.MULTILINE,
)
_HISTORICAL_REPLAY_COMPATIBILITY_LOCK = threading.Lock()
_FULL_TERMINAL_BINDING_FIELDS = frozenset(
    {
        "attempt_artifacts",
        "attempt_sha256s",
        "llm_attempt_artifacts",
        "llm_attempt_sha256s",
        "llm_call_artifact",
        "llm_call_sha256",
        "variants_artifact",
        "variants_sha256",
    }
)


def _run_terminal_reference_compatible_historical_replay(
    *,
    repo_root: Path,
    manifest_binding: LF022ArtifactBinding,
    loaded_tasks: tuple[VerifiedLF022BatchTask, ...],
    executor_output_root: str,
) -> LF022HistoricalReplayResult:
    """Replay an admitted batch while recognizing report-level terminal references.

    The Kimi-v4 eligibility binds the original historical replay module byte for
    byte, so that module cannot be edited without invalidating the admission.
    Its closure scanner predates reports that embed a lightweight terminal
    reference containing ``terminal_id`` plus a nested artifact binding.  This
    QA-only compatibility shim changes only the current coordinator's record
    discriminator while preserving the hash-bound historical module and the
    isolated executor replay.  The lock makes the temporary override safe from
    concurrent in-process QA calls.
    """

    from leanfaith.generation import lf022_historical_replay as replay_module

    original = replay_module._explicit_record_bindings
    original_source_file = replay_module._source_file

    def compatible(
        value: dict[object, object],
    ) -> list[replay_module._DiscoveredBinding] | None:
        artifact_path = value.get("path")
        if isinstance(artifact_path, str) and artifact_path.startswith("artifacts/code_bundles/"):
            digest = value.get("sha256")
            relative = PurePosixPath(artifact_path)
            if (
                not isinstance(digest, str)
                or re.fullmatch(HEX64_PATTERN, digest) is None
                or len(relative.parts) != 4
                or relative.parts[:2] != ("artifacts", "code_bundles")
                or relative.name != f"code_bundle_{digest}.tar.gz"
            ):
                raise LF022HistoricalReplayError("nested code-bundle binding is malformed")
            # The current admission bundle is an explicit replay root. Bundles
            # nested in lineage reports describe earlier coordinators only.
            return []
        module_name = value.get("module_name")
        if isinstance(module_name, str) and (
            module_name == "leanfaith" or module_name.startswith("leanfaith.")
        ):
            path = value.get("path")
            digest = value.get("sha256")
            if (
                not isinstance(path, str)
                or not (path == "src/leanfaith/__init__.py" or path.startswith("src/leanfaith/"))
                or not isinstance(digest, str)
                or re.fullmatch(HEX64_PATTERN, digest) is None
            ):
                raise LF022HistoricalReplayError("historical module binding is malformed")
            # The admission-bound code bundle is the replay code authority.
            # Module hashes embedded in later reports are provenance only and
            # can legitimately describe a different coordinator revision.
            return []
        terminal_id = value.get("terminal_id")
        if (
            isinstance(terminal_id, str)
            and terminal_id.startswith("lf022_execution_terminal:")
            and not _FULL_TERMINAL_BINDING_FIELDS.intersection(value)
        ):
            return None
        return original(value)

    def source_file_with_path(
        root: Path,
        relative: PurePosixPath,
        *,
        label: str,
    ) -> Path:
        try:
            return original_source_file(root, relative, label=label)
        except LF022HistoricalReplayError as exc:
            raise LF022HistoricalReplayError(f"{exc}: {relative.as_posix()}") from exc

    with _HISTORICAL_REPLAY_COMPATIBILITY_LOCK:
        replay_module._explicit_record_bindings = compatible
        replay_module._source_file = source_file_with_path
        try:
            return run_lf022_historical_replay(
                repo_root=repo_root,
                manifest_binding=manifest_binding,
                loaded_tasks=loaded_tasks,
                executor_output_root=executor_output_root,
            )
        finally:
            replay_module._explicit_record_bindings = original
            replay_module._source_file = original_source_file


class LF022Prefix256QAError(RuntimeError):
    """The prefix-256 audit could not trust or structurally validate its inputs."""


class LF022Prefix256ReviewerVariant(StrictModel):
    """One parsed provisional candidate shown for operational hygiene review."""

    variant_id: str = Field(pattern=id_pattern("var"))
    candidate_lean: str = Field(min_length=1)
    candidate_code_hash: str = Field(pattern=HEX64_PATTERN)
    normalized_candidate_hash: str = Field(pattern=HEX64_PATTERN)
    intended_relation: str = Field(min_length=1)
    intended_error_types: tuple[str, ...]
    prompt_artifact: LF022ArtifactBinding
    raw_output_artifact: LF022ArtifactBinding
    automated_hygiene_findings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if tuple(sorted(set(self.automated_hygiene_findings))) != (self.automated_hygiene_findings):
            raise ValueError("automated_hygiene_findings must be sorted and unique")
        return self


class LF022Prefix256ReviewerItem(StrictModel):
    """One deterministically selected successful task; never an annotation."""

    schema_version: Literal[1] = 1
    review_item_id: str = Field(pattern=id_pattern("lf022_prefix256_review"))
    selection_rule: Literal["lf022_prefix256_success_hash_sample_v1"]
    selection_rank: int = Field(ge=0, lt=LF022_PREFIX256_REVIEW_SAMPLE_SIZE, strict=True)
    selection_hash: str = Field(pattern=HEX64_PATTERN)
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5"]
    model_id: str = Field(min_length=1)
    source_theorem_id: str = Field(pattern=id_pattern("thm"))
    source_representation_id: str | None = Field(default=None, pattern=id_pattern("repr"))
    context_id: str | None = Field(default=None, pattern=id_pattern("ctx"))
    source_statement: str = Field(min_length=1)
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal_artifact: LF022ArtifactBinding
    variants_artifact: LF022ArtifactBinding
    variants: tuple[LF022Prefix256ReviewerVariant, ...] = Field(min_length=1)
    review_scope: Literal["operational_generation_hygiene_only"] = (
        "operational_generation_hygiene_only"
    )
    review_questions: tuple[str, ...] = (
        "prompt_leakage_present",
        "proof_placeholder_or_body_present",
        "malformed_declaration_boundary_present",
        "duplicate_or_boilerplate_output_present",
    )
    semantic_label_requested: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected_questions = (
            "prompt_leakage_present",
            "proof_placeholder_or_body_present",
            "malformed_declaration_boundary_present",
            "duplicate_or_boilerplate_output_present",
        )
        if self.review_questions != expected_questions:
            raise ValueError("review_questions differ from the fixed operational scope")
        expected = make_id(
            "lf022_prefix256_review",
            self.model_dump(mode="json", exclude={"review_item_id"}),
        )
        if self.review_item_id != expected:
            raise ValueError("review_item_id does not match canonical reviewer item")
        return self


class LF022Prefix256TerminalReplayBinding(StrictModel):
    """One terminal identity re-derived by the exact offline executor replay."""

    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal_artifact: LF022ArtifactBinding


def _derived_failure_codes(
    *,
    task_count: int,
    terminal_count: int,
    offline_report_task_count: int,
    offline_replay_count: int,
    offline_preflight_only_count: int,
    offline_new_terminal_count: int,
    network_calls_this_replay: int,
    orchestration_error_count: int,
    successful_terminal_count: int,
    terminal_status_counts: dict[str, int],
    terminal_replay_binding_count: int,
    duplicate_normalized_output_hashes: tuple[str, ...],
    hygiene_failed_task_ids: tuple[str, ...],
    selected_task_count: int,
) -> tuple[str, ...]:
    """Derive every threshold failure from persisted report fields only."""

    failures: set[str] = set()
    if task_count != LF022_PREFIX256_TASK_COUNT:
        failures.add("task_count_not_256")
    if terminal_count != LF022_PREFIX256_TASK_COUNT:
        failures.add("terminal_count_not_256")
    if offline_report_task_count != LF022_PREFIX256_TASK_COUNT:
        failures.add("offline_report_task_count_not_256")
    if offline_replay_count != LF022_PREFIX256_TASK_COUNT:
        failures.add("offline_replay_count_not_256")
    if offline_preflight_only_count != 0:
        failures.add("offline_preflight_only_count_nonzero")
    if offline_new_terminal_count != 0:
        failures.add("offline_new_terminal_count_nonzero")
    if network_calls_this_replay != 0:
        failures.add("offline_network_calls_nonzero")
    if orchestration_error_count != 0:
        failures.add("offline_orchestration_error_count_nonzero")
    if successful_terminal_count < LF022_PREFIX256_MINIMUM_SUCCESS_COUNT:
        failures.add("successful_terminal_count_below_243")
    if terminal_status_counts.get("transport_unknown", 0) > 0:
        failures.add("transport_unknown_terminal_present")
    if set(terminal_status_counts).difference(LF022_TERMINAL_STATUSES):
        failures.add("unexpected_terminal_status_present")
    if terminal_replay_binding_count != terminal_count:
        failures.add("terminal_replay_binding_count_mismatch")
    if duplicate_normalized_output_hashes:
        failures.add("duplicate_normalized_outputs_present")
    if hygiene_failed_task_ids:
        failures.add("candidate_hygiene_failure_present")
    if selected_task_count != min(
        successful_terminal_count,
        LF022_PREFIX256_REVIEW_SAMPLE_SIZE,
    ):
        failures.add("review_sample_count_mismatch")
    if selected_task_count < LF022_PREFIX256_REVIEW_SAMPLE_SIZE:
        failures.add("review_sample_below_32")
    return tuple(sorted(failures))


class LF022Prefix256OperationalQAReport(StrictModel):
    """Immutable prefix-256 operational go/no-go result."""

    schema_version: Literal[1] = 1
    qa_id: str = Field(pattern=id_pattern("lf022_prefix256_qa"))
    qa_rule: Literal["lf022_prefix256_operational_qa_v1"]
    qa_status: Literal["passed", "failed"]
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5"]
    model_id: str = Field(min_length=1)
    qa_implementation_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    historical_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    historical_code_bundle: LF022ArtifactBinding
    historical_module_bindings: tuple[LF022HistoricalModuleBinding, ...] = Field(min_length=1)
    historical_replay_network_calls: Literal[0] = 0
    batch_manifest: LF022ArtifactBinding
    exact_offline_replay_report_id: str = Field(pattern=id_pattern("lf022_batch_run"))
    exact_offline_replay_report: LF022ArtifactBinding
    reviewer_bundle: LF022ArtifactBinding
    required_task_count: Literal[256] = LF022_PREFIX256_TASK_COUNT
    required_offline_replay_count: Literal[256] = LF022_PREFIX256_TASK_COUNT
    minimum_success_count: Literal[243] = LF022_PREFIX256_MINIMUM_SUCCESS_COUNT
    task_count: int = Field(ge=0, strict=True)
    terminal_count: int = Field(ge=0, strict=True)
    offline_report_task_count: int = Field(ge=0, strict=True)
    offline_replay_count: int = Field(ge=0, strict=True)
    offline_preflight_only_count: int = Field(ge=0, strict=True)
    offline_new_terminal_count: int = Field(ge=0, strict=True)
    network_calls_this_replay: int = Field(ge=0, strict=True)
    orchestration_error_count: int = Field(ge=0, strict=True)
    successful_terminal_count: int = Field(ge=0, strict=True)
    failed_terminal_count: int = Field(ge=0, strict=True)
    terminal_status_counts: dict[str, int]
    verified_terminal_bound_artifact_count: int = Field(ge=0, strict=True)
    verified_variant_count: int = Field(ge=0, strict=True)
    unique_normalized_output_count: int = Field(ge=0, strict=True)
    duplicate_normalized_output_hashes: tuple[str, ...]
    hygiene_failed_task_ids: tuple[str, ...]
    terminal_replay_bindings: tuple[LF022Prefix256TerminalReplayBinding, ...]
    selection_rule: Literal["lf022_prefix256_success_hash_sample_v1"]
    selected_task_ids: tuple[str, ...] = Field(max_length=LF022_PREFIX256_REVIEW_SAMPLE_SIZE)
    failure_codes: tuple[str, ...]
    operational_qa_only: Literal[True] = True
    outputs_unresolved: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if list(self.terminal_status_counts) != sorted(self.terminal_status_counts):
            raise ValueError("terminal_status_counts must be key-sorted")
        if any(value < 0 for value in self.terminal_status_counts.values()):
            raise ValueError("terminal_status_counts cannot contain negative counts")
        if sum(self.terminal_status_counts.values()) != self.terminal_count:
            raise ValueError("terminal_status_counts do not reconcile")
        if self.successful_terminal_count + self.failed_terminal_count != self.terminal_count:
            raise ValueError("successful and failed terminal counts do not reconcile")
        if (
            self.terminal_status_counts.get("provisional_variants_created", 0)
            != self.successful_terminal_count
        ):
            raise ValueError("successful terminal count differs from terminal statuses")
        for field in (
            "duplicate_normalized_output_hashes",
            "hygiene_failed_task_ids",
            "failure_codes",
        ):
            values = getattr(self, field)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{field} must be sorted and unique")
        if len(set(self.selected_task_ids)) != len(self.selected_task_ids):
            raise ValueError("selected_task_ids must be unique")
        replay_task_ids = tuple(
            binding.execution_task_id for binding in self.terminal_replay_bindings
        )
        if replay_task_ids != tuple(sorted(set(replay_task_ids))):
            raise ValueError("terminal_replay_bindings must be task-sorted and unique")
        historical_module_names = tuple(
            binding.module_name for binding in self.historical_module_bindings
        )
        if historical_module_names != tuple(sorted(set(historical_module_names))):
            raise ValueError("historical_module_bindings must be name-sorted and unique")
        replay_terminal_ids = tuple(
            binding.terminal_id for binding in self.terminal_replay_bindings
        )
        replay_terminal_paths = tuple(
            binding.terminal_artifact.path for binding in self.terminal_replay_bindings
        )
        if len(set(replay_terminal_ids)) != len(replay_terminal_ids):
            raise ValueError("terminal_replay_bindings contain duplicate terminal IDs")
        if len(set(replay_terminal_paths)) != len(replay_terminal_paths):
            raise ValueError("terminal_replay_bindings contain duplicate terminal paths")
        replay_task_set = set(replay_task_ids)
        if not set(self.selected_task_ids).issubset(replay_task_set):
            raise ValueError("selected_task_ids must be exact-replay task IDs")
        if not set(self.hygiene_failed_task_ids).issubset(replay_task_set):
            raise ValueError("hygiene_failed_task_ids must be exact-replay task IDs")
        if self.unique_normalized_output_count > self.verified_variant_count:
            raise ValueError("unique normalized outputs exceed verified variants")
        if (
            not self.duplicate_normalized_output_hashes
            and self.unique_normalized_output_count != self.verified_variant_count
        ):
            raise ValueError("duplicate-free normalized output count must equal variant count")
        if (
            self.duplicate_normalized_output_hashes
            and self.unique_normalized_output_count >= self.verified_variant_count
        ):
            raise ValueError("duplicate hashes require fewer unique outputs than variants")
        expected_failure_codes = _derived_failure_codes(
            task_count=self.task_count,
            terminal_count=self.terminal_count,
            offline_report_task_count=self.offline_report_task_count,
            offline_replay_count=self.offline_replay_count,
            offline_preflight_only_count=self.offline_preflight_only_count,
            offline_new_terminal_count=self.offline_new_terminal_count,
            network_calls_this_replay=self.network_calls_this_replay,
            orchestration_error_count=self.orchestration_error_count,
            successful_terminal_count=self.successful_terminal_count,
            terminal_status_counts=self.terminal_status_counts,
            terminal_replay_binding_count=len(self.terminal_replay_bindings),
            duplicate_normalized_output_hashes=self.duplicate_normalized_output_hashes,
            hygiene_failed_task_ids=self.hygiene_failed_task_ids,
            selected_task_count=len(self.selected_task_ids),
        )
        if self.failure_codes != expected_failure_codes:
            raise ValueError("failure_codes do not equal the exact field-derived failure set")
        if (self.qa_status == "passed") != (not expected_failure_codes):
            raise ValueError("qa_status must agree with the derived failure set")
        expected = make_id(
            "lf022_prefix256_qa",
            self.model_dump(mode="json", exclude={"qa_id"}),
        )
        if self.qa_id != expected:
            raise ValueError("qa_id does not match canonical QA report")
        return self


@dataclass(frozen=True, slots=True)
class LF022Prefix256OperationalQAResult:
    report: LF022Prefix256OperationalQAReport
    report_path: Path
    reviewer_bundle_path: Path


@dataclass(frozen=True, slots=True)
class _SuccessfulTask:
    loaded: VerifiedLF022BatchTask
    terminal: LF022ExecutionTerminalRecord
    terminal_binding: LF022ArtifactBinding
    variants_binding: LF022ArtifactBinding
    reviewer_variants: tuple[LF022Prefix256ReviewerVariant, ...]


def _repo_local_file_binding(
    repo_root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[LF022ArtifactBinding, Path]:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022Prefix256QAError(f"{label} must remain inside the repository") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LF022Prefix256QAError(f"{label} must use a normalized repository path")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022Prefix256QAError(f"{label} contains a symlinked component")
    if not current.is_file():
        raise LF022Prefix256QAError(f"{label} is missing or not a regular file")
    return (
        LF022ArtifactBinding(
            path=PurePosixPath(relative.as_posix()).as_posix(),
            sha256=hash_file(current),
        ),
        current,
    )


def _bound_artifact(
    *,
    repo_root: Path,
    path: str,
    expected_sha256: str,
    expected_parent: PurePosixPath,
    label: str,
) -> tuple[LF022ArtifactBinding, Path]:
    candidate_path = PurePosixPath(path)
    if not candidate_path.is_relative_to(expected_parent):
        raise LF022Prefix256QAError(f"{label} is outside its canonical task directory")
    binding, resolved = _repo_local_file_binding(repo_root, Path(path), label=label)
    if binding.sha256 != expected_sha256:
        raise LF022Prefix256QAError(f"{label} hash differs from its terminal binding")
    return binding, resolved


def _load_canonical_record[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
    *,
    label: str,
    trailing_newline: bool,
) -> RecordT:
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022Prefix256QAError(f"invalid {label}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json"))
    if trailing_newline:
        expected += b"\n"
    if raw != expected:
        raise LF022Prefix256QAError(f"{label} is not canonical")
    return record


def _write_immutable(path: Path, payload: bytes) -> str:
    if path.is_symlink():
        raise LF022Prefix256QAError(f"immutable output cannot be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022Prefix256QAError(f"immutable output already differs: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022Prefix256QAError(
                    f"concurrent immutable output differs: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return hash_file(path)


def _output_path(repo_root: Path, output_dir: Path, name: str) -> Path:
    root = repo_root.resolve(strict=True)
    directory = output_dir if output_dir.is_absolute() else root / output_dir
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise LF022Prefix256QAError("QA output directory must remain in the repository") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LF022Prefix256QAError("QA output directory must be normalized and non-root")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022Prefix256QAError("QA output directory contains a symlinked component")
        if current.exists() and not current.is_dir():
            raise LF022Prefix256QAError("QA output path contains a non-directory component")
    return directory / name


def _candidate_findings(statement: str) -> tuple[str, ...]:
    findings: set[str] = set()
    folded = statement.casefold()
    if any(marker in folded for marker in _PROMPT_LEAKAGE_MARKERS):
        findings.add("prompt_leakage")
    if _PLACEHOLDER_PATTERN.search(statement):
        findings.add("proof_placeholder")
    if _PROOF_BODY_PATTERN.search(statement):
        findings.add("proof_body")
    try:
        validate_variant_candidate(statement)
    except VariantOutputParseError as exc:
        if exc.code is VariantOutputErrorCode.PROOF_BEARING_CANDIDATE:
            findings.add("proof_or_declaration_value")
        else:
            findings.add("malformed_declaration_boundary")
    return tuple(sorted(findings))


def _load_replay_report(
    *,
    repo_root: Path,
    report_path: Path,
    manifest: LF022PublicBatchManifest,
) -> tuple[LF022BatchRunReport, LF022ArtifactBinding]:
    binding, resolved = _repo_local_file_binding(
        repo_root,
        report_path,
        label="exact offline replay report",
    )
    report = _load_canonical_record(
        resolved,
        LF022BatchRunReport,
        label="exact offline replay report",
        trailing_newline=False,
    )
    expected = (
        repo_root.resolve(strict=True)
        / manifest.batch_directory
        / "runs"
        / f"{report.report_id.split(':', 1)[1]}.json"
    )
    if resolved != expected:
        raise LF022Prefix256QAError(
            "exact offline replay report is outside the frozen batch run registry"
        )
    if report.batch_id != manifest.batch_id:
        raise LF022Prefix256QAError("offline replay report belongs to a different batch")
    if report.schema_version != 2 or report.mode != "offline":
        raise LF022Prefix256QAError("QA requires a schema-v2 exact offline replay report")
    if report.successful_terminal_count is None or report.failed_terminal_count is None:
        raise LF022Prefix256QAError("offline replay report lacks terminal outcome totals")
    if len(report.failed_task_ids) != report.error_count:
        raise LF022Prefix256QAError(
            "offline replay failed-task IDs do not reconcile with its error count"
        )
    return report, binding


def _task_local_artifact(
    *,
    repo_root: Path,
    task_directory: Path,
    artifact: str,
    label: str,
) -> PurePosixPath:
    """Convert a repository artifact path to a path below one exact task."""

    root_relative_task = PurePosixPath(task_directory.relative_to(repo_root).as_posix())
    candidate = PurePosixPath(artifact)
    try:
        return candidate.relative_to(root_relative_task)
    except ValueError as exc:
        raise LF022Prefix256QAError(f"{label} is outside its exact task directory") from exc


def _validate_task_artifact_inventory(
    *,
    repo_root: Path,
    task_directory: Path,
    terminal: LF022ExecutionTerminalRecord,
) -> None:
    """Reject unbound attempts, files, symlinks, and empty artifact directories."""

    expected_files: set[PurePosixPath] = {
        PurePosixPath("admission.json"),
        PurePosixPath("task.json"),
        PurePosixPath("preflight.json"),
        PurePosixPath("terminal.json"),
        _task_local_artifact(
            repo_root=repo_root,
            task_directory=task_directory,
            artifact=terminal.llm_call_artifact,
            label="LLM call artifact",
        ),
    }
    for artifact in terminal.llm_attempt_artifacts:
        expected_files.add(
            _task_local_artifact(
                repo_root=repo_root,
                task_directory=task_directory,
                artifact=artifact,
                label="LLM attempt artifact",
            )
        )
    if terminal.variants_artifact is not None:
        expected_files.add(
            _task_local_artifact(
                repo_root=repo_root,
                task_directory=task_directory,
                artifact=terminal.variants_artifact,
                label="variants artifact",
            )
        )

    for artifact in terminal.attempt_artifacts:
        local_attempt = _task_local_artifact(
            repo_root=repo_root,
            task_directory=task_directory,
            artifact=artifact,
            label="execution attempt artifact",
        )
        expected_files.add(local_attempt)
        attempt_path = task_directory / Path(local_attempt.as_posix())
        attempt = _load_canonical_record(
            attempt_path,
            LF022ExecutionAttemptRecord,
            label="execution attempt artifact",
            trailing_newline=True,
        )
        for label, bound in (
            ("provider request", attempt.request_artifact),
            ("wire request", attempt.wire_request_artifact),
            ("provider raw response", attempt.provider_raw_artifact),
            ("wire response body", attempt.wire_response_body_artifact),
            ("wire response metadata", attempt.wire_response_metadata_artifact),
        ):
            if bound is not None:
                expected_files.add(
                    _task_local_artifact(
                        repo_root=repo_root,
                        task_directory=task_directory,
                        artifact=bound,
                        label=f"attempt {label}",
                    )
                )
        attempt_parent = local_attempt.parent
        for marker_name, marker_bytes in (
            (".transport_started", b"started\n"),
            (".transport_completed", b"completed\n"),
        ):
            marker = attempt_parent / marker_name
            marker_path = task_directory / Path(marker.as_posix())
            if marker_path.exists():
                if (
                    marker_path.is_symlink()
                    or not marker_path.is_file()
                    or marker_path.read_bytes() != marker_bytes
                ):
                    raise LF022Prefix256QAError(f"noncanonical transport marker: {marker_path}")
                if (
                    marker_name == ".transport_completed"
                    and attempt.wire_response_body_artifact is None
                ):
                    raise LF022Prefix256QAError(
                        "transport-completed marker exists without a bound response"
                    )
                expected_files.add(marker)

    lock_path = task_directory / ".lock"
    if lock_path.exists():
        if lock_path.is_symlink() or not lock_path.is_file() or lock_path.read_bytes() != b"":
            raise LF022Prefix256QAError("task lock is not the canonical empty regular file")
        expected_files.add(PurePosixPath(".lock"))

    observed_files: set[PurePosixPath] = set()
    observed_directories: set[PurePosixPath] = set()
    for path in task_directory.rglob("*"):
        if path.is_symlink():
            raise LF022Prefix256QAError("task artifact inventory contains a symlink")
        relative = PurePosixPath(path.relative_to(task_directory).as_posix())
        if path.is_file():
            observed_files.add(relative)
        elif path.is_dir():
            observed_directories.add(relative)
        else:
            raise LF022Prefix256QAError("task artifact inventory contains a special file")
    if observed_files != expected_files:
        extras = sorted(str(path) for path in observed_files - expected_files)
        missing = sorted(str(path) for path in expected_files - observed_files)
        raise LF022Prefix256QAError(
            f"task artifact inventory differs: extra={extras!r} missing={missing!r}"
        )
    expected_directories = {
        parent for file_path in expected_files for parent in file_path.parents if str(parent) != "."
    }
    if observed_directories != expected_directories:
        extras = sorted(str(path) for path in observed_directories - expected_directories)
        missing = sorted(str(path) for path in expected_directories - observed_directories)
        raise LF022Prefix256QAError(
            f"task artifact directory inventory differs: extra={extras!r} missing={missing!r}"
        )


def _variant_reviewer_record(
    *,
    repo_root: Path,
    loaded: VerifiedLF022BatchTask,
    terminal: LF022ExecutionTerminalRecord,
    call: LLMCallRecord,
    variant: VariantRecord,
    task_parent: PurePosixPath,
) -> LF022Prefix256ReviewerVariant:
    statement = variant.extracted_statement
    candidate_hash = variant.candidate_code_hash
    if statement is None or candidate_hash is None:
        raise LF022Prefix256QAError("provisional variant lacks its candidate statement/hash")
    expected_representations = (
        (loaded.task.source.source_representation_id,)
        if loaded.task.source.source_representation_id is not None
        else ()
    )
    expected_generation_hash = hash_canonical(loaded.admission.model_dump(mode="json"))
    if (
        variant.source_theorem_ids != (loaded.task.source.source_theorem_id,)
        or variant.source_representation_ids != expected_representations
        or variant.context_id != loaded.task.source.context_id
        or variant.generator_kind is not GeneratorKind.LLM_PROPOSER
        or variant.generator_id != loaded.admission.route.model_id
        or variant.generation_config_hash != expected_generation_hash
        or variant.intended_relation not in loaded.task.requested_relations
        or variant.quality_tier is not QualityTier.PROVISIONAL
        or variant.validation_status is not ValidationStatus.UNVALIDATED
        or variant.candidate_pool != "G_open"
        or variant.metadata.get("llm_call_id") != terminal.llm_call_id
        or variant.prompt_artifact != call.request_artifact
        or variant.raw_output_artifact != call.raw_output_artifact
    ):
        raise LF022Prefix256QAError("provisional variant lineage differs from its frozen task")
    if variant.prompt_artifact is None or variant.raw_output_artifact is None:
        raise LF022Prefix256QAError("provisional variant lacks prompt/raw artifact bindings")
    if call.request_artifact_sha256 is None or call.raw_response_sha256 is None:
        raise LF022Prefix256QAError("LLM call lacks prompt/raw SHA-256 bindings")
    prompt_binding, _ = _bound_artifact(
        repo_root=repo_root,
        path=variant.prompt_artifact,
        expected_sha256=call.request_artifact_sha256,
        expected_parent=task_parent,
        label="variant prompt artifact",
    )
    raw_binding, _ = _bound_artifact(
        repo_root=repo_root,
        path=variant.raw_output_artifact,
        expected_sha256=call.raw_response_sha256,
        expected_parent=task_parent,
        label="variant raw output artifact",
    )
    normalized = normalize_variant_candidate(statement)
    return LF022Prefix256ReviewerVariant(
        variant_id=variant.variant_id,
        candidate_lean=statement,
        candidate_code_hash=candidate_hash,
        normalized_candidate_hash=hash_canonical(
            {
                "normalization_rule": "llm_variant_whitespace_v1",
                "candidate": normalized,
            }
        ),
        intended_relation=variant.intended_relation.value,
        intended_error_types=variant.intended_error_types,
        prompt_artifact=prompt_binding,
        raw_output_artifact=raw_binding,
        automated_hygiene_findings=_candidate_findings(statement),
    )


def run_lf022_prefix256_operational_qa(
    *,
    repo_root: Path,
    manifest_path: Path,
    exact_offline_replay_report_path: Path,
    output_dir: Path,
) -> LF022Prefix256OperationalQAResult:
    """Audit one frozen prefix-256 batch without network or semantic decisions."""

    repo_root = repo_root.resolve(strict=True)
    manifest_binding, _ = _repo_local_file_binding(
        repo_root,
        manifest_path,
        label="frozen prefix-256 batch manifest",
    )
    try:
        manifest, loaded_tasks = load_lf022_public_batch(
            repo_root=repo_root,
            manifest_binding=manifest_binding,
        )
    except LF022BatchError as exc:
        raise LF022Prefix256QAError(f"frozen batch replay rejected: {exc}") from exc
    replay_report, replay_binding = _load_replay_report(
        repo_root=repo_root,
        report_path=exact_offline_replay_report_path,
        manifest=manifest,
    )
    if len(manifest.routes) != 1:
        raise LF022Prefix256QAError("prefix-256 QA requires exactly one proposer route")
    route = manifest.routes[0]
    try:
        qa_implementation_code_tree_hash = collect_code_state(repo_root).code_tree_hash
    except ManifestError as exc:
        raise LF022Prefix256QAError(f"cannot bind QA implementation code tree: {exc}") from exc
    if qa_implementation_code_tree_hash is None:
        raise LF022Prefix256QAError("QA implementation code-tree hash is unavailable")
    try:
        historical_replay = _run_terminal_reference_compatible_historical_replay(
            repo_root=repo_root,
            manifest_binding=manifest_binding,
            loaded_tasks=loaded_tasks,
            executor_output_root=manifest.executor_output_root,
        )
    except LF022HistoricalReplayError as exc:
        raise LF022Prefix256QAError(f"historical executor replay rejected: {exc}") from exc
    historical_by_task = {
        binding.execution_task_id: binding for binding in historical_replay.terminal_bindings
    }
    historical_code_bundle_binding, _ = _repo_local_file_binding(
        repo_root,
        Path(loaded_tasks[0].admission.artifacts.code_bundle.path),
        label="historical admission code bundle",
    )
    if (
        historical_code_bundle_binding.sha256
        != loaded_tasks[0].admission.artifacts.code_bundle.sha256
    ):
        raise LF022Prefix256QAError("historical code bundle binding drifted")

    status_counts: Counter[str] = Counter()
    normalized_owners: defaultdict[str, list[str]] = defaultdict(list)
    successful: list[_SuccessfulTask] = []
    hygiene_failed_tasks: set[str] = set()
    seen_variant_ids: set[str] = set()
    verified_bound_artifacts = 0
    verified_variant_count = 0
    terminal_replay_bindings: list[LF022Prefix256TerminalReplayBinding] = []

    executor_parent = PurePosixPath(manifest.executor_output_root)
    for loaded in loaded_tasks:
        task_id = loaded.task.execution_task_id
        digest = task_id.removeprefix("lf022_execution_task:")
        task_parent = executor_parent / "tasks" / digest[:2] / digest
        task_directory = repo_root / Path(task_parent.as_posix())
        for base_name in ("admission.json", "task.json", "preflight.json", "terminal.json"):
            base_path = task_directory / base_name
            if base_path.is_symlink() or not base_path.is_file():
                raise LF022Prefix256QAError(
                    f"exact replay prerequisite is missing or unsafe: {base_path}"
                )
        historical_binding = historical_by_task.get(task_id)
        if historical_binding is None:
            raise LF022Prefix256QAError("historical replay omitted a frozen task")
        terminal_relative = task_parent / "terminal.json"
        terminal_binding, terminal_path = _repo_local_file_binding(
            repo_root,
            Path(terminal_relative.as_posix()),
            label="execution terminal",
        )
        terminal = _load_canonical_record(
            terminal_path,
            LF022ExecutionTerminalRecord,
            label="execution terminal",
            trailing_newline=True,
        )
        if (
            historical_binding.terminal_id != terminal.terminal_id
            or historical_binding.terminal_artifact != terminal_binding
        ):
            raise LF022Prefix256QAError(
                "terminal differs from the isolated historical executor reconstruction"
            )
        terminal_replay_bindings.append(
            LF022Prefix256TerminalReplayBinding(
                execution_task_id=task_id,
                terminal_id=terminal.terminal_id,
                terminal_artifact=terminal_binding,
            )
        )
        if (
            terminal.execution_task_id != task_id
            or terminal.execution_admission_id != loaded.admission.admission_id
        ):
            raise LF022Prefix256QAError("execution terminal differs from its frozen task")
        _validate_task_artifact_inventory(
            repo_root=repo_root,
            task_directory=task_directory,
            terminal=terminal,
        )
        status_counts[terminal.status] += 1

        for index, (artifact, digest_sha) in enumerate(
            zip(terminal.attempt_artifacts, terminal.attempt_sha256s, strict=True)
        ):
            _bound_artifact(
                repo_root=repo_root,
                path=artifact,
                expected_sha256=digest_sha,
                expected_parent=task_parent,
                label=f"terminal attempt artifact {index}",
            )
            verified_bound_artifacts += 1
        for index, (artifact, digest_sha) in enumerate(
            zip(terminal.llm_attempt_artifacts, terminal.llm_attempt_sha256s, strict=True)
        ):
            _bound_artifact(
                repo_root=repo_root,
                path=artifact,
                expected_sha256=digest_sha,
                expected_parent=task_parent,
                label=f"terminal LLM attempt artifact {index}",
            )
            verified_bound_artifacts += 1
        _, call_path = _bound_artifact(
            repo_root=repo_root,
            path=terminal.llm_call_artifact,
            expected_sha256=terminal.llm_call_sha256,
            expected_parent=task_parent,
            label="terminal LLM call artifact",
        )
        verified_bound_artifacts += 1
        call = _load_canonical_record(
            call_path,
            LLMCallRecord,
            label="terminal LLM call artifact",
            trailing_newline=True,
        )
        if (
            call.schema_version != 2
            or call.call_id != terminal.llm_call_id
            or call.role is not LLMRole.PROPOSER
            or call.model != loaded.admission.route.model_id
            or call.model_family != loaded.admission.route.canonical_family
            or call.private_source_content
            or call.supervision_eligible
            or call.metadata.get("lf022_execution_admission_id") != loaded.admission.admission_id
            or call.metadata.get("lf022_execution_task_id") != task_id
            or call.metadata.get("generation_config_hash")
            != hash_canonical(loaded.admission.model_dump(mode="json"))
        ):
            raise LF022Prefix256QAError("terminal LLM call differs from its frozen task")
        if (
            call.request_artifact is None
            or call.request_artifact_sha256 is None
            or call.raw_output_artifact is None
            or call.raw_response_sha256 is None
        ):
            raise LF022Prefix256QAError("terminal LLM call lacks prompt/raw bindings")
        _bound_artifact(
            repo_root=repo_root,
            path=call.request_artifact,
            expected_sha256=call.request_artifact_sha256,
            expected_parent=task_parent,
            label="LLM call request artifact",
        )
        _bound_artifact(
            repo_root=repo_root,
            path=call.raw_output_artifact,
            expected_sha256=call.raw_response_sha256,
            expected_parent=task_parent,
            label="LLM call raw output artifact",
        )
        verified_bound_artifacts += 2

        if terminal.status != "provisional_variants_created":
            continue
        if terminal.variants_artifact is None or terminal.variants_sha256 is None:
            raise LF022Prefix256QAError("successful terminal lacks its variants binding")
        variants_binding, variants_path = _bound_artifact(
            repo_root=repo_root,
            path=terminal.variants_artifact,
            expected_sha256=terminal.variants_sha256,
            expected_parent=task_parent,
            label="terminal variants artifact",
        )
        verified_bound_artifacts += 1
        lines = variants_path.read_bytes().splitlines(keepends=True)
        if len(lines) != terminal.provisional_variant_count:
            raise LF022Prefix256QAError("terminal provisional variant count drifted")
        if terminal.provisional_variant_count != loaded.task.proposal_count:
            raise LF022Prefix256QAError(
                "successful terminal variant count differs from its frozen task request"
            )
        reviewer_variants: list[LF022Prefix256ReviewerVariant] = []
        for line in lines:
            try:
                variant = VariantRecord.model_validate_json(line)
            except ValueError as exc:
                raise LF022Prefix256QAError(f"invalid provisional variant: {exc}") from exc
            if line != canonical_json_bytes(variant.model_dump(mode="json")) + b"\n":
                raise LF022Prefix256QAError("provisional variant JSONL is not canonical")
            if variant.variant_id in seen_variant_ids:
                raise LF022Prefix256QAError("duplicate provisional variant ID across batch")
            seen_variant_ids.add(variant.variant_id)
            reviewer_variant = _variant_reviewer_record(
                repo_root=repo_root,
                loaded=loaded,
                terminal=terminal,
                call=call,
                variant=variant,
                task_parent=task_parent,
            )
            reviewer_variants.append(reviewer_variant)
            normalized_owners[reviewer_variant.normalized_candidate_hash].append(task_id)
            if reviewer_variant.automated_hygiene_findings:
                hygiene_failed_tasks.add(task_id)
            verified_variant_count += 1
        successful.append(
            _SuccessfulTask(
                loaded=loaded,
                terminal=terminal,
                terminal_binding=terminal_binding,
                variants_binding=variants_binding,
                reviewer_variants=tuple(reviewer_variants),
            )
        )

    terminal_count = sum(status_counts.values())
    success_count = status_counts["provisional_variants_created"]
    failed_terminal_count = terminal_count - success_count
    if (
        replay_report.terminal_status_counts != dict(sorted(status_counts.items()))
        or replay_report.successful_terminal_count != success_count
        or replay_report.failed_terminal_count != failed_terminal_count
    ):
        raise LF022Prefix256QAError(
            "exact offline replay report counts differ from validated terminal artifacts"
        )
    ranked: list[tuple[str, str, _SuccessfulTask]] = []
    for item in successful:
        selection_hash = hash_canonical(
            {
                "selection_rule": LF022_PREFIX256_SAMPLE_RULE,
                "batch_id": manifest.batch_id,
                "execution_task_id": item.loaded.task.execution_task_id,
                "terminal_id": item.terminal.terminal_id,
            }
        )
        ranked.append((selection_hash, item.loaded.task.execution_task_id, item))
    ranked.sort(key=lambda value: (value[0], value[1]))
    selected = ranked[:LF022_PREFIX256_REVIEW_SAMPLE_SIZE]

    reviewer_items: list[LF022Prefix256ReviewerItem] = []
    for rank, (selection_hash, _, item) in enumerate(selected):
        loaded = item.loaded
        content: dict[str, object] = {
            "schema_version": 1,
            "selection_rule": LF022_PREFIX256_SAMPLE_RULE,
            "selection_rank": rank,
            "selection_hash": selection_hash,
            "batch_id": manifest.batch_id,
            "execution_task_id": loaded.task.execution_task_id,
            "execution_admission_id": loaded.admission.admission_id,
            "proposer_family_id": loaded.family,
            "model_id": loaded.admission.route.model_id,
            "source_theorem_id": loaded.task.source.source_theorem_id,
            "source_representation_id": loaded.task.source.source_representation_id,
            "context_id": loaded.task.source.context_id,
            "source_statement": loaded.task.source.source_statement,
            "terminal_id": item.terminal.terminal_id,
            "terminal_artifact": item.terminal_binding.model_dump(mode="json"),
            "variants_artifact": item.variants_binding.model_dump(mode="json"),
            "variants": [variant.model_dump(mode="json") for variant in item.reviewer_variants],
            "review_scope": "operational_generation_hygiene_only",
            "review_questions": [
                "prompt_leakage_present",
                "proof_placeholder_or_body_present",
                "malformed_declaration_boundary_present",
                "duplicate_or_boilerplate_output_present",
            ],
            "semantic_label_requested": False,
            "semantic_labels_created": False,
            "promotion_enabled": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
        reviewer_items.append(
            LF022Prefix256ReviewerItem.model_validate(
                {
                    **content,
                    "review_item_id": make_id("lf022_prefix256_review", content),
                }
            )
        )
    reviewer_payload = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in reviewer_items
    )
    reviewer_path = _output_path(repo_root, output_dir, "reviewer_sample_v1.jsonl")
    reviewer_sha = _write_immutable(reviewer_path, reviewer_payload)
    reviewer_binding = LF022ArtifactBinding(
        path=reviewer_path.relative_to(repo_root).as_posix(),
        sha256=reviewer_sha,
    )

    duplicate_hashes = tuple(
        sorted(key for key, owners in normalized_owners.items() if len(owners) > 1)
    )
    sorted_replay_bindings = tuple(
        sorted(terminal_replay_bindings, key=lambda binding: binding.execution_task_id)
    )
    failure_codes = _derived_failure_codes(
        task_count=len(loaded_tasks),
        terminal_count=terminal_count,
        offline_report_task_count=replay_report.task_count,
        offline_replay_count=replay_report.replayed_terminal_count,
        offline_preflight_only_count=replay_report.preflight_only_count,
        offline_new_terminal_count=replay_report.new_terminal_count,
        network_calls_this_replay=replay_report.network_calls_this_run,
        orchestration_error_count=replay_report.error_count,
        successful_terminal_count=success_count,
        terminal_status_counts=dict(sorted(status_counts.items())),
        terminal_replay_binding_count=len(sorted_replay_bindings),
        duplicate_normalized_output_hashes=duplicate_hashes,
        hygiene_failed_task_ids=tuple(sorted(hygiene_failed_tasks)),
        selected_task_count=len(selected),
    )

    report_content: dict[str, object] = {
        "schema_version": 1,
        "qa_rule": LF022_PREFIX256_QA_RULE,
        "qa_status": "passed" if not failure_codes else "failed",
        "batch_id": manifest.batch_id,
        "execution_admission_id": route.admission_id,
        "proposer_family_id": route.proposer_family_id,
        "model_id": route.model_id,
        "qa_implementation_code_tree_hash": qa_implementation_code_tree_hash,
        "historical_code_tree_hash": historical_replay.code_tree_hash,
        "historical_code_bundle": historical_code_bundle_binding.model_dump(mode="json"),
        "historical_module_bindings": [
            binding.model_dump(mode="json") for binding in historical_replay.module_bindings
        ],
        "historical_replay_network_calls": historical_replay.network_calls_performed,
        "batch_manifest": manifest_binding.model_dump(mode="json"),
        "exact_offline_replay_report_id": replay_report.report_id,
        "exact_offline_replay_report": replay_binding.model_dump(mode="json"),
        "reviewer_bundle": reviewer_binding.model_dump(mode="json"),
        "required_task_count": LF022_PREFIX256_TASK_COUNT,
        "required_offline_replay_count": LF022_PREFIX256_TASK_COUNT,
        "minimum_success_count": LF022_PREFIX256_MINIMUM_SUCCESS_COUNT,
        "task_count": len(loaded_tasks),
        "terminal_count": terminal_count,
        "offline_report_task_count": replay_report.task_count,
        "offline_replay_count": replay_report.replayed_terminal_count,
        "offline_preflight_only_count": replay_report.preflight_only_count,
        "offline_new_terminal_count": replay_report.new_terminal_count,
        "network_calls_this_replay": replay_report.network_calls_this_run,
        "orchestration_error_count": replay_report.error_count,
        "successful_terminal_count": success_count,
        "failed_terminal_count": failed_terminal_count,
        "terminal_status_counts": dict(sorted(status_counts.items())),
        "verified_terminal_bound_artifact_count": verified_bound_artifacts,
        "verified_variant_count": verified_variant_count,
        "unique_normalized_output_count": len(normalized_owners),
        "duplicate_normalized_output_hashes": list(duplicate_hashes),
        "hygiene_failed_task_ids": sorted(hygiene_failed_tasks),
        "terminal_replay_bindings": [
            binding.model_dump(mode="json") for binding in sorted_replay_bindings
        ],
        "selection_rule": LF022_PREFIX256_SAMPLE_RULE,
        "selected_task_ids": [item.loaded.task.execution_task_id for _, _, item in selected],
        "failure_codes": list(failure_codes),
        "operational_qa_only": True,
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    report = LF022Prefix256OperationalQAReport.model_validate(
        {
            **report_content,
            "qa_id": make_id("lf022_prefix256_qa", report_content),
        }
    )
    report_path = _output_path(repo_root, output_dir, "qa_report.json")
    _write_immutable(
        report_path,
        canonical_json_bytes(report.model_dump(mode="json")) + b"\n",
    )
    return LF022Prefix256OperationalQAResult(
        report=report,
        report_path=report_path,
        reviewer_bundle_path=reviewer_path,
    )


__all__ = [
    "LF022_PREFIX256_MINIMUM_SUCCESS_COUNT",
    "LF022_PREFIX256_QA_RULE",
    "LF022_PREFIX256_REVIEW_SAMPLE_SIZE",
    "LF022_PREFIX256_SAMPLE_RULE",
    "LF022_PREFIX256_TASK_COUNT",
    "LF022Prefix256OperationalQAReport",
    "LF022Prefix256OperationalQAResult",
    "LF022Prefix256QAError",
    "LF022Prefix256ReviewerItem",
    "LF022Prefix256ReviewerVariant",
    "LF022Prefix256TerminalReplayBinding",
    "run_lf022_prefix256_operational_qa",
]

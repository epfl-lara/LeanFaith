"""Resumable proof-certified Wave 5 generation over compiler-data roots.

The module deliberately keeps the large, cheap phase separate from Lean.  It
selects a stable bounded subset of the content-addressed compiler inventory,
requires the completed 1,000-root compiler-context audit, and only then sends
exact reconstructed sources through the shared typed Wave 3/Wave 4 hook.

Every deterministic root terminal is cached by its complete semantic/source
identity.  Release shards are independent transactions: three-field model
rows, keyed sidecars, and logical closure groups are written first and a
content-bound shard receipt is written last.  A later interruption therefore
cannot invalidate an earlier completed shard.  Each completed shard also gets
a durable publication-queue item; ``publish-pending`` commits only those exact
files to a fresh additive private prefix and writes its revision receipt only
after remote verification.  ``recover-publication`` verifies an immutable Hub
tree and its parent without re-uploading after a post-commit timeout.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast

import yaml

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import RetryPolicy, run_batch_with_retries
from leanfaith.representations import goal_v1 as goal_v1_module
from leanfaith.representations.goal_v1 import GoalV1Error
from leanfaith.sft1.sprint import compiler_inventory as inventory_module
from leanfaith.sft1.sprint import compiler_replay as replay_module
from leanfaith.sft1.sprint import engine as engine_module
from leanfaith.sft1.sprint import orbit as orbit_module
from leanfaith.sft1.sprint import screens as screens_module
from leanfaith.sft1.sprint import shortcut as shortcut_module
from leanfaith.sft1.sprint import square as square_module
from leanfaith.sft1.sprint.compiler_inventory import (
    iter_inventory_records,
    load_pinned_input_shards,
)
from leanfaith.sft1.sprint.compiler_replay import (
    CompilerAuditRunner,
    CompilerAuditSettings,
    CompilerAuditSource,
    CompilerReplayError,
    CompilerTypedHookSpec,
    CompilerTypedWave4Selection,
    build_typed_descriptor_batch_request,
    build_typed_wave4_selected_batch_request,
    parse_typed_descriptor_batch_payloads,
    resolve_audit_sources,
    validate_typed_wave4_selected_batch_result,
)
from leanfaith.sft1.sprint.engine import OPERATIONS
from leanfaith.sft1.sprint.orbit import OrbitError, OrbitPolicy
from leanfaith.sft1.sprint.screens import GoldBlocklist, residue_violation
from leanfaith.sft1.sprint.square import (
    SQUARE_OPERATIONS,
    Wave4ClosureMaterialization,
    Wave4Runner,
    load_wave4_config,
    materialize_wave4_records,
    preselect_wave4_variant_descriptors,
    select_wave4_release_groups,
)
from leanfaith.sft1.sprint.store import Journal, StoreError, read_json_object, write_atomic
from leanfaith.sft1.sprint.views import wave3_pair_delta

SCALE_SCHEMA_VERSION = "sft1_wave5_compiler_scale_v1"
SCALE_RUN_SPEC_VERSION = "sft1_wave5_compiler_scale_run_v1"
SCALE_CACHE_VERSION = "sft1_wave5_compiler_root_cache_v1"
SCALE_SELECTION_VERSION = "sft1_wave5_compiler_root_selection_v1"
SCALE_SHARD_VERSION = "sft1_wave5_compiler_release_shard_v1"
SCALE_REPLAY_VERSION = "sft1_wave5_compiler_scale_replay_v1"
SCALE_TERMINAL_VERSION = "sft1_wave5_compiler_scale_terminal_v1"
SCALE_MIGRATION_VERSION = "sft1_wave5_compiler_scale_migration_required_v1"
SCALE_MILESTONE_VERSION = "sft1_wave5_compiler_scale_milestone_v1"
SCALE_METADATA_PUBLICATION_VERSION = "sft1_wave5_metadata_publication_v1"

DEFAULT_ROOT_CEILING = 500_000
MAXIMUM_ROOT_CEILING = 500_000
DEFAULT_CHECKPOINTS = (500_000, 1_000_000, 2_000_000)
MAXIMUM_RELEASE_ROWS = 3_000_000
DEFAULT_PUBLICATION_REPO_ID = "Lemmy00/leanfaith-sft1-deterministic-v1"
DEFAULT_PUBLICATION_PREFIX = "wave5/compiler_core_v1"
DEFAULT_ROOTS_PER_SHARD = 256
DEFAULT_ROOT_BATCH_SIZE = 8
DEFAULT_SELECTION_SALT = "sft1-wave5-compiler-scale-roots-v1"
PILOT_ROOT_MILESTONES = (1, 100, 10_000)
RUNTIME_PROJECTION_DECISION_ROOTS = 10_000
DEFAULT_FEATURES = frozenset(
    {
        "equality",
        "disequality",
        "strict_order",
        "non_strict_order",
        "bounded_quantifier",
        "existential",
        "implication",
        "membership",
        "numeral",
    }
)
DEFAULT_ORBIT_OPERATIONS = tuple(
    sorted(
        operation
        for operation in SQUARE_OPERATIONS
        if operation.startswith("ORBIT_WAVE4_")
        and SQUARE_OPERATIONS[operation]["negative"] != "N19_WHOLE_CLAIM_NEGATION_V1"
    )
)
RETRYABLE_STATUSES = frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT})
INFRASTRUCTURE_STATUSES = frozenset(
    {
        LeanStatus.CRASH,
        LeanStatus.INTERNAL_ERROR,
        LeanStatus.TIMEOUT,
        LeanStatus.SETUP_ERROR,
        LeanStatus.UNSUPPORTED,
    }
)


class CompilerScaleError(RuntimeError):
    """The scale run, source evidence, or durable state is inconsistent."""


class CompilerScaleInfrastructureError(CompilerScaleError):
    """The bounded Lean backend failed without a semantic terminal."""


class CompilerScaleCertificateError(CompilerScaleError):
    """A purported retained certificate or frozen render failed validation."""


@dataclass(frozen=True, slots=True)
class CompilerScaleSettings:
    audit: CompilerAuditSettings
    wave4_config_path: Path
    output_root: Path
    typed_spec: CompilerTypedHookSpec
    root_ceiling: int = DEFAULT_ROOT_CEILING
    maximum_release_rows: int = MAXIMUM_RELEASE_ROWS
    checkpoints: tuple[int, ...] = DEFAULT_CHECKPOINTS
    roots_per_shard: int = DEFAULT_ROOTS_PER_SHARD
    root_batch_size: int = DEFAULT_ROOT_BATCH_SIZE
    selection_salt: str = DEFAULT_SELECTION_SALT
    required_any_features: frozenset[str] = DEFAULT_FEATURES
    maximum_full_source_characters: int = 200_000
    n25_maximum_share: float = 0.25
    projected_runtime_limit_hours: float = 36.0
    terminal_marker: str = "complete.json"
    publication_repo_id: str = DEFAULT_PUBLICATION_REPO_ID
    publication_prefix: str = DEFAULT_PUBLICATION_PREFIX

    def __post_init__(self) -> None:
        if not 1 <= self.root_ceiling <= MAXIMUM_ROOT_CEILING:
            raise ValueError("Wave 5 root ceiling must be in [1, 500000]")
        if not 1 <= self.maximum_release_rows <= MAXIMUM_RELEASE_ROWS:
            raise ValueError("Wave 5 row ceiling must be in [1, 3000000]")
        if (
            not self.checkpoints
            or tuple(sorted(set(self.checkpoints))) != self.checkpoints
            or any(value <= 0 or value > self.maximum_release_rows for value in self.checkpoints)
        ):
            raise ValueError("Wave 5 checkpoints must be unique increasing values within the cap")
        if self.roots_per_shard <= 0 or self.root_batch_size <= 0:
            raise ValueError("Wave 5 root shard/batch sizes must be positive")
        if self.root_batch_size > self.roots_per_shard:
            raise ValueError("Wave 5 root batch cannot exceed the root shard")
        if not self.selection_salt:
            raise ValueError("Wave 5 selection salt must be nonempty")
        if not self.required_any_features:
            raise ValueError("Wave 5 requires at least one eligible signature feature")
        if self.maximum_full_source_characters <= 0:
            raise ValueError("Wave 5 source-length ceiling must be positive")
        if not 0 <= self.n25_maximum_share <= 0.25:
            raise ValueError("Wave 5 N25 share may not exceed 25 percent")
        if self.projected_runtime_limit_hours <= 0:
            raise ValueError("Wave 5 runtime projection limit must be positive")
        if self.publication_repo_id != DEFAULT_PUBLICATION_REPO_ID:
            raise ValueError(
                "Wave 5 publication repository must remain the authorized private repo"
            )
        prefix_parts = self.publication_prefix.split("/")
        immutable_prefixes = {
            "wave2/core_v1",
            "sprint_v1/core_v5_combined_square",
            "sprint_v1/aux_n19_square_curriculum",
        }
        if (
            not self.publication_prefix
            or prefix_parts[0] != "wave5"
            or any(
                self.publication_prefix == immutable
                or self.publication_prefix.startswith(f"{immutable}/")
                for immutable in immutable_prefixes
            )
            or any(part in {"", ".", ".."} for part in prefix_parts)
        ):
            raise ValueError("Wave 5 publication prefix is unsafe or immutable")
        if self.audit.lean_workers > 2 or self.audit.lean_rss_claim_gib > 40:
            raise ValueError("Wave 5 exceeds the shared two-worker/40-GiB host budget")
        if any(
            operation == "N19_WHOLE_CLAIM_NEGATION_V1" for operation in self.typed_spec.operations
        ):
            raise ValueError("N19 is forbidden from the Wave 5 core")
        if any(
            SQUARE_OPERATIONS[operation]["negative"] == "N19_WHOLE_CLAIM_NEGATION_V1"
            for operation in self.typed_spec.orbit_operations
        ):
            raise ValueError("N19 orbit operations are forbidden from the Wave 5 core")

    @property
    def selection_path(self) -> Path:
        return self.output_root / "_state" / "eligible_roots.jsonl"

    @property
    def selection_receipt_path(self) -> Path:
        return self.output_root / "_state" / "eligible_roots.receipt.json"

    @property
    def run_spec_path(self) -> Path:
        return self.output_root / "_state" / "run_spec.json"

    @property
    def journal_path(self) -> Path:
        return self.output_root / "_state" / "journal.jsonl"

    @property
    def status_path(self) -> Path:
        return self.output_root / "status.json"

    @property
    def complete_path(self) -> Path:
        return self.output_root / self.terminal_marker

    @property
    def migration_required_path(self) -> Path:
        return self.output_root / "migration_required.json"


@dataclass(frozen=True, slots=True)
class CompilerScaleRootOutcome:
    root_id: str
    status: Literal["retained", "rejected"]
    taxonomy: str
    records: tuple[Mapping[str, Any], ...] = ()
    groups: tuple[Mapping[str, Any], ...] = ()
    proof_summary: Mapping[str, Any] | None = None
    request_hashes: tuple[str, ...] = ()
    lean_requests: int = 0
    lean_elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class CompilerScaleResult:
    run_id: str
    status: Literal["complete", "complete_below_first_checkpoint"]
    selected_roots: int
    processed_roots: int
    retained_roots: int
    released_rows: int
    release_shards: int
    cache_hits: int
    lean_requests: int
    complete_path: Path


@dataclass(frozen=True, slots=True)
class PublicationChain:
    shards: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    head_revision: str | None
    pending_checkpoint: tuple[int, int] | None


class CompilerScaleExecutor(Protocol):
    def execute_batch(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> Sequence[CompilerScaleRootOutcome]: ...

    def close(self) -> None: ...


ExecutorFactory = Callable[[], CompilerScaleExecutor]
AuditReplay = Callable[[CompilerAuditSettings], Mapping[str, Any]]
TypedGateVerifier = Callable[
    [CompilerAuditSettings, CompilerTypedHookSpec, OrbitPolicy], Mapping[str, Any]
]
SourceResolver = Callable[
    [CompilerAuditSettings, Sequence[Mapping[str, Any]]], Sequence[CompilerAuditSource]
]


class IncrementalShardUploader(Protocol):
    def __call__(
        self,
        *,
        repo_id: str,
        local_root: Path,
        files: Sequence[Path],
        remote_prefix: str,
        commit_message: str,
        expected_parent: str | None,
    ) -> tuple[str, str, Mapping[str, str]]: ...


class IncrementalShardRecoveryVerifier(Protocol):
    def __call__(
        self,
        *,
        repo_id: str,
        local_root: Path,
        files: Sequence[Path],
        remote_prefix: str,
        commit_message: str,
        revision: str,
        parent_revision: str,
    ) -> Mapping[str, str]: ...


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompilerScaleError(f"{context} must be a mapping")
    return cast(dict[str, Any], value)


def _string_sequence(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CompilerScaleError(f"{context} must be a string list")
    return tuple(cast(list[str], value))


def _write_exact(path: Path, payload: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(dict(payload)) + b"\n"
    if path.is_symlink():
        raise CompilerScaleError(f"unsafe symlink at immutable Wave 5 artifact {path}")
    if path.is_file() and path.read_bytes() != data:
        raise CompilerScaleError(f"immutable Wave 5 artifact differs: {path}")
    write_atomic(path, data)


def _write_once_exact(path: Path, payload: Mapping[str, Any]) -> None:
    """Create immutable evidence once; validate, but never rewrite, existing bytes."""

    data = canonical_json_bytes(dict(payload)) + b"\n"
    if path.is_symlink():
        raise CompilerScaleError(f"unsafe symlink at immutable Wave 5 evidence {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise CompilerScaleError(f"immutable Wave 5 evidence differs: {path}")
        return
    write_atomic(path, data)


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    data = b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)
    write_atomic(path, data)


def _git_output(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CompilerScaleError(f"cannot inspect Git state at {path}")
    return completed.stdout.strip()


@cache
def _git_commit(path: str) -> str:
    return _git_output(Path(path), "rev-parse", "HEAD")


def _require_clean_generation_state(settings: CompilerScaleSettings) -> None:
    repo_root = find_repo_root(settings.wave4_config_path)
    if _git_output(repo_root, "status", "--porcelain"):
        raise CompilerScaleError("Wave 5 scale generation requires a clean implementation worktree")
    if _git_output(settings.audit.project_dir, "status", "--porcelain"):
        raise CompilerScaleError("Wave 5 scale generation requires a clean Lean project worktree")


def _default_audit_replay(settings: CompilerAuditSettings) -> Mapping[str, Any]:
    def explode(_settings: BackendSettings) -> LeanBackend:
        raise AssertionError("audit replay must never construct a Lean backend")

    return CompilerAuditRunner(
        settings,
        backend_factory=explode,
        manage_resources=False,
        verify_project=False,
    ).replay()


def _default_typed_gate_verifier(
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
) -> Mapping[str, Any]:
    verifier = getattr(replay_module, "verify_typed_certificate_gate", None)
    if not callable(verifier):
        raise CompilerScaleError("typed 1,000-root certificate gate verifier is unavailable")
    return cast(Mapping[str, Any], verifier(settings, spec, policy))


def verify_audit_gate(
    settings: CompilerAuditSettings,
    *,
    typed_spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    replay: AuditReplay = _default_audit_replay,
    typed_gate_verifier: TypedGateVerifier = _default_typed_gate_verifier,
) -> dict[str, Any]:
    """Require the exact completed all-pass audit and its zero-call replay."""

    if not settings.complete_path.is_file():
        raise CompilerScaleError("Wave 5 compiler-context audit terminal is absent")
    complete = read_json_object(settings.complete_path)
    required = {
        "artifact_kind": "sft1_wave5_compiler_context_audit_terminal",
        "status": "passed",
        "roots": settings.expected_rows,
        "compatible": settings.expected_rows,
        "incompatible": 0,
    }
    for field, expected in required.items():
        if complete.get(field) != expected:
            raise CompilerScaleError(
                f"Wave 5 compiler-context audit gate differs at {field}: "
                f"expected {expected!r}, got {complete.get(field)!r}"
            )
    proof = _mapping(complete.get("proof_contract"), "compiler audit proof_contract")
    for field in (
        "exact_prefix_plus_literal_by_plus_body",
        "source_label_true",
        "qualified_local_theorem_resolved",
        "meta_and_kernel_source_proof_checked",
    ):
        if proof.get(field) is not True:
            raise CompilerScaleError(f"compiler audit proof contract is false at {field}")
    receipt = dict(replay(settings))
    if receipt.get("lean_requests") != 0 or receipt.get("backend_constructed") is not False:
        raise CompilerScaleError("compiler audit replay was not a zero-call replay")
    if receipt.get("roots_verified") != settings.expected_rows:
        raise CompilerScaleError("compiler audit replay did not cover the full sample")
    typed_gate = dict(typed_gate_verifier(settings, typed_spec, policy))
    typed_terminal_sha256 = typed_gate.get("terminal_sha256")
    sample_sha256 = typed_gate.get("audit_sample_sha256")
    typed_replay = typed_gate.get("replay")
    if typed_gate.get("passed") is not True:
        raise CompilerScaleError("typed 1,000-root certificate gate did not pass")
    if not isinstance(typed_terminal_sha256, str) or len(typed_terminal_sha256) != 64:
        raise CompilerScaleError("typed certificate gate terminal hash is malformed")
    if not isinstance(sample_sha256, str) or len(sample_sha256) != 64:
        raise CompilerScaleError("typed certificate gate sample hash is malformed")
    if not isinstance(typed_replay, Mapping):
        raise CompilerScaleError("typed certificate gate replay evidence is absent")
    if (
        typed_replay.get("lean_requests") != 0
        or typed_replay.get("backend_constructed") is not False
        or typed_replay.get("forced_resume") is not True
    ):
        raise CompilerScaleError("typed certificate gate was not a forced zero-call replay")
    return {
        "terminal": complete,
        "terminal_sha256": hash_file(settings.complete_path),
        "replay": receipt,
        "typed_gate": typed_gate,
        "typed_gate_passed": True,
        "typed_gate_terminal_sha256": typed_terminal_sha256,
        "typed_gate_sample_sha256": sample_sha256,
        "typed_gate_replay_zero_call": True,
        "typed_gate_resume_replay": dict(typed_replay),
        "typed_gate_manual_inspection_verdict": typed_gate.get(
            "manual_inspection_verdict", "not_recorded"
        ),
    }


def _record_is_eligible(
    record: Mapping[str, Any], settings: CompilerScaleSettings
) -> tuple[bool, str]:
    root_id = record.get("root_id")
    record_hash = record.get("inventory_record_sha256")
    if not isinstance(root_id, str) or len(root_id) != 64:
        raise CompilerScaleError("compiler inventory row has a malformed root_id")
    if not isinstance(record_hash, str) or len(record_hash) != 64:
        raise CompilerScaleError("compiler inventory row has a malformed record hash")
    unhashed = dict(record)
    unhashed.pop("inventory_record_sha256", None)
    if hash_canonical(unhashed) != record_hash:
        raise CompilerScaleError(f"compiler inventory record hash differs for {root_id}")
    declaration = _mapping(record.get("declaration"), "inventory declaration")
    context = _mapping(record.get("context"), "inventory context")
    lengths = _mapping(record.get("lengths"), "inventory lengths")
    features = record.get("features")
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
        raise CompilerScaleError("compiler inventory features are malformed")
    if declaration.get("qualified_name_status") != "simple_namespace_stack_v1":
        return False, "namespace_context_unresolved"
    qualified = declaration.get("qualified_name_candidate")
    if not isinstance(qualified, str) or not qualified:
        return False, "qualified_name_unresolved"
    imports = context.get("imports")
    if (
        not isinstance(imports, list)
        or not imports
        or not all(isinstance(item, str) and item for item in imports)
    ):
        return False, "explicit_imports_absent"
    full_length = lengths.get("full_source_characters")
    if not isinstance(full_length, int) or full_length < 0:
        raise CompilerScaleError("compiler inventory source length is malformed")
    if full_length > settings.maximum_full_source_characters:
        return False, "full_source_too_long"
    if not settings.required_any_features.intersection(cast(list[str], features)):
        return False, "no_enabled_signature_feature"
    return True, "eligible"


def _selection_identity(settings: CompilerScaleSettings) -> dict[str, Any]:
    inventory_manifest = settings.audit.inventory.output_root / "manifest.json"
    return {
        "schema_version": SCALE_SELECTION_VERSION,
        "inventory_manifest_sha256": hash_file(inventory_manifest),
        "source_pin": settings.audit.inventory.pin.to_dict(),
        "project": settings.audit.inventory.project.to_dict(),
        "root_ceiling": settings.root_ceiling,
        "selection_salt": settings.selection_salt,
        "required_any_features": sorted(settings.required_any_features),
        "maximum_full_source_characters": settings.maximum_full_source_characters,
        "ordering": "sha256(selection_salt,root_id),root_id",
    }


def _load_selection(settings: CompilerScaleSettings) -> tuple[dict[str, Any], ...]:
    receipt = read_json_object(settings.selection_receipt_path)
    if receipt.get("identity") != _selection_identity(settings):
        raise CompilerScaleError("Wave 5 eligible-root selection identity differs")
    if receipt.get("sha256") != hash_file(settings.selection_path):
        raise CompilerScaleError("Wave 5 eligible-root selection hash differs")
    rows: list[dict[str, Any]] = []
    with settings.selection_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            rows.append(_mapping(value, f"eligible root line {line_number}"))
    if len(rows) != receipt.get("selected_rows"):
        raise CompilerScaleError("Wave 5 eligible-root selection row count differs")
    root_ids = [str(row.get("root_id")) for row in rows]
    if len(root_ids) != len(set(root_ids)):
        raise CompilerScaleError("Wave 5 eligible-root selection repeats a root")
    if receipt.get("root_ids_sha256") != hash_canonical(root_ids):
        raise CompilerScaleError("Wave 5 eligible-root order hash differs")
    for record in rows:
        eligible, reason = _record_is_eligible(record, settings)
        if not eligible:
            raise CompilerScaleError(f"stored eligible root is now ineligible: {reason}")
    return tuple(rows)


def build_eligible_selection(settings: CompilerScaleSettings) -> tuple[dict[str, Any], ...]:
    """Select exactly the stable top-K eligible roots without loading source text."""

    if settings.selection_receipt_path.is_file():
        if not settings.selection_path.is_file():
            raise CompilerScaleError("eligible-root receipt exists without its data")
        return _load_selection(settings)

    heap: list[tuple[int, str]] = []
    failures: Counter[str] = Counter()
    population = 0
    seen: set[str] = set()
    for record in iter_inventory_records(settings.audit.inventory.output_root):
        root_id = str(record.get("root_id", ""))
        if root_id in seen:
            raise CompilerScaleError(f"compiler inventory repeats root {root_id}")
        seen.add(root_id)
        eligible, reason = _record_is_eligible(record, settings)
        if not eligible:
            failures[reason] += 1
            continue
        population += 1
        rank_hex = hash_canonical(
            {
                "kind": SCALE_SELECTION_VERSION,
                "salt": settings.selection_salt,
                "root_id": root_id,
            }
        )
        rank = int(rank_hex, 16)
        item = (-rank, root_id)
        if len(heap) < settings.root_ceiling:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    selected_ids = {root_id for _rank, root_id in heap}
    expected = min(settings.root_ceiling, population)
    if len(selected_ids) != expected:
        raise CompilerScaleError("stable eligible-root selection underfilled")

    # A small on-disk ordering index avoids retaining hundreds of thousands of
    # inventory mappings in Python memory.  It is state only; the JSONL receipt
    # below remains authoritative.
    index_path = settings.output_root / "_state" / "eligible_roots.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS selected (rank TEXT PRIMARY KEY, root_id TEXT UNIQUE, "
            "record_json TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM selected")
        for record in iter_inventory_records(settings.audit.inventory.output_root):
            root_id = str(record["root_id"])
            if root_id not in selected_ids:
                continue
            rank_hex = hash_canonical(
                {
                    "kind": SCALE_SELECTION_VERSION,
                    "salt": settings.selection_salt,
                    "root_id": root_id,
                }
            )
            connection.execute(
                "INSERT INTO selected(rank,root_id,record_json) VALUES(?,?,?)",
                (rank_hex, root_id, canonical_json_bytes(record).decode("utf-8")),
            )
        connection.commit()
        output: list[dict[str, Any]] = []
        cursor = connection.execute("SELECT record_json FROM selected ORDER BY rank,root_id")
        for (record_json,) in cursor:
            output.append(_mapping(json.loads(str(record_json)), "selected inventory record"))
    finally:
        connection.close()
    if len(output) != expected:
        raise CompilerScaleError("eligible-root materialization underfilled")
    _write_jsonl_atomic(settings.selection_path, output)
    root_ids = [str(record["root_id"]) for record in output]
    receipt = {
        "artifact_kind": "sft1_wave5_compiler_eligible_roots",
        "identity": _selection_identity(settings),
        "eligible_population": population,
        "selected_rows": len(output),
        "ineligible_taxonomy": dict(sorted(failures.items())),
        "file": settings.selection_path.name,
        "bytes": settings.selection_path.stat().st_size,
        "sha256": hash_file(settings.selection_path),
        "root_ids_sha256": hash_canonical(root_ids),
        "lean_calls": 0,
    }
    _write_exact(settings.selection_receipt_path, receipt)
    return tuple(output)


def _checker_dependency_hashes(settings: CompilerScaleSettings) -> dict[str, str]:
    return {
        "compiler_scale": hash_file(Path(__file__).resolve()),
        "compiler_inventory": hash_file(Path(inventory_module.__file__).resolve()),
        "compiler_replay": hash_file(Path(replay_module.__file__).resolve()),
        "square": hash_file(Path(square_module.__file__).resolve()),
        "orbit": hash_file(Path(orbit_module.__file__).resolve()),
        "engine_python": hash_file(Path(engine_module.__file__).resolve()),
        "screens": hash_file(Path(screens_module.__file__).resolve()),
        "goal_v1": hash_file(Path(goal_v1_module.__file__).resolve()),
        "lean_engine": hash_file(settings.audit.engine_path),
    }


def _root_cache_identity(
    record: Mapping[str, Any], settings: CompilerScaleSettings, policy: OrbitPolicy
) -> dict[str, Any]:
    return {
        "cache_version": SCALE_CACHE_VERSION,
        "root_id": record["root_id"],
        "source_row_id": record["source_row_id"],
        "inventory_record_sha256": record["inventory_record_sha256"],
        "source_hashes": record["hashes"],
        "source_locator": record["source"],
        "context": record["context"],
        "source_pin": settings.audit.inventory.pin.to_dict(),
        "project": settings.audit.inventory.project.to_dict(),
        "engine_source_sha256": hash_file(settings.audit.engine_path),
        "compiler_config_sha256": settings.audit.config_sha256,
        "gold_blocklist_sha256": settings.audit.inventory.gold_blocklist_sha256,
        "typed_hook": settings.typed_spec.semantic_payload(),
        "wave4_policy_hash": policy.policy_hash,
        "semantic_dependencies": _checker_dependency_hashes(settings),
    }


def _root_cache_path(settings: CompilerScaleSettings, key: str) -> Path:
    return settings.output_root / "cache" / "roots" / key[:2] / f"{key}.json"


@cache
def _cached_gold_blocklist(path: str, expected_sha256: str) -> GoldBlocklist:
    return GoldBlocklist.load(Path(path), expected_sha256=expected_sha256)


def _validate_root_terminal(
    terminal: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    settings: CompilerScaleSettings,
    policy: OrbitPolicy,
) -> Wave4ClosureMaterialization | None:
    identity = _root_cache_identity(record, settings, policy)
    key = hash_canonical(identity)
    if terminal.get("cache_key") != key or terminal.get("identity") != identity:
        raise CompilerScaleError(f"compiler root cache identity differs for {record['root_id']}")
    if terminal.get("root_id") != record["root_id"]:
        raise CompilerScaleError("compiler root cache changes its root")
    status = terminal.get("status")
    if status not in {"retained", "rejected"}:
        raise CompilerScaleError("compiler root cache has a nonterminal status")
    taxonomy = terminal.get("taxonomy")
    if not isinstance(taxonomy, str) or not taxonomy or any(char.isspace() for char in taxonomy):
        raise CompilerScaleError("compiler root cache has a malformed taxonomy")
    records_value = terminal.get("records")
    groups_value = terminal.get("groups")
    if not isinstance(records_value, list) or not isinstance(groups_value, list):
        raise CompilerScaleError("compiler root cache records/groups are malformed")
    if status == "rejected":
        if records_value or groups_value:
            raise CompilerScaleError("rejected compiler root cache contains release rows")
        return None
    if not records_value or not groups_value:
        raise CompilerScaleError("retained compiler root cache has an empty closure")
    proof_summary = _mapping(terminal.get("proof_summary"), "compiler root proof summary")
    source_check = _mapping(
        proof_summary.get("source_proof_check"), "compiler root source proof check"
    )
    if (
        set(source_check)
        != {
            "meta_checked",
            "kernel_checked",
            "kernel_level_instantiation",
            "proof_expr_hash_u64",
        }
        or source_check.get("kernel_level_instantiation") not in {"none", "all_zero"}
        or not isinstance(source_check.get("proof_expr_hash_u64"), str)
        or not str(source_check["proof_expr_hash_u64"]).isdigit()
        or source_check.get("meta_checked") is not True
        or source_check.get("kernel_checked") is not True
    ):
        raise CompilerScaleCertificateError(
            "retained compiler root lacks its exact meta/kernel source proof checks"
        )
    if (
        not isinstance(proof_summary.get("engine_semantic_version"), str)
        or not proof_summary["engine_semantic_version"]
    ):
        raise CompilerScaleCertificateError(
            "retained compiler root lacks its engine semantic version"
        )
    materialized = materialize_wave4_records(
        [_mapping(value, "compiler root record") for value in records_value],
        [_mapping(value, "compiler root group") for value in groups_value],
    )
    gold = _cached_gold_blocklist(
        str(settings.audit.inventory.gold_blocklist_path),
        settings.audit.inventory.gold_blocklist_sha256,
    )
    for item in materialized.rows:
        row = _mapping(item.get("row"), "compiler root model row")
        sidecar = _mapping(item.get("sidecar"), "compiler root sidecar")
        if set(row) != {"reference", "candidate", "label"}:
            raise CompilerScaleError("Wave 5 model row is not exactly three fields")
        if sidecar.get("root_id") != record["root_id"]:
            raise CompilerScaleError("Wave 5 compiler closure crosses ancestry roots")
        implementation_commit = sidecar.get("implementation_commit")
        if (
            not isinstance(implementation_commit, str)
            or len(implementation_commit) != 40
            or any(char not in "0123456789abcdef" for char in implementation_commit)
        ):
            raise CompilerScaleCertificateError(
                "retained compiler row lacks its exact clean implementation commit"
            )
        if row["reference"] == row["candidate"]:
            raise CompilerScaleCertificateError("Wave 5 compiler closure contains a self-pair")
        if residue_violation(str(row["reference"])) is not None:
            raise CompilerScaleCertificateError("Wave 5 reference failed the residue screen")
        if residue_violation(str(row["candidate"])) is not None:
            raise CompilerScaleCertificateError("Wave 5 candidate failed the residue screen")
        if gold.hit(str(row["reference"])):
            raise CompilerScaleCertificateError("Wave 5 reference hit the gold blocklist")
        if gold.hit(str(row["candidate"])):
            raise CompilerScaleCertificateError("Wave 5 candidate hit the gold blocklist")
    if any(group.operation_id == "N19_WHOLE_CLAIM_NEGATION_V1" for group in materialized.groups):
        raise CompilerScaleCertificateError("N19 entered the Wave 5 compiler cache")
    return materialized


def _load_root_cache(
    record: Mapping[str, Any], settings: CompilerScaleSettings, policy: OrbitPolicy
) -> tuple[dict[str, Any], Path] | None:
    identity = _root_cache_identity(record, settings, policy)
    key = hash_canonical(identity)
    path = _root_cache_path(settings, key)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise CompilerScaleError(f"unsafe compiler root cache symlink: {path}")
    try:
        terminal = read_json_object(path)
    except (OSError, ValueError, StoreError) as exc:
        raise CompilerScaleError(f"cannot read compiler root cache {path}") from exc
    _validate_root_terminal(terminal, record=record, settings=settings, policy=policy)
    return terminal, path


def _persist_root_cache(
    outcome: CompilerScaleRootOutcome,
    *,
    record: Mapping[str, Any],
    settings: CompilerScaleSettings,
    policy: OrbitPolicy,
) -> tuple[dict[str, Any], Path]:
    if outcome.root_id != record["root_id"]:
        raise CompilerScaleError("compiler executor returned a terminal for another root")
    identity = _root_cache_identity(record, settings, policy)
    key = hash_canonical(identity)
    terminal: dict[str, Any] = {
        "artifact_kind": "sft1_wave5_compiler_root_terminal",
        "cache_key": key,
        "identity": identity,
        "root_id": outcome.root_id,
        "status": outcome.status,
        "taxonomy": outcome.taxonomy,
        "records": [dict(item) for item in outcome.records],
        "groups": [dict(item) for item in outcome.groups],
        "proof_summary": dict(outcome.proof_summary or {}),
        "request_hashes": list(outcome.request_hashes),
        "execution": {
            "lean_requests": outcome.lean_requests,
            "lean_elapsed_ms": outcome.lean_elapsed_ms,
        },
    }
    _validate_root_terminal(terminal, record=record, settings=settings, policy=policy)
    path = _root_cache_path(settings, key)
    _write_exact(path, terminal)
    return terminal, path


class _DictView:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        for key, value in payload.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _materialize_selected_records(
    *,
    source: CompilerAuditSource,
    settings: CompilerScaleSettings,
    policy: OrbitPolicy,
    operation_id: str,
    root_payload: Mapping[str, Any],
    combined_payload: Mapping[str, Any],
    selected_records: Sequence[Mapping[str, Any]],
    descriptor_request_hash: str,
    selected_request_hash: str,
) -> Wave4ClosureMaterialization:
    """Use the established Wave4Runner row builder and closure validator."""

    engine_semantic = str(root_payload.get("engine_semantic_version", ""))
    if not engine_semantic:
        raise CompilerScaleCertificateError("compiler root omitted engine semantic version")
    project = settings.audit.inventory.project
    engine_identity = {
        "source_sha256": hash_file(settings.audit.engine_path),
        "semantic_version": engine_semantic,
        "import_options_fingerprint": hash_canonical(
            {
                "context_fingerprint": source.context_fingerprint,
                "project_revision": project.project_revision,
                "lean_version": project.lean_version,
            }
        ),
        "compile_context_id": "ctx:" + source.context_fingerprint,
    }
    builder = cast(Any, object.__new__(Wave4Runner))
    builder.operation_id = operation_id
    builder.policy = policy
    builder.maximum_depth = settings.typed_spec.maximum_depth
    builder.statements = {str(source.qualified_name): source.theorem}
    builder.base = SimpleNamespace(
        root_id=lambda _name: source.root_id,
        pins=_DictView(
            {
                "project_id": project.project_id,
                "project_dir": str(settings.audit.project_dir),
                "project_revision": project.project_revision,
                "lean_version": project.lean_version,
                "lean_interact_version": project.lean_interact_version,
                "repl_revision": project.repl_revision,
                "import_header": "\n".join(
                    f"import {value}"
                    for value in _string_sequence(
                        _mapping(source.inventory_record["context"], "context").get("imports"),
                        "context imports",
                    )
                ),
                "options": {"Elab.async": False},
            }
        ),
        identity=_DictView(engine_identity),
    )
    cache_key = hash_canonical(
        {
            "kind": SCALE_CACHE_VERSION,
            "root_id": source.root_id,
            "operation_id": operation_id,
            "descriptor_request_hash": descriptor_request_hash,
            "selected_request_hash": selected_request_hash,
        }
    )
    implementation_commit = _git_commit(str(find_repo_root(settings.wave4_config_path)))
    builder.square_root_key = lambda _name: cache_key
    record = {
        "status": "retained",
        "selected": [dict(item) for item in selected_records],
        "payload": dict(combined_payload),
        "enumeration_hash": combined_payload.get("enumeration_hash"),
        "engine": engine_identity,
        "implementation_commit": implementation_commit,
        "process_request_hash": descriptor_request_hash,
        "render_request_hash": selected_request_hash,
    }
    rows, groups = Wave4Runner.build_wave4_rows(
        builder,
        str(source.qualified_name),
        record,
        {"compiler_root_id": source.root_id},
    )
    return materialize_wave4_records(rows, groups)


class _LeanTypedExecutor:
    """Persistent central-backend executor for the typed compiler hook."""

    def __init__(
        self,
        settings: CompilerScaleSettings,
        policy: OrbitPolicy,
        *,
        backend_factory: Callable[[BackendSettings], LeanBackend] = LeanInteractBackend,
    ) -> None:
        self.settings = settings
        self.policy = policy
        replay_module._verify_project(settings.audit)
        backend_settings = replay_module._backend_settings(
            replace(
                settings.audit,
                output_root=settings.output_root / "_lean",
            )
        )
        self.backend_factory = backend_factory
        self.backend_settings = backend_settings
        self.backend = backend_factory(backend_settings)
        self.retry_policy = RetryPolicy(
            max_attempts=settings.audit.retry_max_attempts,
            retry_statuses=settings.audit.retry_statuses,
        )

    def close(self) -> None:
        self.backend.close()

    def _reset(self, _attempt: int, _pending: tuple[int, ...]) -> None:
        self.backend.close()
        self.backend = self.backend_factory(self.backend_settings)

    def _run_requests(
        self, requests: Sequence[LeanRequest]
    ) -> tuple[tuple[LeanResult, ...], tuple[int, ...], tuple[int, ...]]:
        outcome = run_batch_with_retries(
            self.backend.run_batch,
            requests,
            self.retry_policy,
            before_retry=self._reset,
        )
        for result in outcome.results:
            if result.status in INFRASTRUCTURE_STATUSES:
                raise CompilerScaleInfrastructureError(
                    f"typed compiler hook infrastructure terminal: {result.status.value}"
                )
        return (
            outcome.results,
            tuple(len(lineage) for lineage in outcome.attempts),
            tuple(sum(result.elapsed_ms for result in lineage) for lineage in outcome.attempts),
        )

    def execute_batch(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> Sequence[CompilerScaleRootOutcome]:
        if not sources:
            return ()
        if len({source.root_id for source in sources}) != len(sources):
            raise CompilerScaleError("compiler typed batch repeats a root")
        context_id = "ctx:" + replay_module._backend_context_fingerprint(self.settings.audit)

        # A Lean request may share its imported/preamble environment only when
        # the reconstructed context bytes are identical.  Grouping is stable by
        # first appearance; duplicate local names force a separate request.
        context_groups: dict[tuple[str, str], list[CompilerAuditSource]] = {}
        for source in sources:
            context_groups.setdefault(
                (source.context_fingerprint, source.context_prefix), []
            ).append(source)
        descriptor_source_batches: list[tuple[CompilerAuditSource, ...]] = []
        for group in context_groups.values():
            current: list[CompilerAuditSource] = []
            names: set[str | None] = set()
            for source in group:
                if (
                    len(current) >= self.settings.audit.context_request_max_roots
                    or source.qualified_name in names
                ):
                    descriptor_source_batches.append(tuple(current))
                    current = []
                    names = set()
                current.append(source)
                names.add(source.qualified_name)
            if current:
                descriptor_source_batches.append(tuple(current))

        descriptor_prepared = [
            build_typed_descriptor_batch_request(
                batch,
                settings=self.settings.audit,
                spec=self.settings.typed_spec,
                context_id=context_id,
                timeout_seconds=self.settings.audit.request_timeout_seconds,
                run_id=run_id,
            )
            for batch in descriptor_source_batches
        ]
        descriptor_results, descriptor_attempts, descriptor_elapsed = self._run_requests(
            [item.request for item in descriptor_prepared]
        )
        per_root_calls = Counter[str]()
        per_root_ms = Counter[str]()
        request_hashes: dict[str, list[str]] = {source.root_id: [] for source in sources}
        states: dict[str, dict[str, Any]] = {}
        selected_candidates: list[tuple[CompilerAuditSource, str, Mapping[str, Any], Any]] = []
        rejected: dict[str, str] = {}
        for prepared, result, attempts, elapsed in zip(
            descriptor_prepared,
            descriptor_results,
            descriptor_attempts,
            descriptor_elapsed,
            strict=True,
        ):
            owner = prepared.sources[0].root_id
            per_root_calls[owner] += attempts
            per_root_ms[owner] += elapsed
            for source in prepared.sources:
                request_hashes[source.root_id].append(result.request_hash)
            if result.status != LeanStatus.VALID or result.sorries:
                for source in prepared.sources:
                    rejected[source.root_id] = f"descriptor_{result.status.value}"
                continue
            try:
                parsed = parse_typed_descriptor_batch_payloads(
                    prepared.sources,
                    settings=self.settings.audit,
                    spec=self.settings.typed_spec,
                    messages=result.messages,
                )
            except (CompilerReplayError, OrbitError, ValueError) as exc:
                raise CompilerScaleCertificateError(
                    f"typed descriptor batch evidence failed: {exc}"
                ) from exc
            for source in prepared.sources:
                root_payload, descriptors = parsed[source.root_id]
                proof_check = root_payload.get("source_proof_check")
                if (
                    root_payload.get("root_status") != "ok"
                    or not isinstance(proof_check, dict)
                    or proof_check.get("meta_checked") is not True
                    or proof_check.get("kernel_checked") is not True
                ):
                    rejected[source.root_id] = "source_proof_check_failed"
                    continue
                states[source.root_id] = {
                    "source": source,
                    "root_payload": root_payload,
                    "descriptor_payloads": descriptors,
                    "materializations": [],
                    "selected_failures": [],
                }
                for operation_id in self.settings.typed_spec.orbit_operations:
                    descriptor_payload = descriptors[operation_id]
                    if descriptor_payload.get("status") != "described":
                        continue
                    try:
                        chosen = preselect_wave4_variant_descriptors(
                            descriptor_payload,
                            operation_id=operation_id,
                            policy=self.policy,
                            maximum_depth=self.settings.typed_spec.maximum_depth,
                            expected_root=source.qualified_name,
                            selection_root_id=source.root_id,
                        )
                    except (OrbitError, ValueError) as exc:
                        raise CompilerScaleCertificateError(
                            f"typed descriptor validation failed for {source.root_id}: {exc}"
                        ) from exc
                    if chosen:
                        selected_candidates.append(
                            (source, operation_id, descriptor_payload, chosen)
                        )

        # A source can have several applicable negative orbits.  Batch by exact
        # context *and* operation so one request still has one action per root.
        selected_groups: dict[
            tuple[str, str, str],
            list[tuple[CompilerAuditSource, str, Mapping[str, Any], Any]],
        ] = {}
        for item in selected_candidates:
            source, operation_id, _payload, _chosen = item
            selected_groups.setdefault(
                (source.context_fingerprint, source.context_prefix, operation_id), []
            ).append(item)
        selected_batches: list[
            tuple[
                Any,
                tuple[tuple[CompilerAuditSource, str, Mapping[str, Any], Any], ...],
                str,
            ]
        ] = []
        for selected_group_values in selected_groups.values():
            request_batches: list[
                tuple[tuple[CompilerAuditSource, str, Mapping[str, Any], Any], ...]
            ] = []
            current_items: list[tuple[CompilerAuditSource, str, Mapping[str, Any], Any]] = []
            current_names: set[str | None] = set()
            for selected_item in selected_group_values:
                source = selected_item[0]
                if (
                    len(current_items) >= self.settings.audit.context_request_max_roots
                    or source.qualified_name in current_names
                ):
                    request_batches.append(tuple(current_items))
                    current_items = []
                    current_names = set()
                current_items.append(selected_item)
                current_names.add(source.qualified_name)
            if current_items:
                request_batches.append(tuple(current_items))
            for selected_batch in request_batches:
                selections = tuple(
                    CompilerTypedWave4Selection(
                        source=source,
                        operation_id=operation_id,
                        selected_indices=tuple(item.index for item in chosen),
                    )
                    for source, operation_id, _payload, chosen in selected_batch
                )
                scope = "sft1-wave5-scale:" + hash_canonical(
                    [
                        {
                            "root_id": selection.source.root_id,
                            "operation_id": selection.operation_id,
                            "indices": list(selection.selected_indices),
                        }
                        for selection in selections
                    ]
                )
                prepared = build_typed_wave4_selected_batch_request(
                    selections,
                    settings=self.settings.audit,
                    spec=self.settings.typed_spec,
                    render_scope_id=scope,
                    context_id=context_id,
                    timeout_seconds=self.settings.audit.request_timeout_seconds,
                    run_id=run_id,
                )
                selected_batches.append((prepared, selected_batch, scope))
        if selected_batches:
            selected_results, selected_attempts, selected_elapsed = self._run_requests(
                [prepared.request for prepared, _batch, _scope in selected_batches]
            )
        else:
            selected_results, selected_attempts, selected_elapsed = (), (), ()
        for metadata, result, attempts, elapsed in zip(
            selected_batches,
            selected_results,
            selected_attempts,
            selected_elapsed,
            strict=True,
        ):
            prepared, selected_batch, scope = metadata
            owner = prepared.sources[0].root_id
            per_root_calls[owner] += attempts
            per_root_ms[owner] += elapsed
            for source in prepared.sources:
                request_hashes[source.root_id].append(result.request_hash)
            if result.status != LeanStatus.VALID or result.sorries:
                for source, operation_id, _payload, _chosen in selected_batch:
                    states[source.root_id]["selected_failures"].append(
                        f"{operation_id}:{result.status.value}"
                    )
                continue
            try:
                validated_by_root = validate_typed_wave4_selected_batch_result(
                    prepared.selections,
                    settings=self.settings.audit,
                    spec=self.settings.typed_spec,
                    descriptor_payloads={
                        source.root_id: descriptor_payload
                        for source, _operation_id, descriptor_payload, _chosen in selected_batch
                    },
                    selected_descriptors={
                        source.root_id: chosen
                        for source, _operation_id, _payload, chosen in selected_batch
                    },
                    render_scope_id=scope,
                    policy=self.policy,
                    result=result,
                )
            except (CompilerReplayError, GoalV1Error, OrbitError, ValueError) as exc:
                raise CompilerScaleCertificateError(
                    f"selected batch certificate/render failed: {exc}"
                ) from exc
            for source, operation_id, _payload, _chosen in selected_batch:
                combined, _validated, selected_records = validated_by_root[source.root_id]
                state = states[source.root_id]
                materialized = _materialize_selected_records(
                    source=source,
                    settings=self.settings,
                    policy=self.policy,
                    operation_id=operation_id,
                    root_payload=state["root_payload"],
                    combined_payload=combined,
                    selected_records=selected_records,
                    descriptor_request_hash=request_hashes[source.root_id][0],
                    selected_request_hash=result.request_hash,
                )
                state["materializations"].append(materialized)

        outcomes: list[CompilerScaleRootOutcome] = []
        for source in sources:
            taxonomy = rejected.get(source.root_id)
            root_state = states.get(source.root_id)
            if taxonomy is not None or root_state is None:
                outcomes.append(
                    CompilerScaleRootOutcome(
                        root_id=source.root_id,
                        status="rejected",
                        taxonomy=taxonomy or "descriptor_rejected",
                        request_hashes=tuple(request_hashes[source.root_id]),
                        lean_requests=per_root_calls[source.root_id],
                        lean_elapsed_ms=per_root_ms[source.root_id],
                    )
                )
                continue
            materials = cast(list[Wave4ClosureMaterialization], root_state["materializations"])
            if not materials:
                outcomes.append(
                    CompilerScaleRootOutcome(
                        root_id=source.root_id,
                        status="rejected",
                        taxonomy="no_certified_wave4_closure",
                        proof_summary={
                            "source_proof_check": root_state["root_payload"].get(
                                "source_proof_check"
                            ),
                            "selected_failures": root_state["selected_failures"],
                        },
                        request_hashes=tuple(request_hashes[source.root_id]),
                        lean_requests=per_root_calls[source.root_id],
                        lean_elapsed_ms=per_root_ms[source.root_id],
                    )
                )
                continue
            records = [row for material in materials for row in material.rows]
            groups = [group.record for material in materials for group in material.groups]
            combined_materialization = materialize_wave4_records(records, groups)
            outcomes.append(
                CompilerScaleRootOutcome(
                    root_id=source.root_id,
                    status="retained",
                    taxonomy="proof_certified_wave4_closure",
                    records=combined_materialization.rows,
                    groups=tuple(group.record for group in combined_materialization.groups),
                    proof_summary={
                        "source_proof_check": root_state["root_payload"].get("source_proof_check"),
                        "engine_semantic_version": root_state["root_payload"].get(
                            "engine_semantic_version"
                        ),
                        "selected_failures": root_state["selected_failures"],
                    },
                    request_hashes=tuple(request_hashes[source.root_id]),
                    lean_requests=per_root_calls[source.root_id],
                    lean_elapsed_ms=per_root_ms[source.root_id],
                )
            )
        return outcomes


def _run_identity(
    settings: CompilerScaleSettings,
    *,
    policy: OrbitPolicy,
    audit_gate: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "run_spec_version": SCALE_RUN_SPEC_VERSION,
        "implementation_sha256": hash_file(Path(__file__).resolve()),
        "implementation_commit": _git_commit(str(find_repo_root(settings.wave4_config_path))),
        "semantic_dependencies": {
            **_checker_dependency_hashes(settings),
            "wave4_config": hash_file(settings.wave4_config_path),
        },
        "inventory_manifest_sha256": hash_file(
            settings.audit.inventory.output_root / "manifest.json"
        ),
        "eligible_selection_sha256": hash_file(settings.selection_path),
        "eligible_selection_receipt_sha256": hash_file(settings.selection_receipt_path),
        "audit_terminal_sha256": audit_gate["terminal_sha256"],
        "audit_run_id": _mapping(audit_gate["terminal"], "audit terminal")["run_id"],
        "typed_certificate_gate_terminal_sha256": audit_gate["typed_gate_terminal_sha256"],
        "typed_certificate_gate_sample_sha256": audit_gate["typed_gate_sample_sha256"],
        "source_pin": settings.audit.inventory.pin.to_dict(),
        "project": settings.audit.inventory.project.to_dict(),
        "typed_hook": settings.typed_spec.semantic_payload(),
        "wave4_policy_hash": policy.policy_hash,
        "root_ceiling": settings.root_ceiling,
        "maximum_release_rows": settings.maximum_release_rows,
        "checkpoints": list(settings.checkpoints),
        "roots_per_shard": settings.roots_per_shard,
        "root_batch_size": settings.root_batch_size,
        "n25_maximum_share": settings.n25_maximum_share,
        "publication": {
            "repo_id": settings.publication_repo_id,
            "prefix": settings.publication_prefix,
            "private_first": True,
            "immutable_additive_shards": True,
        },
        "lean_workers": settings.audit.lean_workers,
        "lean_rss_claim_gib": settings.audit.lean_rss_claim_gib,
        "retry_statuses": sorted(status.value for status in settings.audit.retry_statuses),
        "retry_max_attempts": settings.audit.retry_max_attempts,
        "elab_async": False,
    }


def _ensure_run_spec(
    settings: CompilerScaleSettings,
    *,
    policy: OrbitPolicy,
    audit_gate: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    identity = _run_identity(settings, policy=policy, audit_gate=audit_gate)
    run_id = hash_canonical(identity)
    payload = {"run_id": run_id, **identity}
    _write_exact(settings.run_spec_path, payload)
    return run_id, payload


def _terminal_index_from_events(
    events: Iterable[Mapping[str, Any]], run_id: str
) -> dict[str, dict[str, Any]]:
    terminals: dict[str, dict[str, Any]] = {}
    for raw_event in events:
        event = dict(raw_event)
        if event.get("event") != "root_terminal" or event.get("run_id") != run_id:
            continue
        root_id = str(event.get("root_id", ""))
        comparable = {
            field: event.get(field)
            for field in ("root_id", "cache_key", "cache_sha256", "status", "taxonomy")
        }
        prior = terminals.get(root_id)
        if prior is not None and comparable != prior:
            raise CompilerScaleError(f"conflicting Wave 5 journal terminal for {root_id}")
        terminals[root_id] = comparable
    return terminals


def _journal_terminals(journal: Journal, run_id: str) -> dict[str, dict[str, Any]]:
    return _terminal_index_from_events(journal.read(), run_id)


def _root_terminal_event(
    *, run_id: str, terminal: Mapping[str, Any], path: Path, source: str
) -> dict[str, Any]:
    return {
        "event": "root_terminal",
        "run_id": run_id,
        "root_id": str(terminal["root_id"]),
        "cache_key": terminal["cache_key"],
        "cache_sha256": hash_file(path),
        "status": terminal["status"],
        "taxonomy": terminal["taxonomy"],
        "source": source,
    }


def _append_root_terminal_events(
    journal: Journal,
    terminal_index: dict[str, dict[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Validate against one in-memory index and fsync new terminals as one batch."""

    comparable_fields = ("root_id", "cache_key", "cache_sha256", "status", "taxonomy")
    pending_events: list[dict[str, Any]] = []
    pending_index: dict[str, dict[str, Any]] = {}
    for raw_event in events:
        event = dict(raw_event)
        root_id = str(event["root_id"])
        comparable = {field: event.get(field) for field in comparable_fields}
        prior = pending_index.get(root_id) or terminal_index.get(root_id)
        if prior is not None:
            if prior != comparable:
                raise CompilerScaleError(f"journal/cache disagreement for compiler root {root_id}")
            continue
        pending_events.append(event)
        pending_index[root_id] = comparable
    journal.append_many(pending_events)
    terminal_index.update(pending_index)


def _chunked[T](values: Sequence[T], size: int) -> Iterator[tuple[T, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


def _scale_chunks[T](values: Sequence[T], steady_size: int) -> tuple[tuple[T, ...], ...]:
    """Preserve the mandatory 1/100/10K measured pilot boundaries."""

    chunks: list[tuple[T, ...]] = []
    cursor = 0
    for milestone in PILOT_ROOT_MILESTONES:
        if cursor >= len(values):
            break
        if milestone > len(values):
            break
        while cursor < milestone:
            end = min(milestone, cursor + steady_size)
            chunks.append(tuple(values[cursor:end]))
            cursor = end
    chunks.extend(_chunked(values[cursor:], steady_size))
    return tuple(chunk for chunk in chunks if chunk)


def _shard_paths(settings: CompilerScaleSettings, part: int) -> dict[str, Path]:
    state_name = f"shard-{part:05d}"
    shard_dir = settings.output_root / "release" / f"shard-{part + 1:04d}"
    return {
        "rows": shard_dir / "rows.jsonl",
        "sidecars": shard_dir / "sidecars.jsonl",
        "groups": shard_dir / "closure_groups.jsonl",
        "manifest": shard_dir / "manifest.json",
        "receipt": settings.output_root / "_state" / "shards" / f"{state_name}.json",
    }


def _validate_shard_receipt(
    settings: CompilerScaleSettings,
    receipt: Mapping[str, Any],
    *,
    run_id: str,
    part: int,
    expected_records: Sequence[Mapping[str, Any]],
    policy: OrbitPolicy,
) -> None:
    expected_root_ids = [str(record["root_id"]) for record in expected_records]
    expected = {
        "artifact_kind": "sft1_wave5_compiler_release_shard",
        "schema_version": SCALE_SHARD_VERSION,
        "run_id": run_id,
        "part": part,
        "input_root_ids": expected_root_ids,
        "input_record_hashes": [
            str(record["inventory_record_sha256"]) for record in expected_records
        ],
        "input_roots": len(expected_records),
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise CompilerScaleError(f"Wave 5 shard {part} differs at {field}")
    paths = _shard_paths(settings, part)
    files = _mapping(receipt.get("files"), f"Wave 5 shard {part} files")
    for kind in ("rows", "sidecars", "groups", "manifest"):
        file_receipt = _mapping(files.get(kind), f"Wave 5 shard {part} {kind}")
        path = paths[kind]
        if path.name != file_receipt.get("file") or not path.is_file():
            raise CompilerScaleError(f"Wave 5 shard {part} {kind} file is absent")
        if hash_file(path) != file_receipt.get("sha256"):
            raise CompilerScaleError(f"Wave 5 shard {part} {kind} hash differs")
    row_records: list[dict[str, Any]] = []
    sidecars: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for kind, target in (("rows", row_records), ("sidecars", sidecars), ("groups", groups)):
        with paths[kind].open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    target.append(_mapping(json.loads(line), f"Wave 5 shard {part} {kind}"))
    if len(row_records) != receipt.get("rows") or len(sidecars) != receipt.get("rows"):
        raise CompilerScaleError(f"Wave 5 shard {part} model/sidecar count differs")
    if len(groups) != receipt.get("closure_groups"):
        raise CompilerScaleError(f"Wave 5 shard {part} closure count differs")
    records: list[dict[str, Any]] = []
    for row, sidecar_record in zip(row_records, sidecars, strict=True):
        if set(row) != {"reference", "candidate", "label"}:
            raise CompilerScaleError("released compiler row is not exactly three fields")
        sidecar = dict(sidecar_record)
        unordered_key = sidecar.pop("unordered_pair_key", None)
        row_hash = sidecar.pop("row_hash", None)
        if not isinstance(sidecar.get("pair_id"), str):
            raise CompilerScaleError("released compiler sidecar lacks its pair ID")
        records.append(
            {
                "row": row,
                "sidecar": sidecar,
                "unordered_pair_key": unordered_key,
                "row_hash": row_hash,
                "label": row["label"],
                "operation_id": sidecar.get("operation_id"),
                "root_name": sidecar.get("root_name"),
                "mechanism": sidecar.get("mechanism"),
            }
        )
    materialized = materialize_wave4_records(records, groups)
    if len(materialized.rows) != len(row_records):
        raise CompilerScaleError(f"Wave 5 shard {part} materialization count differs")
    balance = _mapping(
        receipt.get("pair_delta_balance_report"), f"Wave 5 shard {part} pair-delta report"
    )
    quarantined = balance.get("quarantined_group_ids")
    if (
        receipt.get("pair_delta_balance_report_sha256") != hash_canonical(balance)
        or balance.get("policy") != "whole_ancestry_closure_inverse_pair_delta_match_v1"
        or balance.get("selected_groups") != len(materialized.groups)
        or balance.get("selected_physical_rows") != len(materialized.rows)
        or not isinstance(quarantined, list)
        or not all(isinstance(value, str) for value in quarantined)
        or quarantined != sorted(set(quarantined))
        or receipt.get("pair_delta_quarantined_group_ids") != quarantined
        or receipt.get("pair_delta_quarantined_group_count") != len(quarantined)
        or receipt.get("pair_delta_quarantined_group_ids_sha256") != hash_canonical(quarantined)
    ):
        raise CompilerScaleError(f"Wave 5 shard {part} pair-delta balance evidence differs")
    after_cells = _mapping(balance.get("cell_counts_after"), "pair-delta cells after selection")
    n25_guard = _mapping(balance.get("post_balance_n25_guard"), "post-balance N25 guard")
    if any(
        _mapping(counts, "pair-delta cell counts").get("positive")
        != _mapping(counts, "pair-delta cell counts").get("negative")
        for counts in after_cells.values()
    ) or (materialized.rows and balance.get("passed") is not True):
        raise CompilerScaleError(f"Wave 5 shard {part} pair-delta balance did not pass")
    if (
        n25_guard.get("maximum_share") != settings.n25_maximum_share
        or n25_guard.get("final_rows") != len(materialized.rows)
        or n25_guard.get("final_n25_rows") != _n25_row_count(materialized)
        or n25_guard.get("passed") is not True
        or n25_guard.get("fallback") not in {None, "drop_all_n25_then_rebalance"}
    ):
        raise CompilerScaleError(f"Wave 5 shard {part} post-balance N25 guard differs")
    n25_rows = _n25_row_count(materialized)
    if n25_rows > int(len(materialized.rows) * settings.n25_maximum_share):
        raise CompilerScaleError(f"Wave 5 shard {part} exceeds the N25 row cap")
    shard_manifest = read_json_object(paths["manifest"])
    expected_shard_manifest = _mapping(
        receipt.get("publication_manifest"), f"Wave 5 shard {part} publication manifest"
    )
    if shard_manifest != expected_shard_manifest:
        raise CompilerScaleError(f"Wave 5 shard {part} publication manifest differs")
    expected_publication = {
        "schema_version": 1,
        "shard": part + 1,
        "row_count": len(row_records),
        "rows_sha256": hash_file(paths["rows"]),
        "sidecars_sha256": hash_file(paths["sidecars"]),
        "closure_groups_sha256": hash_file(paths["groups"]),
        "complete": True,
        "finalized": True,
    }
    if shard_manifest != expected_publication:
        raise CompilerScaleError(f"Wave 5 shard {part} publication declaration differs")

    cache_receipts = receipt.get("root_cache_receipts")
    if not isinstance(cache_receipts, list):
        raise CompilerScaleError(f"Wave 5 shard {part} root cache receipts are malformed")
    observed_cache_receipts: list[dict[str, Any]] = []
    for record in expected_records:
        cached = _load_root_cache(record, settings, policy)
        if cached is None:
            raise CompilerScaleError(f"Wave 5 shard {part} lacks root cache {record['root_id']}")
        terminal, path = cached
        observed_cache_receipts.append(
            {
                "root_id": str(terminal["root_id"]),
                "cache_key": str(terminal["cache_key"]),
                "cache_sha256": hash_file(path),
                "status": str(terminal["status"]),
            }
        )
    observed_cache_receipts.sort(key=lambda value: str(value["root_id"]))
    if cache_receipts != observed_cache_receipts:
        raise CompilerScaleError(f"Wave 5 shard {part} root cache receipts differ")
    if receipt.get("root_cache_receipts_sha256") != hash_canonical(observed_cache_receipts):
        raise CompilerScaleError(f"Wave 5 shard {part} root cache receipt hash differs")
    metrics = _mapping(receipt.get("completion_metrics"), f"Wave 5 shard {part} completion metrics")
    required_nonnegative_numbers = (
        "elapsed_seconds",
        "executor_construction_seconds",
        "first_lean_batch_wall_seconds",
        "roots_per_second",
        "rows_per_second",
        "projected_total_hours",
        "peak_rss_bytes",
        "peak_rss_gib",
        "cache_hits",
        "cache_hit_rate",
        "lean_requests",
    )
    if any(
        not isinstance(metrics.get(field), (int, float))
        or isinstance(metrics.get(field), bool)
        or float(metrics[field]) < 0
        for field in required_nonnegative_numbers
    ):
        raise CompilerScaleError(f"Wave 5 shard {part} completion metrics are malformed")
    if (
        not isinstance(metrics.get("failure_taxonomy"), dict)
        or not isinstance(metrics.get("decision"), str)
        or not isinstance(metrics.get("startup_measurement"), str)
    ):
        raise CompilerScaleError(f"Wave 5 shard {part} completion evidence is incomplete")
    if float(metrics["cache_hit_rate"]) > 1.0:
        raise CompilerScaleError(f"Wave 5 shard {part} cache-hit rate is impossible")


def _write_release_shard(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    part: int,
    input_records: Sequence[Mapping[str, Any]],
    terminals: Sequence[tuple[Mapping[str, Any], Path]],
    materialized: Wave4ClosureMaterialization,
    duplicate_dropped_roots: Sequence[str],
    pair_delta_balance_report: Mapping[str, Any],
    completion_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    n25_rows = _n25_row_count(materialized)
    if n25_rows > int(len(materialized.rows) * settings.n25_maximum_share):
        raise CompilerScaleError(f"Wave 5 shard {part} exceeds the N25 row cap before write")
    paths = _shard_paths(settings, part)
    rows = [dict(_mapping(record["row"], "model row")) for record in materialized.rows]
    sidecars: list[dict[str, Any]] = []
    for record in materialized.rows:
        sidecar = dict(_mapping(record["sidecar"], "row sidecar"))
        sidecar["core_family"] = str(
            sidecar.get("negative_operation") or sidecar.get("mechanism") or "unassigned"
        )
        sidecar["unordered_pair_key"] = record["unordered_pair_key"]
        sidecar["row_hash"] = record["row_hash"]
        sidecars.append(sidecar)
    groups = [dict(group.record) for group in materialized.groups]
    _write_jsonl_atomic(paths["rows"], rows)
    _write_jsonl_atomic(paths["sidecars"], sidecars)
    _write_jsonl_atomic(paths["groups"], groups)
    publication_manifest = {
        "schema_version": 1,
        "shard": part + 1,
        "row_count": len(rows),
        "rows_sha256": hash_file(paths["rows"]),
        "sidecars_sha256": hash_file(paths["sidecars"]),
        "closure_groups_sha256": hash_file(paths["groups"]),
        "complete": True,
        "finalized": True,
    }
    _write_exact(paths["manifest"], publication_manifest)
    root_ids = [str(record["root_id"]) for record in input_records]
    terminal_receipts = sorted(
        (
            {
                "root_id": str(terminal["root_id"]),
                "cache_key": str(terminal["cache_key"]),
                "cache_sha256": hash_file(path),
                "status": str(terminal["status"]),
            }
            for terminal, path in terminals
        ),
        key=lambda value: value["root_id"],
    )
    files = {
        kind: {
            "file": paths[kind].name,
            "bytes": paths[kind].stat().st_size,
            "sha256": hash_file(paths[kind]),
        }
        for kind in ("rows", "sidecars", "groups", "manifest")
    }
    negative_counts: Counter[str] = Counter(group.operation_id for group in materialized.groups)
    quarantined_group_ids = list(
        cast(Sequence[str], pair_delta_balance_report.get("quarantined_group_ids", []))
    )
    receipt: dict[str, Any] = {
        "artifact_kind": "sft1_wave5_compiler_release_shard",
        "schema_version": SCALE_SHARD_VERSION,
        "run_id": run_id,
        "part": part,
        "input_root_ids": root_ids,
        "input_record_hashes": [str(record["inventory_record_sha256"]) for record in input_records],
        "input_roots": len(input_records),
        "retained_roots": len({group.root_id for group in materialized.groups}),
        "rows": len(rows),
        "closure_groups": len(groups),
        "logical_rows": materialized.logical_row_count,
        "negative_family_groups": dict(sorted(negative_counts.items())),
        "n19_rows": 0,
        "n25_rows": n25_rows,
        "duplicate_dropped_roots": sorted(duplicate_dropped_roots),
        "pair_delta_balance_report": dict(pair_delta_balance_report),
        "pair_delta_balance_report_sha256": hash_canonical(pair_delta_balance_report),
        "pair_delta_quarantined_group_ids": quarantined_group_ids,
        "pair_delta_quarantined_group_count": len(quarantined_group_ids),
        "pair_delta_quarantined_group_ids_sha256": hash_canonical(quarantined_group_ids),
        "completion_metrics": dict(completion_metrics),
        "root_cache_receipts": terminal_receipts,
        "root_cache_receipts_sha256": hash_canonical(terminal_receipts),
        "publication_manifest": publication_manifest,
        "files": files,
    }
    _write_exact(paths["receipt"], receipt)
    return receipt


def _publication_queue_path(settings: CompilerScaleSettings, part: int) -> Path:
    return settings.output_root / "_state" / "publication_queue" / f"shard-{part + 1:04d}.json"


def _publication_receipt_path(settings: CompilerScaleSettings, part: int) -> Path:
    return settings.output_root / "_state" / "publication_receipts" / f"shard-{part + 1:04d}.json"


def _publication_checkpoint_path(settings: CompilerScaleSettings, checkpoint: int) -> Path:
    return (
        settings.output_root
        / "_state"
        / "publication_receipts"
        / "checkpoints"
        / f"rows-{checkpoint:010d}.json"
    )


def _publication_checkpoint_queue_path(settings: CompilerScaleSettings, checkpoint: int) -> Path:
    return (
        settings.output_root
        / "_state"
        / "publication_queue"
        / "checkpoints"
        / f"rows-{checkpoint:010d}.json"
    )


def _checkpoint_release_root(settings: CompilerScaleSettings, checkpoint: int) -> Path:
    return settings.output_root / "release" / "checkpoints" / f"rows-{checkpoint:010d}"


def _aggregate_publication_queue_path(settings: CompilerScaleSettings) -> Path:
    return settings.output_root / "_state" / "publication_queue" / "aggregate.json"


def _aggregate_publication_receipt_path(settings: CompilerScaleSettings) -> Path:
    return settings.output_root / "_state" / "publication_receipts" / "aggregate.json"


def _write_publication_queue(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    part: int,
    receipt: Mapping[str, Any],
    prior_rows: int,
) -> dict[str, Any]:
    paths = _shard_paths(settings, part)
    release_root = settings.output_root / "release"
    local_files = {
        str(paths[kind].relative_to(release_root)): hash_file(paths[kind])
        for kind in ("rows", "sidecars", "groups", "manifest")
    }
    files = {
        paths[kind].name: hash_file(paths[kind])
        for kind in ("rows", "sidecars", "groups", "manifest")
    }
    cumulative_rows = prior_rows + int(receipt["rows"])
    previous_queue = _publication_queue_path(settings, part - 1) if part else None
    payload = {
        "artifact_kind": "sft1_wave5_incremental_publication_queue_item",
        "schema_version": 1,
        "run_id": run_id,
        "part": part,
        "shard": part + 1,
        "local_release_root": str(release_root),
        "local_file_sha256": local_files,
        "local_shard_manifest_sha256": hash_file(paths["manifest"]),
        "files": files,
        "repo_id": settings.publication_repo_id,
        "repo_type": "dataset",
        "private_required": True,
        "remote_prefix": f"{settings.publication_prefix}/shards/shard-{part + 1:04d}",
        "overwrite_forbidden": True,
        "fresh_remote_verification_required": True,
        "post_commit_timeout_recovery": {
            "allowed": True,
            "upload_forbidden": True,
            "immutable_tree_and_parent_verification_required": True,
        },
        "required_parent": (
            {
                "kind": "latest_verified_publication_event",
                "previous_shard_receipt": str(_publication_receipt_path(settings, part - 1)),
                "previous_queue_sha256": hash_file(previous_queue),
            }
            if previous_queue is not None
            else {"kind": "record_repository_head_anchor_before_first_upload"}
        ),
        "rows": int(receipt["rows"]),
        "cumulative_rows": cumulative_rows,
        "checkpoints_crossed": [
            checkpoint
            for checkpoint in settings.checkpoints
            if prior_rows < checkpoint <= cumulative_rows
        ],
        "shard_receipt_sha256": hash_file(paths["receipt"]),
    }
    _write_exact(_publication_queue_path(settings, part), payload)
    return payload


def _ensure_publication_queue(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    queued: list[dict[str, Any]] = []
    prior_rows = 0
    for part, receipt in enumerate(receipts):
        queue = _write_publication_queue(
            settings,
            run_id=run_id,
            part=part,
            receipt=receipt,
            prior_rows=prior_rows,
        )
        queued.append(queue)
        prior_rows += int(receipt["rows"])
    return tuple(queued)


def _default_incremental_shard_uploader(
    *,
    repo_id: str,
    local_root: Path,
    files: Sequence[Path],
    remote_prefix: str,
    commit_message: str,
    expected_parent: str | None,
) -> tuple[str, str, Mapping[str, str]]:
    """Upload one immutable shard with the shared private/fresh verifier."""

    from huggingface_hub import HfApi

    from leanfaith.sft1.sprint import publish as publish_module

    api = HfApi()
    if expected_parent is not None:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset")
        if not bool(info.private):
            raise CompilerScaleError("refusing to publish a Wave 5 shard to a public repository")
        if str(info.sha) != expected_parent:
            raise CompilerScaleError(
                "Wave 5 Hub HEAD is not the prior verified shard revision; "
                "refusing an interleaved publication"
            )
    revision, parent, verified = publish_module._upload_verified(
        api,
        repo_id=repo_id,
        local_root=local_root,
        files=files,
        remote_prefix=remote_prefix,
        commit_message=commit_message,
        expected_parent=expected_parent,
    )
    return revision, parent, verified


def _default_incremental_shard_recovery_verifier(
    *,
    repo_id: str,
    local_root: Path,
    files: Sequence[Path],
    remote_prefix: str,
    commit_message: str,
    revision: str,
    parent_revision: str,
) -> Mapping[str, str]:
    """Verify a timed-out commit's immutable tree and immediate parent, without upload."""

    from huggingface_hub import HfApi

    from leanfaith.sft1.sprint import publish as publish_module

    api = HfApi()
    verification, verified = publish_module._verify_existing_prefix(
        api,
        repo_id=repo_id,
        revision=revision,
        remote_prefix=remote_prefix,
        local_root=local_root,
        files=files,
    )
    if verification.get("immutable_revision") != revision:
        raise CompilerScaleError("Wave 5 recovery did not verify the requested immutable tree")
    commits = api.list_repo_commits(repo_id=repo_id, repo_type="dataset", revision=revision)
    if len(commits) < 2 or str(commits[0].commit_id) != revision:
        raise CompilerScaleError("Wave 5 recovery history does not start at the requested revision")
    if str(commits[1].commit_id) != parent_revision:
        raise CompilerScaleError("Wave 5 recovery immutable parent differs")
    if str(commits[0].title) != commit_message:
        raise CompilerScaleError("Wave 5 recovery immutable commit title differs")
    return verified


def _validate_hub_revision(value: object, *, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise CompilerScaleError(f"{context} is not a full lowercase Hub revision")
    return value


def _record_incremental_publication(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    part: int,
    queue: Mapping[str, Any],
    revision: object,
    parent_revision: object,
    verified_remote_hashes: Mapping[str, str],
    previous_revision: str | None,
    verification_method: str,
    upload_performed: bool,
) -> dict[str, Any]:
    """Write the publication receipt last, after exact fresh remote verification."""

    revision_value = _validate_hub_revision(revision, context="published Hub revision")
    parent_value = _validate_hub_revision(parent_revision, context="published Hub parent revision")
    if revision_value == parent_value:
        raise CompilerScaleError("Wave 5 publication did not advance the Hub revision")
    if previous_revision is not None and parent_value != previous_revision:
        raise CompilerScaleError(
            "Wave 5 incremental shard commit is not a direct child of the prior shard revision"
        )
    prefix = str(queue["remote_prefix"]).rstrip("/")
    expected_remote = {
        f"{prefix}/{name}": digest for name, digest in cast(dict[str, str], queue["files"]).items()
    }
    if dict(verified_remote_hashes) != expected_remote:
        raise CompilerScaleError(
            "Wave 5 fresh remote verification does not match the queued shard bytes"
        )
    receipt = {
        "artifact_kind": "sft1_wave5_incremental_publication_receipt",
        "schema_version": 1,
        "run_id": run_id,
        "part": part,
        "queue_sha256": hash_file(_publication_queue_path(settings, part)),
        "repo_id": settings.publication_repo_id,
        "remote_prefix": queue["remote_prefix"],
        "private": True,
        "overwrite_performed": False,
        "atomic_commit": True,
        "fresh_remote_verification": verification_method == "fresh_download_sha256",
        "immutable_tree_verification": (
            verification_method == "immutable_hub_tree_digest_recovery"
        ),
        "verification_method": verification_method,
        "upload_performed": upload_performed,
        "verified_file_sha256": queue["files"],
        "verified_remote_file_sha256": expected_remote,
        "revision": revision_value,
        "parent_revision": parent_value,
        "checkpoints_crossed": queue["checkpoints_crossed"],
    }
    _write_once_exact(_publication_receipt_path(settings, part), receipt)
    return receipt


def _metadata_publication_files(
    queue: Mapping[str, Any], *, context: str
) -> tuple[Path, tuple[Path, ...]]:
    root = Path(str(queue["local_root"]))
    declared = _mapping(queue.get("files"), f"{context} files")
    files: list[Path] = []
    ordered = sorted(declared.items(), key=lambda item: (item[0] == "manifest.json", item[0]))
    for relative, expected_hash in ordered:
        path = root / relative
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise CompilerScaleError(f"{context} file escapes its local root") from exc
        if not path.is_file() or path.is_symlink() or hash_file(path) != expected_hash:
            raise CompilerScaleError(f"{context} file differs: {relative}")
        files.append(path)
    return root, tuple(files)


def _record_metadata_publication(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    metadata_kind: Literal["checkpoint", "aggregate"],
    queue_path: Path,
    receipt_path: Path,
    queue: Mapping[str, Any],
    revision: object,
    parent_revision: object,
    verified_remote_hashes: Mapping[str, str],
    previous_revision: str,
    verification_method: str,
    upload_performed: bool,
) -> dict[str, Any]:
    revision_value = _validate_hub_revision(revision, context="metadata Hub revision")
    parent_value = _validate_hub_revision(parent_revision, context="metadata Hub parent revision")
    if parent_value != previous_revision or revision_value == parent_value:
        raise CompilerScaleError("Wave 5 metadata publication breaks the revision chain")
    prefix = str(queue["remote_prefix"]).rstrip("/")
    expected_remote = {
        f"{prefix}/{name}": digest for name, digest in cast(dict[str, str], queue["files"]).items()
    }
    if dict(verified_remote_hashes) != expected_remote:
        raise CompilerScaleError("Wave 5 metadata remote verification differs")
    receipt = {
        "artifact_kind": "sft1_wave5_metadata_publication_receipt",
        "schema_version": SCALE_METADATA_PUBLICATION_VERSION,
        "metadata_kind": metadata_kind,
        "run_id": run_id,
        "queue_sha256": hash_file(queue_path),
        "repo_id": settings.publication_repo_id,
        "remote_prefix": queue["remote_prefix"],
        "private": True,
        "overwrite_performed": False,
        "atomic_commit": True,
        "fresh_remote_verification": verification_method == "fresh_download_sha256",
        "immutable_tree_verification": (
            verification_method == "immutable_hub_tree_digest_recovery"
        ),
        "verification_method": verification_method,
        "upload_performed": upload_performed,
        "verified_file_sha256": queue["files"],
        "verified_remote_file_sha256": expected_remote,
        "revision": revision_value,
        "parent_revision": parent_value,
        "content_manifest_sha256": queue["content_manifest_sha256"],
    }
    _write_once_exact(receipt_path, receipt)
    return receipt


def _validate_metadata_publication(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    metadata_kind: Literal["checkpoint", "aggregate"],
    queue_path: Path,
    receipt_path: Path,
    previous_revision: str,
) -> dict[str, Any] | None:
    if not receipt_path.is_file():
        return None
    queue = read_json_object(queue_path)
    receipt = read_json_object(receipt_path)
    checkpoint = queue.get("checkpoint_rows")
    if metadata_kind == "checkpoint" and type(checkpoint) is not int:
        raise CompilerScaleError("Wave 5 checkpoint queue lacks its row boundary")
    expected_prefix = (
        f"{settings.publication_prefix}/checkpoints/rows-{checkpoint:010d}"
        if metadata_kind == "checkpoint"
        else f"{settings.publication_prefix}/aggregate/{run_id}"
    )
    queue_expected = {
        "artifact_kind": "sft1_wave5_metadata_publication_queue",
        "schema_version": SCALE_METADATA_PUBLICATION_VERSION,
        "metadata_kind": metadata_kind,
        "run_id": run_id,
        "repo_id": settings.publication_repo_id,
        "private_required": True,
        "overwrite_forbidden": True,
        "fresh_remote_verification_required": True,
        "required_parent_revision": previous_revision,
        "remote_prefix": expected_prefix,
    }
    for field, value in queue_expected.items():
        if queue.get(field) != value:
            raise CompilerScaleError(f"Wave 5 {metadata_kind} queue differs at {field}")
    declared_files = _mapping(queue.get("files"), f"Wave 5 {metadata_kind} queue files")
    if queue.get("content_manifest_sha256") != declared_files.get("manifest.json"):
        raise CompilerScaleError(f"Wave 5 {metadata_kind} queue manifest binding differs")
    prefix = str(queue["remote_prefix"]).rstrip("/")
    expected = {
        "artifact_kind": "sft1_wave5_metadata_publication_receipt",
        "schema_version": SCALE_METADATA_PUBLICATION_VERSION,
        "metadata_kind": metadata_kind,
        "run_id": run_id,
        "queue_sha256": hash_file(queue_path),
        "repo_id": settings.publication_repo_id,
        "remote_prefix": queue["remote_prefix"],
        "private": True,
        "overwrite_performed": False,
        "atomic_commit": True,
        "verified_file_sha256": queue["files"],
        "verified_remote_file_sha256": {
            f"{prefix}/{name}": digest
            for name, digest in cast(dict[str, str], queue["files"]).items()
        },
        "parent_revision": previous_revision,
        "content_manifest_sha256": queue["content_manifest_sha256"],
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise CompilerScaleError(f"Wave 5 {metadata_kind} publication differs at {field}")
    mode = (
        receipt.get("verification_method"),
        receipt.get("upload_performed"),
        receipt.get("fresh_remote_verification"),
        receipt.get("immutable_tree_verification"),
    )
    if mode not in {
        ("fresh_download_sha256", True, True, False),
        ("immutable_hub_tree_digest_recovery", False, False, True),
    }:
        raise CompilerScaleError(f"Wave 5 {metadata_kind} verification mode differs")
    revision = _validate_hub_revision(receipt.get("revision"), context=f"{metadata_kind} revision")
    if revision == previous_revision:
        raise CompilerScaleError(f"Wave 5 {metadata_kind} revision did not advance")
    _metadata_publication_files(queue, context=f"Wave 5 {metadata_kind} publication")
    return receipt


def _checkpoint_commit_message(queue: Mapping[str, Any]) -> str:
    return (
        "sft1 wave5: publish proof-certified checkpoint "
        f"{int(queue['checkpoint_rows'])} ({int(queue['actual_rows'])} rows)"
    )


def _aggregate_commit_message(queue: Mapping[str, Any]) -> str:
    return f"sft1 wave5: publish final aggregate {queue['run_id']}"


def _checkpoint_screen_sample(
    settings: CompilerScaleSettings, through_part: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    def records() -> Iterator[dict[str, Any]]:
        for part in range(through_part + 1):
            paths = _shard_paths(settings, part)
            with (
                paths["rows"].open("r", encoding="utf-8") as rows,
                paths["sidecars"].open("r", encoding="utf-8") as sidecars,
            ):
                for row_line, sidecar_line in zip(rows, sidecars, strict=True):
                    if row_line.strip():
                        yield {"row": json.loads(row_line), "sidecar": json.loads(sidecar_line)}

    total = 0
    per_root: Counter[str] = Counter()
    pair_delta_cells: dict[str, Counter[str]] = {}
    for record in records():
        total += 1
        per_root[str(_mapping(record["sidecar"], "checkpoint sidecar")["root_id"])] += 1
        cell = str(wave3_pair_delta(record)["cell"])
        label = "positive" if _mapping(record["row"], "checkpoint row")["label"] else "negative"
        pair_delta_cells.setdefault(cell, Counter())[label] += 1
    cells = {
        cell: dict(sorted(counts.items())) for cell, counts in sorted(pair_delta_cells.items())
    }
    if total <= shortcut_module.SCREEN_MAX_ROWS:
        return list(records()), {
            "rows_total": total,
            "rows_screened": total,
            "method": "full_view",
            "pair_delta_cells": cells,
        }
    ranked = sorted(
        per_root,
        key=lambda root: hash_canonical([shortcut_module.SCREEN_SAMPLE_SALT, root]),
    )
    chosen: set[str] = set()
    sampled_rows = 0
    for root in ranked:
        if sampled_rows + per_root[root] > shortcut_module.SCREEN_MAX_ROWS:
            break
        chosen.add(root)
        sampled_rows += per_root[root]
    sample = [
        record
        for record in records()
        if str(_mapping(record["sidecar"], "checkpoint sidecar")["root_id"]) in chosen
    ]
    return sample, {
        "rows_total": total,
        "rows_screened": len(sample),
        "roots_screened": len(chosen),
        "method": "stable_salted_root_hash_prefix_whole_roots",
        "salt": shortcut_module.SCREEN_SAMPLE_SALT,
        "max_rows": shortcut_module.SCREEN_MAX_ROWS,
        "pair_delta_cells": cells,
    }


def _ensure_checkpoint_bundle(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    checkpoint: int,
    through_part: int,
    receipts: Sequence[Mapping[str, Any]],
    shard_publications: Sequence[Mapping[str, Any]],
    prior_events: Sequence[Mapping[str, Any]],
    previous_revision: str,
) -> dict[str, Any]:
    included = receipts[: through_part + 1]
    actual_rows = sum(int(item["rows"]) for item in included)
    if actual_rows < checkpoint or len(shard_publications) < through_part + 1:
        raise CompilerScaleError("Wave 5 checkpoint bundle is premature")
    root = _checkpoint_release_root(settings, checkpoint)
    aggregates = _release_aggregates(settings, included)
    sample, sample_info = _checkpoint_screen_sample(settings, through_part)
    shortcut = shortcut_module.run_screens_v3(sample)
    pairwise = shortcut_module.pairwise_shortcut_diagnostics(sample)
    screen_by_name = {
        str(item.get("name")): item
        for item in cast(Sequence[Mapping[str, Any]], shortcut.get("screens", []))
    }
    cells = cast(Mapping[str, Mapping[str, int]], sample_info["pair_delta_cells"])
    offending_cells = {
        cell: dict(counts)
        for cell, counts in cells.items()
        if counts.get("positive", 0) != counts.get("negative", 0)
    }
    pair_delta_policy = {
        "policy": "exact_label_balance_per_joint_pair_delta_cell_v1",
        "offending_cells": offending_cells,
        "failure_action": "block_checkpoint_then_lean_free_rebalance_or_cell_quarantine",
        "passed": not offending_cells,
    }
    useful_families = sorted(
        operation
        for operation, count in cast(dict[str, int], aggregates["negative_family_groups"]).items()
        if count > 0
        and operation not in {"N19_WHOLE_CLAIM_NEGATION_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"}
    )
    requested_families = {
        operation
        for operation in settings.typed_spec.operations
        if operation.startswith("N")
        and operation not in {"N19_WHOLE_CLAIM_NEGATION_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"}
    }
    required_useful_families = min(3, len(requested_families))
    shard_bindings = [
        {
            "shard": part + 1,
            "local_manifest_sha256": hash_file(_shard_paths(settings, part)["manifest"]),
            "local_receipt_sha256": hash_file(_shard_paths(settings, part)["receipt"]),
            "remote_prefix": shard_publications[part]["remote_prefix"],
            "revision": shard_publications[part]["revision"],
            "publication_receipt_sha256": hash_file(_publication_receipt_path(settings, part)),
        }
        for part in range(through_part + 1)
    ]
    checks = {
        "checkpoint_reached": actual_rows >= checkpoint,
        "all_included_shards_complete": all(
            item.get("complete") is True and item.get("finalized") is True
            for item in (
                _mapping(receipt["publication_manifest"], "checkpoint shard")
                for receipt in included
            )
        ),
        "all_included_shards_published_and_verified": len(shard_bindings) == through_part + 1,
        "proof_certified_core_only": True,
        "n19_forbidden": "N19_WHOLE_CLAIM_NEGATION_V1"
        not in cast(dict[str, int], aggregates["negative_family_groups"]),
        "n25_share_capped": sum(int(item["n25_rows"]) for item in included)
        <= int(actual_rows * settings.n25_maximum_share),
        "useful_negative_family_yield": len(useful_families) >= required_useful_families,
        "pair_delta_cells_balanced_or_quarantined": pair_delta_policy["passed"] is True,
        "pair_delta_diagnostics_passed": pairwise.get("rows") == len(sample)
        and bool(pairwise.get("rules"))
        and float(pairwise.get("max_balanced_accuracy", 1.0)) <= 0.65,
        "candidate_only_shortcut_screen_passed": screen_by_name.get("candidate_only", {}).get(
            "passed"
        )
        is True,
        "reference_only_shortcut_screen_passed": screen_by_name.get("reference_only", {}).get(
            "passed"
        )
        is True,
        "family_held_out_shortcut_screen_passed": screen_by_name.get("family_held_out", {}).get(
            "passed"
        )
        is True,
        "complete_shortcut_screen_contract_passed": shortcut.get("passed") is True,
    }
    if not all(checks.values()):
        raise CompilerScaleError("Wave 5 checkpoint integrity gate failed")
    run_spec = read_json_object(settings.run_spec_path)
    release_report = {
        "artifact_kind": "sft1_wave5_checkpoint_release_report",
        "schema_version": 1,
        "run_id": run_id,
        "run_spec_sha256": hash_file(settings.run_spec_path),
        "typed_certificate_gate_terminal_sha256": run_spec[
            "typed_certificate_gate_terminal_sha256"
        ],
        "checkpoint_rows": checkpoint,
        "actual_rows": actual_rows,
        "processed_roots": sum(int(item["input_roots"]) for item in included),
        "unique_ancestry_roots": aggregates["unique_ancestry_roots"],
        "negative_family_groups": aggregates["negative_family_groups"],
        "preserving_family_groups": aggregates["preserving_family_groups"],
        "failure_taxonomy": aggregates["failure_taxonomy"],
        "useful_negative_families": useful_families,
        "required_useful_negative_families": required_useful_families,
        "shortcut_sample": sample_info,
        "shortcut_screens": shortcut,
        "pair_delta_diagnostics": pairwise,
        "pair_delta_policy": pair_delta_policy,
        "published_shards": shard_bindings,
        "prior_publication_events_sha256": hash_canonical(prior_events),
        "checks": checks,
        "passed": all(checks.values()),
    }
    integrity_report = {
        "artifact_kind": "sft1_wave5_checkpoint_integrity_report",
        "schema_version": 1,
        "run_id": run_id,
        "run_spec_sha256": hash_file(settings.run_spec_path),
        "typed_certificate_gate_terminal_sha256": run_spec[
            "typed_certificate_gate_terminal_sha256"
        ],
        "checkpoint_rows": checkpoint,
        "rows_checked": actual_rows,
        "checks": checks,
        "issues": [name for name, passed in checks.items() if not passed],
        "issue_counts": {name: 0 if passed else 1 for name, passed in checks.items()},
        "passed": all(checks.values()),
    }
    report_path = root / "release_report.json"
    integrity_path = root / "integrity_report.json"
    _write_once_exact(report_path, release_report)
    _write_once_exact(integrity_path, integrity_report)
    manifest = {
        "artifact_kind": "sft1_wave5_proof_certified_checkpoint",
        "schema_version": 1,
        "run_id": run_id,
        "run_spec_sha256": hash_file(settings.run_spec_path),
        "typed_certificate_gate_terminal_sha256": run_spec[
            "typed_certificate_gate_terminal_sha256"
        ],
        "checkpoint_rows": checkpoint,
        "actual_rows": actual_rows,
        "proof_certified_core_only": True,
        "lower_confidence_rows": 0,
        "row_fields": ["reference", "candidate", "label"],
        "published_shards": shard_bindings,
        "published_shards_sha256": hash_canonical(shard_bindings),
        "prior_publication_events_sha256": hash_canonical(prior_events),
        "release_report_sha256": hash_file(report_path),
        "integrity_report_sha256": hash_file(integrity_path),
        "finalized": True,
    }
    manifest_path = root / "manifest.json"
    _write_once_exact(manifest_path, manifest)
    files = {path.name: hash_file(path) for path in (report_path, integrity_path, manifest_path)}
    queue = {
        "artifact_kind": "sft1_wave5_metadata_publication_queue",
        "schema_version": SCALE_METADATA_PUBLICATION_VERSION,
        "metadata_kind": "checkpoint",
        "run_id": run_id,
        "checkpoint_rows": checkpoint,
        "actual_rows": actual_rows,
        "local_root": str(root),
        "files": files,
        "content_manifest_sha256": hash_file(manifest_path),
        "repo_id": settings.publication_repo_id,
        "private_required": True,
        "overwrite_forbidden": True,
        "fresh_remote_verification_required": True,
        "required_parent_revision": previous_revision,
        "remote_prefix": f"{settings.publication_prefix}/checkpoints/rows-{checkpoint:010d}",
    }
    _write_once_exact(_publication_checkpoint_queue_path(settings, checkpoint), queue)
    return queue


def _ensure_aggregate_publication_queue(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    publication_events: Sequence[Mapping[str, Any]],
    previous_revision: str,
) -> dict[str, Any]:
    root = settings.output_root / "release"
    manifest_path = root / "manifest.json"
    files_to_publish = [
        root / "release_report.json",
        root / "integrity_report.json",
        root / "pairwise_diagnostics.json",
        *[
            _milestone_release_path(settings, value)
            for value in PILOT_ROOT_MILESTONES
            if _milestone_release_path(settings, value).is_file()
        ],
        manifest_path,
    ]
    files = {path.relative_to(root).as_posix(): hash_file(path) for path in files_to_publish}
    queue = {
        "artifact_kind": "sft1_wave5_metadata_publication_queue",
        "schema_version": SCALE_METADATA_PUBLICATION_VERSION,
        "metadata_kind": "aggregate",
        "run_id": run_id,
        "local_root": str(root),
        "files": files,
        "content_manifest_sha256": hash_file(manifest_path),
        "publication_events_sha256": hash_canonical(publication_events),
        "repo_id": settings.publication_repo_id,
        "private_required": True,
        "overwrite_forbidden": True,
        "fresh_remote_verification_required": True,
        "required_parent_revision": previous_revision,
        "remote_prefix": f"{settings.publication_prefix}/aggregate/{run_id}",
    }
    _write_once_exact(_aggregate_publication_queue_path(settings), queue)
    return queue


def _validated_publication_chain(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    receipts: Sequence[Mapping[str, Any]],
    recompute_checkpoints: bool = False,
) -> PublicationChain:
    """Validate the contiguous, fresh-verified immutable Hub revision chain."""

    verified: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    previous_revision: str | None = None
    seen_revisions: set[str] = set()
    pending_checkpoint: tuple[int, int] | None = None
    for part, _receipt in enumerate(receipts):
        path = _publication_receipt_path(settings, part)
        if not path.is_file():
            break
        publication = read_json_object(path)
        queue_path = _publication_queue_path(settings, part)
        queue = read_json_object(queue_path)
        expected = {
            "artifact_kind": "sft1_wave5_incremental_publication_receipt",
            "schema_version": 1,
            "run_id": run_id,
            "part": part,
            "queue_sha256": hash_file(queue_path),
            "repo_id": settings.publication_repo_id,
            "remote_prefix": queue["remote_prefix"],
            "private": True,
            "fresh_remote_verification": publication.get("fresh_remote_verification"),
            "immutable_tree_verification": publication.get("immutable_tree_verification"),
            "verification_method": publication.get("verification_method"),
            "upload_performed": publication.get("upload_performed"),
            "verified_file_sha256": queue["files"],
            "verified_remote_file_sha256": {
                f"{str(queue['remote_prefix']).rstrip('/')}/{name}": digest
                for name, digest in cast(dict[str, str], queue["files"]).items()
            },
            "overwrite_performed": False,
            "atomic_commit": True,
            "checkpoints_crossed": queue["checkpoints_crossed"],
        }
        for field, value in expected.items():
            if publication.get(field) != value:
                raise CompilerScaleError(
                    f"Wave 5 incremental publication receipt {part} differs at {field}"
                )
        method = publication.get("verification_method")
        upload_performed = publication.get("upload_performed")
        if (
            method,
            upload_performed,
            publication.get("fresh_remote_verification"),
            publication.get("immutable_tree_verification"),
        ) not in {
            ("fresh_download_sha256", True, True, False),
            ("immutable_hub_tree_digest_recovery", False, False, True),
        }:
            raise CompilerScaleError(
                f"Wave 5 incremental publication receipt {part} has invalid verification mode"
            )
        revision = publication.get("revision")
        parent_revision = publication.get("parent_revision")
        if (
            not isinstance(revision, str)
            or len(revision) != 40
            or any(char not in "0123456789abcdef" for char in revision)
            or not isinstance(parent_revision, str)
            or len(parent_revision) != 40
            or any(char not in "0123456789abcdef" for char in parent_revision)
        ):
            raise CompilerScaleError("Wave 5 publication revision chain is malformed")
        if revision == parent_revision or revision in seen_revisions:
            raise CompilerScaleError("Wave 5 publication revision chain contains a cycle")
        if previous_revision is not None and parent_revision != previous_revision:
            raise CompilerScaleError("Wave 5 publication receipts do not form a parent chain")
        seen_revisions.add(revision)
        previous_revision = revision
        verified.append(publication)
        events.append(publication)
        for checkpoint in cast(list[int], queue["checkpoints_crossed"]):
            checkpoint_path = _publication_checkpoint_path(settings, checkpoint)
            if not checkpoint_path.is_file():
                pending_checkpoint = (checkpoint, part)
                break
            if recompute_checkpoints:
                _ensure_checkpoint_bundle(
                    settings,
                    run_id=run_id,
                    checkpoint=checkpoint,
                    through_part=part,
                    receipts=receipts,
                    shard_publications=verified,
                    prior_events=events,
                    previous_revision=previous_revision,
                )
            checkpoint_receipt = _validate_metadata_publication(
                settings,
                run_id=run_id,
                metadata_kind="checkpoint",
                queue_path=_publication_checkpoint_queue_path(settings, checkpoint),
                receipt_path=checkpoint_path,
                previous_revision=previous_revision,
            )
            if checkpoint_receipt is None:
                raise AssertionError("checkpoint receipt disappeared during validation")
            checkpoint_revision = str(checkpoint_receipt["revision"])
            if checkpoint_revision in seen_revisions:
                raise CompilerScaleError("Wave 5 metadata revision chain contains a cycle")
            seen_revisions.add(checkpoint_revision)
            previous_revision = checkpoint_revision
            events.append(checkpoint_receipt)
        if pending_checkpoint is not None:
            break
    for later in range(len(verified), len(receipts)):
        if _publication_receipt_path(settings, later).is_file():
            raise CompilerScaleError("Wave 5 publication receipts are not a contiguous prefix")
    return PublicationChain(
        shards=tuple(verified),
        events=tuple(events),
        head_revision=previous_revision,
        pending_checkpoint=pending_checkpoint,
    )


def _incremental_commit_message(queue: Mapping[str, Any]) -> str:
    return (
        "sft1 wave5: publish proof-certified shard "
        f"{int(queue['shard']):04d} ({int(queue['rows'])} rows)"
    )


def _queued_shard_files(
    settings: CompilerScaleSettings,
    *,
    part: int,
    queue: Mapping[str, Any],
) -> tuple[Path, tuple[Path, ...]]:
    paths = _shard_paths(settings, part)
    root = paths["rows"].parent
    ordered = tuple(paths[kind] for kind in ("rows", "sidecars", "groups", "manifest"))
    expected = {path.name: hash_file(path) for path in ordered}
    if queue.get("files") != expected:
        raise CompilerScaleError(
            f"Wave 5 publication queue {part} no longer binds the local shard bytes"
        )
    if queue.get("local_shard_manifest_sha256") != expected["manifest.json"]:
        raise CompilerScaleError(
            f"Wave 5 publication queue {part} no longer binds the manifest-last marker"
        )
    return root, ordered


def _empty_materialization() -> Wave4ClosureMaterialization:
    return Wave4ClosureMaterialization(rows=(), groups=())


def _group_negative_operation(group: Any) -> str:
    record = _mapping(getattr(group, "record", None), "Wave 5 closure group")
    operation = record.get("negative_operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise CompilerScaleError("Wave 5 closure group has an unknown negative operation")
    return operation


def _n25_row_count(materialized: Wave4ClosureMaterialization) -> int:
    return len(
        {
            row_id
            for group in materialized.groups
            if _group_negative_operation(group) == "N25_TOGGLE_EQ_NE_PROOF_V1"
            for row_id in group.row_ids
        }
    )


def _merge_materializations(
    materials: Sequence[Wave4ClosureMaterialization],
    *,
    prior_unordered: set[str],
) -> tuple[Wave4ClosureMaterialization, tuple[str, ...]]:
    accepted: list[Wave4ClosureMaterialization] = []
    seen = set(prior_unordered)
    dropped: list[str] = []
    for material in materials:
        root_ids = {group.root_id for group in material.groups}
        if len(root_ids) != 1:
            raise CompilerScaleError("one compiler root cache spans multiple ancestry roots")
        root_id = next(iter(root_ids))
        keys = {str(record["unordered_pair_key"]) for record in material.rows}
        if keys & seen:
            dropped.append(root_id)
            continue
        seen.update(keys)
        accepted.append(material)
    if not accepted:
        return _empty_materialization(), tuple(sorted(dropped))
    merged = materialize_wave4_records(
        [record for material in accepted for record in material.rows],
        [group.record for material in accepted for group in material.groups],
    )
    return merged, tuple(sorted(dropped))


def _read_completed_receipts(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    selected: Sequence[Mapping[str, Any]],
    policy: OrbitPolicy,
) -> tuple[list[dict[str, Any]], set[str]]:
    receipts: list[dict[str, Any]] = []
    unordered: set[str] = set()
    chunks = _scale_chunks(selected, settings.roots_per_shard)
    for part, records in enumerate(chunks):
        path = _shard_paths(settings, part)["receipt"]
        if not path.is_file():
            break
        try:
            receipt = read_json_object(path)
        except (OSError, ValueError, StoreError) as exc:
            raise CompilerScaleError(f"cannot read Wave 5 shard receipt {path}") from exc
        _validate_shard_receipt(
            settings,
            receipt,
            run_id=run_id,
            part=part,
            expected_records=records,
            policy=policy,
        )
        receipts.append(receipt)
        sidecars_path = _shard_paths(settings, part)["sidecars"]
        with sidecars_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    key = str(_mapping(json.loads(line), "completed sidecar")["unordered_pair_key"])
                    if key in unordered:
                        raise CompilerScaleError("completed Wave 5 shards repeat an unordered pair")
                    unordered.add(key)
    for later in range(len(receipts) + 1, len(chunks)):
        if _shard_paths(settings, later)["receipt"].is_file():
            raise CompilerScaleError("Wave 5 completed shards are not a contiguous prefix")
    return receipts, unordered


def _runtime_projection(
    *, started: float, processed_roots: int, selected_roots: int, limit_hours: float
) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started)
    rate = processed_roots / elapsed if elapsed > 0 else 0.0
    projected_total = selected_roots / rate if rate > 0 else None
    projected_remaining = (
        max(0.0, projected_total - elapsed) if projected_total is not None else None
    )
    exceeds = projected_total is not None and projected_total > limit_hours * 3600
    return {
        "wall_seconds": round(elapsed, 6),
        "processed_roots_per_second": round(rate, 8),
        "projected_total_seconds": (
            round(projected_total, 3) if projected_total is not None else None
        ),
        "projected_remaining_seconds": (
            round(projected_remaining, 3) if projected_remaining is not None else None
        ),
        "projection_limit_hours": limit_hours,
        "exceeds_local_runtime_limit": exceeds,
        "migration_requirement": (
            "CPU/RAM host with two persistent Lean workers and at least 40 GiB measured RSS; "
            "GPU acceleration does not reduce Lean elaboration time"
            if exceeds
            else None
        ),
    }


def _runtime_migration_required(*, processed_roots: int, projection: Mapping[str, Any]) -> bool:
    """Decide only after the required 10K pilot; earlier projections are telemetry."""

    return (
        processed_roots >= RUNTIME_PROJECTION_DECISION_ROOTS
        and projection.get("exceeds_local_runtime_limit") is True
    )


def _pilot_decision(
    *,
    processed_roots: int,
    peak_rss_bytes: int,
    rss_limit_bytes: int,
    projection: Mapping[str, Any],
) -> str:
    if peak_rss_bytes > rss_limit_bytes:
        return "migrate_measured_rss_exceeded"
    if processed_roots < 100:
        return "continue_to_100_root_measurement"
    if processed_roots < RUNTIME_PROJECTION_DECISION_ROOTS:
        return "continue_to_10000_root_measurement"
    if _runtime_migration_required(processed_roots=processed_roots, projection=projection):
        return "migrate_projected_runtime_exceeds_limit"
    return "continue_authorized_scale"


def _failure_taxonomy_through_chunk(
    settings: CompilerScaleSettings,
    prior_receipts: Sequence[Mapping[str, Any]],
    terminals: Sequence[tuple[Mapping[str, Any], Path]],
) -> dict[str, int]:
    taxonomy: Counter[str] = Counter()
    for receipt in prior_receipts:
        for cache_value in cast(list[object], receipt["root_cache_receipts"]):
            cache_receipt = _mapping(cache_value, "Wave 5 milestone cache receipt")
            terminal = read_json_object(_root_cache_path(settings, str(cache_receipt["cache_key"])))
            taxonomy[str(terminal["taxonomy"])] += 1
    for terminal_record, _path in terminals:
        taxonomy[str(terminal_record["taxonomy"])] += 1
    return dict(sorted(taxonomy.items()))


def _lean_requests_through_receipts(
    settings: CompilerScaleSettings, receipts: Sequence[Mapping[str, Any]]
) -> int:
    requests = 0
    for receipt in receipts:
        for cache_value in cast(list[object], receipt["root_cache_receipts"]):
            cache_receipt = _mapping(cache_value, "Wave 5 execution cache receipt")
            terminal = read_json_object(_root_cache_path(settings, str(cache_receipt["cache_key"])))
            requests += int(_mapping(terminal["execution"], "root execution")["lean_requests"])
    return requests


def _completion_metrics(
    settings: CompilerScaleSettings,
    *,
    started: float,
    selected_roots: int,
    processed_roots: int,
    released_rows: int,
    cache_hits: int,
    lean_requests: int,
    peak_rss_bytes: int,
    executor_construction_seconds: float,
    first_lean_batch_wall_seconds: float,
    failure_taxonomy: Mapping[str, int],
    prior_elapsed_seconds: float,
) -> dict[str, Any]:
    if cache_hits > processed_roots:
        raise CompilerScaleError("Wave 5 cache hits exceed processed roots")
    invocation_elapsed = max(0.0, time.monotonic() - started)
    elapsed = round(prior_elapsed_seconds + invocation_elapsed, 6)
    rate = processed_roots / elapsed if elapsed > 0 else 0.0
    projected_seconds = selected_roots / rate if rate > 0 else None
    projection = {
        "exceeds_local_runtime_limit": (
            projected_seconds is not None
            and projected_seconds > settings.projected_runtime_limit_hours * 3600
        )
    }
    projected_hours = (
        round(float(projected_seconds) / 3600, 6)
        if isinstance(projected_seconds, (int, float))
        else 0.0
    )
    rss_limit = int(settings.audit.lean_rss_claim_gib * (1024**3))
    return {
        "processed_roots": processed_roots,
        "released_rows": released_rows,
        "elapsed_seconds": elapsed,
        "invocation_elapsed_seconds": round(invocation_elapsed, 6),
        "executor_construction_seconds": round(executor_construction_seconds, 6),
        "first_lean_batch_wall_seconds": round(first_lean_batch_wall_seconds, 6),
        "startup_measurement": (
            "first_lean_batch_wall_seconds includes lazy Lean backend startup and first-batch "
            "elaboration"
            if first_lean_batch_wall_seconds > 0
            else "lean_backend_not_started_cache_only"
        ),
        "roots_per_second": round(processed_roots / elapsed, 8) if elapsed > 0 else 0.0,
        "rows_per_second": round(released_rows / elapsed, 8) if elapsed > 0 else 0.0,
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / processed_roots, 8) if processed_roots else 0.0,
        "lean_requests": lean_requests,
        "failure_taxonomy": dict(sorted(failure_taxonomy.items())),
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_gib": round(peak_rss_bytes / (1024**3), 6),
        "projected_total_hours": projected_hours,
        "projection_limit_hours": settings.projected_runtime_limit_hours,
        "projection_exceeds_limit": projection["exceeds_local_runtime_limit"],
        "decision": _pilot_decision(
            processed_roots=processed_roots,
            peak_rss_bytes=peak_rss_bytes,
            rss_limit_bytes=rss_limit,
            projection=projection,
        ),
    }


def _milestone_path(settings: CompilerScaleSettings, roots: int) -> Path:
    return settings.output_root / "_state" / "milestones" / f"roots-{roots:010d}.json"


def _milestone_release_path(settings: CompilerScaleSettings, roots: int) -> Path:
    return settings.output_root / "release" / "milestones" / f"roots-{roots:010d}.json"


def _ensure_milestone_receipts(
    settings: CompilerScaleSettings,
    *,
    run_id: str,
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Materialize immutable pilot evidence from hash-bound shard completion metrics."""

    cumulative_roots = 0
    cumulative_rows = 0
    reached: dict[int, dict[str, Any]] = {}
    shard_bindings: list[dict[str, Any]] = []
    for part, receipt in enumerate(receipts):
        cumulative_roots += int(receipt["input_roots"])
        cumulative_rows += int(receipt["rows"])
        metrics = _mapping(receipt["completion_metrics"], "milestone completion metrics")
        if metrics.get("processed_roots") != cumulative_roots:
            raise CompilerScaleError("Wave 5 completion metrics root count differs")
        if metrics.get("released_rows") != cumulative_rows:
            raise CompilerScaleError("Wave 5 completion metrics row count differs")
        shard_path = _shard_paths(settings, part)["receipt"]
        shard_bindings.append(
            {
                "part": part,
                "shard": part + 1,
                "receipt_sha256": hash_file(shard_path),
                "manifest_sha256": hash_file(_shard_paths(settings, part)["manifest"]),
                "cumulative_roots": cumulative_roots,
                "cumulative_rows": cumulative_rows,
            }
        )
        if cumulative_roots not in PILOT_ROOT_MILESTONES:
            continue
        payload = {
            "artifact_kind": "sft1_wave5_compiler_scale_milestone",
            "schema_version": SCALE_MILESTONE_VERSION,
            "run_id": run_id,
            "milestone_roots": cumulative_roots,
            "processed_roots": cumulative_roots,
            "released_rows": cumulative_rows,
            "elapsed_seconds": metrics["elapsed_seconds"],
            "executor_construction_seconds": metrics["executor_construction_seconds"],
            "first_lean_batch_wall_seconds": metrics["first_lean_batch_wall_seconds"],
            "startup_measurement": metrics["startup_measurement"],
            "roots_per_second": metrics["roots_per_second"],
            "rows_per_second": metrics["rows_per_second"],
            "cache_hits": metrics["cache_hits"],
            "cache_hit_rate": metrics["cache_hit_rate"],
            "lean_requests": metrics["lean_requests"],
            "failure_taxonomy": metrics["failure_taxonomy"],
            "peak_rss_bytes": metrics["peak_rss_bytes"],
            "peak_rss_gib": metrics["peak_rss_gib"],
            "projected_total_hours": metrics["projected_total_hours"],
            "projection_limit_hours": metrics["projection_limit_hours"],
            "projection_exceeds_limit": metrics["projection_exceeds_limit"],
            "decision": metrics["decision"],
            "run_spec_sha256": hash_file(settings.run_spec_path),
            "selection_sha256": hash_file(settings.selection_path),
            "completed_shards": list(shard_bindings),
            "completed_shards_sha256": hash_canonical(shard_bindings),
        }
        _write_once_exact(_milestone_path(settings, cumulative_roots), payload)
        _write_once_exact(_milestone_release_path(settings, cumulative_roots), payload)
        reached[cumulative_roots] = payload
    for milestone in PILOT_ROOT_MILESTONES:
        if cumulative_roots >= milestone and milestone not in reached:
            raise CompilerScaleError(
                f"Wave 5 completed roots skipped required milestone {milestone}"
            )
    expected_names = {f"roots-{value:010d}.json" for value in reached}
    for directory in (
        settings.output_root / "_state" / "milestones",
        settings.output_root / "release" / "milestones",
    ):
        if {path.name for path in directory.glob("roots-*.json")} != expected_names:
            raise CompilerScaleError("Wave 5 milestone directory contains unbound evidence")
    return tuple(
        {
            "roots": milestone,
            "file": f"milestones/roots-{milestone:010d}.json",
            "sha256": hash_file(_milestone_release_path(settings, milestone)),
            "decision": reached[milestone]["decision"],
        }
        for milestone in PILOT_ROOT_MILESTONES
        if milestone in reached
    )


def _release_aggregates(
    settings: CompilerScaleSettings,
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    labels: Counter[str] = Counter()
    negative_families: Counter[str] = Counter()
    preserving_families: Counter[str] = Counter()
    failure_taxonomy: Counter[str] = Counter()
    root_ids: set[str] = set()
    lean_requests = 0
    lean_elapsed_ms = 0
    for part, receipt in enumerate(receipts):
        paths = _shard_paths(settings, part)
        with paths["rows"].open("r", encoding="utf-8") as rows_handle:
            for line in rows_handle:
                if line.strip():
                    row = _mapping(json.loads(line), "Wave 5 aggregate model row")
                    labels["positive" if row.get("label") is True else "negative"] += 1
        with paths["groups"].open("r", encoding="utf-8") as groups_handle:
            for line in groups_handle:
                if not line.strip():
                    continue
                group = _mapping(json.loads(line), "Wave 5 aggregate closure group")
                negative = group.get("negative_operation")
                if not isinstance(negative, str) or negative not in OPERATIONS:
                    raise CompilerScaleError("Wave 5 aggregate has an unknown negative family")
                negative_families[negative] += 1
                chain = group.get("preserving_mechanism_chain")
                if not isinstance(chain, list) or not all(
                    isinstance(value, str) and value for value in chain
                ):
                    raise CompilerScaleError("Wave 5 aggregate has a malformed preserving chain")
                preserving_families.update(set(cast(list[str], chain)))
                root_id = group.get("root_id")
                if not isinstance(root_id, str):
                    raise CompilerScaleError("Wave 5 aggregate closure lacks its ancestry root")
                root_ids.add(root_id)
        for cache_receipt_value in cast(list[object], receipt["root_cache_receipts"]):
            cache_receipt = _mapping(cache_receipt_value, "Wave 5 aggregate cache receipt")
            key = str(cache_receipt["cache_key"])
            path = _root_cache_path(settings, key)
            terminal = read_json_object(path)
            taxonomy = terminal.get("taxonomy")
            if not isinstance(taxonomy, str):
                raise CompilerScaleError("Wave 5 root terminal taxonomy is malformed")
            failure_taxonomy[taxonomy] += 1
            execution = _mapping(terminal.get("execution"), "Wave 5 root execution")
            lean_requests += int(execution.get("lean_requests", 0))
            lean_elapsed_ms += int(execution.get("lean_elapsed_ms", 0))
    return {
        "labels": dict(sorted(labels.items())),
        "negative_family_groups": dict(sorted(negative_families.items())),
        "preserving_family_groups": dict(sorted(preserving_families.items())),
        "failure_taxonomy": dict(sorted(failure_taxonomy.items())),
        "unique_ancestry_roots": len(root_ids),
        "lean_requests": lean_requests,
        "lean_elapsed_ms": lean_elapsed_ms,
    }


class CompilerScaleRunner:
    """Manifest-last, root-cached Wave 5 scale orchestration."""

    def __init__(
        self,
        settings: CompilerScaleSettings,
        *,
        executor_factory: ExecutorFactory | None = None,
        audit_replay: AuditReplay = _default_audit_replay,
        typed_gate_verifier: TypedGateVerifier = _default_typed_gate_verifier,
        source_resolver: SourceResolver = resolve_audit_sources,
        manage_resources: bool = True,
    ) -> None:
        self.settings = settings
        self.loaded_wave4 = load_wave4_config(
            find_repo_root(settings.wave4_config_path), settings.wave4_config_path
        )
        if self.loaded_wave4.policy.maximum_depth < settings.typed_spec.maximum_depth:
            raise CompilerScaleError("typed Wave 5 depth exceeds the Wave 4 policy")
        if (
            self.loaded_wave4.policy.maximum_variants_per_root
            < settings.typed_spec.maximum_variants_per_orbit
        ):
            raise CompilerScaleError("typed Wave 5 variant cap exceeds the Wave 4 policy")
        self.policy = self.loaded_wave4.policy
        self.audit_replay = audit_replay
        self.typed_gate_verifier = typed_gate_verifier
        self.source_resolver = source_resolver
        self._uses_default_source_resolver = source_resolver is resolve_audit_sources
        self._prevalidated_input_shards: tuple[Any, ...] | None = None
        self.manage_resources = manage_resources
        self.executor_factory = executor_factory or (
            lambda: _LeanTypedExecutor(settings, self.policy)
        )
        self.journal = Journal(settings.journal_path)
        self._peak_rss_bytes = 0
        self._executor_construction_seconds: float | None = None
        self._first_lean_batch_wall_seconds: float | None = None
        self._terminal_index_run_id: str | None = None
        self._terminal_index: dict[str, dict[str, Any]] = {}

    def _record_root_terminals(
        self,
        *,
        run_id: str,
        values: Sequence[tuple[Mapping[str, Any], Path, str]],
    ) -> None:
        if not values:
            return
        if self._terminal_index_run_id != run_id:
            self._terminal_index = _journal_terminals(self.journal, run_id)
            self._terminal_index_run_id = run_id
        _append_root_terminal_events(
            self.journal,
            self._terminal_index,
            [
                _root_terminal_event(
                    run_id=run_id,
                    terminal=terminal,
                    path=path,
                    source=source,
                )
                for terminal, path, source in values
            ],
        )

    def _resolve_sources(
        self, records: Sequence[Mapping[str, Any]]
    ) -> Sequence[CompilerAuditSource]:
        if not self._uses_default_source_resolver:
            return self.source_resolver(self.settings.audit, records)
        if self._prevalidated_input_shards is None:
            self._prevalidated_input_shards = tuple(
                load_pinned_input_shards(self.settings.audit.inventory)
            )
        return resolve_audit_sources(
            self.settings.audit,
            records,
            shards=self._prevalidated_input_shards,
        )

    def _load_or_execute_chunk(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        executor: CompilerScaleExecutor | None,
    ) -> tuple[
        list[tuple[dict[str, Any], Path]],
        list[Wave4ClosureMaterialization],
        CompilerScaleExecutor | None,
        int,
        int,
    ]:
        terminals: dict[str, tuple[dict[str, Any], Path]] = {}
        materials: dict[str, Wave4ClosureMaterialization] = {}
        missing: list[Mapping[str, Any]] = []
        cached_journal_values: list[tuple[Mapping[str, Any], Path, str]] = []
        cache_hits = 0
        lean_requests = 0
        for record in records:
            cached = _load_root_cache(record, self.settings, self.policy)
            if cached is None:
                missing.append(record)
                continue
            terminal, path = cached
            terminals[str(record["root_id"])] = (terminal, path)
            materialized = _validate_root_terminal(
                terminal,
                record=record,
                settings=self.settings,
                policy=self.policy,
            )
            if materialized is not None:
                materials[str(record["root_id"])] = materialized
            cached_journal_values.append((terminal, path, "cache"))
            cache_hits += 1
            lean_requests += int(_mapping(terminal["execution"], "root execution")["lean_requests"])
        self._record_root_terminals(run_id=run_id, values=cached_journal_values)

        if missing:
            sources = tuple(self._resolve_sources(missing))
            if [source.root_id for source in sources] != [
                str(record["root_id"]) for record in missing
            ]:
                raise CompilerScaleError("compiler source resolver changed root order or coverage")
            if executor is None:
                startup_started = time.monotonic()
                executor = self.executor_factory()
                self._executor_construction_seconds = time.monotonic() - startup_started
                self.journal.append(
                    {
                        "event": "lean_executor_constructed",
                        "run_id": run_id,
                        "executor_construction_seconds": round(
                            self._executor_construction_seconds, 6
                        ),
                    }
                )
            for source_batch in _chunked(sources, self.settings.root_batch_size):
                batch_started = time.monotonic()
                outcomes = tuple(executor.execute_batch(source_batch, run_id=run_id))
                if self._first_lean_batch_wall_seconds is None:
                    self._first_lean_batch_wall_seconds = time.monotonic() - batch_started
                    self.journal.append(
                        {
                            "event": "first_lean_batch_complete",
                            "run_id": run_id,
                            "first_lean_batch_wall_seconds": round(
                                self._first_lean_batch_wall_seconds, 6
                            ),
                        }
                    )
                if len(outcomes) != len(source_batch):
                    raise CompilerScaleError(
                        "compiler executor returned incomplete batch terminals"
                    )
                expected_ids = [source.root_id for source in source_batch]
                if [outcome.root_id for outcome in outcomes] != expected_ids:
                    raise CompilerScaleError("compiler executor changed batch root order")
                record_by_id = {str(record["root_id"]): record for record in missing}
                batch_journal_values: list[tuple[Mapping[str, Any], Path, str]] = []
                for outcome in outcomes:
                    record = record_by_id[outcome.root_id]
                    terminal, path = _persist_root_cache(
                        outcome,
                        record=record,
                        settings=self.settings,
                        policy=self.policy,
                    )
                    terminals[outcome.root_id] = (terminal, path)
                    materialized = _validate_root_terminal(
                        terminal,
                        record=record,
                        settings=self.settings,
                        policy=self.policy,
                    )
                    if materialized is not None:
                        materials[outcome.root_id] = materialized
                    batch_journal_values.append((terminal, path, "lean"))
                    lean_requests += outcome.lean_requests
                self._record_root_terminals(run_id=run_id, values=batch_journal_values)
        ordered_terminals = [terminals[str(record["root_id"])] for record in records]
        ordered_materials = [
            materials[str(record["root_id"])]
            for record in records
            if str(record["root_id"]) in materials
        ]
        return ordered_terminals, ordered_materials, executor, cache_hits, lean_requests

    def _write_status(
        self,
        *,
        run_id: str,
        selected_roots: int,
        receipts: Sequence[Mapping[str, Any]],
        started: float,
        cache_hits: int,
        lean_requests: int,
        final: bool,
    ) -> dict[str, Any]:
        processed = sum(int(receipt["input_roots"]) for receipt in receipts)
        rows = sum(int(receipt["rows"]) for receipt in receipts)
        retained_roots = sum(int(receipt["retained_roots"]) for receipt in receipts)
        try:
            current_rss = replay_module._descendant_rss_bytes(os.getpid())
        except OSError:
            current_rss = 0
        prior_peak = 0
        if self.settings.status_path.is_file():
            try:
                prior = read_json_object(self.settings.status_path)
                if prior.get("run_id") == run_id:
                    prior_peak = int(prior.get("peak_rss_bytes", 0))
            except (OSError, ValueError, StoreError):
                prior_peak = 0
        self._peak_rss_bytes = max(self._peak_rss_bytes, prior_peak, current_rss)
        runtime_projection = _runtime_projection(
            started=started,
            processed_roots=processed,
            selected_roots=selected_roots,
            limit_hours=self.settings.projected_runtime_limit_hours,
        )
        if receipts:
            metrics = _mapping(receipts[-1]["completion_metrics"], "completion metrics")
            projected_hours = float(metrics["projected_total_hours"])
            elapsed_seconds = float(metrics["elapsed_seconds"])
            runtime_projection = {
                "wall_seconds": elapsed_seconds,
                "processed_roots_per_second": metrics["roots_per_second"],
                "projected_total_seconds": round(projected_hours * 3600, 3),
                "projected_remaining_seconds": round(
                    max(0.0, projected_hours * 3600 - elapsed_seconds), 3
                ),
                "projection_limit_hours": metrics["projection_limit_hours"],
                "exceeds_local_runtime_limit": metrics["projection_exceeds_limit"],
                "migration_requirement": (
                    "CPU/RAM host with two persistent Lean workers and at least 40 GiB measured "
                    "RSS; GPU acceleration does not reduce Lean elaboration time"
                    if metrics["projection_exceeds_limit"] is True
                    else None
                ),
            }
        status: dict[str, Any] = {
            "artifact_kind": "sft1_wave5_compiler_scale_status",
            "schema_version": SCALE_SCHEMA_VERSION,
            "run_id": run_id,
            "selected_roots": selected_roots,
            "processed_roots": processed,
            "retained_roots": retained_roots,
            "released_rows": rows,
            "release_shards": len(receipts),
            "cache_hits_this_invocation": cache_hits,
            "lean_requests_this_invocation": lean_requests,
            "checkpoints_reached": [value for value in self.settings.checkpoints if rows >= value],
            "pilot_milestones": [
                {"roots": value, "reached": processed >= value} for value in PILOT_ROOT_MILESTONES
            ],
            "peak_rss_bytes": self._peak_rss_bytes,
            "peak_rss_gib": round(self._peak_rss_bytes / (1024**3), 6),
            "runtime_projection": runtime_projection,
            "final": final,
        }
        write_atomic(self.settings.status_path, canonical_json_bytes(status) + b"\n")
        return status

    def _write_migration_required(
        self,
        *,
        run_id: str,
        status: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> None:
        milestones = _ensure_milestone_receipts(self.settings, run_id=run_id, receipts=receipts)
        payload = {
            "artifact_kind": "sft1_wave5_compiler_scale_migration_required",
            "schema_version": SCALE_MIGRATION_VERSION,
            "run_id": run_id,
            "status": "migration_required",
            "reason": reason,
            "processed_roots": sum(int(receipt["input_roots"]) for receipt in receipts),
            "released_rows": sum(int(receipt["rows"]) for receipt in receipts),
            "complete_shards": len(receipts),
            "runtime_projection": status["runtime_projection"],
            "resource_requirement": (
                "CPU/RAM host with two persistent Lean workers and at least 40 GiB measured "
                "RSS; GPUs do not accelerate Lean elaboration"
            ),
            "completed_shards_remain_valid": True,
            "milestone_receipts": list(milestones),
            "milestone_receipts_sha256": hash_canonical(milestones),
            "status_sha256": hash_file(self.settings.status_path),
            "recovery_command": (
                "uv run python -m leanfaith.sft1.sprint.compiler_scale "
                f"--config {self.settings.audit.config_path} run"
            ),
        }
        _write_exact(self.settings.migration_required_path, payload)

    def _write_manifest_and_terminal(
        self,
        *,
        run_id: str,
        selected: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
        status: Mapping[str, Any],
        audit_gate: Mapping[str, Any],
    ) -> dict[str, Any]:
        rows = sum(int(receipt["rows"]) for receipt in receipts)
        processed = sum(int(receipt["input_roots"]) for receipt in receipts)
        n25_rows = sum(int(receipt["n25_rows"]) for receipt in receipts)
        if n25_rows > int(rows * self.settings.n25_maximum_share):
            raise CompilerScaleError("Wave 5 aggregate N25 share exceeds 25 percent")
        publication_shards = [
            _mapping(receipt["publication_manifest"], "publication shard manifest")
            for receipt in receipts
        ]
        release_tree_sha256 = hash_canonical(publication_shards)
        aggregates = _release_aggregates(self.settings, receipts)
        milestone_receipts = _ensure_milestone_receipts(
            self.settings, run_id=run_id, receipts=receipts
        )
        complete_boundary = processed == len(selected) or rows >= self.settings.maximum_release_rows
        checkpoint_reached = rows >= self.settings.checkpoints[0]
        publication_state = _validated_publication_chain(
            self.settings,
            run_id=run_id,
            receipts=receipts,
            recompute_checkpoints=True,
        )
        publication_chain = publication_state.shards
        publication_events = publication_state.events
        if checkpoint_reached and (
            len(publication_chain) != len(receipts)
            or publication_state.pending_checkpoint is not None
        ):
            raise CompilerScaleError(
                "Wave 5 checkpoint reached with pending incremental shard publications; "
                f"queue={self.settings.output_root / '_state/publication_queue'}"
            )
        useful_negative_families = sum(
            count > 0
            for operation, count in cast(
                dict[str, int], aggregates["negative_family_groups"]
            ).items()
            if operation != "N25_TOGGLE_EQ_NE_PROOF_V1"
        )

        release_root = self.settings.output_root / "release"
        sample, sample_info = _checkpoint_screen_sample(self.settings, len(receipts) - 1)
        pairwise = shortcut_module.pairwise_shortcut_diagnostics(sample)
        pair_delta_cells = cast(Mapping[str, Mapping[str, int]], sample_info["pair_delta_cells"])
        pair_delta_offenders = {
            cell: dict(counts)
            for cell, counts in pair_delta_cells.items()
            if counts.get("positive", 0) != counts.get("negative", 0)
        }
        pair_delta_policy = {
            "policy": "exact_label_balance_per_joint_pair_delta_cell_v1",
            "offending_cells": pair_delta_offenders,
            "failure_action": "block_release_then_lean_free_rebalance_or_cell_quarantine",
            "passed": not pair_delta_offenders,
        }
        pairwise_payload = {
            "schema_version": 1,
            "sample": sample_info,
            "diagnostics": pairwise,
        }
        pairwise_path = release_root / "pairwise_diagnostics.json"
        _write_exact(pairwise_path, pairwise_payload)
        if checkpoint_reached:
            shortcut = shortcut_module.run_screens_v3(sample)
        else:
            shortcut = {
                "passed": False,
                "reason": "first_release_checkpoint_not_reached",
                "rows": len(sample),
                "screens": [],
            }

        finalization_path = self.settings.output_root / "_state" / "finalization_metrics.json"
        if finalization_path.is_file():
            finalization = read_json_object(finalization_path)
            if finalization.get("run_id") != run_id:
                raise CompilerScaleError("Wave 5 finalization metrics belong to another run")
        else:
            finalization = {
                "artifact_kind": "sft1_wave5_compiler_finalization_metrics",
                "run_id": run_id,
                "runtime_projection": status["runtime_projection"],
                "peak_rss_bytes": status["peak_rss_bytes"],
                "peak_rss_gib": status["peak_rss_gib"],
                "lean_requests": aggregates["lean_requests"],
                "lean_elapsed_ms": aggregates["lean_elapsed_ms"],
            }
            _write_exact(finalization_path, finalization)

        checks = {
            "nonempty": rows > 0,
            "first_checkpoint_reached": checkpoint_reached,
            "complete_processing_boundary": complete_boundary,
            "exact_three_field_model_rows": True,
            "all_source_and_closure_certificates_revalidated": True,
            "zero_self_pairs_partial_groups_conflicts_or_duplicates": True,
            "all_shards_complete_and_hash_bound": all(
                item.get("complete") is True and item.get("finalized") is True
                for item in publication_shards
            ),
            "all_shards_published_private_and_remote_verified": len(publication_chain)
            == len(receipts)
            and publication_state.pending_checkpoint is None,
            "all_required_pilot_milestones_hash_bound": len(milestone_receipts)
            == sum(processed >= value for value in PILOT_ROOT_MILESTONES),
            "n19_forbidden": all(
                operation != "N19_WHOLE_CLAIM_NEGATION_V1"
                for operation in cast(dict[str, int], aggregates["negative_family_groups"])
            ),
            "n25_released_share_capped": n25_rows <= int(rows * self.settings.n25_maximum_share),
            "three_useful_non_n25_negative_families": useful_negative_families >= 3,
            "pair_delta_cells_balanced_or_quarantined": pair_delta_policy["passed"] is True,
            "pair_delta_diagnostics_passed": pairwise.get("rows") == len(sample)
            and bool(pairwise.get("rules"))
            and float(pairwise.get("max_balanced_accuracy", 1.0)) <= 0.65,
            "candidate_reference_and_family_screens": shortcut.get("passed") is True,
            "typed_1000_root_certificate_gate": audit_gate.get("typed_gate_passed") is True,
            "typed_gate_forced_resume_zero_call_replay": audit_gate.get(
                "typed_gate_replay_zero_call"
            )
            is True,
        }
        passed = all(checks.values())
        failed_checks = [name for name, value in checks.items() if not value]
        binding = {
            "run_id": run_id,
            "run_spec_sha256": hash_file(self.settings.run_spec_path),
            "implementation_commit": read_json_object(self.settings.run_spec_path)[
                "implementation_commit"
            ],
            "selection_sha256": hash_file(self.settings.selection_path),
            "context_audit_terminal_sha256": audit_gate["terminal_sha256"],
            "typed_certificate_gate_terminal_sha256": audit_gate.get("typed_gate_terminal_sha256"),
            "audit_sample_sha256": audit_gate.get("typed_gate_sample_sha256"),
            "release_tree_sha256": release_tree_sha256,
            "finalization_metrics_sha256": hash_file(finalization_path),
            "incremental_publication_chain_sha256": hash_canonical(publication_events),
            "milestone_receipts_sha256": hash_canonical(milestone_receipts),
        }
        release_report = {
            "artifact_kind": "sft1_wave5_compiler_release_report",
            "schema_version": 1,
            "evaluated_on": "proof-certified compiler release",
            "implementation_commit": binding["implementation_commit"],
            "content_binding": binding,
            "physical_rows": rows,
            "unique_ancestry_roots": aggregates["unique_ancestry_roots"],
            "source_rows": {"pinned_cpt2_compiler_data": rows},
            "negative_family_groups": aggregates["negative_family_groups"],
            "preserving_family_groups": aggregates["preserving_family_groups"],
            "failure_taxonomy": aggregates["failure_taxonomy"],
            "lean": {
                "requests": aggregates["lean_requests"],
                "elapsed_ms": aggregates["lean_elapsed_ms"],
                "workers": self.settings.audit.lean_workers,
                "cache_terminals": processed,
            },
            "runtime": finalization,
            "resume_replay": {
                "root_terminals": processed,
                "independent_complete_shards": len(receipts),
                "typed_gate": audit_gate.get("typed_gate_resume_replay"),
                "scale_zero_call_replay_available": True,
            },
            "manual_inspection_verdict": audit_gate.get(
                "typed_gate_manual_inspection_verdict", "not_recorded"
            ),
            "shortcut_sample": sample_info,
            "shortcut": shortcut,
            "pairwise_diagnostics": pairwise,
            "pair_delta_policy": pair_delta_policy,
            "checks": checks,
            "passed": passed,
        }
        integrity_report = {
            "artifact_kind": "sft1_wave5_compiler_integrity_report",
            "schema_version": 1,
            "run_id": run_id,
            "implementation_commit": binding["implementation_commit"],
            "content_binding": binding,
            "rows_checked": rows,
            "shards": len(receipts),
            "checks": checks,
            "issue_counts": {name: 0 if value else 1 for name, value in checks.items()},
            "issues": failed_checks,
            "passed": passed,
        }
        release_report_path = release_root / "release_report.json"
        integrity_report_path = release_root / "integrity_report.json"
        _write_exact(release_report_path, release_report)
        _write_exact(integrity_report_path, integrity_report)
        manifest = {
            "artifact_kind": "sft1_wave5_compiler_proof_certified_release",
            "schema_version": 1,
            "run_id": run_id,
            "implementation_commit": binding["implementation_commit"],
            "run_spec_sha256": hash_file(self.settings.run_spec_path),
            "selection_sha256": hash_file(self.settings.selection_path),
            "audit_terminal_sha256": hash_file(self.settings.audit.complete_path),
            "typed_certificate_gate_terminal_sha256": audit_gate.get("typed_gate_terminal_sha256"),
            "audit_sample_sha256": audit_gate.get("typed_gate_sample_sha256"),
            "proof_certified_core_only": True,
            "lower_confidence_rows": 0,
            "row_fields": ["reference", "candidate", "label"],
            "selected_roots": len(selected),
            "processed_roots": processed,
            "retained_roots": sum(int(receipt["retained_roots"]) for receipt in receipts),
            "rows": rows,
            "retained_rows": rows,
            "roots": aggregates["unique_ancestry_roots"],
            "labels": aggregates["labels"],
            "operations": aggregates["negative_family_groups"],
            "negative_mechanisms": aggregates["negative_family_groups"],
            "preserving_families": aggregates["preserving_family_groups"],
            "logical_closure_rows": sum(int(receipt["logical_rows"]) for receipt in receipts),
            "closure_groups": sum(int(receipt["closure_groups"]) for receipt in receipts),
            "n19_rows": 0,
            "n25_rows": n25_rows,
            "n25_share": n25_rows / rows if rows else 0.0,
            "root_ceiling": self.settings.root_ceiling,
            "maximum_release_rows": self.settings.maximum_release_rows,
            "checkpoints": list(self.settings.checkpoints),
            "checkpoints_reached": status["checkpoints_reached"],
            "pilot_milestone_receipts": list(milestone_receipts),
            "pilot_milestone_receipts_sha256": hash_canonical(milestone_receipts),
            "release_tree_sha256": release_tree_sha256,
            "shards": publication_shards,
            "cache_snapshots": [],
            "pairwise_diagnostics": {
                "file": pairwise_path.name,
                "sha256": hash_file(pairwise_path),
            },
            "release_report_sha256": hash_file(release_report_path),
            "integrity_report_sha256": hash_file(integrity_report_path),
            "operational_shard_receipts_sha256": hash_canonical(
                [dict(receipt) for receipt in receipts]
            ),
            "incremental_publication_revisions": [
                {
                    "shard": int(receipt["part"]) + 1,
                    "parent_revision": receipt["parent_revision"],
                    "revision": receipt["revision"],
                    "remote_prefix": receipt["remote_prefix"],
                    "receipt_sha256": hash_file(
                        _publication_receipt_path(self.settings, int(receipt["part"]))
                    ),
                }
                for receipt in publication_chain
            ],
            "incremental_publication_events": list(publication_events),
            "incremental_publication_chain_sha256": hash_canonical(publication_events),
            "runtime": finalization,
            "provenance": {
                "source": "pinned_cpt2_compiler_data",
                "source_pin": self.settings.audit.inventory.pin.to_dict(),
                "project": self.settings.audit.inventory.project.to_dict(),
                "proof_check_time": "original_generation",
            },
            "finalized": True,
            "artifact_status": (
                "wave5_compiler_proof_certified_release"
                if passed
                else "candidate_wave5_release_gate_failed"
            ),
        }
        manifest_path = self.settings.output_root / "release" / "manifest.json"
        _write_exact(manifest_path, manifest)
        aggregate_publication: dict[str, Any] | None = None
        if checkpoint_reached and passed:
            if publication_state.head_revision is None:
                raise CompilerScaleError("Wave 5 aggregate publication lacks a parent revision")
            _ensure_aggregate_publication_queue(
                self.settings,
                run_id=run_id,
                publication_events=publication_events,
                previous_revision=publication_state.head_revision,
            )
            aggregate_publication = _validate_metadata_publication(
                self.settings,
                run_id=run_id,
                metadata_kind="aggregate",
                queue_path=_aggregate_publication_queue_path(self.settings),
                receipt_path=_aggregate_publication_receipt_path(self.settings),
                previous_revision=publication_state.head_revision,
            )
            if aggregate_publication is None:
                raise CompilerScaleError(
                    "Wave 5 aggregate manifest/report publication is pending; run "
                    "compiler_scale publish-pending"
                )
        terminal_status: Literal["complete", "complete_below_first_checkpoint"] = (
            "complete"
            if rows >= self.settings.checkpoints[0]
            else "complete_below_first_checkpoint"
        )
        terminal = {
            "artifact_kind": "sft1_wave5_compiler_scale_terminal",
            "schema_version": SCALE_TERMINAL_VERSION,
            "run_id": run_id,
            "status": terminal_status,
            "manifest": str(manifest_path),
            "manifest_sha256": hash_file(manifest_path),
            "release_report_sha256": hash_file(release_report_path),
            "integrity_report_sha256": hash_file(integrity_report_path),
            "selected_roots": len(selected),
            "processed_roots": processed,
            "released_rows": rows,
            "checkpoints_reached": status["checkpoints_reached"],
            "proof_certified_core_only": True,
            "release_gate_passed": passed,
            "aggregate_publication_revision": (
                aggregate_publication["revision"] if aggregate_publication is not None else None
            ),
            "aggregate_publication_receipt_sha256": (
                hash_file(_aggregate_publication_receipt_path(self.settings))
                if aggregate_publication is not None
                else None
            ),
        }
        if checkpoint_reached and not passed:
            raise CompilerScaleError(
                "Wave 5 release gate failed after reaching a publishable checkpoint: "
                + ", ".join(failed_checks)
            )
        _write_exact(self.settings.complete_path, terminal)
        return terminal

    def run(self) -> CompilerScaleResult:
        started = time.monotonic()
        if self.manage_resources:
            _require_clean_generation_state(self.settings)
        audit_gate = verify_audit_gate(
            self.settings.audit,
            typed_spec=self.settings.typed_spec,
            policy=self.policy,
            replay=self.audit_replay,
            typed_gate_verifier=self.typed_gate_verifier,
        )
        selected = build_eligible_selection(self.settings)
        run_id, _run_spec = _ensure_run_spec(
            self.settings, policy=self.policy, audit_gate=audit_gate
        )
        receipts, prior_unordered = _read_completed_receipts(
            self.settings, run_id=run_id, selected=selected, policy=self.policy
        )
        _ensure_milestone_receipts(self.settings, run_id=run_id, receipts=receipts)
        _ensure_publication_queue(self.settings, run_id=run_id, receipts=receipts)
        if self.settings.complete_path.is_file():
            terminal = read_json_object(self.settings.complete_path)
            self._validate_complete(terminal, run_id=run_id, selected=selected, receipts=receipts)
            return self._result(terminal, receipts, cache_hits=len(selected), lean_requests=0)
        if self.settings.migration_required_path.is_file():
            migration = read_json_object(self.settings.migration_required_path)
            if migration.get("run_id") != run_id or migration.get("status") != "migration_required":
                raise CompilerScaleError("Wave 5 migration terminal belongs to another run")
            raise CompilerScaleInfrastructureError(
                f"Wave 5 requires host migration; see {self.settings.migration_required_path}"
            )

        cache_hits = sum(int(receipt["input_roots"]) for receipt in receipts)
        lean_requests = _lean_requests_through_receipts(self.settings, receipts)
        base_elapsed_seconds = (
            float(
                _mapping(receipts[-1]["completion_metrics"], "completion metrics")[
                    "elapsed_seconds"
                ]
            )
            if receipts
            else 0.0
        )
        if receipts:
            self._peak_rss_bytes = int(
                _mapping(receipts[-1]["completion_metrics"], "completion metrics")["peak_rss_bytes"]
            )
        first_metrics = (
            _mapping(receipts[0]["completion_metrics"], "completion metrics") if receipts else None
        )
        construction_events: list[dict[str, Any]] = []
        first_batch_events: list[dict[str, Any]] = []

        def run_journal_events() -> Iterator[dict[str, Any]]:
            for event in self.journal.read():
                if event.get("run_id") != run_id:
                    continue
                if event.get("event") == "lean_executor_constructed":
                    construction_events.append(event)
                elif event.get("event") == "first_lean_batch_complete":
                    first_batch_events.append(event)
                yield event

        self._terminal_index = _terminal_index_from_events(run_journal_events(), run_id)
        self._terminal_index_run_id = run_id
        executor_construction_seconds = float(
            first_metrics["executor_construction_seconds"]
            if first_metrics is not None
            else construction_events[0]["executor_construction_seconds"]
            if construction_events
            else 0.0
        )
        first_lean_batch_wall_seconds = float(
            first_metrics["first_lean_batch_wall_seconds"]
            if first_metrics is not None
            else first_batch_events[0]["first_lean_batch_wall_seconds"]
            if first_batch_events
            else 0.0
        )
        self._executor_construction_seconds = executor_construction_seconds or None
        self._first_lean_batch_wall_seconds = first_lean_batch_wall_seconds or None
        executor: CompilerScaleExecutor | None = None
        chunks = _scale_chunks(selected, self.settings.roots_per_shard)
        try:
            with replay_module._resource_claim(
                self.settings.audit,
                owner_session="codex-sft1-wave5-scale",
                enabled=self.manage_resources,
            ):
                for part in range(len(receipts), len(chunks)):
                    if (
                        sum(int(receipt["rows"]) for receipt in receipts)
                        >= self.settings.maximum_release_rows
                    ):
                        break
                    records = chunks[part]
                    with replay_module._RssSampler() as sampler:
                        (
                            terminals,
                            materials,
                            executor,
                            chunk_cache_hits,
                            chunk_lean_requests,
                        ) = self._load_or_execute_chunk(
                            records,
                            run_id=run_id,
                            executor=executor,
                        )
                    self._peak_rss_bytes = max(self._peak_rss_bytes, sampler.peak)
                    cache_hits += chunk_cache_hits
                    lean_requests += chunk_lean_requests
                    merged, duplicate_roots = _merge_materializations(
                        materials, prior_unordered=prior_unordered
                    )
                    remaining = self.settings.maximum_release_rows - sum(
                        int(receipt["rows"]) for receipt in receipts
                    )
                    release_salt = f"{self.settings.selection_salt}:release:{part}"
                    selection = select_wave4_release_groups(
                        merged,
                        maximum_rows=remaining,
                        n25_maximum_share=self.settings.n25_maximum_share,
                        selection_salt=release_salt,
                        enforce_pair_delta_balance=True,
                    )
                    released = selection.materialized
                    initial_selected_rows = len(released.rows)
                    initial_n25_rows = _n25_row_count(released)
                    fallback = initial_n25_rows > int(
                        len(released.rows) * self.settings.n25_maximum_share
                    )
                    initial_balance_hash = hash_canonical(selection.pair_delta_balance_report)
                    if fallback:
                        selection = select_wave4_release_groups(
                            merged,
                            maximum_rows=remaining,
                            n25_maximum_share=0.0,
                            selection_salt=f"{release_salt}:drop-n25-fallback",
                            enforce_pair_delta_balance=True,
                        )
                        released = selection.materialized
                    final_n25_rows = _n25_row_count(released)
                    n25_guard_passed = final_n25_rows <= int(
                        len(released.rows) * self.settings.n25_maximum_share
                    )
                    balance_report = dict(selection.pair_delta_balance_report)
                    balance_report["post_balance_n25_guard"] = {
                        "maximum_share": self.settings.n25_maximum_share,
                        "fallback": "drop_all_n25_then_rebalance" if fallback else None,
                        "initial_rows": initial_selected_rows,
                        "initial_n25_rows": initial_n25_rows,
                        "initial_pair_delta_balance_report_sha256": initial_balance_hash,
                        "final_rows": len(released.rows),
                        "final_n25_rows": final_n25_rows,
                        "passed": n25_guard_passed,
                    }
                    if not n25_guard_passed:
                        raise CompilerScaleError("Wave 5 post-balance N25 guard failed")
                    prior_rows = sum(int(item["rows"]) for item in receipts)
                    processed_after = sum(int(item["input_roots"]) for item in receipts) + len(
                        records
                    )
                    rows_after = prior_rows + len(released.rows)
                    failure_taxonomy = (
                        _failure_taxonomy_through_chunk(self.settings, receipts, terminals)
                        if processed_after in PILOT_ROOT_MILESTONES
                        else dict(
                            sorted(Counter(str(item[0]["taxonomy"]) for item in terminals).items())
                        )
                    )
                    completion_metrics = _completion_metrics(
                        self.settings,
                        started=started,
                        selected_roots=len(selected),
                        processed_roots=processed_after,
                        released_rows=rows_after,
                        cache_hits=cache_hits,
                        lean_requests=lean_requests,
                        peak_rss_bytes=self._peak_rss_bytes,
                        executor_construction_seconds=(
                            executor_construction_seconds
                            if receipts
                            else float(self._executor_construction_seconds or 0.0)
                        ),
                        first_lean_batch_wall_seconds=(
                            first_lean_batch_wall_seconds
                            if receipts
                            else float(self._first_lean_batch_wall_seconds or 0.0)
                        ),
                        failure_taxonomy=failure_taxonomy,
                        prior_elapsed_seconds=base_elapsed_seconds,
                    )
                    receipt = _write_release_shard(
                        self.settings,
                        run_id=run_id,
                        part=part,
                        input_records=records,
                        terminals=terminals,
                        materialized=released,
                        duplicate_dropped_roots=duplicate_roots,
                        pair_delta_balance_report=balance_report,
                        completion_metrics=completion_metrics,
                    )
                    receipts.append(receipt)
                    if len(receipts) == 1:
                        executor_construction_seconds = float(
                            completion_metrics["executor_construction_seconds"]
                        )
                        first_lean_batch_wall_seconds = float(
                            completion_metrics["first_lean_batch_wall_seconds"]
                        )
                    _ensure_milestone_receipts(self.settings, run_id=run_id, receipts=receipts)
                    _write_publication_queue(
                        self.settings,
                        run_id=run_id,
                        part=part,
                        receipt=receipt,
                        prior_rows=prior_rows,
                    )
                    prior_unordered.update(
                        str(record["unordered_pair_key"]) for record in released.rows
                    )
                    self.journal.append(
                        {
                            "event": "release_shard_complete",
                            "run_id": run_id,
                            "part": part,
                            "input_roots": len(records),
                            "rows": receipt["rows"],
                            "receipt_sha256": hash_file(
                                _shard_paths(self.settings, part)["receipt"]
                            ),
                        }
                    )
                    status = self._write_status(
                        run_id=run_id,
                        selected_roots=len(selected),
                        receipts=receipts,
                        started=started,
                        cache_hits=cache_hits,
                        lean_requests=lean_requests,
                        final=False,
                    )
                    projection = _mapping(status["runtime_projection"], "runtime projection")
                    processed = sum(int(item["input_roots"]) for item in receipts)
                    if int(status["peak_rss_bytes"]) > self.settings.audit.lean_rss_claim_gib * (
                        1024**3
                    ):
                        self._write_migration_required(
                            run_id=run_id,
                            status=status,
                            receipts=receipts,
                            reason="measured_rss_exceeded_claim",
                        )
                        raise CompilerScaleInfrastructureError(
                            "measured Wave 5 process-tree RSS exceeded the 40-GiB host budget"
                        )
                    if _runtime_migration_required(
                        processed_roots=processed, projection=projection
                    ):
                        self._write_migration_required(
                            run_id=run_id,
                            status=status,
                            receipts=receipts,
                            reason="projected_runtime_exceeds_limit",
                        )
                        raise CompilerScaleInfrastructureError(
                            "measured Wave 5 projection exceeds 36 hours; migrate the recorded "
                            "two-worker/40-GiB CPU workload before continuing"
                        )
        finally:
            if executor is not None:
                executor.close()

        status = self._write_status(
            run_id=run_id,
            selected_roots=len(selected),
            receipts=receipts,
            started=started,
            cache_hits=cache_hits,
            lean_requests=lean_requests,
            final=True,
        )
        terminal = self._write_manifest_and_terminal(
            run_id=run_id,
            selected=selected,
            receipts=receipts,
            status=status,
            audit_gate=audit_gate,
        )
        return self._result(terminal, receipts, cache_hits=cache_hits, lean_requests=lean_requests)

    def _validate_complete(
        self,
        terminal: Mapping[str, Any],
        *,
        run_id: str,
        selected: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
    ) -> None:
        manifest_path = self.settings.output_root / "release" / "manifest.json"
        if terminal.get("run_id") != run_id:
            raise CompilerScaleError("Wave 5 terminal belongs to another run")
        if terminal.get("manifest_sha256") != hash_file(manifest_path):
            raise CompilerScaleError("Wave 5 terminal manifest hash differs")
        manifest = read_json_object(manifest_path)
        publication_state = _validated_publication_chain(
            self.settings,
            run_id=run_id,
            receipts=receipts,
            recompute_checkpoints=True,
        )
        milestone_receipts = _ensure_milestone_receipts(
            self.settings, run_id=run_id, receipts=receipts
        )
        expected = {
            "run_id": run_id,
            "run_spec_sha256": hash_file(self.settings.run_spec_path),
            "selection_sha256": hash_file(self.settings.selection_path),
            "selected_roots": len(selected),
            "processed_roots": sum(int(receipt["input_roots"]) for receipt in receipts),
            "rows": sum(int(receipt["rows"]) for receipt in receipts),
            "retained_rows": sum(int(receipt["rows"]) for receipt in receipts),
            "shards": [
                _mapping(receipt["publication_manifest"], "publication shard manifest")
                for receipt in receipts
            ],
            "operational_shard_receipts_sha256": hash_canonical(
                [dict(receipt) for receipt in receipts]
            ),
            "incremental_publication_chain_sha256": hash_canonical(publication_state.events),
            "pilot_milestone_receipts": list(milestone_receipts),
            "pilot_milestone_receipts_sha256": hash_canonical(milestone_receipts),
            "proof_certified_core_only": True,
            "lower_confidence_rows": 0,
        }
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise CompilerScaleError(f"Wave 5 completed manifest differs at {field}")
        release_report_path = manifest_path.parent / "release_report.json"
        integrity_report_path = manifest_path.parent / "integrity_report.json"
        if manifest.get("release_report_sha256") != hash_file(release_report_path):
            raise CompilerScaleError("Wave 5 release report hash differs")
        if manifest.get("integrity_report_sha256") != hash_file(integrity_report_path):
            raise CompilerScaleError("Wave 5 integrity report hash differs")
        release_report = read_json_object(release_report_path)
        integrity_report = read_json_object(integrity_report_path)
        expected_pass = terminal.get("status") == "complete"
        if release_report.get("passed") is not expected_pass:
            raise CompilerScaleError("Wave 5 release report pass state differs")
        if integrity_report.get("passed") is not expected_pass:
            raise CompilerScaleError("Wave 5 integrity report pass state differs")
        if expected_pass:
            if publication_state.head_revision is None:
                raise CompilerScaleError("Wave 5 complete release lacks publication head")
            _ensure_aggregate_publication_queue(
                self.settings,
                run_id=run_id,
                publication_events=publication_state.events,
                previous_revision=publication_state.head_revision,
            )
            aggregate = _validate_metadata_publication(
                self.settings,
                run_id=run_id,
                metadata_kind="aggregate",
                queue_path=_aggregate_publication_queue_path(self.settings),
                receipt_path=_aggregate_publication_receipt_path(self.settings),
                previous_revision=publication_state.head_revision,
            )
            if aggregate is None:
                raise CompilerScaleError("Wave 5 complete release lacks aggregate publication")
            if terminal.get("aggregate_publication_revision") != aggregate["revision"]:
                raise CompilerScaleError("Wave 5 aggregate publication revision differs")
            if terminal.get("aggregate_publication_receipt_sha256") != hash_file(
                _aggregate_publication_receipt_path(self.settings)
            ):
                raise CompilerScaleError("Wave 5 aggregate publication receipt hash differs")
        issues = integrity_report.get("issues")
        counts = integrity_report.get("issue_counts")
        if expected_pass and (
            not isinstance(issues, list)
            or issues
            or not isinstance(counts, dict)
            or any(value != 0 for value in counts.values())
        ):
            raise CompilerScaleError("Wave 5 passing integrity report contains issues")

    def _result(
        self,
        terminal: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        *,
        cache_hits: int,
        lean_requests: int,
    ) -> CompilerScaleResult:
        return CompilerScaleResult(
            run_id=str(terminal["run_id"]),
            status=cast(Literal["complete", "complete_below_first_checkpoint"], terminal["status"]),
            selected_roots=int(terminal["selected_roots"]),
            processed_roots=int(terminal["processed_roots"]),
            retained_roots=sum(int(receipt["retained_roots"]) for receipt in receipts),
            released_rows=int(terminal["released_rows"]),
            release_shards=len(receipts),
            cache_hits=cache_hits,
            lean_requests=lean_requests,
            complete_path=self.settings.complete_path,
        )

    def _publication_state(
        self,
    ) -> tuple[str, list[dict[str, Any]], PublicationChain]:
        """Revalidate the zero-call gates and every manifest-last local shard."""

        audit_gate = verify_audit_gate(
            self.settings.audit,
            typed_spec=self.settings.typed_spec,
            policy=self.policy,
            replay=self.audit_replay,
            typed_gate_verifier=self.typed_gate_verifier,
        )
        selected = _load_selection(self.settings)
        run_id, _ = _ensure_run_spec(self.settings, policy=self.policy, audit_gate=audit_gate)
        receipts, _ = _read_completed_receipts(
            self.settings, run_id=run_id, selected=selected, policy=self.policy
        )
        _ensure_milestone_receipts(self.settings, run_id=run_id, receipts=receipts)
        _ensure_publication_queue(self.settings, run_id=run_id, receipts=receipts)
        chain = _validated_publication_chain(
            self.settings,
            run_id=run_id,
            receipts=receipts,
            recompute_checkpoints=True,
        )
        return run_id, receipts, chain

    def _publish_metadata_queue(
        self,
        *,
        run_id: str,
        metadata_kind: Literal["checkpoint", "aggregate"],
        queue_path: Path,
        receipt_path: Path,
        previous_revision: str,
        uploader: IncrementalShardUploader,
    ) -> dict[str, Any]:
        queue = read_json_object(queue_path)
        local_root, files = _metadata_publication_files(
            queue, context=f"Wave 5 {metadata_kind} publication"
        )
        commit_message = (
            _checkpoint_commit_message(queue)
            if metadata_kind == "checkpoint"
            else _aggregate_commit_message(queue)
        )
        revision, parent, verified = uploader(
            repo_id=self.settings.publication_repo_id,
            local_root=local_root,
            files=files,
            remote_prefix=str(queue["remote_prefix"]),
            commit_message=commit_message,
            expected_parent=previous_revision,
        )
        receipt = _record_metadata_publication(
            self.settings,
            run_id=run_id,
            metadata_kind=metadata_kind,
            queue_path=queue_path,
            receipt_path=receipt_path,
            queue=queue,
            revision=revision,
            parent_revision=parent,
            verified_remote_hashes=verified,
            previous_revision=previous_revision,
            verification_method="fresh_download_sha256",
            upload_performed=True,
        )
        self.journal.append(
            {
                "event": f"{metadata_kind}_published_and_fresh_verified",
                "run_id": run_id,
                "revision": receipt["revision"],
                "parent_revision": receipt["parent_revision"],
                "receipt_sha256": hash_file(receipt_path),
            }
        )
        return receipt

    def publish_pending(
        self,
        *,
        maximum_shards: int | None = None,
        uploader: IncrementalShardUploader = _default_incremental_shard_uploader,
    ) -> tuple[dict[str, Any], ...]:
        """Publish and freshly verify a contiguous prefix of completed local shards."""

        if maximum_shards is not None and maximum_shards <= 0:
            raise CompilerScaleError("maximum_shards must be positive when provided")
        run_id, receipts, state = self._publication_state()
        published: list[dict[str, Any]] = []
        limit = len(receipts) if maximum_shards is None else maximum_shards
        published_shards = 0
        while True:
            if state.pending_checkpoint is not None:
                checkpoint, through_part = state.pending_checkpoint
                if state.head_revision is None:
                    raise CompilerScaleError("checkpoint publication lacks its shard parent")
                queue = _ensure_checkpoint_bundle(
                    self.settings,
                    run_id=run_id,
                    checkpoint=checkpoint,
                    through_part=through_part,
                    receipts=receipts,
                    shard_publications=state.shards,
                    prior_events=state.events,
                    previous_revision=state.head_revision,
                )
                del queue
                published.append(
                    self._publish_metadata_queue(
                        run_id=run_id,
                        metadata_kind="checkpoint",
                        queue_path=_publication_checkpoint_queue_path(self.settings, checkpoint),
                        receipt_path=_publication_checkpoint_path(self.settings, checkpoint),
                        previous_revision=state.head_revision,
                        uploader=uploader,
                    )
                )
                state = _validated_publication_chain(
                    self.settings, run_id=run_id, receipts=receipts
                )
                continue
            if len(state.shards) >= len(receipts) or published_shards >= limit:
                break
            part = len(state.shards)
            queue_path = _publication_queue_path(self.settings, part)
            queue = read_json_object(queue_path)
            local_root, files = _queued_shard_files(self.settings, part=part, queue=queue)
            revision, parent, verified = uploader(
                repo_id=self.settings.publication_repo_id,
                local_root=local_root,
                files=files,
                remote_prefix=str(queue["remote_prefix"]),
                commit_message=_incremental_commit_message(queue),
                expected_parent=state.head_revision,
            )
            publication = _record_incremental_publication(
                self.settings,
                run_id=run_id,
                part=part,
                queue=queue,
                revision=revision,
                parent_revision=parent,
                verified_remote_hashes=verified,
                previous_revision=state.head_revision,
                verification_method="fresh_download_sha256",
                upload_performed=True,
            )
            self.journal.append(
                {
                    "event": "release_shard_published_and_fresh_verified",
                    "run_id": run_id,
                    "part": part,
                    "revision": publication["revision"],
                    "parent_revision": publication["parent_revision"],
                    "receipt_sha256": hash_file(_publication_receipt_path(self.settings, part)),
                }
            )
            published.append(publication)
            published_shards += 1
            state = _validated_publication_chain(self.settings, run_id=run_id, receipts=receipts)
        if (
            _aggregate_publication_queue_path(self.settings).is_file()
            and len(state.shards) == len(receipts)
            and state.pending_checkpoint is None
        ):
            if state.head_revision is None:
                raise CompilerScaleError("aggregate publication lacks its shard parent")
            _ensure_aggregate_publication_queue(
                self.settings,
                run_id=run_id,
                publication_events=state.events,
                previous_revision=state.head_revision,
            )
            existing_aggregate = _validate_metadata_publication(
                self.settings,
                run_id=run_id,
                metadata_kind="aggregate",
                queue_path=_aggregate_publication_queue_path(self.settings),
                receipt_path=_aggregate_publication_receipt_path(self.settings),
                previous_revision=state.head_revision,
            )
            if existing_aggregate is None:
                published.append(
                    self._publish_metadata_queue(
                        run_id=run_id,
                        metadata_kind="aggregate",
                        queue_path=_aggregate_publication_queue_path(self.settings),
                        receipt_path=_aggregate_publication_receipt_path(self.settings),
                        previous_revision=state.head_revision,
                        uploader=uploader,
                    )
                )
        _validated_publication_chain(self.settings, run_id=run_id, receipts=receipts)
        return tuple(published)

    def recover_incremental_publication(
        self,
        *,
        shard: int,
        revision: str,
        parent_revision: str,
        verifier: IncrementalShardRecoveryVerifier = (_default_incremental_shard_recovery_verifier),
    ) -> dict[str, Any]:
        """Recover only the next missing receipt from an immutable Hub commit."""

        if shard <= 0:
            raise CompilerScaleError("Wave 5 recovery shard number must be one-based")
        revision_value = _validate_hub_revision(revision, context="recovery revision")
        parent_value = _validate_hub_revision(parent_revision, context="recovery parent revision")
        part = shard - 1
        run_id, receipts, state = self._publication_state()
        if part < len(state.shards):
            existing = state.shards[part]
            if (
                existing.get("revision") == revision_value
                and existing.get("parent_revision") == parent_value
            ):
                return existing
            raise CompilerScaleError(
                "Wave 5 recovery target already has a different immutable receipt"
            )
        if (
            state.pending_checkpoint is not None
            or part != len(state.shards)
            or part >= len(receipts)
        ):
            raise CompilerScaleError(
                "Wave 5 recovery must target the next completed, unreceipted shard"
            )
        previous_revision = state.head_revision
        if previous_revision is not None and parent_value != previous_revision:
            raise CompilerScaleError(
                "Wave 5 recovery parent is not the prior verified shard revision"
            )
        queue = read_json_object(_publication_queue_path(self.settings, part))
        local_root, files = _queued_shard_files(self.settings, part=part, queue=queue)
        verified = verifier(
            repo_id=self.settings.publication_repo_id,
            local_root=local_root,
            files=files,
            remote_prefix=str(queue["remote_prefix"]),
            commit_message=_incremental_commit_message(queue),
            revision=revision_value,
            parent_revision=parent_value,
        )
        publication = _record_incremental_publication(
            self.settings,
            run_id=run_id,
            part=part,
            queue=queue,
            revision=revision_value,
            parent_revision=parent_value,
            verified_remote_hashes=verified,
            previous_revision=previous_revision,
            verification_method="immutable_hub_tree_digest_recovery",
            upload_performed=False,
        )
        self.journal.append(
            {
                "event": "release_shard_publication_receipt_recovered",
                "run_id": run_id,
                "part": part,
                "revision": revision_value,
                "parent_revision": parent_value,
                "receipt_sha256": hash_file(_publication_receipt_path(self.settings, part)),
            }
        )
        _validated_publication_chain(self.settings, run_id=run_id, receipts=receipts)
        return publication

    def recover_metadata_publication(
        self,
        *,
        metadata_kind: Literal["checkpoint", "aggregate"],
        revision: str,
        parent_revision: str,
        checkpoint: int | None = None,
        verifier: IncrementalShardRecoveryVerifier = (_default_incremental_shard_recovery_verifier),
    ) -> dict[str, Any]:
        """Recover a timed-out checkpoint/final commit without a duplicate upload."""

        revision_value = _validate_hub_revision(revision, context="recovery revision")
        parent_value = _validate_hub_revision(parent_revision, context="recovery parent revision")
        run_id, receipts, state = self._publication_state()
        if metadata_kind == "checkpoint":
            if checkpoint is None:
                raise CompilerScaleError("checkpoint recovery requires its row boundary")
            existing_path = _publication_checkpoint_path(self.settings, checkpoint)
            if existing_path.is_file():
                existing_receipt = read_json_object(existing_path)
                if existing_receipt not in state.events:
                    raise CompilerScaleError("checkpoint receipt is outside the verified chain")
                if (
                    existing_receipt.get("revision") == revision_value
                    and existing_receipt.get("parent_revision") == parent_value
                ):
                    return existing_receipt
                raise CompilerScaleError("metadata recovery target already has another receipt")
            if state.pending_checkpoint is None:
                raise CompilerScaleError("no matching checkpoint publication is pending")
            expected_checkpoint, through_part = state.pending_checkpoint
            if checkpoint != expected_checkpoint or state.head_revision is None:
                raise CompilerScaleError("checkpoint recovery is out of publication order")
            _ensure_checkpoint_bundle(
                self.settings,
                run_id=run_id,
                checkpoint=checkpoint,
                through_part=through_part,
                receipts=receipts,
                shard_publications=state.shards,
                prior_events=state.events,
                previous_revision=state.head_revision,
            )
            queue_path = _publication_checkpoint_queue_path(self.settings, checkpoint)
            receipt_path = _publication_checkpoint_path(self.settings, checkpoint)
        else:
            if (
                checkpoint is not None
                or not _aggregate_publication_queue_path(self.settings).is_file()
            ):
                raise CompilerScaleError("final aggregate publication is not pending")
            if len(state.shards) != len(receipts) or state.pending_checkpoint is not None:
                raise CompilerScaleError("final aggregate recovery precedes shard/checkpoint chain")
            if state.head_revision is None:
                raise CompilerScaleError("final aggregate recovery lacks its parent")
            _ensure_aggregate_publication_queue(
                self.settings,
                run_id=run_id,
                publication_events=state.events,
                previous_revision=state.head_revision,
            )
            queue_path = _aggregate_publication_queue_path(self.settings)
            receipt_path = _aggregate_publication_receipt_path(self.settings)
        validated_existing = _validate_metadata_publication(
            self.settings,
            run_id=run_id,
            metadata_kind=metadata_kind,
            queue_path=queue_path,
            receipt_path=receipt_path,
            previous_revision=str(state.head_revision),
        )
        if validated_existing is not None:
            if (
                validated_existing.get("revision") == revision_value
                and validated_existing.get("parent_revision") == parent_value
            ):
                return validated_existing
            raise CompilerScaleError("metadata recovery target already has another receipt")
        if parent_value != state.head_revision:
            raise CompilerScaleError("metadata recovery parent differs from publication head")
        queue = read_json_object(queue_path)
        local_root, files = _metadata_publication_files(
            queue, context=f"Wave 5 {metadata_kind} recovery"
        )
        commit_message = (
            _checkpoint_commit_message(queue)
            if metadata_kind == "checkpoint"
            else _aggregate_commit_message(queue)
        )
        verified = verifier(
            repo_id=self.settings.publication_repo_id,
            local_root=local_root,
            files=files,
            remote_prefix=str(queue["remote_prefix"]),
            commit_message=commit_message,
            revision=revision_value,
            parent_revision=parent_value,
        )
        publication = _record_metadata_publication(
            self.settings,
            run_id=run_id,
            metadata_kind=metadata_kind,
            queue_path=queue_path,
            receipt_path=receipt_path,
            queue=queue,
            revision=revision_value,
            parent_revision=parent_value,
            verified_remote_hashes=verified,
            previous_revision=parent_value,
            verification_method="immutable_hub_tree_digest_recovery",
            upload_performed=False,
        )
        validated = _validate_metadata_publication(
            self.settings,
            run_id=run_id,
            metadata_kind=metadata_kind,
            queue_path=queue_path,
            receipt_path=receipt_path,
            previous_revision=parent_value,
        )
        if validated != publication:
            raise CompilerScaleError("recovered metadata receipt failed immediate replay")
        if metadata_kind == "checkpoint":
            _validated_publication_chain(
                self.settings,
                run_id=run_id,
                receipts=receipts,
                recompute_checkpoints=True,
            )
        self.journal.append(
            {
                "event": f"{metadata_kind}_publication_receipt_recovered",
                "run_id": run_id,
                "revision": revision_value,
                "parent_revision": parent_value,
                "receipt_sha256": hash_file(receipt_path),
            }
        )
        return publication

    def replay(self) -> dict[str, Any]:
        """Verify all durable roots/shards/terminal without a backend construction."""

        audit_gate = verify_audit_gate(
            self.settings.audit,
            typed_spec=self.settings.typed_spec,
            policy=self.policy,
            replay=self.audit_replay,
            typed_gate_verifier=self.typed_gate_verifier,
        )
        selected = _load_selection(self.settings)
        run_id, _ = _ensure_run_spec(self.settings, policy=self.policy, audit_gate=audit_gate)
        receipts, _ = _read_completed_receipts(
            self.settings, run_id=run_id, selected=selected, policy=self.policy
        )
        terminal = read_json_object(self.settings.complete_path)
        self._validate_complete(terminal, run_id=run_id, selected=selected, receipts=receipts)
        cache_receipts: list[dict[str, str]] = []
        processed = sum(int(receipt["input_roots"]) for receipt in receipts)
        for record in selected[:processed]:
            cached = _load_root_cache(record, self.settings, self.policy)
            if cached is None:
                raise CompilerScaleError(f"Wave 5 replay lacks root cache {record['root_id']}")
            terminal_record, path = cached
            cache_receipts.append(
                {
                    "root_id": str(record["root_id"]),
                    "cache_key": str(terminal_record["cache_key"]),
                    "cache_sha256": hash_file(path),
                }
            )
        receipt = {
            "artifact_kind": "sft1_wave5_compiler_scale_replay",
            "schema_version": SCALE_REPLAY_VERSION,
            "run_id": run_id,
            "terminal_sha256": hash_file(self.settings.complete_path),
            "manifest_sha256": terminal["manifest_sha256"],
            "roots_verified": processed,
            "release_shards_verified": len(receipts),
            "released_rows_verified": terminal["released_rows"],
            "cache_receipts_sha256": hash_canonical(cache_receipts),
            "cache_hits": processed,
            "lean_requests": 0,
            "backend_constructed": False,
            "resource_claimed": False,
        }
        _write_exact(self.settings.output_root / "replay_receipt.json", receipt)
        return receipt


def load_compiler_scale_settings(
    config_path: Path,
) -> CompilerScaleSettings:
    """Load the authoritative, additive ``compiler_scale`` YAML contract."""

    audit = replay_module.load_compiler_audit_config(config_path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CompilerScaleError(f"cannot load Wave 5 scale config {config_path}: {exc}") from exc
    root = _mapping(document, "Wave 5 config")
    scale = _mapping(root.get("compiler_scale"), "compiler_scale")
    release = _mapping(root.get("release"), "release")
    if release.get("private_first") is not True:
        raise CompilerScaleError("Wave 5 compiler release must remain private-first")
    if release.get("model_facing_fields") != ["reference", "candidate", "label"]:
        raise CompilerScaleError("Wave 5 compiler release row fields differ")
    if release.get("proof_certified_core_only") is not True:
        raise CompilerScaleError("Wave 5 compiler release must remain proof-certified only")
    expected_keys = {
        "schema_version",
        "output_root",
        "wave4_config_path",
        "root_ceiling",
        "maximum_release_rows",
        "checkpoints",
        "roots_per_shard",
        "root_batch_size",
        "selection_salt",
        "required_any_features",
        "maximum_full_source_characters",
        "n25_maximum_share",
        "projected_runtime_limit_hours",
        "terminal_marker",
        "operations",
        "orbit_operations",
        "maximum_depth",
        "maximum_variants_per_orbit",
        "typed_selection_salt",
    }
    if set(scale) != expected_keys:
        raise CompilerScaleError(
            "compiler_scale keys differ: "
            f"missing={sorted(expected_keys - set(scale))}, "
            f"extra={sorted(set(scale) - expected_keys)}"
        )
    if scale["schema_version"] != "sft1_wave5_compiler_scale_config_v1":
        raise CompilerScaleError("compiler_scale.schema_version differs")
    repo_root = find_repo_root(config_path)

    def resolve_path(value: object, field: str) -> Path:
        if not isinstance(value, str) or not value:
            raise CompilerScaleError(f"compiler_scale.{field} must be a nonempty path")
        path = Path(value)
        return path if path.is_absolute() else repo_root / path

    operations = _string_sequence(scale["operations"], "compiler_scale.operations")
    if set(operations) != set(OPERATIONS) or len(operations) != len(OPERATIONS):
        raise CompilerScaleError("compiler_scale.operations must be the exact current engine set")
    orbit_operations = _string_sequence(
        scale["orbit_operations"], "compiler_scale.orbit_operations"
    )
    if set(orbit_operations) != set(DEFAULT_ORBIT_OPERATIONS) or len(orbit_operations) != len(
        DEFAULT_ORBIT_OPERATIONS
    ):
        raise CompilerScaleError(
            "compiler_scale.orbit_operations must be the exact supported non-N19 set"
        )
    features = frozenset(
        _string_sequence(scale["required_any_features"], "compiler_scale.required_any_features")
    )
    if not features <= DEFAULT_FEATURES:
        raise CompilerScaleError("compiler_scale.required_any_features contains an unknown cell")
    checkpoints_value = scale["checkpoints"]
    if not isinstance(checkpoints_value, list) or not all(
        type(value) is int for value in checkpoints_value
    ):
        raise CompilerScaleError("compiler_scale.checkpoints must be an integer list")
    return CompilerScaleSettings(
        audit=audit,
        wave4_config_path=resolve_path(scale["wave4_config_path"], "wave4_config_path"),
        output_root=resolve_path(scale["output_root"], "output_root"),
        typed_spec=CompilerTypedHookSpec(
            operations=operations,
            orbit_operations=orbit_operations,
            maximum_depth=int(scale["maximum_depth"]),
            maximum_variants_per_orbit=int(scale["maximum_variants_per_orbit"]),
            selection_salt=str(scale["typed_selection_salt"]),
        ),
        root_ceiling=int(scale["root_ceiling"]),
        maximum_release_rows=int(scale["maximum_release_rows"]),
        checkpoints=tuple(cast(list[int], checkpoints_value)),
        roots_per_shard=int(scale["roots_per_shard"]),
        root_batch_size=int(scale["root_batch_size"]),
        selection_salt=str(scale["selection_salt"]),
        required_any_features=features,
        maximum_full_source_characters=int(scale["maximum_full_source_characters"]),
        n25_maximum_share=float(scale["n25_maximum_share"]),
        projected_runtime_limit_hours=float(scale["projected_runtime_limit_hours"]),
        terminal_marker=str(scale["terminal_marker"]),
        publication_repo_id=str(release.get("destination")),
        publication_prefix=str(release.get("prefix")),
    )


def _result_payload(result: CompilerScaleResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "selected_roots": result.selected_roots,
        "processed_roots": result.processed_roots,
        "retained_roots": result.retained_roots,
        "released_rows": result.released_rows,
        "release_shards": result.release_shards,
        "cache_hits": result.cache_hits,
        "lean_requests": result.lean_requests,
        "complete_path": str(result.complete_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("replay")
    subparsers.add_parser("status")
    publish_parser = subparsers.add_parser("publish-pending")
    publish_parser.add_argument("--maximum-shards", type=int)
    recover_parser = subparsers.add_parser("recover-publication")
    recover_parser.add_argument("--shard", type=int, required=True)
    recover_parser.add_argument("--revision", required=True)
    recover_parser.add_argument("--parent-revision", required=True)
    metadata_parser = subparsers.add_parser("recover-metadata")
    metadata_parser.add_argument("--kind", choices=("checkpoint", "aggregate"), required=True)
    metadata_parser.add_argument("--checkpoint", type=int)
    metadata_parser.add_argument("--revision", required=True)
    metadata_parser.add_argument("--parent-revision", required=True)
    args = parser.parse_args(argv)
    settings = load_compiler_scale_settings(args.config)
    if args.command == "status":
        target = (
            settings.complete_path if settings.complete_path.is_file() else settings.status_path
        )
        print(json.dumps(read_json_object(target), sort_keys=True))
        return 0
    runner = CompilerScaleRunner(settings)
    if args.command == "replay":
        print(json.dumps(runner.replay(), sort_keys=True))
        return 0
    if args.command == "publish-pending":
        receipts = runner.publish_pending(maximum_shards=args.maximum_shards)
        print(
            json.dumps(
                {
                    "published_shards": len(receipts),
                    "receipts": list(receipts),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "recover-publication":
        receipt = runner.recover_incremental_publication(
            shard=args.shard,
            revision=args.revision,
            parent_revision=args.parent_revision,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.command == "recover-metadata":
        receipt = runner.recover_metadata_publication(
            metadata_kind=args.kind,
            checkpoint=args.checkpoint,
            revision=args.revision,
            parent_revision=args.parent_revision,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    result = runner.run()
    print(json.dumps(_result_payload(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

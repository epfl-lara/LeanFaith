"""Proof-bearing compiler-context audit and typed SFT1 hook.

Wave 5 first builds a zero-Lean inventory.  This module consumes only the
inventory's frozen stratified audit sample, resolves every row back to its
pinned CPT2 Parquet locator, reconstructs the exact ``theorem + "by" + body``
source, and asks Lean to check the resulting local theorem constant and proof.

The historical audit driver remains a compatibility receipt.  The additive
typed-hook request builders in this module declare the same exact local
theorem, use ``loadCompilerRoot`` (never the imported-name ``loadRoot``), and
pass its checked type/proof through the shared Wave 3 ``runOp`` and Wave 4
descriptor/selected-certificate implementations.

All Lean execution goes through :mod:`leanfaith.lean.protocol` and the one
central LeanInteract adapter.  Work is grouped by exact source-context hash,
served by at most two persistent workers, retried only for bounded
infrastructure failures, and journaled/cacheable per source row.  Forced
replay validates the terminal cache without constructing a backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.cpt2.splitters import mask_lean_source
from leanfaith.host_resources import (
    Reservation,
    ReservationError,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.lean.leaninteract_backend import (
    METHOD_VERSION as BACKEND_METHOD_VERSION,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import RetryPolicy, ServerMode, run_batch_with_retries
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
    GoalV1Error,
    _closed_expr_command,
    _closed_expr_sidecar_from_payload,
    _implementation_identity,
    _messages_report_sorry,
    _parse_closed_expr_payloads,
)
from leanfaith.sft1.sprint import compiler_inventory as inventory_module
from leanfaith.sft1.sprint.compiler_inventory import (
    InputShard,
    InventorySettings,
    build_compiler_record,
    extract_theorem_signature,
    load_inventory_config,
    load_pinned_input_shards,
    reconstruct_source,
)
from leanfaith.sft1.sprint.engine import (
    OPERATIONS,
    SprintEngineError,
    operation_mask,
    parse_evidence_lines,
    strip_imports,
)
from leanfaith.sft1.sprint.orbit import (
    OrbitError,
    OrbitPolicy,
)
from leanfaith.sft1.sprint.runner import canonical_surface
from leanfaith.sft1.sprint.square import (
    ENDPOINT_ORIGIN,
    ENDPOINT_ROLE,
    ValidatedWave4Root,
    Wave4VariantDescriptor,
    combine_wave4_selected_payload,
    select_wave4_variants,
    validate_wave4_root_payload,
)
from leanfaith.sft1.sprint.store import Journal, read_json_object, write_atomic

AUDIT_SCHEMA_VERSION = "sft1_compiler_context_audit_v1"
AUDIT_CACHE_VERSION = "sft1_compiler_context_audit_cache_v1"
AUDIT_RUN_SPEC_VERSION = "sft1_compiler_context_audit_run_v1"
AUDIT_REPLAY_VERSION = "sft1_compiler_context_audit_replay_v1"
CHECKER_VERSION = "sft1_wave5_compiler_context_replay_v1"
EVIDENCE_MARKER = "LFSFT1COMPILERAUDITJSON "
DOWNSTREAM_MODE = "proof_certified_typed_sft1_hook_v1"
TYPED_HOOK_SCHEMA_VERSION = "sft1_wave5_compiler_typed_hook_v1"
TYPED_HOOK_CACHE_VERSION = "sft1_wave5_compiler_typed_hook_cache_v1"
TYPED_HOOK_RUN_SPEC_VERSION = "sft1_wave5_compiler_typed_hook_run_v1"
TYPED_HOOK_REPLAY_VERSION = "sft1_wave5_compiler_typed_hook_replay_v1"
TYPED_HOOK_CHECKER_VERSION = "sft1_wave5_compiler_typed_hook_v1"
TYPED_HOOK_ORBIT_OPERATIONS = frozenset(
    {
        "ORBIT_WAVE4_N25_V1",
        "ORBIT_WAVE4_N26_V1",
        "ORBIT_WAVE4_N29_V1",
        "ORBIT_WAVE4_N30_V1",
        "ORBIT_WAVE4_N31_V1",
        "ORBIT_WAVE4_N32_V1",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_SAFE_TERMINAL = re.compile(r"[A-Za-z0-9_.-]+\.json")
_RETRYABLE = frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT})
_NONRESULT_INFRASTRUCTURE = frozenset(
    {
        LeanStatus.CRASH,
        LeanStatus.INTERNAL_ERROR,
        LeanStatus.TIMEOUT,
        LeanStatus.SETUP_ERROR,
        LeanStatus.UNSUPPORTED,
    }
)


class CompilerReplayError(RuntimeError):
    """A source, cache, run identity, or audit receipt is inconsistent."""


class CompilerReplayInfrastructureError(CompilerReplayError):
    """The bounded central Lean backend did not return a semantic terminal."""


@dataclass(frozen=True, slots=True)
class CompilerAuditSettings:
    """Pinned execution settings layered over :class:`InventorySettings`."""

    inventory: InventorySettings
    config_path: Path
    config_sha256: str
    output_root: Path
    project_dir: Path
    engine_path: Path
    resource_task: str
    lean_workers: int
    lean_rss_claim_gib: float
    memory_hard_limit_mb: int
    request_timeout_seconds: float
    context_request_max_roots: int
    request_batch_size: int
    retry_max_attempts: int
    retry_statuses: frozenset[LeanStatus]
    terminal_marker: str
    expected_rows: int
    elab_async: bool
    isolate_incremental_commands: bool
    downstream_mode: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.config_sha256) is None:
            raise ValueError("config_sha256 must be a lowercase SHA-256")
        if not 1 <= self.lean_workers <= 2:
            raise ValueError("compiler audit allows one or two Lean workers")
        if not 0 < self.lean_rss_claim_gib <= 40:
            raise ValueError("compiler audit RSS claim must be within 40 GiB")
        if self.memory_hard_limit_mb < 1024:
            raise ValueError("compiler audit hard memory limit is too small")
        if self.request_timeout_seconds <= 0:
            raise ValueError("compiler audit request timeout must be positive")
        if self.context_request_max_roots <= 0 or self.request_batch_size <= 0:
            raise ValueError("compiler audit batch sizes must be positive")
        if self.retry_max_attempts not in {1, 2, 3}:
            raise ValueError("compiler audit retry attempts must be in [1, 3]")
        if not self.retry_statuses <= _RETRYABLE:
            raise ValueError("compiler audit may retry only infrastructure/timeouts")
        if self.expected_rows <= 0:
            raise ValueError("compiler audit expected row count must be positive")
        if self.elab_async:
            raise ValueError("compiler audit requires Elab.async=false")
        if not self.isolate_incremental_commands:
            raise ValueError("compiler audit requires isolated incremental commands")
        if self.downstream_mode != DOWNSTREAM_MODE:
            raise ValueError("compiler audit downstream mode must remain fail-closed")
        if _SAFE_TERMINAL.fullmatch(self.terminal_marker) is None:
            raise ValueError("compiler audit terminal marker must be a safe JSON basename")

    @property
    def sample_path(self) -> Path:
        return self.inventory.output_root / "audit" / f"sample-{self.expected_rows:05d}.jsonl"

    @property
    def sample_receipt_path(self) -> Path:
        return self.inventory.output_root / "_state" / "audit_sample.json"

    @property
    def inventory_manifest_path(self) -> Path:
        return self.inventory.output_root / "manifest.json"

    @property
    def complete_path(self) -> Path:
        return self.output_root / self.terminal_marker


@dataclass(frozen=True, slots=True)
class CompilerAuditSource:
    """One inventory row resolved to its exact pinned proof-bearing source."""

    inventory_record: Mapping[str, Any]
    shard: InputShard
    theorem: str
    body: str
    full_source: str
    context_prefix: str
    declaration_source: str
    qualified_name: str | None

    @property
    def root_id(self) -> str:
        return str(self.inventory_record["root_id"])

    @property
    def context_fingerprint(self) -> str:
        context = _mapping(self.inventory_record["context"], "inventory context")
        return str(context["context_fingerprint"])

    @property
    def inventory_record_sha256(self) -> str:
        return str(self.inventory_record["inventory_record_sha256"])


@dataclass(frozen=True, slots=True)
class PreparedAuditRequest:
    roots: tuple[CompilerAuditSource, ...]
    request: LeanRequest


@dataclass(frozen=True, slots=True)
class CompilerTypedHookSpec:
    """Bounded typed execution policy, hashed into every request and terminal."""

    operations: tuple[str, ...]
    orbit_operations: tuple[str, ...]
    maximum_depth: int = 3
    maximum_variants_per_orbit: int = 5
    selection_salt: str = "sft1-wave5-compiler-typed-selection-v1"

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError("compiler typed hook requires at least one Wave 3 operation")
        if len(self.operations) != len(set(self.operations)) or any(
            operation not in OPERATIONS for operation in self.operations
        ):
            raise ValueError("compiler typed hook operations must be unique known operations")
        if len(self.orbit_operations) != len(set(self.orbit_operations)) or any(
            operation not in TYPED_HOOK_ORBIT_OPERATIONS for operation in self.orbit_operations
        ):
            raise ValueError(
                "compiler typed hook orbit operations must be unique Wave 4 operations"
            )
        if not 1 <= self.maximum_depth <= 3:
            raise ValueError("compiler typed hook maximum depth must be in [1, 3]")
        if not 1 <= self.maximum_variants_per_orbit <= 5:
            raise ValueError("compiler typed hook variant bound must be in [1, 5]")
        if not self.selection_salt:
            raise ValueError("compiler typed hook selection salt must be nonempty")

    @property
    def operation_mask(self) -> int:
        return operation_mask(self.operations)

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": TYPED_HOOK_SCHEMA_VERSION,
            "operations": list(self.operations),
            "operation_mask": self.operation_mask,
            "orbit_operations": list(self.orbit_operations),
            "maximum_depth": self.maximum_depth,
            "maximum_variants_per_orbit": self.maximum_variants_per_orbit,
            "selection_salt": self.selection_salt,
        }


@dataclass(frozen=True, slots=True)
class PreparedTypedHookRequest:
    source: CompilerAuditSource
    phase: Literal["descriptor", "wave3_render", "wave4_selected"]
    request: LeanRequest
    source_binding: Mapping[str, str]
    operation_id: str | None = None
    selected_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CompilerTypedWave4Selection:
    """One root's exact operation and ordered descriptor selection."""

    source: CompilerAuditSource
    operation_id: str
    selected_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PreparedTypedHookBatchRequest:
    """One exact-context Lean request containing several independent roots."""

    sources: tuple[CompilerAuditSource, ...]
    phase: Literal["descriptor", "wave4_selected"]
    request: LeanRequest
    source_bindings: tuple[Mapping[str, str], ...]
    selections: tuple[CompilerTypedWave4Selection, ...] = ()


@dataclass(frozen=True, slots=True)
class CompilerAuditResult:
    run_id: str
    complete_path: Path
    status: Literal["passed", "failed"]
    roots: int
    compatible: int
    incompatible: int
    cache_hits: int
    lean_requests: int


@dataclass(frozen=True, slots=True)
class CompilerTypedHookResult:
    run_id: str
    complete_path: Path
    status: Literal["passed", "failed"]
    roots: int
    certified: int
    rejected: int
    cache_hits: int
    lean_requests: int


BackendFactory = Callable[[BackendSettings], LeanBackend]


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompilerReplayError(f"{context} must be a mapping")
    return cast(dict[str, Any], value)


def _string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CompilerReplayError(f"{context} must be a string list")
    return tuple(cast(list[str], value))


def _resolve_path(repo_root: Path, value: object, context: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompilerReplayError(f"{context} must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def load_compiler_audit_config(path: Path) -> CompilerAuditSettings:
    """Load and strictly validate the Wave 5 audit portion of the shared config."""

    inventory = load_inventory_config(path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CompilerReplayError(f"cannot load compiler audit config {path}: {exc}") from exc
    root = _mapping(document, "Wave 5 config")
    audit = _mapping(root.get("compiler_audit"), "compiler_audit")
    execution = _mapping(root.get("execution"), "execution")
    repo_root = find_repo_root(path)
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise CompilerReplayError("compiler_audit.schema_version differs")
    if audit.get("checker_version") != CHECKER_VERSION:
        raise CompilerReplayError("compiler_audit.checker_version differs")
    statuses = frozenset(
        LeanStatus(item) for item in _string_list(audit.get("retry_statuses"), "retry_statuses")
    )
    expected_rows = int(audit["expected_rows"])
    if inventory.audit_sample is None or inventory.audit_sample.size != expected_rows:
        raise CompilerReplayError("compiler audit size differs from inventory sample size")
    settings = CompilerAuditSettings(
        inventory=inventory,
        config_path=path.resolve(),
        config_sha256=hash_file(path),
        output_root=_resolve_path(repo_root, audit.get("output_root"), "audit output root"),
        project_dir=_resolve_path(repo_root, audit.get("project_dir"), "audit project dir"),
        engine_path=_resolve_path(repo_root, audit.get("engine_path"), "audit engine path"),
        resource_task=str(audit["resource_task"]),
        lean_workers=int(execution["lean_workers_after_inventory"]),
        lean_rss_claim_gib=float(execution["host_rss_ceiling_gib"]),
        memory_hard_limit_mb=int(audit["memory_hard_limit_mb"]),
        request_timeout_seconds=float(audit["request_timeout_seconds"]),
        context_request_max_roots=int(audit["context_request_max_roots"]),
        request_batch_size=int(audit["request_batch_size"]),
        retry_max_attempts=int(audit["retry_max_attempts"]),
        retry_statuses=statuses,
        terminal_marker=str(audit["terminal_marker"]),
        expected_rows=expected_rows,
        elab_async=bool(execution["elab_async"]),
        isolate_incremental_commands=bool(audit["isolate_incremental_commands"]),
        downstream_mode=str(audit["downstream_mode"]),
    )
    if settings.inventory.project.checker_version != CHECKER_VERSION:
        raise CompilerReplayError("inventory and compiler-audit checker versions differ")
    return settings


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                rows.append(_mapping(value, f"{path}:{line_number}"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompilerReplayError(f"cannot read compiler audit sample {path}: {exc}") from exc
    return rows


def load_audit_sample(settings: CompilerAuditSettings) -> tuple[dict[str, Any], ...]:
    """Verify the manifest-last inventory sample before resolving source bytes."""

    manifest = read_json_object(settings.inventory_manifest_path)
    receipt = read_json_object(settings.sample_receipt_path)
    run_spec = read_json_object(settings.inventory.output_root / "_state" / "run_spec.json")
    if manifest.get("run_id") != run_spec.get("run_id"):
        raise CompilerReplayError("inventory manifest and run spec differ")
    if run_spec.get("config_sha256") != settings.config_sha256:
        raise CompilerReplayError("inventory was not built from the exact Wave 5 config")
    if receipt.get("run_id") != manifest.get("run_id"):
        raise CompilerReplayError("audit sample receipt belongs to another inventory run")
    if receipt.get("rows") != settings.expected_rows:
        raise CompilerReplayError("audit sample row count differs")
    if receipt.get("sha256") != hash_file(settings.sample_path):
        raise CompilerReplayError("audit sample SHA-256 differs")
    manifest_audit = _mapping(manifest.get("audit_sample"), "inventory audit_sample")
    if manifest_audit != receipt:
        raise CompilerReplayError("inventory manifest does not bind the audit sample receipt")
    rows = _read_jsonl(settings.sample_path)
    if len(rows) != settings.expected_rows:
        raise CompilerReplayError("audit sample data count differs")
    root_ids: set[str] = set()
    for row in rows:
        root_id = row.get("root_id")
        record_hash = row.get("inventory_record_sha256")
        if not isinstance(root_id, str) or _SHA256.fullmatch(root_id) is None:
            raise CompilerReplayError("audit sample has a malformed root_id")
        if root_id in root_ids:
            raise CompilerReplayError(f"audit sample repeats root {root_id}")
        root_ids.add(root_id)
        if not isinstance(record_hash, str) or _SHA256.fullmatch(record_hash) is None:
            raise CompilerReplayError("audit sample has a malformed record hash")
        unhashed = dict(row)
        del unhashed["inventory_record_sha256"]
        # Selection metadata is appended after the immutable inventory record is
        # hashed.  It is independently bound by the sample artifact checksum and
        # receipt, so it must not be folded back into the inventory-record hash.
        unhashed.pop("audit_selection", None)
        if hash_canonical(unhashed) != record_hash:
            raise CompilerReplayError(f"inventory record hash differs for {root_id}")
    return tuple(rows)


def _parquet_row_groups(
    parquet: pq.ParquetFile, indexes: Sequence[int]
) -> Iterator[tuple[int, int, tuple[int, ...]]]:
    wanted = sorted(set(indexes))
    position = 0
    offset = 0
    for group_index in range(parquet.num_row_groups):
        rows = parquet.metadata.row_group(group_index).num_rows
        selected: list[int] = []
        while position < len(wanted) and wanted[position] < offset + rows:
            if wanted[position] < offset:
                raise CompilerReplayError("Parquet row locator order is inconsistent")
            selected.append(wanted[position])
            position += 1
        if selected:
            yield group_index, offset, tuple(selected)
        offset += rows
    if position != len(wanted):
        raise CompilerReplayError("audit sample contains an out-of-range Parquet row locator")


def _validate_rebuilt_record(
    inventory_record: Mapping[str, Any],
    *,
    theorem: str,
    body: str,
    shard: InputShard,
    settings: InventorySettings,
) -> None:
    source = _mapping(inventory_record["source"], "inventory source")
    draft = build_compiler_record(
        theorem=theorem,
        body=body,
        row_index=int(source["row_index"]),
        shard=shard,
        pin=settings.pin,
        project=settings.project,
    )
    for key, value in draft.record.items():
        if inventory_record.get(key) != value:
            raise CompilerReplayError(
                f"reconstructed inventory field {key!r} differs for {inventory_record['root_id']}"
            )


def resolve_audit_sources(
    settings: CompilerAuditSettings,
    records: Sequence[Mapping[str, Any]],
    *,
    shards: Sequence[InputShard] | None = None,
) -> tuple[CompilerAuditSource, ...]:
    """Resolve locators with row-group reads and exact hash reconstruction.

    Callers processing many small batches may pass one previously validated result
    from :func:`load_pinned_input_shards`.  The default retains the historical
    verify-on-each-call behaviour; supplied shards are never rehashed here, while
    every requested row still has to match its pinned locator and reconstruct its
    complete inventory identity.
    """

    resolved_shards = (
        tuple(load_pinned_input_shards(settings.inventory)) if shards is None else tuple(shards)
    )
    by_locator = {(shard.split, shard.file): shard for shard in resolved_shards}
    if len(by_locator) != len(resolved_shards):
        raise CompilerReplayError("prevalidated CPT2 shards repeat a split/file locator")
    requested: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        source = _mapping(record.get("source"), "inventory source")
        locator = (str(source.get("split")), str(source.get("shard_file")))
        shard = by_locator.get(locator)
        if shard is None:
            raise CompilerReplayError(f"unknown pinned CPT2 shard locator {locator}")
        if source.get("shard_sha256") != shard.sha256 or int(source.get("part", -1)) != shard.part:
            raise CompilerReplayError(f"CPT2 shard identity differs for {record['root_id']}")
        requested[locator].append(record)

    resolved: dict[str, CompilerAuditSource] = {}
    for locator in sorted(requested):
        shard = by_locator[locator]
        rows_by_index: dict[int, Mapping[str, Any]] = {}
        for record in requested[locator]:
            source = _mapping(record["source"], "inventory source")
            row_index = int(source["row_index"])
            if row_index in rows_by_index:
                raise CompilerReplayError(
                    f"duplicate Parquet row locator in audit sample: {locator}"
                )
            rows_by_index[row_index] = record
        parquet = pq.ParquetFile(shard.path)
        if parquet.schema_arrow.names != ["theorem", "body", "label"]:
            raise CompilerReplayError(f"unexpected CPT2 schema at {shard.path}")
        found_indexes: set[int] = set()
        for group_index, group_offset, indexes in _parquet_row_groups(
            parquet, tuple(rows_by_index)
        ):
            table = parquet.read_row_group(group_index, columns=["theorem", "body", "label"])
            for absolute_index in indexes:
                local_index = absolute_index - group_offset
                theorem = table.column("theorem")[local_index].as_py()
                body = table.column("body")[local_index].as_py()
                label = table.column("label")[local_index].as_py()
                if not isinstance(theorem, str) or not isinstance(body, str) or label is not True:
                    raise CompilerReplayError(
                        f"audit locator {shard.file}:{absolute_index} is not valid "
                        "proof-bearing CPT2"
                    )
                record = rows_by_index[absolute_index]
                _validate_rebuilt_record(
                    record,
                    theorem=theorem,
                    body=body,
                    shard=shard,
                    settings=settings.inventory,
                )
                signature = extract_theorem_signature(theorem)
                full_source = reconstruct_source(theorem, body)
                if (
                    full_source
                    != signature.context_prefix
                    + theorem[signature.declaration_offset :]
                    + "by"
                    + body
                ):
                    raise CompilerReplayError("context/declaration reconstruction is not exact")
                declaration = _mapping(record["declaration"], "inventory declaration")
                qualified = declaration.get("qualified_name_candidate")
                resolved[str(record["root_id"])] = CompilerAuditSource(
                    inventory_record=record,
                    shard=shard,
                    theorem=theorem,
                    body=body,
                    full_source=full_source,
                    context_prefix=signature.context_prefix,
                    declaration_source=theorem[signature.declaration_offset :] + "by" + body,
                    qualified_name=str(qualified) if isinstance(qualified, str) else None,
                )
                found_indexes.add(absolute_index)
        if set(rows_by_index) != found_indexes:
            raise CompilerReplayError(f"not every locator was resolved in {shard.file}")
    if len(resolved) != len(records):
        raise CompilerReplayError("not every audit sample row resolved to pinned source")
    return tuple(resolved[str(record["root_id"])] for record in records)


def _name_components(name: str) -> tuple[str, ...]:
    if not name or name.startswith(".") or name.endswith("."):
        raise CompilerReplayError(f"unsafe qualified Lean name {name!r}")
    components: list[str] = []
    current: list[str] = []
    quoted = False
    for char in name:
        if char == "«":
            if quoted:
                raise CompilerReplayError(f"nested guillemets in Lean name {name!r}")
            quoted = True
            current.append(char)
        elif char == "»":
            if not quoted:
                raise CompilerReplayError(f"unmatched guillemet in Lean name {name!r}")
            quoted = False
            current.append(char)
        elif char == "." and not quoted:
            component = "".join(current)
            if not component:
                raise CompilerReplayError(f"empty component in Lean name {name!r}")
            components.append(component)
            current = []
        else:
            current.append(char)
    if quoted:
        raise CompilerReplayError(f"unterminated guillemet in Lean name {name!r}")
    component = "".join(current)
    if not component:
        raise CompilerReplayError(f"empty component in Lean name {name!r}")
    components.append(component)
    cleaned: list[str] = []
    for item in components:
        if item.startswith("«") or item.endswith("»"):
            if not (item.startswith("«") and item.endswith("»") and len(item) > 2):
                raise CompilerReplayError(f"malformed quoted Lean name component {item!r}")
            item = item[1:-1]
        cleaned.append(item)
    return tuple(cleaned)


def _lean_name_expression(name: str) -> str:
    expression = "Name.anonymous"
    for component in _name_components(name):
        expression = f"Name.mkStr ({expression}) {json.dumps(component, ensure_ascii=False)}"
    return expression


def _preflight_reason(source: CompilerAuditSource) -> str | None:
    declaration = _mapping(source.inventory_record["declaration"], "inventory declaration")
    context = _mapping(source.inventory_record["context"], "inventory context")
    if declaration.get("qualified_name_status") != "simple_namespace_stack_v1":
        return "unresolved_namespace_context"
    if context.get("namespace_status") != "simple_namespace_stack_v1":
        return "unresolved_namespace_context"
    if source.qualified_name is None:
        return "qualified_constant_name_unresolved"
    try:
        _name_components(source.qualified_name)
    except CompilerReplayError:
        return "qualified_constant_name_unresolved"
    for command in _string_list(context.get("option_commands"), "context option commands"):
        if re.fullmatch(r"set_option\s+Elab\.async\s+true", command):
            return "source_reenables_async_elaboration"
    return None


_TYPED_IMPORT = re.compile(r"^(?:(?:public|meta)\s+)*import\s+(?P<modules>\S+(?:\s+\S+)*)$")


def _typed_source_binding(
    source: CompilerAuditSource, settings: CompilerAuditSettings
) -> dict[str, str]:
    """Recompute and bind every source/type/proof/context identity before Lean."""

    _validate_rebuilt_record(
        source.inventory_record,
        theorem=source.theorem,
        body=source.body,
        shard=source.shard,
        settings=settings.inventory,
    )
    unhashed_record = dict(source.inventory_record)
    recorded_inventory_hash = unhashed_record.pop("inventory_record_sha256", None)
    unhashed_record.pop("audit_selection", None)
    if recorded_inventory_hash != hash_canonical(unhashed_record):
        raise CompilerReplayError("typed hook inventory record SHA-256 differs")
    hashes = _mapping(source.inventory_record["hashes"], "inventory hashes")
    context = _mapping(source.inventory_record["context"], "inventory context")
    declaration = _mapping(source.inventory_record["declaration"], "inventory declaration")
    expected = {
        "theorem_sha256": sha256_hex(source.theorem.encode("utf-8")),
        "body_sha256": sha256_hex(source.body.encode("utf-8")),
        "full_source_sha256": sha256_hex(source.full_source.encode("utf-8")),
        "context_sha256": sha256_hex(source.context_prefix.encode("utf-8")),
    }
    for field, observed in expected.items():
        recorded = context[field] if field == "context_sha256" else hashes[field]
        if recorded != observed:
            raise CompilerReplayError(
                f"typed hook reconstructed {field} differs for {source.root_id}"
            )
    if source.full_source != source.context_prefix + source.declaration_source:
        raise CompilerReplayError("typed hook declaration does not reconstruct full source")
    if source.qualified_name is None:
        raise CompilerReplayError("typed hook requires an exact qualified theorem name")
    if declaration.get("kind") not in {"theorem", "lemma"}:
        raise CompilerReplayError("typed hook source is not a theorem or lemma")
    if declaration.get("qualified_name_candidate") != source.qualified_name:
        raise CompilerReplayError("typed hook qualified name differs from its inventory record")
    binding = {
        "rootId": source.root_id,
        "sourceRowId": str(source.inventory_record["source_row_id"]),
        "inventoryRecordSha256": source.inventory_record_sha256,
        "theoremSha256": str(hashes["theorem_sha256"]),
        "proofSourceSha256": str(hashes["body_sha256"]),
        "typeSourceSha256": str(hashes["normalized_signature_sha256"]),
        "fullSourceSha256": str(hashes["full_source_sha256"]),
        "declarationSourceSha256": sha256_hex(source.declaration_source.encode("utf-8")),
        "contextSha256": str(context["context_sha256"]),
        "contextFingerprint": str(context["context_fingerprint"]),
        "qualifiedName": source.qualified_name,
        "sourceRevision": settings.inventory.pin.final_revision,
        "projectRevision": settings.inventory.project.project_revision,
        "checkerVersion": TYPED_HOOK_CHECKER_VERSION,
    }
    digest_fields = {
        "rootId",
        "sourceRowId",
        "inventoryRecordSha256",
        "theoremSha256",
        "proofSourceSha256",
        "typeSourceSha256",
        "fullSourceSha256",
        "declarationSourceSha256",
        "contextSha256",
        "contextFingerprint",
    }
    for field in digest_fields:
        if _SHA256.fullmatch(binding[field]) is None:
            raise CompilerReplayError(f"typed source binding has malformed {field}")
    for field in ("sourceRevision", "projectRevision"):
        if _REVISION.fullmatch(binding[field]) is None:
            raise CompilerReplayError(f"typed source binding has malformed {field}")
    return binding


def _lean_compiler_binding(binding: Mapping[str, str]) -> str:
    fields = tuple(camel for camel, _snake in _COMPILER_BINDING_FIELDS)
    if set(binding) != set(fields):
        raise CompilerReplayError("typed source binding field set differs")
    assignments = ",\n      ".join(
        f"{field} := {json.dumps(binding[field], ensure_ascii=False)}" for field in fields
    )
    return "{\n      " + assignments + "\n    }"


_COMPILER_BINDING_FIELDS = (
    ("rootId", "root_id"),
    ("sourceRowId", "source_row_id"),
    ("inventoryRecordSha256", "inventory_record_sha256"),
    ("theoremSha256", "theorem_sha256"),
    ("proofSourceSha256", "proof_source_sha256"),
    ("typeSourceSha256", "type_source_sha256"),
    ("fullSourceSha256", "full_source_sha256"),
    ("declarationSourceSha256", "declaration_source_sha256"),
    ("contextSha256", "context_sha256"),
    ("contextFingerprint", "context_fingerprint"),
    ("qualifiedName", "qualified_name"),
    ("sourceRevision", "source_revision"),
    ("projectRevision", "project_revision"),
    ("checkerVersion", "checker_version"),
)


def _compiler_binding_json(binding: Mapping[str, str]) -> dict[str, str]:
    """Translate the Python/Lean constructor fields to Lean's evidence keys."""

    expected = {camel for camel, _snake in _COMPILER_BINDING_FIELDS}
    if set(binding) != expected:
        raise CompilerReplayError("typed source binding field set differs")
    return {snake: binding[camel] for camel, snake in _COMPILER_BINDING_FIELDS}


def _split_typed_context_imports(source: CompilerAuditSource) -> tuple[str, str]:
    """Move only leading import commands ahead of the injected engine.

    Every non-import byte remains in order immediately before the exact local
    declaration.  Contexts with ``prelude`` or a late import fail closed because
    inserting the shared engine there would change compilation semantics.
    """

    raw_lines = source.context_prefix.splitlines(keepends=True)
    masked_lines = mask_lean_source(source.context_prefix).splitlines(keepends=True)
    if len(raw_lines) != len(masked_lines):
        raise CompilerReplayError("typed hook context masking changed line topology")
    import_lines: list[str] = []
    imported_modules: list[str] = []
    remainder: list[str] = []
    saw_nonimport_command = False
    for raw, masked in zip(raw_lines, masked_lines, strict=True):
        normalized = " ".join(masked.split())
        match = _TYPED_IMPORT.fullmatch(normalized)
        if match is not None:
            if saw_nonimport_command:
                raise CompilerReplayError("typed hook refuses an import after a context command")
            import_lines.append(normalized)
            imported_modules.extend(match.group("modules").split())
            if raw.endswith("\r\n"):
                remainder.append("\r\n")
            elif raw.endswith("\n") or raw.endswith("\r"):
                remainder.append(raw[-1])
            continue
        if normalized == "prelude":
            raise CompilerReplayError("typed hook cannot inject the engine into a prelude context")
        if normalized:
            saw_nonimport_command = True
        remainder.append(raw)
    if not import_lines:
        raise CompilerReplayError("typed hook requires an explicit source import")
    context = _mapping(source.inventory_record["context"], "inventory context")
    recorded_imports = _string_list(context.get("imports"), "inventory imports")
    if tuple(imported_modules) != recorded_imports:
        raise CompilerReplayError("typed hook import extraction differs from inventory")
    return "\n".join(import_lines), "".join(remainder)


def _typed_compile_context(
    source: CompilerAuditSource, settings: CompilerAuditSettings
) -> tuple[CompileContext, str]:
    import_header, context_remainder = _split_typed_context_imports(source)
    return (
        CompileContext(
            project_id=settings.inventory.project.project_id,
            project_revision=settings.inventory.project.project_revision,
            lean_version=settings.inventory.project.lean_version,
            import_header=import_header,
            command_preamble=strip_imports(settings.engine_path),
            options={},
        ),
        context_remainder,
    )


def _typed_session_source(source: CompilerAuditSource, context_remainder: str, action: str) -> str:
    declaration = context_remainder + source.declaration_source
    if not declaration.endswith(("\n", "\r")):
        declaration += "\n"
    return declaration + "\n" + action.strip() + "\n"


_TYPED_ENDPOINTS = ("p", "c", "p_prime", "c_prime")


def typed_wave4_endpoint_id(root_id: str, slot: int, endpoint: str) -> str:
    """Return the collision-free endpoint identity used by multi-root requests."""

    if _SHA256.fullmatch(root_id) is None:
        raise CompilerReplayError("typed Wave 4 endpoint requires a lowercase SHA-256 root ID")
    if type(slot) is not int or slot < 0:
        raise CompilerReplayError("typed Wave 4 endpoint slot must be a natural number")
    if endpoint not in _TYPED_ENDPOINTS:
        raise CompilerReplayError(f"unknown typed Wave 4 endpoint {endpoint!r}")
    return f"{root_id}.{slot}.{endpoint}"


def _typed_batch_context(
    sources: Sequence[CompilerAuditSource],
    settings: CompilerAuditSettings,
) -> tuple[
    tuple[CompilerAuditSource, ...],
    tuple[dict[str, str], ...],
    CompileContext,
    str,
]:
    """Validate one byte-identical source context before composing a Lean request."""

    exact_sources = tuple(sources)
    if not exact_sources:
        raise CompilerReplayError("typed hook batch must contain at least one source")
    if len(exact_sources) > settings.context_request_max_roots:
        raise CompilerReplayError("typed hook batch exceeds the configured context root bound")
    root_ids = [source.root_id for source in exact_sources]
    names = [source.qualified_name for source in exact_sources]
    if len(root_ids) != len(set(root_ids)):
        raise CompilerReplayError("typed hook batch repeats a compiler root ID")
    if any(name is None for name in names) or len(names) != len(set(names)):
        raise CompilerReplayError("typed hook batch requires unique qualified theorem names")

    first = exact_sources[0]
    first_reason = _preflight_reason(first)
    if first_reason is not None:
        raise CompilerReplayError(f"typed hook preflight rejected source: {first_reason}")
    compile_context, context_remainder = _typed_compile_context(first, settings)
    bindings: list[dict[str, str]] = [_typed_source_binding(first, settings)]
    for source in exact_sources[1:]:
        reason = _preflight_reason(source)
        if reason is not None:
            raise CompilerReplayError(f"typed hook preflight rejected source: {reason}")
        if (
            source.context_prefix != first.context_prefix
            or source.context_fingerprint != first.context_fingerprint
        ):
            raise CompilerReplayError(
                "typed hook batch sources do not share one byte-identical context"
            )
        import_header, remainder = _split_typed_context_imports(source)
        if import_header != compile_context.import_header or remainder != context_remainder:
            raise CompilerReplayError(
                "typed hook batch source context extraction is not byte-identical"
            )
        bindings.append(_typed_source_binding(source, settings))
    return exact_sources, tuple(bindings), compile_context, context_remainder


def _typed_batch_session_source(
    sources: Sequence[CompilerAuditSource],
    context_remainder: str,
    actions: Sequence[str],
) -> str:
    if len(sources) != len(actions):
        raise CompilerReplayError("typed hook batch must have exactly one action per root")
    pieces = [context_remainder]
    for source in sources:
        pieces.append(source.declaration_source)
        if not source.declaration_source.endswith(("\n", "\r")):
            pieces.append("\n")
        pieces.append("\n")
    for action in actions:
        pieces.append(action.strip())
        pieces.append("\n")
    return "".join(pieces)


def _typed_descriptor_action(
    source: CompilerAuditSource,
    binding: Mapping[str, str],
    spec: CompilerTypedHookSpec,
) -> str:
    orbit_literals = ", ".join(
        json.dumps(operation, ensure_ascii=False) for operation in spec.orbit_operations
    )
    return f"""
set_option Elab.async false in
run_meta do
  let binding : LeanFaith.SFT1.Sprint.CompilerSourceBinding := {_lean_compiler_binding(binding)}
  LeanFaith.SFT1.Sprint.processCompilerRoot
    {json.dumps(source.qualified_name, ensure_ascii=False)} binding {spec.operation_mask}
    #[{orbit_literals}] {spec.maximum_depth}
"""


def build_typed_descriptor_batch_request(
    sources: Sequence[CompilerAuditSource],
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    context_id: str,
    timeout_seconds: float,
    run_id: str,
) -> PreparedTypedHookBatchRequest:
    """Build one shared-preamble descriptor request with one action per local root."""

    exact_sources, bindings, compile_context, remainder = _typed_batch_context(sources, settings)
    actions = tuple(
        _typed_descriptor_action(source, binding, spec)
        for source, binding in zip(exact_sources, bindings, strict=True)
    )
    code = _closed_expr_command(
        compile_context,
        _typed_batch_session_source(exact_sources, remainder, actions),
    )
    request_identity = {
        "schema_version": TYPED_HOOK_SCHEMA_VERSION,
        "phase": "descriptor_batch",
        "run_id": run_id,
        "ordered_source_bindings": list(bindings),
        "spec": spec.semantic_payload(),
        "compile_context_id": compile_context.compile_context_id,
        "engine_source_sha256": hash_file(settings.engine_path),
    }
    root_ids = [source.root_id for source in exact_sources]
    request = LeanRequest(
        request_id=f"sft1-wave5-typed-descriptor-batch:{hash_canonical(request_identity)}",
        context_id=context_id,
        code=code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "typed_hook_schema_version": TYPED_HOOK_SCHEMA_VERSION,
            "typed_hook_phase": "descriptor_batch",
            "typed_hook_root_ids": json.dumps(root_ids),
            "typed_hook_qualified_names": json.dumps(
                [s.qualified_name for s in exact_sources], ensure_ascii=False
            ),
            "typed_hook_source_bindings": json.dumps(
                bindings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "typed_hook_spec": json.dumps(
                spec.semantic_payload(), sort_keys=True, separators=(",", ":")
            ),
            "typed_hook_ordered_roots_hash": hash_canonical(root_ids),
        },
    )
    return PreparedTypedHookBatchRequest(
        sources=exact_sources,
        phase="descriptor",
        request=request,
        source_bindings=bindings,
    )


def _validate_typed_selection(
    selection: CompilerTypedWave4Selection,
    spec: CompilerTypedHookSpec,
) -> CompilerTypedWave4Selection:
    if selection.operation_id not in spec.orbit_operations:
        raise CompilerReplayError("selected Wave 4 operation is outside the typed hook spec")
    indices = tuple(selection.selected_indices)
    if not indices or len(indices) > spec.maximum_variants_per_orbit:
        raise CompilerReplayError("selected Wave 4 descriptor count is outside its bound")
    if len(indices) != len(set(indices)) or any(
        type(index) is not int or index < 0 for index in indices
    ):
        raise CompilerReplayError("selected Wave 4 descriptor indices must be unique naturals")
    return CompilerTypedWave4Selection(
        source=selection.source,
        operation_id=selection.operation_id,
        selected_indices=indices,
    )


def _typed_selected_action(
    selection: CompilerTypedWave4Selection,
    binding: Mapping[str, str],
    *,
    spec: CompilerTypedHookSpec,
    render_scope_id: str,
    endpoint_prefix: str | None,
) -> str:
    source = selection.source
    index_literals = ", ".join(str(index) for index in selection.selected_indices)
    lines = [
        "set_option Elab.async false in",
        "run_meta do",
        "  let binding : LeanFaith.SFT1.Sprint.CompilerSourceBinding := "
        + _lean_compiler_binding(binding).replace("\n", "\n  "),
        f"  let indices : Array Nat := #[{index_literals}]",
        "  let orbits ← LeanFaith.SFT1.Sprint.rebuildSelectedCompilerWave4Orbits",
        f"    {json.dumps(source.qualified_name, ensure_ascii=False)} binding",
        f"    {json.dumps(selection.operation_id)} {spec.maximum_depth} indices",
        "  LeanFaith.SFT1.Sprint.emitSelectedCompilerWave4Report",
        f"    {json.dumps(source.qualified_name, ensure_ascii=False)} binding",
        f"    {json.dumps(selection.operation_id)} {spec.maximum_depth} indices orbits",
    ]
    endpoint_fields = {
        "p": "p",
        "c": "c",
        "p_prime": "pPrime",
        "c_prime": "cPrime",
    }
    for slot in range(len(selection.selected_indices)):
        lines.append(
            f"  let some orbit{slot} := orbits[{slot}]? | "
            f'throwError "missing compiler Wave 4 orbit at slot {slot}"'
        )
        for endpoint, field in endpoint_fields.items():
            endpoint_id = (
                f"{slot}.{endpoint}"
                if endpoint_prefix is None
                else typed_wave4_endpoint_id(endpoint_prefix, slot, endpoint)
            )
            lines.append(
                "  LeanFaith.GoalV1.emitClosedProp "
                f"{json.dumps(endpoint_id)} {json.dumps(render_scope_id)} "
                f"{json.dumps(ENDPOINT_ORIGIN[endpoint])} orbit{slot}.{field}"
            )
    return "\n".join(lines)


def build_typed_wave4_selected_batch_request(
    selections: Sequence[CompilerTypedWave4Selection],
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    render_scope_id: str,
    context_id: str,
    timeout_seconds: float,
    run_id: str,
) -> PreparedTypedHookBatchRequest:
    """Build one exact-context selected-certificate request for several roots."""

    exact_selections = tuple(_validate_typed_selection(item, spec) for item in selections)
    if not render_scope_id:
        raise CompilerReplayError("typed Wave 4 render scope must be nonempty")
    exact_sources, bindings, compile_context, remainder = _typed_batch_context(
        [selection.source for selection in exact_selections], settings
    )
    actions = tuple(
        _typed_selected_action(
            selection,
            binding,
            spec=spec,
            render_scope_id=render_scope_id,
            endpoint_prefix=selection.source.root_id,
        )
        for selection, binding in zip(exact_selections, bindings, strict=True)
    )
    code = _closed_expr_command(
        compile_context,
        _typed_batch_session_source(exact_sources, remainder, actions),
    )
    selections_payload = [
        {
            "source_binding": binding,
            "operation_id": selection.operation_id,
            "selected_indices": list(selection.selected_indices),
        }
        for selection, binding in zip(exact_selections, bindings, strict=True)
    ]
    request_identity = {
        "schema_version": TYPED_HOOK_SCHEMA_VERSION,
        "phase": "wave4_selected_batch",
        "run_id": run_id,
        "ordered_selections": selections_payload,
        "spec": spec.semantic_payload(),
        "render_scope_id": render_scope_id,
        "compile_context_id": compile_context.compile_context_id,
        "engine_source_sha256": hash_file(settings.engine_path),
    }
    request = LeanRequest(
        request_id=f"sft1-wave5-typed-selected-batch:{hash_canonical(request_identity)}",
        context_id=context_id,
        code=code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "typed_hook_schema_version": TYPED_HOOK_SCHEMA_VERSION,
            "typed_hook_phase": "wave4_selected_batch",
            "typed_hook_root_ids": json.dumps([s.root_id for s in exact_sources]),
            "typed_hook_qualified_names": json.dumps(
                [s.qualified_name for s in exact_sources], ensure_ascii=False
            ),
            "typed_hook_selections": json.dumps(
                selections_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "typed_hook_spec": json.dumps(
                spec.semantic_payload(), sort_keys=True, separators=(",", ":")
            ),
            "typed_hook_render_scope_id": render_scope_id,
        },
    )
    return PreparedTypedHookBatchRequest(
        sources=exact_sources,
        phase="wave4_selected",
        request=request,
        source_bindings=bindings,
        selections=exact_selections,
    )


def build_typed_descriptor_request(
    source: CompilerAuditSource,
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    context_id: str,
    timeout_seconds: float,
    run_id: str,
) -> PreparedTypedHookRequest:
    """Build one request that checks the local proof, runs Wave 3, and describes Wave 4."""

    reason = _preflight_reason(source)
    if reason is not None:
        raise CompilerReplayError(f"typed hook preflight rejected source: {reason}")
    binding = _typed_source_binding(source, settings)
    compile_context, remainder = _typed_compile_context(source, settings)
    orbit_literals = ", ".join(
        json.dumps(operation, ensure_ascii=False) for operation in spec.orbit_operations
    )
    action = f"""
set_option Elab.async false in
run_meta do
  let binding : LeanFaith.SFT1.Sprint.CompilerSourceBinding := {_lean_compiler_binding(binding)}
  LeanFaith.SFT1.Sprint.processCompilerRoot
    {json.dumps(source.qualified_name, ensure_ascii=False)} binding {spec.operation_mask}
    #[{orbit_literals}] {spec.maximum_depth}
"""
    code = _closed_expr_command(compile_context, _typed_session_source(source, remainder, action))
    request_identity = {
        "schema_version": TYPED_HOOK_SCHEMA_VERSION,
        "phase": "descriptor",
        "run_id": run_id,
        "source_binding": binding,
        "spec": spec.semantic_payload(),
        "compile_context_id": compile_context.compile_context_id,
        "engine_source_sha256": hash_file(settings.engine_path),
    }
    request = LeanRequest(
        request_id=f"sft1-wave5-typed-descriptor:{hash_canonical(request_identity)}",
        context_id=context_id,
        code=code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "typed_hook_schema_version": TYPED_HOOK_SCHEMA_VERSION,
            "typed_hook_phase": "descriptor",
            "typed_hook_root_id": source.root_id,
            "typed_hook_source_binding": json.dumps(
                binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "typed_hook_spec": json.dumps(
                spec.semantic_payload(), sort_keys=True, separators=(",", ":")
            ),
        },
    )
    return PreparedTypedHookRequest(
        source=source,
        phase="descriptor",
        request=request,
        source_binding=binding,
    )


def build_typed_wave4_selected_request(
    source: CompilerAuditSource,
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    operation_id: str,
    selected_indices: Sequence[int],
    render_scope_id: str,
    context_id: str,
    timeout_seconds: float,
    run_id: str,
) -> PreparedTypedHookRequest:
    """Rebuild, fully certify, and frozen-render only selected Wave 4 descriptors."""

    if operation_id not in spec.orbit_operations:
        raise CompilerReplayError("selected Wave 4 operation is outside the typed hook spec")
    indices = tuple(selected_indices)
    if not indices or len(indices) > spec.maximum_variants_per_orbit:
        raise CompilerReplayError("selected Wave 4 descriptor count is outside its bound")
    if len(indices) != len(set(indices)) or any(
        type(index) is not int or index < 0 for index in indices
    ):
        raise CompilerReplayError("selected Wave 4 descriptor indices must be unique naturals")
    reason = _preflight_reason(source)
    if reason is not None:
        raise CompilerReplayError(f"typed hook preflight rejected source: {reason}")
    binding = _typed_source_binding(source, settings)
    compile_context, remainder = _typed_compile_context(source, settings)
    index_literals = ", ".join(str(index) for index in indices)
    lines = [
        "set_option Elab.async false in",
        "run_meta do",
        "  let binding : LeanFaith.SFT1.Sprint.CompilerSourceBinding := "
        + _lean_compiler_binding(binding).replace("\n", "\n  "),
        f"  let indices : Array Nat := #[{index_literals}]",
        "  let orbits ← LeanFaith.SFT1.Sprint.rebuildSelectedCompilerWave4Orbits",
        f"    {json.dumps(source.qualified_name, ensure_ascii=False)} binding",
        f"    {json.dumps(operation_id)} {spec.maximum_depth} indices",
        "  LeanFaith.SFT1.Sprint.emitSelectedCompilerWave4Report",
        f"    {json.dumps(source.qualified_name, ensure_ascii=False)} binding",
        f"    {json.dumps(operation_id)} {spec.maximum_depth} indices orbits",
    ]
    endpoint_fields = {
        "p": ("reference", "p"),
        "c": ("candidate", "c"),
        "p_prime": ("reference", "pPrime"),
        "c_prime": ("candidate", "cPrime"),
    }
    for slot in range(len(indices)):
        lines.append(
            f"  let some orbit{slot} := orbits[{slot}]? | "
            f'throwError "missing compiler Wave 4 orbit at slot {slot}"'
        )
        for endpoint, (_role, field) in endpoint_fields.items():
            origin = "loaded_constant_type" if endpoint == "p" else "sft1_transformed_expr"
            lines.append(
                "  LeanFaith.GoalV1.emitClosedProp "
                f"{json.dumps(f'{slot}.{endpoint}')} {json.dumps(render_scope_id)} "
                f"{json.dumps(origin)} orbit{slot}.{field}"
            )
    code = _closed_expr_command(
        compile_context,
        _typed_session_source(source, remainder, "\n".join(lines)),
    )
    request_identity = {
        "schema_version": TYPED_HOOK_SCHEMA_VERSION,
        "phase": "wave4_selected",
        "run_id": run_id,
        "source_binding": binding,
        "spec": spec.semantic_payload(),
        "operation_id": operation_id,
        "selected_indices": list(indices),
        "render_scope_id": render_scope_id,
        "compile_context_id": compile_context.compile_context_id,
        "engine_source_sha256": hash_file(settings.engine_path),
    }
    request = LeanRequest(
        request_id=f"sft1-wave5-typed-selected:{hash_canonical(request_identity)}",
        context_id=context_id,
        code=code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "typed_hook_schema_version": TYPED_HOOK_SCHEMA_VERSION,
            "typed_hook_phase": "wave4_selected",
            "typed_hook_root_id": source.root_id,
            "typed_hook_operation_id": operation_id,
            "typed_hook_selected_indices": json.dumps(indices, separators=(",", ":")),
            "typed_hook_source_binding": json.dumps(
                binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        },
    )
    return PreparedTypedHookRequest(
        source=source,
        phase="wave4_selected",
        request=request,
        source_binding=binding,
        operation_id=operation_id,
        selected_indices=indices,
    )


def parse_typed_descriptor_payloads(
    source: CompilerAuditSource,
    spec: CompilerTypedHookSpec,
    messages: Sequence[Mapping[str, object]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Extract one compiler root and at most one descriptor terminal per orbit."""

    payloads = parse_evidence_lines(messages)
    roots = [
        payload
        for payload in payloads
        if payload.get("kind") == "compiler_root" and payload.get("root") == source.qualified_name
    ]
    if len(roots) != 1:
        raise CompilerReplayError("typed descriptor request lacks one compiler_root payload")
    root = roots[0]
    binding = root.get("compiler_source_binding")
    if not isinstance(binding, dict) or binding.get("root_id") != source.root_id:
        raise CompilerReplayError("typed compiler root changes its source binding")
    descriptors: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        operation = payload.get("operation_id")
        if (
            payload.get("kind") != "wave4_descriptor_root"
            or payload.get("root") != source.qualified_name
            or operation not in spec.orbit_operations
        ):
            continue
        if not isinstance(operation, str) or operation in descriptors:
            raise CompilerReplayError("typed descriptor request repeats an orbit payload")
        payload_binding = payload.get("compiler_source_binding")
        if (
            not isinstance(payload_binding, dict)
            or payload_binding.get("root_id") != source.root_id
        ):
            raise CompilerReplayError("typed Wave 4 descriptor changes its source binding")
        descriptors[operation] = payload
    if set(descriptors) != set(spec.orbit_operations):
        raise CompilerReplayError("typed descriptor request omitted an orbit terminal")
    return root, descriptors


def _require_exact_compiler_binding(
    payload: Mapping[str, Any],
    expected: Mapping[str, str],
    *,
    context: str,
) -> None:
    observed = payload.get("compiler_source_binding")
    if not isinstance(observed, dict) or observed != expected:
        raise CompilerReplayError(f"{context} changes its exact compiler source binding")


def _parse_typed_evidence(
    messages: Sequence[Mapping[str, object]],
) -> list[dict[str, Any]]:
    try:
        return parse_evidence_lines(messages)
    except SprintEngineError as exc:
        raise CompilerReplayError(f"typed hook emitted malformed engine evidence: {exc}") from exc


def parse_typed_descriptor_batch_payloads(
    sources: Sequence[CompilerAuditSource],
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    messages: Sequence[Mapping[str, object]],
) -> dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    """Parse a descriptor batch without allowing cross-root evidence attachment."""

    exact_sources, bindings, _compile_context, _remainder = _typed_batch_context(sources, settings)
    by_name = {source.qualified_name: source for source in exact_sources}
    expected_bindings = {
        source.root_id: _compiler_binding_json(binding)
        for source, binding in zip(exact_sources, bindings, strict=True)
    }
    roots: dict[str, dict[str, Any]] = {}
    descriptors: dict[str, dict[str, dict[str, Any]]] = {
        source.root_id: {} for source in exact_sources
    }
    for payload in _parse_typed_evidence(messages):
        kind = payload.get("kind")
        if kind not in {"compiler_root", "wave4_descriptor_root"}:
            continue
        name = payload.get("root")
        if not isinstance(name, str) or name not in by_name:
            raise CompilerReplayError("typed descriptor batch emitted an unexpected root")
        source = by_name[name]
        expected_binding = expected_bindings[source.root_id]
        _require_exact_compiler_binding(
            payload,
            expected_binding,
            context=f"typed descriptor payload for {source.root_id}",
        )
        if kind == "compiler_root":
            if source.root_id in roots:
                raise CompilerReplayError("typed descriptor batch repeats a compiler root")
            roots[source.root_id] = payload
            continue
        operation = payload.get("operation_id")
        if not isinstance(operation, str) or operation not in spec.orbit_operations:
            raise CompilerReplayError("typed descriptor batch emitted an unexpected operation")
        root_descriptors = descriptors[source.root_id]
        if operation in root_descriptors:
            raise CompilerReplayError("typed descriptor batch repeats an orbit payload")
        root_descriptors[operation] = payload

    result: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for source in exact_sources:
        root = roots.get(source.root_id)
        if root is None:
            raise CompilerReplayError("typed descriptor batch omitted a compiler root")
        root_descriptors = descriptors[source.root_id]
        expected_operations = (
            set(spec.orbit_operations) if root.get("root_status") == "ok" else set()
        )
        if set(root_descriptors) != expected_operations:
            raise CompilerReplayError(
                "typed descriptor batch omitted or spuriously emitted an orbit terminal"
            )
        if root.get("root_status") == "ok":
            _require_checked_source_proof(root)
            for descriptor in root_descriptors.values():
                _require_checked_source_proof(descriptor)
                if descriptor.get("source_proof_check") != root.get(
                    "source_proof_check"
                ) or descriptor.get("engine_semantic_version") != root.get(
                    "engine_semantic_version"
                ):
                    raise CompilerReplayError(
                        "typed descriptor terminal changes its root proof or engine identity"
                    )
        result[source.root_id] = (root, root_descriptors)
    return result


def _require_checked_source_proof(payload: Mapping[str, Any]) -> None:
    check = payload.get("source_proof_check")
    expected_fields = {
        "meta_checked",
        "kernel_checked",
        "kernel_level_instantiation",
        "proof_expr_hash_u64",
    }
    if not isinstance(check, dict) or set(check) != expected_fields:
        raise CompilerReplayError("typed selected payload lacks an exact source proof check")
    if check.get("meta_checked") is not True or check.get("kernel_checked") is not True:
        raise CompilerReplayError("typed selected source proof was not fully checked")
    if check.get("kernel_level_instantiation") not in {"none", "all_zero"}:
        raise CompilerReplayError("typed selected source proof has an unknown kernel level policy")
    proof_hash = check.get("proof_expr_hash_u64")
    if not isinstance(proof_hash, str) or not proof_hash.isdigit():
        raise CompilerReplayError("typed selected source proof hash is malformed")


def _messages_for_endpoint_prefix(
    messages: Sequence[dict[str, object]],
    endpoint_prefix: str,
) -> tuple[dict[str, object], ...]:
    """Retain this root's closed-Expr lines while preserving all malformed lines."""

    marker = "LFGOALV1EXPRJSON "
    selected: list[dict[str, object]] = []
    for message in messages:
        retained_lines: list[str] = []
        for line in str(message.get("data", "")).splitlines():
            position = line.find(marker)
            if position < 0:
                continue
            try:
                value = json.loads(line[position + len(marker) :])
            except json.JSONDecodeError:
                retained_lines.append(line)
                continue
            endpoint_id = value.get("endpoint_id") if isinstance(value, dict) else None
            if not isinstance(endpoint_id, str) or endpoint_id.startswith(endpoint_prefix + "."):
                retained_lines.append(line)
        if retained_lines:
            selected.append(
                {"severity": message.get("severity"), "data": "\n".join(retained_lines)}
            )
    return tuple(selected)


def validate_typed_wave4_selected_result(
    source: CompilerAuditSource,
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    operation_id: str,
    descriptor_payload: Mapping[str, Any],
    selected_descriptors: Sequence[Wave4VariantDescriptor],
    render_scope_id: str,
    policy: OrbitPolicy,
    result: LeanResult,
    endpoint_prefix: str | None = None,
    batch_expected_endpoint_ids: set[str] | None = None,
    _parsed_endpoint_payloads: Mapping[str, dict[str, object]] | None = None,
) -> tuple[dict[str, Any], ValidatedWave4Root, tuple[dict[str, Any], ...]]:
    """Validate one compiler root's selected closure and frozen GoalV1 payloads.

    ``batch_expected_endpoint_ids`` should be supplied by multi-root callers.  It
    makes unexpected or missing closed-Expr output anywhere in the shared request
    an atomic failure.  ``endpoint_prefix=None`` preserves the legacy single-root
    endpoint IDs.
    """

    if (
        result.status != LeanStatus.VALID
        or result.sorries
        or _messages_report_sorry(result.messages)
    ):
        raise CompilerReplayError("typed selected Lean request was not VALID without sorry")
    if operation_id not in spec.orbit_operations:
        raise CompilerReplayError("selected Wave 4 operation is outside the typed hook spec")
    chosen = tuple(selected_descriptors)
    if not chosen or len(chosen) > spec.maximum_variants_per_orbit:
        raise CompilerReplayError("selected Wave 4 descriptor count is outside its bound")
    indices = [descriptor.index for descriptor in chosen]
    if len(indices) != len(set(indices)) or any(index < 0 for index in indices):
        raise CompilerReplayError("selected Wave 4 descriptors repeat an invalid index")
    if not render_scope_id:
        raise CompilerReplayError("typed Wave 4 render scope must be nonempty")
    if endpoint_prefix is not None and endpoint_prefix != source.root_id:
        raise CompilerReplayError("typed Wave 4 endpoint prefix is detached from the root ID")

    reason = _preflight_reason(source)
    if reason is not None:
        raise CompilerReplayError(f"typed hook preflight rejected source: {reason}")
    expected_binding = _compiler_binding_json(_typed_source_binding(source, settings))
    _require_exact_compiler_binding(
        descriptor_payload, expected_binding, context="typed Wave 4 descriptor"
    )
    if (
        descriptor_payload.get("root") != source.qualified_name
        or descriptor_payload.get("operation_id") != operation_id
    ):
        raise CompilerReplayError("typed Wave 4 descriptor changes its root or operation")

    reports = [
        payload
        for payload in _parse_typed_evidence(result.messages)
        if payload.get("kind") == "wave4_selected_root"
    ]
    if (
        len(reports) != 1
        or reports[0].get("root") != source.qualified_name
        or reports[0].get("operation_id") != operation_id
    ):
        raise CompilerReplayError("typed selected request lacks one exact root report")
    selected_payload = reports[0]
    _require_exact_compiler_binding(
        selected_payload, expected_binding, context="typed Wave 4 selected report"
    )
    _require_checked_source_proof(selected_payload)

    try:
        combined = combine_wave4_selected_payload(
            descriptor_payload,
            selected_payload,
            expected_indices=indices,
        )
        validated = validate_wave4_root_payload(
            combined,
            operation_id=operation_id,
            policy=policy,
            maximum_depth=spec.maximum_depth,
            expected_root=source.qualified_name,
            selected_descriptors=chosen,
            selection_root_id=source.root_id,
        )
        selected = select_wave4_variants(validated, policy)
    except (OrbitError, KeyError, TypeError) as exc:
        raise CompilerReplayError(f"typed Wave 4 certificate validation failed: {exc}") from exc
    if [variant.index for variant in selected] != indices:
        raise CompilerReplayError("typed Wave 4 certificate order differs from preselection")
    combined["enumeration_hash"] = validated.enumeration_hash
    combined["compiler_source_binding"] = expected_binding
    combined["source_proof_check"] = selected_payload["source_proof_check"]

    endpoint_ids = {
        (
            f"{slot}.{endpoint}"
            if endpoint_prefix is None
            else typed_wave4_endpoint_id(endpoint_prefix, slot, endpoint)
        )
        for slot in range(len(chosen))
        for endpoint in _TYPED_ENDPOINTS
    }
    if _parsed_endpoint_payloads is not None:
        if batch_expected_endpoint_ids is None:
            raise CompilerReplayError("preparsed endpoints require an exact batch endpoint set")
        if set(_parsed_endpoint_payloads) != batch_expected_endpoint_ids:
            raise CompilerReplayError("preparsed typed Wave 4 endpoint set is incomplete")
        parsed = dict(_parsed_endpoint_payloads)
        issues: tuple[str, ...] = ()
    elif batch_expected_endpoint_ids is not None:
        if not endpoint_ids <= batch_expected_endpoint_ids:
            raise CompilerReplayError("typed Wave 4 batch endpoint set omits this root")
        parsed, issues = _parse_closed_expr_payloads(result.messages, batch_expected_endpoint_ids)
        if set(parsed) != batch_expected_endpoint_ids:
            issues = (*issues, "typed Wave 4 batch omitted a frozen endpoint")
    else:
        endpoint_messages = (
            result.messages
            if endpoint_prefix is None
            else _messages_for_endpoint_prefix(result.messages, endpoint_prefix)
        )
        parsed, issues = _parse_closed_expr_payloads(endpoint_messages, endpoint_ids)
        if set(parsed) != endpoint_ids:
            issues = (*issues, "typed Wave 4 request omitted a frozen endpoint")
    if issues:
        raise CompilerReplayError("typed Wave 4 frozen render failed: " + "; ".join(issues))

    compile_context, _remainder = _typed_compile_context(source, settings)
    implementation_identity = _implementation_identity()
    selected_records: list[dict[str, Any]] = []
    for slot, variant in enumerate(selected):
        goals = variant.raw.get("goals")
        if not isinstance(goals, dict):
            raise CompilerReplayError("typed Wave 4 variant goals must be a mapping")
        endpoints: dict[str, Any] = {}
        for endpoint in _TYPED_ENDPOINTS:
            endpoint_id = (
                f"{slot}.{endpoint}"
                if endpoint_prefix is None
                else typed_wave4_endpoint_id(endpoint_prefix, slot, endpoint)
            )
            if endpoint == "p":
                material = ClosedExprSourceMaterial(
                    kind="raw_statement", raw_statement=source.theorem
                )
            else:
                material = ClosedExprSourceMaterial(
                    kind="constructed_expr_no_source_text",
                    absence_reason=(
                        f"Wave 4 endpoint {endpoint} constructed from compiler root "
                        f"{source.root_id} by a checked orbit"
                    ),
                )
            item = ClosedExprInput(
                endpoint_id=endpoint_id,
                endpoint_role=cast(Any, ENDPOINT_ROLE[endpoint]),
                expr_origin=cast(Any, ENDPOINT_ORIGIN[endpoint]),
                source_material=material,
            )
            try:
                sidecar = _closed_expr_sidecar_from_payload(
                    payload=parsed[endpoint_id],
                    item=item,
                    compile_context=compile_context,
                    render_scope_id=render_scope_id,
                    implementation_identity=implementation_identity,
                )
            except (GoalV1Error, ValueError) as exc:
                raise CompilerReplayError(
                    f"typed Wave 4 endpoint {endpoint_id} failed GoalV1 validation: {exc}"
                ) from exc
            expected_goal, violation = canonical_surface(str(goals.get(endpoint)))
            if expected_goal is None or sidecar.core_text() != expected_goal:
                raise CompilerReplayError(
                    f"typed Wave 4 render mismatch at {endpoint_id}: {violation or 'text'}"
                )
            endpoints[endpoint] = {
                "record": sidecar.record.to_dict(),
                "source_material": sidecar.source_material.to_dict(),
            }
        selected_records.append(
            {
                "index": variant.index,
                "selection_hash": variant.selection_hash,
                "content_hash": variant.content_hash,
                "reference_chain_hash": variant.reference_chain_hash,
                "candidate_chain_hash": variant.candidate_chain_hash,
                "reference_site_hash": variant.reference_site_hash,
                "candidate_site_hash": variant.candidate_site_hash,
                "variant": variant.raw,
                "render": endpoints,
            }
        )
    return combined, validated, tuple(selected_records)


def validate_typed_wave4_selected_batch_result(
    selections: Sequence[CompilerTypedWave4Selection],
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    descriptor_payloads: Mapping[str, Mapping[str, Any]],
    selected_descriptors: Mapping[str, Sequence[Wave4VariantDescriptor]],
    render_scope_id: str,
    policy: OrbitPolicy,
    result: LeanResult,
) -> dict[
    str,
    tuple[dict[str, Any], ValidatedWave4Root, tuple[dict[str, Any], ...]],
]:
    """Atomically demultiplex and validate one multi-root selected Lean result."""

    exact_selections = tuple(_validate_typed_selection(item, spec) for item in selections)
    exact_sources, _bindings, _context, _remainder = _typed_batch_context(
        [selection.source for selection in exact_selections], settings
    )
    root_ids = [source.root_id for source in exact_sources]
    expected_root_ids = set(root_ids)
    if set(descriptor_payloads) != expected_root_ids:
        raise CompilerReplayError("typed Wave 4 batch descriptor roots differ from selections")
    if set(selected_descriptors) != expected_root_ids:
        raise CompilerReplayError("typed Wave 4 batch selected roots differ from selections")
    for selection in exact_selections:
        if (
            tuple(descriptor.index for descriptor in selected_descriptors[selection.source.root_id])
            != selection.selected_indices
        ):
            raise CompilerReplayError(
                "typed Wave 4 batch descriptors differ from the requested selection"
            )
    if (
        result.status != LeanStatus.VALID
        or result.sorries
        or _messages_report_sorry(result.messages)
    ):
        raise CompilerReplayError("typed selected Lean batch was not VALID without sorry")

    expected_reports = {
        (selection.source.qualified_name, selection.operation_id) for selection in exact_selections
    }
    observed_reports: list[tuple[object, object]] = []
    for payload in _parse_typed_evidence(result.messages):
        if payload.get("kind") != "wave4_selected_root":
            continue
        identity = (payload.get("root"), payload.get("operation_id"))
        if identity not in expected_reports:
            raise CompilerReplayError("typed selected batch emitted an unexpected root report")
        observed_reports.append(identity)
    if len(observed_reports) != len(expected_reports) or set(observed_reports) != expected_reports:
        raise CompilerReplayError("typed selected batch omitted or repeated a root report")

    endpoint_ids = {
        typed_wave4_endpoint_id(selection.source.root_id, slot, endpoint)
        for selection in exact_selections
        for slot in range(len(selection.selected_indices))
        for endpoint in _TYPED_ENDPOINTS
    }
    parsed, issues = _parse_closed_expr_payloads(result.messages, endpoint_ids)
    if issues or set(parsed) != endpoint_ids:
        detail = "; ".join((*issues, "typed selected batch endpoint set is incomplete"))
        raise CompilerReplayError(f"typed Wave 4 batch frozen render failed: {detail}")

    validated: dict[
        str,
        tuple[dict[str, Any], ValidatedWave4Root, tuple[dict[str, Any], ...]],
    ] = {}
    for selection in exact_selections:
        root_id = selection.source.root_id
        validated[root_id] = validate_typed_wave4_selected_result(
            selection.source,
            settings=settings,
            spec=spec,
            operation_id=selection.operation_id,
            descriptor_payload=descriptor_payloads[root_id],
            selected_descriptors=selected_descriptors[root_id],
            render_scope_id=render_scope_id,
            policy=policy,
            result=result,
            endpoint_prefix=root_id,
            batch_expected_endpoint_ids=endpoint_ids,
            _parsed_endpoint_payloads=parsed,
        )
    return validated


def _audit_meta_block(source: CompilerAuditSource) -> str:
    if source.qualified_name is None:
        raise CompilerReplayError("cannot build Lean audit for an unresolved qualified name")
    root_literal = json.dumps(source.root_id, ensure_ascii=False)
    requested_literal = json.dumps(source.qualified_name, ensure_ascii=False)
    name_expression = _lean_name_expression(source.qualified_name)
    return f"""
set_option Elab.async false in
run_meta do
  let rootId := {root_literal}
  let requestedName := {requested_literal}
  let lookup : Name := {name_expression}
  let emit (status taxonomy detail : String) (extra : List (Prod String Json)) : MetaM Unit := do
    let payload := Json.mkObj ([
      ("schema_version", Json.str "{AUDIT_SCHEMA_VERSION}"),
      ("checker_version", Json.str "{CHECKER_VERSION}"),
      ("root_id", Json.str rootId),
      ("requested_qualified_name", Json.str requestedName),
      ("resolved_qualified_name", Json.str lookup.toString),
      ("status", Json.str status),
      ("taxonomy", Json.str taxonomy),
      ("detail", Json.str detail)
    ] ++ extra)
    IO.println s!"{EVIDENCE_MARKER}{{payload.compress}}"
  let env ← getEnv
  match env.find? lookup with
  | none =>
      emit "incompatible" "qualified_constant_not_found"
        "local theorem name did not resolve" []
  | some (.thmInfo info) =>
      let theoremType ← instantiateMVars info.type
      let proofValue ← instantiateMVars info.value
      if theoremType.hasExprMVar || theoremType.hasLevelMVar || theoremType.hasFVar ||
          theoremType.hasLooseBVars || proofValue.hasExprMVar || proofValue.hasLevelMVar ||
          proofValue.hasFVar || proofValue.hasLooseBVars then
        emit "incompatible" "open_theorem_or_proof" "the local theorem or proof is not closed" []
      else if proofValue.hasSorry then
        emit "incompatible" "source_proof_contains_sorry"
          "the local theorem proof contains sorry" []
      else
        let metaOk ← tryCatchRuntimeEx (do check proofValue; pure true) fun ex => do
          if ex.isInterrupt then throw ex
          pure false
        if !metaOk then
          emit "incompatible" "source_proof_meta_check_failed" "Meta.check rejected the proof" []
        else
          let actualType ← inferType proofValue
          if !(← withoutModifyingMCtx (isDefEq actualType theoremType)) then
            emit "incompatible" "source_proof_type_mismatch"
              "proof type differs from theorem type" []
          else if !(← isProp theoremType) then
            emit "incompatible" "source_type_not_prop" "local theorem type is not Prop" []
          else
            let levels := info.levelParams.map fun _ => Level.zero
            let proof0 := proofValue.instantiateLevelParams info.levelParams levels
            let type0 := theoremType.instantiateLevelParams info.levelParams levels
            match Kernel.check env {{}} proof0 with
            | .error _ =>
                emit "incompatible" "source_proof_kernel_check_failed"
                  "Kernel.check rejected the proof" []
            | .ok kernelType =>
                match Kernel.isDefEq env {{}} kernelType type0 with
                | .ok true =>
                    emit "compatible" "verified_local_theorem_proof"
                      "exact source proof and proposition checked" [
                      ("constant_kind", Json.str "theorem"),
                      ("type_expr_hash_u64", Json.str (toString (hash theoremType))),
                      ("proof_expr_hash_u64", Json.str (toString (hash proofValue))),
                      ("level_params", Json.arr
                        (info.levelParams.toArray.map fun n => Json.str n.toString)),
                      ("kernel_level_instantiation", Json.str
                        (if info.levelParams.isEmpty then "none" else "all_zero")),
                      ("meta_checked", Json.bool true),
                      ("kernel_checked", Json.bool true),
                      ("source_proof_type_matches", Json.bool true),
                      ("closed_prop", Json.bool true),
                      ("environment_origin", Json.str "current_compilation_unit")
                    ]
                | _ =>
                    emit "incompatible" "source_proof_kernel_type_mismatch"
                      "kernel proof type differs" []
  | some _ =>
      emit "incompatible" "resolved_constant_not_theorem"
        "resolved declaration is not a theorem" []
""".strip()


def build_context_request(
    roots: Sequence[CompilerAuditSource],
    *,
    context_id: str,
    timeout_seconds: float,
    run_id: str,
) -> PreparedAuditRequest:
    """Build one exact-context request containing independent local theorems."""

    if not roots:
        raise ValueError("a compiler audit request needs at least one root")
    context = roots[0].context_prefix
    fingerprint = roots[0].context_fingerprint
    if any(root.context_prefix != context for root in roots):
        raise CompilerReplayError("one context request mixed different source prefixes")
    if any(root.context_fingerprint != fingerprint for root in roots):
        raise CompilerReplayError("one context request mixed context fingerprints")
    names = [root.qualified_name for root in roots]
    if None in names or len(set(names)) != len(names):
        raise CompilerReplayError("one context request has unresolved or duplicate theorem names")
    declarations = "\n\n".join(root.declaration_source for root in roots)
    checks = "\n\n".join(_audit_meta_block(root) for root in roots)
    code = context + declarations + "\n\n" + checks + "\n"
    root_ids = [root.root_id for root in roots]
    request_key = hash_canonical(
        {
            "run_id": run_id,
            "context_fingerprint": fingerprint,
            "roots": root_ids,
            "full_source_sha256": [
                _mapping(root.inventory_record["hashes"], "hashes")["full_source_sha256"]
                for root in roots
            ],
            "checker_version": CHECKER_VERSION,
        }
    )
    request = LeanRequest(
        request_id=f"sft1-wave5-audit:{request_key}",
        context_id=context_id,
        code=code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "audit_context_fingerprint": fingerprint,
            "audit_root_ids": json.dumps(root_ids, separators=(",", ":")),
            "audit_qualified_names": json.dumps(names, ensure_ascii=False, separators=(",", ":")),
        },
    )
    return PreparedAuditRequest(roots=tuple(roots), request=request)


def parse_audit_payloads(messages: Sequence[Mapping[str, object]]) -> dict[str, dict[str, Any]]:
    """Extract one marker payload per root, rejecting duplicate or malformed evidence."""

    payloads: dict[str, dict[str, Any]] = {}
    for message in messages:
        data = str(message.get("data", ""))
        for line in data.splitlines():
            marker = line.find(EVIDENCE_MARKER)
            if marker < 0:
                continue
            raw = line[marker + len(EVIDENCE_MARKER) :].strip()
            try:
                payload = _mapping(json.loads(raw), "compiler audit payload")
            except (json.JSONDecodeError, CompilerReplayError) as exc:
                raise CompilerReplayError(f"malformed compiler audit payload: {exc}") from exc
            root_id = payload.get("root_id")
            if not isinstance(root_id, str) or root_id in payloads:
                raise CompilerReplayError("compiler audit emitted a missing/duplicate root_id")
            payloads[root_id] = payload
    return payloads


def _terminal_key_payload(
    source: CompilerAuditSource, *, run_id: str, settings: CompilerAuditSettings
) -> dict[str, object]:
    hashes = _mapping(source.inventory_record["hashes"], "inventory hashes")
    context = _mapping(source.inventory_record["context"], "inventory context")
    declaration = _mapping(source.inventory_record["declaration"], "inventory declaration")
    return {
        "cache_version": AUDIT_CACHE_VERSION,
        "run_id": run_id,
        "root_id": source.root_id,
        "inventory_record_sha256": source.inventory_record_sha256,
        "source_row_id": source.inventory_record["source_row_id"],
        "theorem_sha256": hashes["theorem_sha256"],
        "body_sha256": hashes["body_sha256"],
        "full_source_sha256": hashes["full_source_sha256"],
        "context_sha256": context["context_sha256"],
        "context_fingerprint": context["context_fingerprint"],
        "qualified_name": declaration["qualified_name_candidate"],
        "project": settings.inventory.project.to_dict(),
        "checker_version": CHECKER_VERSION,
        "backend_method_version": BACKEND_METHOD_VERSION,
        "engine_source_sha256": hash_file(settings.engine_path),
        "downstream_mode": settings.downstream_mode,
    }


def _cache_key(source: CompilerAuditSource, *, run_id: str, settings: CompilerAuditSettings) -> str:
    return hash_canonical(_terminal_key_payload(source, run_id=run_id, settings=settings))


def _cache_path(settings: CompilerAuditSettings, key: str) -> Path:
    return settings.output_root / "cache" / key[:2] / f"{key}.json"


def _write_exact(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise CompilerReplayError(f"unsafe symlink at immutable audit artifact {path}")
    if path.is_file():
        if path.read_bytes() != data:
            raise CompilerReplayError(f"immutable compiler audit artifact differs: {path}")
        return
    write_atomic(path, data)


def _load_cached_terminal(
    source: CompilerAuditSource, *, run_id: str, settings: CompilerAuditSettings
) -> tuple[dict[str, Any], Path] | None:
    key = _cache_key(source, run_id=run_id, settings=settings)
    path = _cache_path(settings, key)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise CompilerReplayError(f"unsafe audit cache symlink: {path}")
    try:
        record = read_json_object(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CompilerReplayError(f"cannot read immutable audit cache {path}: {exc}") from exc
    if record.get("cache_key") != key:
        raise CompilerReplayError(f"audit cache key differs: {path}")
    if record.get("key_payload") != _terminal_key_payload(source, run_id=run_id, settings=settings):
        raise CompilerReplayError(f"audit cache semantic identity differs: {path}")
    if record.get("status") not in {"compatible", "incompatible"}:
        raise CompilerReplayError(f"audit cache contains a nonterminal status: {path}")
    if record.get("root_id") != source.root_id:
        raise CompilerReplayError(f"audit cache root differs: {path}")
    return record, path


def _terminal_record(
    source: CompilerAuditSource,
    *,
    run_id: str,
    settings: CompilerAuditSettings,
    status: Literal["compatible", "incompatible"],
    taxonomy: str,
    detail: str,
    payload: Mapping[str, Any] | None,
    result: LeanResult | None,
) -> dict[str, Any]:
    key_payload = _terminal_key_payload(source, run_id=run_id, settings=settings)
    key = hash_canonical(key_payload)
    return {
        "cache_version": AUDIT_CACHE_VERSION,
        "cache_key": key,
        "key_payload": key_payload,
        "root_id": source.root_id,
        "status": status,
        "taxonomy": taxonomy,
        "detail": detail[:1000],
        "proof_compatibility": dict(payload) if payload is not None else None,
        "lean": (
            {
                "status": result.status.value,
                "request_hash": result.request_hash,
                "elapsed_ms": result.elapsed_ms,
                "raw_response_path": result.raw_response_path,
                "sorries": len(result.sorries),
            }
            if result is not None
            else {
                "status": "not_called_preflight",
                "request_hash": None,
                "elapsed_ms": 0,
                "raw_response_path": None,
                "sorries": 0,
            }
        ),
        "source_locator": dict(_mapping(source.inventory_record["source"], "source")),
        "source_hashes": dict(_mapping(source.inventory_record["hashes"], "hashes")),
        "qualified_constant": {
            "candidate": source.qualified_name,
            "origin": "current_compilation_unit" if status == "compatible" else None,
            "imported_constant": False,
        },
        "downstream": {
            "mode": settings.downstream_mode,
            "name_based_sprint_runner_compatible": False,
            "reason": (
                "Sprint.loadRoot requires an imported module constant; this checked theorem is "
                "local to its exact reconstructed compilation unit"
            ),
            "required_hook": (
                "inside the same request, pass the verified local theorem type and proof value "
                "directly to the existing Sprint Wave 3/4 typed machinery"
            ),
        },
    }


def _validate_compatible_payload(
    source: CompilerAuditSource, payload: Mapping[str, Any]
) -> tuple[Literal["compatible", "incompatible"], str, str]:
    if payload.get("schema_version") != AUDIT_SCHEMA_VERSION:
        return "incompatible", "audit_payload_schema_mismatch", "payload schema differs"
    if payload.get("checker_version") != CHECKER_VERSION:
        return "incompatible", "audit_payload_checker_mismatch", "payload checker differs"
    if payload.get("root_id") != source.root_id:
        return "incompatible", "audit_payload_root_mismatch", "payload root differs"
    if payload.get("requested_qualified_name") != source.qualified_name:
        return "incompatible", "audit_payload_name_mismatch", "requested name differs"
    if payload.get("resolved_qualified_name") != source.qualified_name:
        return "incompatible", "qualified_constant_resolution_mismatch", "resolved name differs"
    status = payload.get("status")
    taxonomy = str(payload.get("taxonomy", "audit_payload_taxonomy_missing"))
    detail = str(payload.get("detail", taxonomy))
    if status != "compatible":
        return "incompatible", taxonomy, detail
    required_true = (
        "meta_checked",
        "kernel_checked",
        "source_proof_type_matches",
        "closed_prop",
    )
    if any(payload.get(field) is not True for field in required_true):
        return "incompatible", "source_proof_check_incomplete", "proof checks are incomplete"
    if payload.get("constant_kind") != "theorem":
        return "incompatible", "resolved_constant_not_theorem", "constant kind differs"
    if payload.get("environment_origin") != "current_compilation_unit":
        return "incompatible", "source_environment_origin_mismatch", "origin differs"
    return "compatible", taxonomy, detail


def _backend_context_fingerprint(settings: CompilerAuditSettings) -> str:
    return hash_canonical(
        {
            "kind": "sft1_wave5_compiler_audit_environment",
            "project": settings.inventory.project.to_dict(),
            "project_dir": str(settings.project_dir),
            "checker_version": CHECKER_VERSION,
            "backend_method_version": BACKEND_METHOD_VERSION,
            "elab_async": False,
            "isolate_incremental_commands": True,
        }
    )


def _backend_settings(settings: CompilerAuditSettings) -> BackendSettings:
    return BackendSettings(
        project_dir=settings.project_dir,
        context_fingerprint=_backend_context_fingerprint(settings),
        environment_schema_version=1,
        raw_response_dir=settings.output_root / "raw_responses",
        server_mode=ServerMode.POOL if settings.lean_workers > 1 else ServerMode.STABLE,
        workers=settings.lean_workers if settings.lean_workers > 1 else None,
        memory_hard_limit_mb=settings.memory_hard_limit_mb,
        method_version=f"{BACKEND_METHOD_VERSION}:{CHECKER_VERSION}",
        enable_parallel_elaboration=False,
        isolate_incremental_commands=settings.isolate_incremental_commands,
        confirm_invalid_on_fresh_process=False,
    )


def _default_backend_factory(settings: BackendSettings) -> LeanBackend:
    return LeanInteractBackend(settings)


def _verify_project(settings: CompilerAuditSettings) -> None:
    try:
        project_dir = settings.project_dir.resolve(strict=True)
    except OSError as exc:
        raise CompilerReplayError(
            f"compiler audit project directory is unavailable: {exc}"
        ) from exc
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=project_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        completed.returncode != 0
        or completed.stdout.strip() != settings.inventory.project.project_revision
    ):
        raise CompilerReplayError("compiler audit project revision differs from its pin")
    toolchain_path = project_dir / "lean-toolchain"
    try:
        toolchain = toolchain_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CompilerReplayError(f"cannot read compiler audit Lean toolchain: {exc}") from exc
    if toolchain.rsplit(":", maxsplit=1)[-1] != settings.inventory.project.lean_version:
        raise CompilerReplayError("compiler audit Lean toolchain differs from its pin")


@contextmanager
def _resource_claim(
    settings: CompilerAuditSettings, *, owner_session: str, enabled: bool
) -> Iterator[Reservation | None]:
    if not enabled:
        yield None
        return
    try:
        reservation = claim_resources(
            task=settings.resource_task,
            lean_workers=settings.lean_workers,
            lean_rss_gib=settings.lean_rss_claim_gib,
            gpu=False,
            pid=os.getpid(),
            owner_session=owner_session,
            worktree=find_repo_root(settings.config_path),
        )
    except ReservationError as exc:
        raise CompilerReplayInfrastructureError(f"shared Lean capacity unavailable: {exc}") from exc
    try:
        yield reservation
    finally:
        current = [item for item in list_reservations() if item.task == reservation.task]
        if current == [reservation]:
            release_resources(task=reservation.task)


def _descendant_rss_bytes(root_pid: int) -> int:
    parents: dict[int, int] = {}
    rss: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        parent = 0
        resident = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                parent = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                resident = int(line.split()[1]) * 1024
        pid = int(entry.name)
        parents[pid] = parent
        rss[pid] = resident
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(rss.get(pid, 0) for pid in descendants)


class _RssSampler:
    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self.peak = max(self.peak, _descendant_rss_bytes(os.getpid()))

    def __enter__(self) -> _RssSampler:
        self.peak = _descendant_rss_bytes(os.getpid())
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.peak = max(self.peak, _descendant_rss_bytes(os.getpid()))


class CompilerAuditRunner:
    """Resumable audit runner; backend construction is delayed until after cache reads."""

    def __init__(
        self,
        settings: CompilerAuditSettings,
        *,
        backend_factory: BackendFactory = _default_backend_factory,
        owner_session: str = "codex-sft1-wave5-audit",
        manage_resources: bool = True,
        verify_project: bool = True,
    ) -> None:
        self.settings = settings
        self.backend_factory = backend_factory
        self.owner_session = owner_session
        self.manage_resources = manage_resources
        self.verify_project = verify_project
        self.journal = Journal(settings.output_root / "journal.jsonl")
        self.status_path = settings.output_root / "status.json"
        self.run_spec_path = settings.output_root / "run_spec.json"

    def _run_identity(self, sample: Sequence[Mapping[str, Any]]) -> dict[str, object]:
        dependencies: dict[str, str] = {}
        for label, path in (
            ("compiler_replay", Path(__file__).resolve()),
            ("compiler_inventory", Path(inventory_module.__file__).resolve()),
            ("engine", self.settings.engine_path.resolve()),
        ):
            dependencies[label] = hash_file(path)
        return {
            "run_spec_version": AUDIT_RUN_SPEC_VERSION,
            "config_sha256": self.settings.config_sha256,
            "inventory_manifest_sha256": hash_file(self.settings.inventory_manifest_path),
            "inventory_run_spec_sha256": hash_file(
                self.settings.inventory.output_root / "_state" / "run_spec.json"
            ),
            "audit_sample_sha256": hash_file(self.settings.sample_path),
            "audit_sample_receipt_sha256": hash_file(self.settings.sample_receipt_path),
            "sample_rows": len(sample),
            "sample_root_ids_sha256": hash_canonical([str(record["root_id"]) for record in sample]),
            "source_pin": self.settings.inventory.pin.to_dict(),
            "project": self.settings.inventory.project.to_dict(),
            "backend_method_version": BACKEND_METHOD_VERSION,
            "checker_version": CHECKER_VERSION,
            "semantic_dependency_sha256": dependencies,
            "execution": {
                "lean_workers": self.settings.lean_workers,
                "lean_rss_claim_gib": self.settings.lean_rss_claim_gib,
                "memory_hard_limit_mb": self.settings.memory_hard_limit_mb,
                "request_timeout_seconds": self.settings.request_timeout_seconds,
                "context_request_max_roots": self.settings.context_request_max_roots,
                "request_batch_size": self.settings.request_batch_size,
                "retry_max_attempts": self.settings.retry_max_attempts,
                "retry_statuses": sorted(status.value for status in self.settings.retry_statuses),
                "elab_async": False,
                "isolate_incremental_commands": True,
            },
            "downstream_mode": self.settings.downstream_mode,
        }

    def _ensure_run_spec(self, sample: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
        identity = self._run_identity(sample)
        run_id = hash_canonical(identity)
        payload: dict[str, Any] = {"run_id": run_id, **identity}
        _write_exact(self.run_spec_path, canonical_json_bytes(payload) + b"\n")
        return run_id, payload

    def _journal_terminals(self, run_id: str) -> dict[str, dict[str, Any]]:
        terminals: dict[str, dict[str, Any]] = {}
        for record in self.journal.read():
            if record.get("event") != "root_terminal" or record.get("run_id") != run_id:
                continue
            root_id = str(record.get("root_id", ""))
            prior = terminals.get(root_id)
            semantic = {
                key: record.get(key)
                for key in ("root_id", "cache_key", "cache_sha256", "status", "taxonomy")
            }
            if prior is not None:
                previous = {
                    key: prior.get(key)
                    for key in ("root_id", "cache_key", "cache_sha256", "status", "taxonomy")
                }
                if semantic != previous:
                    raise CompilerReplayError(f"conflicting audit journal terminals for {root_id}")
            terminals[root_id] = record
        return terminals

    def _persist_terminal(
        self, source: CompilerAuditSource, terminal: Mapping[str, Any], *, run_id: str
    ) -> tuple[dict[str, Any], Path]:
        key = str(terminal["cache_key"])
        path = _cache_path(self.settings, key)
        data = canonical_json_bytes(terminal) + b"\n"
        _write_exact(path, data)
        existing = self._journal_terminals(run_id).get(source.root_id)
        journal_terminal = {
            "event": "root_terminal",
            "run_id": run_id,
            "root_id": source.root_id,
            "cache_key": key,
            "cache_sha256": hash_file(path),
            "status": terminal["status"],
            "taxonomy": terminal["taxonomy"],
        }
        if existing is None:
            self.journal.append(journal_terminal)
        else:
            comparison = {
                key_name: existing.get(key_name)
                for key_name in journal_terminal
                if key_name != "event"
            }
            expected = {
                key_name: value
                for key_name, value in journal_terminal.items()
                if key_name != "event"
            }
            if comparison != expected:
                raise CompilerReplayError(f"journal/cache disagreement for {source.root_id}")
        return dict(terminal), path

    def _prepare_requests(
        self, roots: Sequence[CompilerAuditSource], *, run_id: str
    ) -> list[PreparedAuditRequest]:
        grouped: dict[tuple[str, str], list[CompilerAuditSource]] = defaultdict(list)
        for root in roots:
            grouped[(root.context_fingerprint, sha256_hex(root.context_prefix.encode()))].append(
                root
            )
        requests: list[PreparedAuditRequest] = []
        context_id = f"ctx:{_backend_context_fingerprint(self.settings)}"
        for group_key in sorted(grouped):
            ordered = sorted(grouped[group_key], key=lambda item: item.root_id)
            # A repeated qualified name is safe only in separate exact-source requests.
            chunks: list[list[CompilerAuditSource]] = []
            current: list[CompilerAuditSource] = []
            current_names: set[str | None] = set()
            for root in ordered:
                if (
                    len(current) >= self.settings.context_request_max_roots
                    or root.qualified_name in current_names
                ):
                    chunks.append(current)
                    current = []
                    current_names = set()
                current.append(root)
                current_names.add(root.qualified_name)
            if current:
                chunks.append(current)
            requests.extend(
                build_context_request(
                    chunk,
                    context_id=context_id,
                    timeout_seconds=self.settings.request_timeout_seconds,
                    run_id=run_id,
                )
                for chunk in chunks
            )
        return requests

    def _single_request(self, root: CompilerAuditSource, *, run_id: str) -> PreparedAuditRequest:
        return build_context_request(
            [root],
            context_id=f"ctx:{_backend_context_fingerprint(self.settings)}",
            timeout_seconds=self.settings.request_timeout_seconds,
            run_id=run_id,
        )

    def _record_batch_attempt(
        self,
        *,
        run_id: str,
        requests: Sequence[LeanRequest],
        results: Sequence[LeanResult],
        wall_seconds: float,
    ) -> None:
        self.journal.append(
            {
                "event": "lean_batch_attempt",
                "run_id": run_id,
                "requests": len(requests),
                "request_ids": [request.request_id for request in requests],
                "request_hashes": [result.request_hash for result in results],
                "statuses": [result.status.value for result in results],
                "lean_elapsed_ms": sum(result.elapsed_ms for result in results),
                "wall_seconds": round(wall_seconds, 6),
            }
        )

    def _execute(
        self,
        roots: Sequence[CompilerAuditSource],
        *,
        run_id: str,
    ) -> tuple[int, int]:
        queue = self._prepare_requests(roots, run_id=run_id)
        backend_settings = _backend_settings(self.settings)
        backend = self.backend_factory(backend_settings)
        lean_requests = 0
        fallback_requests = 0
        policy = RetryPolicy(
            max_attempts=self.settings.retry_max_attempts,
            retry_statuses=self.settings.retry_statuses,
        )

        def run_batch(requests: Sequence[LeanRequest]) -> Sequence[LeanResult]:
            nonlocal lean_requests
            started = time.monotonic()
            results = backend.run_batch(requests)
            lean_requests += len(requests)
            self._record_batch_attempt(
                run_id=run_id,
                requests=requests,
                results=results,
                wall_seconds=time.monotonic() - started,
            )
            return results

        def reset_before_retry(_attempt: int, _pending: tuple[int, ...]) -> None:
            nonlocal backend
            backend.close()
            backend = self.backend_factory(backend_settings)

        try:
            while queue:
                batch = queue[: self.settings.request_batch_size]
                del queue[: self.settings.request_batch_size]
                outcome = run_batch_with_retries(
                    run_batch,
                    [item.request for item in batch],
                    policy,
                    before_retry=reset_before_retry,
                )
                for prepared, result in zip(batch, outcome.results, strict=True):
                    if result.status in _NONRESULT_INFRASTRUCTURE:
                        raise CompilerReplayInfrastructureError(
                            f"compiler audit infrastructure terminal for "
                            f"{prepared.request.request_id}: {result.status.value}"
                        )
                    if result.status != LeanStatus.VALID or result.sorries:
                        if len(prepared.roots) > 1:
                            singles = [
                                self._single_request(root, run_id=run_id) for root in prepared.roots
                            ]
                            fallback_requests += len(singles)
                            queue[0:0] = singles
                            continue
                        source = prepared.roots[0]
                        taxonomy = (
                            "source_contains_sorry_or_valid_with_sorry"
                            if result.status == LeanStatus.VALID_WITH_SORRY or result.sorries
                            else "lean_invalid_source_or_audit"
                        )
                        detail = result.infrastructure_error or "; ".join(
                            str(message.get("data", "")) for message in result.messages
                        )
                        terminal = _terminal_record(
                            source,
                            run_id=run_id,
                            settings=self.settings,
                            status="incompatible",
                            taxonomy=taxonomy,
                            detail=detail or taxonomy,
                            payload=None,
                            result=result,
                        )
                        self._persist_terminal(source, terminal, run_id=run_id)
                        continue
                    try:
                        payloads = parse_audit_payloads(result.messages)
                    except CompilerReplayError:
                        if len(prepared.roots) > 1:
                            singles = [
                                self._single_request(root, run_id=run_id) for root in prepared.roots
                            ]
                            fallback_requests += len(singles)
                            queue[0:0] = singles
                            continue
                        payloads = {}
                    expected_ids = {root.root_id for root in prepared.roots}
                    if set(payloads) - expected_ids:
                        if len(prepared.roots) > 1:
                            singles = [
                                self._single_request(root, run_id=run_id) for root in prepared.roots
                            ]
                            fallback_requests += len(singles)
                            queue[0:0] = singles
                            continue
                        payloads = {}
                    for source in prepared.roots:
                        payload = payloads.get(source.root_id)
                        if payload is None:
                            if len(prepared.roots) > 1:
                                single = self._single_request(source, run_id=run_id)
                                fallback_requests += 1
                                queue.insert(0, single)
                                continue
                            status: Literal["compatible", "incompatible"] = "incompatible"
                            taxonomy = "audit_payload_missing_or_malformed"
                            detail = "valid Lean response lacked one exact audit payload"
                        else:
                            status, taxonomy, detail = _validate_compatible_payload(source, payload)
                        terminal = _terminal_record(
                            source,
                            run_id=run_id,
                            settings=self.settings,
                            status=status,
                            taxonomy=taxonomy,
                            detail=detail,
                            payload=payload,
                            result=result,
                        )
                        self._persist_terminal(source, terminal, run_id=run_id)
        finally:
            backend.close()
        return lean_requests, fallback_requests

    def _attempt_totals(self, run_id: str) -> dict[str, object]:
        lean_requests = 0
        lean_elapsed_ms = 0
        wall_seconds = 0.0
        attempts = 0
        peaks: list[int] = []
        for record in self.journal.read():
            if record.get("run_id") != run_id:
                continue
            if record.get("event") == "lean_batch_attempt":
                attempts += 1
                lean_requests += int(record.get("requests", 0))
                lean_elapsed_ms += int(record.get("lean_elapsed_ms", 0))
                wall_seconds += float(record.get("wall_seconds", 0.0))
            elif record.get("event") == "invocation_complete":
                peaks.append(int(record.get("peak_rss_bytes", 0)))
        return {
            "lean_batch_attempts": attempts,
            "lean_requests": lean_requests,
            "lean_elapsed_ms": lean_elapsed_ms,
            "backend_wall_seconds": round(wall_seconds, 6),
            "peak_rss_bytes": max(peaks, default=0),
        }

    def _validated_cache_state(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> tuple[list[dict[str, str]], Counter[str]]:
        receipts: list[dict[str, str]] = []
        status_counts: Counter[str] = Counter()
        for source in sources:
            cached = _load_cached_terminal(source, run_id=run_id, settings=self.settings)
            if cached is None:
                raise CompilerReplayError(f"audit root lacks terminal cache: {source.root_id}")
            terminal, path = cached
            status_counts[str(terminal["status"])] += 1
            receipts.append(
                {
                    "root_id": source.root_id,
                    "cache_key": str(terminal["cache_key"]),
                    "cache_sha256": hash_file(path),
                }
            )
        receipts.sort(key=lambda item: item["root_id"])
        return receipts, status_counts

    def _validate_complete_marker(
        self,
        complete: Mapping[str, Any],
        sources: Sequence[CompilerAuditSource],
        *,
        run_id: str,
    ) -> None:
        receipts, status_counts = self._validated_cache_state(sources, run_id=run_id)
        expected = {
            "run_id": run_id,
            "run_spec_sha256": hash_file(self.run_spec_path),
            "inventory_manifest_sha256": hash_file(self.settings.inventory_manifest_path),
            "audit_sample_sha256": hash_file(self.settings.sample_path),
            "roots": len(sources),
            "compatible": status_counts["compatible"],
            "incompatible": status_counts["incompatible"],
            "cache_receipts_sha256": hash_canonical(receipts),
        }
        for key, value in expected.items():
            if complete.get(key) != value:
                raise CompilerReplayError(f"completed compiler audit marker differs at {key}")
        expected_status = "passed" if status_counts["compatible"] == len(sources) else "failed"
        if complete.get("status") != expected_status:
            raise CompilerReplayError("completed compiler audit status differs")

    def _complete(
        self,
        *,
        run_id: str,
        sources: Sequence[CompilerAuditSource],
        invocation_cache_hits: int,
        invocation_lean_requests: int,
        fallback_requests: int,
    ) -> dict[str, Any]:
        cache_receipts, counts = self._validated_cache_state(sources, run_id=run_id)
        taxonomy: Counter[str] = Counter()
        for source in sources:
            cached = _load_cached_terminal(source, run_id=run_id, settings=self.settings)
            assert cached is not None
            taxonomy[str(cached[0]["taxonomy"])] += 1
        complete: dict[str, Any] = {
            "artifact_kind": "sft1_wave5_compiler_context_audit_terminal",
            "schema_version": AUDIT_SCHEMA_VERSION,
            "run_id": run_id,
            "run_spec_sha256": hash_file(self.run_spec_path),
            "inventory_manifest_sha256": hash_file(self.settings.inventory_manifest_path),
            "audit_sample_sha256": hash_file(self.settings.sample_path),
            "roots": len(sources),
            "compatible": counts["compatible"],
            "incompatible": counts["incompatible"],
            "status": "passed" if counts["compatible"] == len(sources) else "failed",
            "failure_taxonomy": dict(sorted(taxonomy.items())),
            "cache_receipts_sha256": hash_canonical(cache_receipts),
            "cache_receipts": cache_receipts,
            "execution_totals": self._attempt_totals(run_id),
            "last_invocation": {
                "cache_hits": invocation_cache_hits,
                "lean_requests": invocation_lean_requests,
                "fallback_single_requests": fallback_requests,
            },
            "proof_contract": {
                "exact_prefix_plus_literal_by_plus_body": True,
                "source_label_true": True,
                "qualified_local_theorem_resolved": counts["compatible"] == len(sources),
                "meta_and_kernel_source_proof_checked": counts["compatible"] == len(sources),
            },
            "downstream": {
                "mode": self.settings.downstream_mode,
                "name_based_sprint_runner_compatible": False,
                "verified_roots_may_enter_core": False,
                "required_hook": (
                    "pass each verified local theorem type/proof directly into existing Sprint "
                    "Wave 3/4 logic inside its exact compilation request"
                ),
            },
        }
        data = canonical_json_bytes(complete) + b"\n"
        _write_exact(self.settings.complete_path, data)
        return complete

    def run(self) -> CompilerAuditResult:
        sample = load_audit_sample(self.settings)
        sources = resolve_audit_sources(self.settings, sample)
        run_id, _ = self._ensure_run_spec(sample)
        existing_complete = (
            read_json_object(self.settings.complete_path)
            if self.settings.complete_path.is_file()
            else None
        )
        if existing_complete is not None:
            self._validate_complete_marker(existing_complete, sources, run_id=run_id)
            return CompilerAuditResult(
                run_id=run_id,
                complete_path=self.settings.complete_path,
                status=cast(Literal["passed", "failed"], existing_complete["status"]),
                roots=int(existing_complete["roots"]),
                compatible=int(existing_complete["compatible"]),
                incompatible=int(existing_complete["incompatible"]),
                cache_hits=len(sources),
                lean_requests=0,
            )
        cache_hits = 0
        missing: list[CompilerAuditSource] = []
        for source in sources:
            cached = _load_cached_terminal(source, run_id=run_id, settings=self.settings)
            if cached is not None:
                cache_hits += 1
                continue
            reason = _preflight_reason(source)
            if reason is not None:
                terminal = _terminal_record(
                    source,
                    run_id=run_id,
                    settings=self.settings,
                    status="incompatible",
                    taxonomy=reason,
                    detail="source context cannot be named exactly without guessing",
                    payload=None,
                    result=None,
                )
                self._persist_terminal(source, terminal, run_id=run_id)
            else:
                missing.append(source)
        lean_requests = 0
        fallback_requests = 0
        peak_rss = 0
        if missing:
            if self.verify_project:
                _verify_project(self.settings)
            with _resource_claim(
                self.settings,
                owner_session=self.owner_session,
                enabled=self.manage_resources,
            ):
                with _RssSampler() as sampler:
                    lean_requests, fallback_requests = self._execute(missing, run_id=run_id)
                peak_rss = sampler.peak
            if peak_rss > int(self.settings.lean_rss_claim_gib * 1024**3):
                raise CompilerReplayInfrastructureError(
                    "compiler audit exceeded its measured RSS reservation"
                )
            self.journal.append(
                {
                    "event": "invocation_complete",
                    "run_id": run_id,
                    "cache_hits": cache_hits,
                    "lean_requests": lean_requests,
                    "fallback_single_requests": fallback_requests,
                    "peak_rss_bytes": peak_rss,
                }
            )
        complete = self._complete(
            run_id=run_id,
            sources=sources,
            invocation_cache_hits=cache_hits,
            invocation_lean_requests=lean_requests,
            fallback_requests=fallback_requests,
        )
        write_atomic(self.status_path, canonical_json_bytes(complete) + b"\n")
        return CompilerAuditResult(
            run_id=run_id,
            complete_path=self.settings.complete_path,
            status=cast(Literal["passed", "failed"], complete["status"]),
            roots=int(complete["roots"]),
            compatible=int(complete["compatible"]),
            incompatible=int(complete["incompatible"]),
            cache_hits=cache_hits,
            lean_requests=lean_requests,
        )

    def replay(self) -> dict[str, Any]:
        """Force a zero-backend-call replay of every terminal and immutable marker."""

        sample = load_audit_sample(self.settings)
        sources = resolve_audit_sources(self.settings, sample)
        run_id, _ = self._ensure_run_spec(sample)
        complete = read_json_object(self.settings.complete_path)
        self._validate_complete_marker(complete, sources, run_id=run_id)
        receipts, _ = self._validated_cache_state(sources, run_id=run_id)
        receipt: dict[str, Any] = {
            "artifact_kind": "sft1_wave5_compiler_context_audit_replay",
            "schema_version": AUDIT_REPLAY_VERSION,
            "run_id": run_id,
            "terminal_marker_sha256": hash_file(self.settings.complete_path),
            "roots_verified": len(sources),
            "cache_hits": len(sources),
            "lean_requests": 0,
            "backend_constructed": False,
            "resource_claimed": False,
            "cache_receipts_sha256": hash_canonical(receipts),
            "downstream_mode": self.settings.downstream_mode,
        }
        _write_exact(
            self.settings.output_root / "replay_receipt.json",
            canonical_json_bytes(receipt) + b"\n",
        )
        return receipt


def _result_json(result: CompilerAuditResult) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "complete_path": str(result.complete_path),
        "status": result.status,
        "roots": result.roots,
        "compatible": result.compatible,
        "incompatible": result.incompatible,
        "cache_hits": result.cache_hits,
        "lean_requests": result.lean_requests,
    }


def verify_typed_certificate_gate(
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    *,
    required_sample_rows: int = 1_000,
) -> dict[str, Any]:
    """Compatibility entry point for the additive durable typed gate."""

    from leanfaith.sft1.sprint.compiler_certificate_gate import (
        verify_typed_certificate_gate as verify_gate,
    )

    return verify_gate(
        settings,
        spec,
        policy,
        required_sample_rows=required_sample_rows,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--owner-session", default="codex-sft1-wave5-audit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("replay")
    subparsers.add_parser("status")
    subparsers.add_parser("typed-gate-run")
    subparsers.add_parser("typed-gate-replay")
    subparsers.add_parser("typed-gate-status")
    args = parser.parse_args(argv)
    if args.command.startswith("typed-gate-"):
        from leanfaith.sft1.sprint.compiler_certificate_gate import (
            CompilerTypedCertificateGateRunner,
            _typed_gate_result_json,
            load_typed_certificate_gate_config,
            typed_certificate_gate_complete_path,
            typed_certificate_gate_root,
        )

        settings, spec, policy = load_typed_certificate_gate_config(args.config)
        if args.command == "typed-gate-status":
            complete_path = typed_certificate_gate_complete_path(settings)
            if complete_path.is_file():
                print(
                    json.dumps(
                        verify_typed_certificate_gate(settings, spec, policy),
                        sort_keys=True,
                    )
                )
                return 0
            status_path = typed_certificate_gate_root(settings) / "status.json"
            status = read_json_object(status_path)
            print(json.dumps(status, sort_keys=True))
            return 0 if status.get("status") == "passed" else 2
        typed_runner = CompilerTypedCertificateGateRunner(
            settings,
            spec,
            policy,
            owner_session=args.owner_session,
        )
        if args.command == "typed-gate-replay":
            print(json.dumps(typed_runner.replay(), sort_keys=True))
            return 0
        typed_result = typed_runner.run()
        print(json.dumps(_typed_gate_result_json(typed_result), sort_keys=True))
        return 0 if typed_result.status == "passed" else 2
    settings = load_compiler_audit_config(args.config)
    if args.command == "status":
        path = (
            settings.complete_path
            if settings.complete_path.is_file()
            else settings.output_root / "status.json"
        )
        print(json.dumps(read_json_object(path), sort_keys=True))
        return 0
    runner = CompilerAuditRunner(settings, owner_session=args.owner_session)
    if args.command == "replay":
        print(json.dumps(runner.replay(), sort_keys=True))
        return 0
    result = runner.run()
    print(json.dumps(_result_json(result), sort_keys=True))
    return 0 if result.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

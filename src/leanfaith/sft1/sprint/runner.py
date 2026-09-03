"""Resumable batch runner for the compact SFT1 sprint engine.

Flow per batch of roots: consult the semantic cache, send the remaining roots
to the persistent Mathlib worker in one process request, apply Lean-free
screens, render every surviving pair through the frozen REPR route, then
append one terminal per root/operation to the run journal and the retained
record file.  Completed terminals are never recomputed, so a resumed run adds
zero Lean calls for finished work.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import psutil  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.host_resources import (
    Reservation,
    ReservationError,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations.goal_v1 import (
    ClosedExprSidecar,
    CompileContext,
    GoalV1Error,
    _canonicalize_elaborated_goal,
    validate_goal_v1,
)
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint import engine as engine_module
from leanfaith.sft1.sprint.engine import (
    NEGATIVE_OPERATIONS,
    OPERATIONS,
    POSITIVE_OPERATIONS,
    EngineIdentity,
    ProjectPins,
    SprintSession,
    cacheable_status,
    operation_mask,
    operations_in_mask,
    render_scope_id,
)
from leanfaith.sft1.sprint.inventory import Pool, load_inventory, ordered_roots
from leanfaith.sft1.sprint.provenance import CACHE_SCHEMA_CURRENT, derive_provenance
from leanfaith.sft1.sprint.screens import (
    GoldBlocklist,
    deduplicate,
    instance_dagger_count,
    render_hash,
    residue_violation,
    stable_row_hash,
    unordered_pair_key,
)
from leanfaith.sft1.sprint.shortcut import pairwise_shortcut_diagnostics, run_screens_v3
from leanfaith.sft1.sprint.store import Journal, SemanticCache, read_json_object, write_atomic

DEFAULT_CONFIG = Path("configs/transformations/sft1_value_first_v1/sprint_v1.yaml")
Sha256 = str
NonEmpty = str
TerminalStatus = Literal["retained", "not_applicable", "rejected", "error"]
FIXTURE_ROOT_PREFIX = "LeanFaith.SFT1.Sprint.Fixtures."
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAIR_ID = re.compile(r"^pair:[0-9a-f]{64}$")
INFRASTRUCTURE_MAX_ATTEMPTS = 2


class SprintRunnerError(RuntimeError):
    """Fail-closed runner error."""


@dataclass(frozen=True, slots=True)
class RootsFileSelection:
    """Normalized roots plus the complete content identity of their source file."""

    roots: tuple[str, ...]
    source_path: str | None
    identity: dict[str, object]


def canonical_surface(goal_text: str) -> tuple[str | None, str | None]:
    """Canonicalize a pre-rendered goal exactly as the frozen REPR route will.

    Returns ``(canonical_text, None)`` or ``(None, violation)`` when the frozen
    Python-side surface validation would reject the render.
    """

    try:
        canonical = _canonicalize_elaborated_goal(goal_text)
        validate_goal_v1(canonical)
    except GoalV1Error as exc:
        return None, f"repr_surface:{str(exc)[:200]}"
    return canonical, None


class ProjectConfig(StrictModel):
    project_id: Literal["mathlib", "physlib", "cslib"]
    project_dir: NonEmpty
    project_revision: NonEmpty
    lean_version: NonEmpty
    lean_interact_version: NonEmpty
    repl_revision: NonEmpty
    import_header: NonEmpty
    options: dict[str, bool]

    @model_validator(mode="after")
    def _options(self) -> ProjectConfig:
        if self.options.get("Elab.async") is not False:
            raise ValueError("sprint runs must disable Elab.async")
        expected_import = {
            "mathlib": "import Mathlib",
            "physlib": "import Physlib",
            "cslib": "import Cslib",
        }[self.project_id]
        if self.import_header != expected_import:
            raise ValueError(
                f"{self.project_id} must use the exact import header {expected_import!r}"
            )
        return self


class EngineConfig(StrictModel):
    path: Literal["LeanFaith/Meta/SFT1/Sprint.lean"]
    operations: tuple[str, ...]

    @model_validator(mode="after")
    def _operations(self) -> EngineConfig:
        if not self.operations or len(set(self.operations)) != len(self.operations):
            raise ValueError("sprint config operations must be nonempty and unique")
        unknown = set(self.operations).difference(OPERATIONS)
        if unknown:
            raise ValueError(f"unknown sprint operations: {sorted(unknown)}")
        expected = tuple(operation for operation in OPERATIONS if operation in self.operations)
        if tuple(self.operations) != expected:
            raise ValueError("sprint operations must follow canonical engine order")
        return self


class PoolConfig(StrictModel):
    pool_id: NonEmpty
    module_prefixes: tuple[str, ...]
    weight: int = Field(ge=0)


class InventoryConfig(StrictModel):
    root: NonEmpty
    order_salt: NonEmpty
    pools: tuple[PoolConfig, ...]


class ScreensConfig(StrictModel):
    gold_blocklist_path: NonEmpty
    gold_blocklist_sha256: Sha256


class ExecutionConfig(StrictModel):
    resource_task: NonEmpty
    batch_roots: int = Field(ge=1)
    render_batch_pairs: int = Field(ge=1)
    request_timeout_seconds: float = Field(gt=0)
    lean_workers: Literal[1]
    lean_rss_claim_gib: int = Field(ge=1)
    memory_hard_limit_mb: int = Field(ge=1024)
    server_restart_rss_gib: float = Field(gt=0)


class OutputConfig(StrictModel):
    staging_root: NonEmpty
    shard_size: int = Field(ge=1)


class FixtureConfig(StrictModel):
    root: NonEmpty
    operation_id: NonEmpty
    expect_status: TerminalStatus
    expect_reason_prefix: str = ""


class FixtureWaiver(StrictModel):
    operation_id: NonEmpty
    reason: NonEmpty


class SprintConfig(StrictModel):
    schema_version: Literal[1]
    sprint_id: NonEmpty
    project: ProjectConfig
    engine: EngineConfig
    inventory: InventoryConfig
    screens: ScreensConfig
    execution: ExecutionConfig
    output: OutputConfig
    fixtures_success_waivers: tuple[FixtureWaiver, ...] = ()
    fixtures: tuple[FixtureConfig, ...]


def load_sprint_config(
    repo_root: Path, config_path: Path | None = None
) -> LoadedConfig[SprintConfig]:
    return load_config(config_path or repo_root / DEFAULT_CONFIG, SprintConfig)


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def project_pins(config: SprintConfig) -> ProjectPins:
    return ProjectPins(
        project_id=config.project.project_id,
        project_dir=Path(config.project.project_dir),
        project_revision=config.project.project_revision,
        lean_version=config.project.lean_version,
        lean_interact_version=config.project.lean_interact_version,
        repl_revision=config.project.repl_revision,
        import_header=config.project.import_header,
        options=config.project.options,
    )


def _root_entry_name(value: object, *, index: int) -> str:
    if isinstance(value, str):
        name = value
    elif isinstance(value, Mapping):
        name_value = value.get("name")
        if not isinstance(name_value, str):
            raise SprintRunnerError(f"roots[{index}] mapping needs a string name")
        for alias in ("root", "root_name"):
            alias_value = value.get(alias)
            if alias_value is not None and alias_value != name_value:
                raise SprintRunnerError(f"roots[{index}] has conflicting name and {alias} fields")
        name = name_value
    else:
        raise SprintRunnerError(f"roots[{index}] must be a string or mapping")
    if not name or name != name.strip():
        raise SprintRunnerError(f"roots[{index}] has an empty or padded name")
    return name


def _pinned_inventory_identity(
    loaded: LoadedConfig[SprintConfig],
) -> tuple[Path, str]:
    """Return the verified inventory path and exact content hash for a config."""

    config = loaded.config
    inventory_dir = Path(config.inventory.root) / config.project.project_revision
    inventory_path = inventory_dir / "inventory.jsonl"
    inventory_manifest = read_json_object(inventory_dir / "manifest.json")
    inventory_sha256 = hash_file(inventory_path)
    if inventory_manifest.get("inventory_sha256") != inventory_sha256:
        raise SprintRunnerError("active inventory content hash disagrees with its manifest")
    manifest_revision = inventory_manifest.get(
        "project_revision", inventory_manifest.get("mathlib_revision")
    )
    if manifest_revision != config.project.project_revision:
        raise SprintRunnerError("active inventory manifest revision mismatch")
    if inventory_manifest.get("project_id", config.project.project_id) != config.project.project_id:
        raise SprintRunnerError("active inventory manifest project mismatch")
    return inventory_path, inventory_sha256


def read_roots_file(
    path: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    expected_file_sha256: str | None = None,
) -> RootsFileSelection:
    """Read and fully bind one deterministic roots file without invoking Lean.

    Historical files containing only ``{"roots": [...]}`` remain valid.  When
    identity metadata is present it is authoritative and must agree with the
    active project and inventory.  The exact file hash and a hash of every
    non-root metadata field are recorded even for the historical simple form.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SprintRunnerError(f"cannot read roots file {path}: {exc}") from exc
    file_sha256 = sha256_hex(raw)
    if expected_file_sha256 is not None:
        if _SHA256.fullmatch(expected_file_sha256) is None:
            raise SprintRunnerError("--roots-file-sha256 must be lowercase SHA-256")
        if file_sha256 != expected_file_sha256:
            raise SprintRunnerError(
                f"roots file SHA-256 mismatch: {file_sha256} != {expected_file_sha256}"
            )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SprintRunnerError(f"roots file is not a UTF-8 JSON object: {path}") from exc
    if not isinstance(payload, dict):
        raise SprintRunnerError("roots file must contain one JSON object")
    entries = payload.get("roots")
    if not isinstance(entries, list):
        raise SprintRunnerError("roots file must contain a roots list")
    names = tuple(_root_entry_name(value, index=index) for index, value in enumerate(entries))
    if len(names) != len(set(names)):
        raise SprintRunnerError("roots file contains duplicate normalized root names")

    config = loaded.config
    inventory_path, inventory_sha256 = _pinned_inventory_identity(loaded)

    expected: dict[str, object] = {
        "project_id": config.project.project_id,
        "project_revision": config.project.project_revision,
        "inventory_sha256": inventory_sha256,
        "count": len(names),
        "roots_sha256": hash_canonical(names),
    }
    aliases = {"root_count": "count"}
    for alias, field in aliases.items():
        if alias in payload and field in payload and payload[alias] != payload[field]:
            raise SprintRunnerError(f"roots file has conflicting {alias} and {field} metadata")
    has_declared_count = "count" in payload or "root_count" in payload
    declared_count = payload.get("count", payload.get("root_count"))
    declared: dict[str, object] = {
        **{key: payload[key] for key in expected if key in payload},
        **({"count": declared_count} if has_declared_count else {}),
    }
    for field, value in declared.items():
        if field == "count" and type(value) is not int:
            raise SprintRunnerError("roots file count must be an integer")
        if field != "count" and not isinstance(value, str):
            raise SprintRunnerError(f"roots file {field} must be a string")
        if value != expected[field]:
            raise SprintRunnerError(
                f"roots file {field} mismatch: {value!r} != {expected[field]!r}"
            )
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        for field in ("project_id", "project_revision", "inventory_sha256"):
            if field in entry and entry[field] != expected[field]:
                raise SprintRunnerError(
                    f"roots[{index}] {field} conflicts with the pinned inventory"
                )
    if payload.get("target_kind") == "lean_free_mixed_family_candidate_only":
        _validate_mixed_target_payload(payload, entries)

    known_names = {str(row["name"]) for row in load_inventory(inventory_path)}
    unknown = [name for name in names if name not in known_names]
    if unknown:
        preview = ", ".join(repr(name) for name in unknown[:3])
        raise SprintRunnerError(
            f"roots file contains names absent from pinned inventory: {preview}"
        )

    metadata = {key: value for key, value in payload.items() if key != "roots"}
    identity = {
        "schema_version": 1,
        "file_sha256": file_sha256,
        "metadata_sha256": hash_canonical(metadata),
        **expected,
    }
    return RootsFileSelection(
        roots=names,
        source_path=str(path.resolve()),
        identity=identity,
    )


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class PeakRss:
    def __init__(self) -> None:
        self.peak = 0
        self._process = psutil.Process()

    def sample(self) -> int:
        total = 0
        for item in [self._process, *self._process.children(recursive=True)]:
            try:
                total += item.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.peak = max(self.peak, total)
        return total


class RunPaths:
    def __init__(self, staging_root: Path, run_id: str) -> None:
        self.staging_root = staging_root
        self.run_dir = staging_root / "runs" / run_id
        self.journal = self.run_dir / "journal.jsonl"
        self.retained = self.run_dir / "retained.jsonl"
        self.status = self.run_dir / "status.json"
        self.run_manifest = self.run_dir / "run.json"
        self.raw = staging_root / "raw"
        self.cache = staging_root / "cache"
        self.compacted = staging_root / "compacted" / run_id
        self.inspection = self.run_dir / "inspection"


class SprintRunner:
    def __init__(
        self,
        repo_root: Path,
        loaded: LoadedConfig[SprintConfig],
        *,
        run_id: str,
        max_roots: int | None = None,
        target_retained: int | None = None,
        explicit_roots: Sequence[str] | None = None,
        owner_session: str = "claude-sft1-sprint",
        operations: Sequence[str] | None = None,
        allow_fixture_roots: bool = False,
        roots_file: RootsFileSelection | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.loaded = loaded
        self.config = loaded.config
        self.run_id = run_id
        self.max_roots = max_roots
        self.target_retained = target_retained
        self.explicit_roots = list(explicit_roots) if explicit_roots is not None else None
        self.owner_session = owner_session
        self.allow_fixture_roots = allow_fixture_roots
        self.roots_file = roots_file
        if roots_file is not None and self.explicit_roots != list(roots_file.roots):
            raise SprintRunnerError("roots-file selection disagrees with explicit roots")
        self.pins = project_pins(self.config)
        self.context: CompileContext = engine_module.build_compile_context(repo_root, self.pins)
        self.identity: EngineIdentity = engine_module.engine_identity(
            repo_root, self.pins, self.context
        )
        self.paths = RunPaths(Path(self.config.output.staging_root), run_id)
        self.journal = Journal(self.paths.journal)
        self.cache = SemanticCache(self.paths.cache)
        self.gold = GoldBlocklist.load(
            repo_root / self.config.screens.gold_blocklist_path,
            expected_sha256=self.config.screens.gold_blocklist_sha256,
        )
        self.requested_operations = tuple(operations) if operations is not None else None
        self.operations: tuple[str, ...] = tuple(self.config.engine.operations)
        self.mask = operation_mask(self.operations)
        self.scope = render_scope_id(self.identity.semantic_version)
        self.implementation_commit = _git(repo_root, "rev-parse", "HEAD")
        self.runner_source_sha256 = hash_file(Path(__file__))
        self.session: SprintSession | None = None
        self.backend: LeanInteractBackend | None = None
        self.rss = PeakRss()
        self.started = time.monotonic()
        self.started_at = utc_now()
        self.statements: dict[str, str] = {}
        self.modules: dict[str, str] = {}
        self.pools: dict[str, str] = {}
        # durable state
        self.observed_operations: set[str] = set()
        self.done: dict[tuple[str, str], str] = {}
        self.roots_seen: set[str] = set()
        self.retained_keys: set[str] = set()
        self.retained_count = 0
        self.counts: dict[str, int] = {}
        self.op_status_counts: dict[str, dict[str, int]] = {op: {} for op in OPERATIONS}
        self.retained_by_op: dict[str, int] = dict.fromkeys(OPERATIONS, 0)
        self.roots_lean = 0
        self.roots_cache = 0
        self.batches = 0
        self.retained_at_start = 0
        self.roots_at_start = 0
        self.replay_mode = False
        self.last_batch: dict[str, object] = {}

    # ----------------------------------------------------------------- setup

    def verify_pins(self) -> None:
        head = _git(self.pins.project_dir, "rev-parse", "HEAD")
        if head != self.pins.project_revision:
            raise SprintRunnerError(
                f"{self.pins.project_id} revision mismatch: {head} != {self.pins.project_revision}"
            )

    def load_inventory_rows(self) -> list[dict[str, object]]:
        inventory_dir = Path(self.config.inventory.root) / self.pins.project_revision
        manifest = read_json_object(inventory_dir / "manifest.json")
        recorded_revision = manifest.get("project_revision", manifest.get("mathlib_revision"))
        if recorded_revision != self.pins.project_revision:
            raise SprintRunnerError("inventory manifest revision mismatch")
        if manifest.get("project_id", self.pins.project_id) != self.pins.project_id:
            raise SprintRunnerError("inventory manifest project mismatch")
        inventory_path = inventory_dir / "inventory.jsonl"
        if hash_file(inventory_path) != manifest.get("inventory_sha256"):
            raise SprintRunnerError("inventory content hash mismatch")
        rows = load_inventory(inventory_path)
        for row in rows:
            name = str(row["name"])
            self.statements.setdefault(name, str(row["statement"]))
            self.modules.setdefault(name, str(row["module"]))
        return rows

    def root_order(self) -> list[tuple[str, str]]:
        rows = self.load_inventory_rows()
        if self.explicit_roots is not None:
            fixture_roots = [
                name for name in self.explicit_roots if name.startswith(FIXTURE_ROOT_PREFIX)
            ]
            if fixture_roots and not self.allow_fixture_roots:
                raise SprintRunnerError(
                    "fixture-only roots are forbidden outside the explicit fixture command"
                )
            return [(name, "explicit") for name in self.explicit_roots]
        pools = [
            Pool(
                pool_id=pool.pool_id,
                module_prefixes=tuple(pool.module_prefixes),
                weight=pool.weight,
            )
            for pool in self.config.inventory.pools
        ]
        return ordered_roots(rows, pools, order_salt=self.config.inventory.order_salt)

    def load_state(self) -> None:
        self._load_journal()
        self.retained_at_start = self.retained_count
        self.roots_at_start = self.roots_lean + self.roots_cache

    def _load_journal(self) -> None:
        for record in self.journal.read():
            batch_value = record.get("batch")
            if type(batch_value) is int:
                self.batches = max(self.batches, batch_value)
            kind = record.get("kind")
            if kind == "terminal":
                key = (str(record["root"]), str(record["operation_id"]))
                status = str(record["status"])
                self.observed_operations.add(key[1])
                if key in self.done:
                    continue
                self.done[key] = status
                self._count(str(record["operation_id"]), status)
                if status == "retained":
                    self.retained_count += 1
                    self.retained_by_op[str(record["operation_id"])] += 1
                    self.retained_keys.add(str(record["unordered_pair_key"]))
            elif kind == "root":
                root = str(record["root"])
                if root in self.roots_seen:
                    continue
                self.roots_seen.add(root)
                if record.get("source") == "cache":
                    self.roots_cache += 1
                else:
                    self.roots_lean += 1

    def _count(self, operation: str, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        per_op = self.op_status_counts.setdefault(operation, {})
        per_op[status] = per_op.get(status, 0) + 1

    def missing_mask(self, name: str) -> int:
        mask = 0
        for operation in operations_in_mask(self.mask):
            if (name, operation) not in self.done:
                mask |= 1 << engine_module.OPERATION_BITS[operation]
        return mask

    # ------------------------------------------------------------- resources

    def claim(self) -> Reservation:
        try:
            return claim_resources(
                task=self.config.execution.resource_task,
                lean_workers=self.config.execution.lean_workers,
                lean_rss_gib=self.config.execution.lean_rss_claim_gib,
                gpu=False,
                pid=os.getpid(),
                owner_session=self.owner_session,
                worktree=self.repo_root,
            )
        except ReservationError as exc:
            raise SprintRunnerError(f"shared Lean capacity unavailable: {exc}") from exc

    def release(self, reservation: Reservation) -> None:
        current = [item for item in list_reservations() if item.task == reservation.task]
        if current == [reservation]:
            release_resources(task=reservation.task)

    def open_session(self) -> SprintSession:
        if self.session is None:
            self.backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=self.pins.project_dir,
                    context_fingerprint=self.context.fingerprint,
                    environment_schema_version=1,
                    raw_response_dir=self.paths.raw,
                    server_mode=ServerMode.STABLE,
                    workers=None,
                    memory_hard_limit_mb=self.config.execution.memory_hard_limit_mb,
                    enable_parallel_elaboration=False,
                    isolate_incremental_commands=False,
                )
            )
            self.session = SprintSession(
                self.backend,
                self.context,
                timeout_seconds=self.config.execution.request_timeout_seconds,
            )
        return self.session

    def close_session(self) -> None:
        if self.backend is not None:
            self.backend.close()
        self.backend = None
        self.session = None

    # ------------------------------------------------------------------ keys

    def root_key(self, name: str) -> str:
        return SemanticCache.root_key(
            project_revision=self.pins.project_revision,
            lean_version=self.pins.lean_version,
            import_options_fingerprint=self.identity.import_options_fingerprint,
            engine_semantic_version=self.identity.semantic_version,
            name=name,
            engine_source_sha256=self.identity.source_sha256,
        )

    def op_key(self, reference_alpha_hash: str, operation: str, name: str) -> str:
        return SemanticCache.op_key(
            reference_alpha_hash=reference_alpha_hash,
            operation_id=operation,
            engine_semantic_version=self.identity.semantic_version,
            lean_version=self.pins.lean_version,
            project_revision=self.pins.project_revision,
            import_options_fingerprint=self.identity.import_options_fingerprint,
            name=name,
            engine_source_sha256=self.identity.source_sha256,
        )

    def root_id(self, name: str) -> str:
        return "root:" + hash_canonical([self.pins.project_id, self.pins.project_revision, name])

    # ------------------------------------------------------------- main loop

    def resolve_operations(self) -> None:
        """Operation set precedence: CLI request > run manifest > journal-observed > config."""

        recorded: tuple[str, ...] | None = None
        if self.paths.run_manifest.is_file():
            value = read_json_object(self.paths.run_manifest).get("operations")
            if isinstance(value, list) and value:
                recorded = tuple(str(item) for item in value)
        observed = tuple(op for op in OPERATIONS if op in self.observed_operations)
        if self.requested_operations is not None:
            chosen = self.requested_operations
            if recorded is not None and set(recorded) != set(chosen):
                raise SprintRunnerError(
                    f"run {self.run_id!r} recorded operations {list(recorded)}; "
                    f"refusing {list(chosen)}"
                )
        elif recorded is not None:
            chosen = recorded
        elif observed:
            chosen = observed
        else:
            chosen = tuple(self.config.engine.operations)
        unknown = [op for op in chosen if op not in OPERATIONS]
        if unknown:
            raise SprintRunnerError(f"unknown operations {unknown}")
        self.operations = tuple(op for op in OPERATIONS if op in set(chosen))
        self.mask = operation_mask(self.operations)

    def run(self, *, require_zero_lean: bool = False) -> dict[str, object]:
        self.replay_mode = require_zero_lean
        self.verify_pins()
        self.load_state()
        self.resolve_operations()
        order = self.root_order()
        self.write_run_manifest(order_size=len(order))
        if not self.paths.retained.exists():
            write_atomic(self.paths.retained, b"")
        reservation: Reservation | None = None
        try:
            pending: list[tuple[str, int]] = []
            considered = 0
            for name, pool_id in order:
                if self.max_roots is not None and considered >= self.max_roots:
                    break
                if self.target_retained is not None and self.retained_count >= self.target_retained:
                    break
                considered += 1
                self.pools[name] = pool_id
                missing = self.missing_mask(name)
                if missing == 0:
                    continue
                missing = self.try_cache(name, missing)
                if missing == 0:
                    continue
                if require_zero_lean:
                    raise SprintRunnerError(f"replay would need Lean for root {name!r}")
                pending.append((name, missing))
                if len(pending) >= self.config.execution.batch_roots:
                    if reservation is None:
                        reservation = self.claim()
                    self.process_batch(pending)
                    pending = []
            if pending:
                if reservation is None:
                    reservation = self.claim()
                self.process_batch(pending)
            summary = self.write_status(final=True)
            return summary
        finally:
            self.close_session()
            if reservation is not None:
                self.release(reservation)

    # ------------------------------------------------------------- caching

    def try_cache(self, name: str, missing: int) -> int:
        """Finalize every missing operation that has a cached record.

        Returns the mask of operations that still need Lean.  Roots whose
        cached record is a root-level failure need nothing further.
        """

        root_record = self.cache.get_root(self.root_key(name))
        if root_record is None:
            return missing
        if not cacheable_status(root_record.get("root_status")):
            return missing
        if root_record.get("root_status") != "ok":
            self.finalize_root_failure(name, root_record, source="cache")
            return 0
        op_keys = root_record.get("ops")
        if not isinstance(op_keys, dict):
            return missing
        hits: dict[str, dict[str, Any]] = {}
        remaining = 0
        for operation in operations_in_mask(missing):
            key = op_keys.get(operation)
            record = self.cache.get_op(key) if isinstance(key, str) else None
            if record is None or (
                record.get("status") == "retained" and not isinstance(record.get("render"), dict)
            ):
                remaining |= 1 << engine_module.OPERATION_BITS[operation]
                continue
            hits[operation] = record
        if hits:
            if name not in self.roots_seen:
                self.journal.append(
                    {
                        "kind": "root",
                        "root": name,
                        "root_status": "ok",
                        "reason": "",
                        "source": "cache",
                        "batch": self.batches,
                        "cached_operations": sorted(hits),
                    }
                )
                self.roots_seen.add(name)
                self.roots_cache += 1
            for operation, record in hits.items():
                self.finalize_from_op_record(name, root_record, operation, record, source="cache")
        return remaining

    def finalize_root_failure(
        self, name: str, root_payload: Mapping[str, Any], *, source: str
    ) -> None:
        status = str(root_payload.get("root_status", "error"))
        reason = str(root_payload.get("reason", ""))
        if name not in self.roots_seen:
            self.journal.append(
                {
                    "kind": "root",
                    "root": name,
                    "root_status": status,
                    "reason": reason,
                    "source": source,
                    "batch": self.batches,
                }
            )
            self.roots_seen.add(name)
            if source == "cache":
                self.roots_cache += 1
            else:
                self.roots_lean += 1
        terminal_status = "not_applicable" if status == "not_applicable" else "error"
        records = []
        for operation in operations_in_mask(self.missing_mask(name)):
            records.append(
                self.terminal_record(
                    name, operation, terminal_status, f"root:{reason}", source=source
                )
            )
            self.done[(name, operation)] = terminal_status
            self._count(operation, terminal_status)
        self.journal.append_many(records)

    def terminal_record(
        self, name: str, operation: str, status: str, reason: str, *, source: str, **extra: object
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": "terminal",
            "root": name,
            "operation_id": operation,
            "status": status,
            "reason": reason,
            "source": source,
            "batch": self.batches,
        }
        record.update(extra)
        return record

    # ------------------------------------------------------------ batches

    def process_batch(
        self, batch: list[tuple[str, int]], *, infrastructure_attempt: int = 0
    ) -> None:
        session = self.open_session()
        self.batches += 1
        batch_started = time.monotonic()
        request_id = (
            f"{self.run_id}:process:{self.batches}:attempt-{infrastructure_attempt}:"
            + hash_canonical(batch)[:16]
        )
        result = session.run_process(batch, request_id=request_id)
        missing = [name for name, _ in batch if name not in result.roots]
        retryable_infrastructure = result.status in {
            LeanStatus.CRASH.value,
            LeanStatus.INTERNAL_ERROR.value,
            LeanStatus.TIMEOUT.value,
        }
        valid_partial_payload = result.status == LeanStatus.VALID.value and bool(missing)
        if (retryable_infrastructure or valid_partial_payload) and len(batch) > 1:
            half = len(batch) // 2
            self.batches -= 1
            self.process_batch(batch[:half])
            self.process_batch(batch[half:])
            return
        if retryable_infrastructure or valid_partial_payload:
            if infrastructure_attempt + 1 < INFRASTRUCTURE_MAX_ATTEMPTS:
                if self.backend is not None:
                    self.backend.reset_session()
                self.process_batch(batch, infrastructure_attempt=infrastructure_attempt + 1)
                return
            detail = "; ".join(result.errors[:2]) or result.status
            raise SprintRunnerError(
                "Lean infrastructure request failed after bounded retries without "
                f"a terminal journal record: request_{result.status}:{detail[:300]}"
            )
        if result.status != LeanStatus.VALID.value or missing:
            detail = "; ".join(result.errors[:2]) or result.status
            for name, _ in batch:
                self.finalize_root_failure(
                    name,
                    {
                        "root_status": "error",
                        "reason": f"request_{result.status}:{detail[:300]}",
                    },
                    source="lean",
                )
            return
        pending_render: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for name, mask in batch:
            payload = result.roots[name]
            if payload.get("root_status") != "ok":
                self.finalize_root_failure(name, payload, source="lean")
                if cacheable_status(payload.get("root_status")):
                    self.cache.put_root(
                        self.root_key(name), self.root_cache_record(name, payload, {})
                    )
                continue
            if name not in self.roots_seen:
                self.journal.append(
                    {
                        "kind": "root",
                        "root": name,
                        "root_status": "ok",
                        "reason": "",
                        "source": "lean",
                        "batch": self.batches,
                        "operations": list(operations_in_mask(mask)),
                    }
                )
                self.roots_seen.add(name)
                self.roots_lean += 1
            reference_goal, reference_violation = self.reference_surface(
                str(payload["reference_goal"])
            )
            op_keys: dict[str, str] = {}
            for terminal in payload.get("terminals", []):
                operation = str(terminal["operation_id"])
                if operation not in operations_in_mask(mask):
                    continue
                key = self.op_key(str(payload["reference_alpha_hash"]), operation, name)
                op_keys[operation] = key
                if terminal.get("status") != "retained":
                    self.cache.put_op(
                        key, self.op_cache_record(name, terminal, None, result.request_hash)
                    )
                    self.finalize_terminal(
                        name,
                        operation,
                        str(terminal["status"]),
                        str(terminal.get("reason", "")),
                        source="lean",
                    )
                    continue
                if reference_violation is not None or reference_goal is None:
                    self.cache.put_op(
                        key, self.op_cache_record(name, terminal, None, result.request_hash)
                    )
                    self.finalize_terminal(
                        name,
                        operation,
                        "rejected",
                        f"screen_reference:{reference_violation}",
                        source="lean",
                    )
                    continue
                violation = self.candidate_screen(reference_goal, str(terminal["candidate_goal"]))
                if violation is not None:
                    self.cache.put_op(
                        key, self.op_cache_record(name, terminal, None, result.request_hash)
                    )
                    self.finalize_terminal(name, operation, "rejected", violation, source="lean")
                    continue
                pending_render.append((name, operation, payload, terminal))
            self.cache.put_root(self.root_key(name), self.root_cache_record(name, payload, op_keys))
        self.render_pending(pending_render, process_request_hash=result.request_hash)
        elapsed = time.monotonic() - batch_started
        rss = self.rss.sample()
        self.last_batch = {
            "batch": self.batches,
            "roots": len(batch),
            "pairs_rendered": len(pending_render),
            "elapsed_seconds": round(elapsed, 3),
            "process_lean_ms": result.elapsed_ms,
            "process_status": result.status,
            "rss_bytes": rss,
        }
        if (
            rss > self.config.execution.server_restart_rss_gib * (1 << 30)
            and self.backend is not None
        ):
            self.backend.reset_session()
        self.write_status(final=False)

    @staticmethod
    def reference_surface(goal_text: str) -> tuple[str | None, str | None]:
        canonical, violation = canonical_surface(goal_text)
        if canonical is None:
            return None, violation
        residue = residue_violation(canonical)
        if residue is not None:
            return None, residue
        return canonical, None

    def candidate_screen(self, reference_goal: str, candidate_goal: str) -> str | None:
        canonical, surface_violation = canonical_surface(candidate_goal)
        if canonical is None:
            return f"screen_candidate:{surface_violation}"
        candidate_goal = canonical
        violation = residue_violation(candidate_goal)
        if violation is not None:
            return f"screen_candidate:{violation}"
        if reference_goal == candidate_goal:
            return "screen:self_pair_text"
        if self.gold.hit(reference_goal) or self.gold.hit(candidate_goal):
            return "screen:gold_blocklist"
        return None

    def render_pending(
        self,
        pending: list[tuple[str, str, dict[str, Any], dict[str, Any]]],
        *,
        process_request_hash: str,
    ) -> None:
        if not pending:
            return
        size = self.config.execution.render_batch_pairs
        for start in range(0, len(pending), size):
            chunk = pending[start : start + size]
            if len(chunk) == 1:
                self.render_chunk(chunk, process_request_hash=process_request_hash, final=True)
                continue
            if not self.render_chunk(chunk, process_request_hash=process_request_hash):
                for item in chunk:
                    self.render_chunk([item], process_request_hash=process_request_hash, final=True)

    def render_chunk(
        self,
        chunk: list[tuple[str, str, dict[str, Any], dict[str, Any]]],
        *,
        process_request_hash: str,
        final: bool = False,
        infrastructure_attempt: int = 0,
    ) -> bool:
        session = self.open_session()
        pairs = [(name, operation) for name, operation, _, _ in chunk]
        request_id = (
            f"{self.run_id}:render:{self.batches}:attempt-{infrastructure_attempt}:"
            + hash_canonical(pairs)[:16]
        )
        try:
            rendered = session.run_render(
                pairs, statements=self.statements, scope=self.scope, request_id=request_id
            )
        except (GoalV1Error, ValueError) as exc:
            if not final:
                return False
            name, operation, _, _ = chunk[0]
            self.finalize_terminal(
                name, operation, "rejected", f"render_failed:route:{str(exc)[:300]}", source="lean"
            )
            return True
        batch = rendered.batch
        render_infrastructure_failure = rendered.status in {
            LeanStatus.CRASH.value,
            LeanStatus.INTERNAL_ERROR.value,
            LeanStatus.TIMEOUT.value,
        }
        if render_infrastructure_failure:
            if infrastructure_attempt + 1 < INFRASTRUCTURE_MAX_ATTEMPTS:
                if self.backend is not None:
                    self.backend.reset_session()
                return self.render_chunk(
                    chunk,
                    process_request_hash=process_request_hash,
                    final=final,
                    infrastructure_attempt=infrastructure_attempt + 1,
                )
            if not final:
                return False
            detail = rendered.infrastructure_error or rendered.status
            raise SprintRunnerError(
                "Lean render infrastructure failed after bounded retries without "
                f"a terminal journal record: request_{rendered.status}:{detail[:300]}"
            )
        if batch.failures:
            if not final:
                return False
            detail = "; ".join(f"{item.endpoint_id}: {item.detail}" for item in batch.failures)
            name, operation, _, _ = chunk[0]
            # Render failures are not cached: a later run may render the pair again.
            self.finalize_terminal(
                name, operation, "rejected", f"render_failed:{detail[:400]}", source="lean"
            )
            return True
        sidecars = {sidecar.record.endpoint_id: sidecar for sidecar in batch.sidecars}
        for index, (name, operation, payload, terminal) in enumerate(chunk):
            reference = sidecars.get(f"{index}.reference")
            candidate = sidecars.get(f"{index}.candidate")
            key = self.op_key(str(payload["reference_alpha_hash"]), operation, name)
            rebuilt = rendered.rebuild_hashes.get(index)
            expected_hashes = (
                str(payload["reference_alpha_hash"]),
                str(terminal["candidate_alpha_hash"]),
            )
            mismatch: str | None = None
            if reference is None or candidate is None:
                mismatch = "render_missing_endpoint"
            elif rebuilt != expected_hashes:
                mismatch = "render_rebuild_hash_mismatch"
            elif reference.core_text() != canonical_surface(str(payload["reference_goal"]))[0]:
                mismatch = "render_reference_text_mismatch"
            elif candidate.core_text() != canonical_surface(str(terminal["candidate_goal"]))[0]:
                mismatch = "render_candidate_text_mismatch"
            if mismatch is not None or reference is None or candidate is None:
                self.finalize_terminal(
                    name,
                    operation,
                    "rejected",
                    mismatch or "render_missing_endpoint",
                    source="lean",
                )
                continue
            render_payload = {
                "reference": self.sidecar_payload(reference),
                "candidate": self.sidecar_payload(candidate),
                "request_hash": batch.request_hash,
            }
            op_record = self.op_cache_record(name, terminal, render_payload, process_request_hash)
            self.cache.put_op(key, op_record)
            root_record = self.root_cache_record(name, payload, {})
            self.finalize_from_op_record(name, root_record, operation, op_record, source="lean")
        return True

    @staticmethod
    def sidecar_payload(sidecar: ClosedExprSidecar) -> dict[str, object]:
        return {
            "record": sidecar.record.to_dict(),
            "source_material": sidecar.source_material.to_dict(),
        }

    # ------------------------------------------------------------- records

    def root_cache_record(
        self, name: str, payload: Mapping[str, Any], op_keys: Mapping[str, str]
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 1,
            "name": name,
            "root_status": payload.get("root_status"),
            "reason": payload.get("reason", ""),
            "module": payload.get("module"),
            "level_params": payload.get("level_params", []),
            "reference_alpha_hash": payload.get("reference_alpha_hash"),
            "reference_goal": payload.get("reference_goal"),
            "source_proof_value_hash_u64": payload.get("source_proof_value_hash_u64"),
            "binders": payload.get("binders", []),
            "engine": self.identity.to_dict(),
            "project_revision": self.pins.project_revision,
            "lean_version": self.pins.lean_version,
            "ops": dict(op_keys),
        }
        existing = self.cache.get_root(self.root_key(name))
        if existing is not None and isinstance(existing.get("ops"), dict) and not op_keys:
            record["ops"] = existing["ops"]
        elif existing is not None and isinstance(existing.get("ops"), dict):
            merged = dict(existing["ops"])
            merged.update(op_keys)
            record["ops"] = merged
        return record

    def op_cache_record(
        self,
        name: str,
        terminal: Mapping[str, Any],
        render_payload: Mapping[str, object] | None,
        process_request_hash: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "root": name,
            "operation_id": terminal["operation_id"],
            "status": terminal["status"],
            "reason": terminal.get("reason", ""),
            "label": terminal.get("label"),
            "site": terminal.get("site"),
            "evidence": terminal.get("evidence"),
            "candidate_alpha_hash": terminal.get("candidate_alpha_hash"),
            "candidate_goal": terminal.get("candidate_goal"),
            "elapsed_ms": terminal.get("elapsed_ms"),
            "engine": self.identity.to_dict(),
            "process_request_hash": process_request_hash,
            "render": dict(render_payload) if render_payload is not None else None,
        }

    def finalize_terminal(
        self,
        name: str,
        operation: str,
        status: str,
        reason: str,
        *,
        source: str,
        **extra: object,
    ) -> None:
        self.journal.append(
            self.terminal_record(name, operation, status, reason, source=source, **extra)
        )
        self.done[(name, operation)] = status
        self._count(operation, status)

    def finalize_from_op_record(
        self,
        name: str,
        root_record: Mapping[str, Any],
        operation: str,
        op_record: Mapping[str, Any],
        *,
        source: str,
    ) -> None:
        status = str(op_record.get("status"))
        if status != "retained":
            self.finalize_terminal(
                name, operation, status, str(op_record.get("reason", "")), source=source
            )
            return
        render = op_record.get("render")
        reference_goal, violation = self.reference_surface(
            str(root_record.get("reference_goal", ""))
        )
        candidate_goal = str(op_record.get("candidate_goal", ""))
        if violation is not None or reference_goal is None:
            self.finalize_terminal(
                name, operation, "rejected", f"screen_reference:{violation}", source=source
            )
            return
        candidate_violation = self.candidate_screen(reference_goal, candidate_goal)
        if candidate_violation is not None:
            self.finalize_terminal(name, operation, "rejected", candidate_violation, source=source)
            return
        if not isinstance(render, dict):
            self.finalize_terminal(name, operation, "rejected", "render_missing", source=source)
            return
        record = self.build_record(name, root_record, operation, op_record, render)
        key = str(record["unordered_pair_key"])
        if key in self.retained_keys:
            self.finalize_terminal(
                name,
                operation,
                "rejected",
                "duplicate_pair_in_run",
                source=source,
                unordered_pair_key=key,
            )
            return
        self.retained_keys.add(key)
        with self.paths.retained.open("ab") as handle:
            handle.write(canonical_json_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.journal.append(
            self.terminal_record(
                name,
                operation,
                "retained",
                "",
                source=source,
                pair_id=record["row"]["pair_id"],
                unordered_pair_key=key,
                row_hash=record["row_hash"],
            )
        )
        self.done[(name, operation)] = "retained"
        self._count(operation, "retained")
        self.retained_count += 1
        self.retained_by_op[operation] += 1

    def build_record(
        self,
        name: str,
        root_record: Mapping[str, Any],
        operation: str,
        op_record: Mapping[str, Any],
        render: Mapping[str, Any],
    ) -> dict[str, Any]:
        reference = cast(dict[str, Any], render["reference"])["record"]
        candidate = cast(dict[str, Any], render["candidate"])["record"]
        reference_text = str(reference["goal_v1"])
        candidate_text = str(candidate["goal_v1"])
        label = operation in POSITIVE_OPERATIONS
        if bool(op_record.get("label")) != label:
            raise SprintRunnerError(
                f"engine label disagrees with operation polarity for {name}/{operation}"
            )
        root_id = self.root_id(name)
        reference_expr_hash = str(reference["provenance"]["expr_hash"])
        candidate_expr_hash = str(candidate["provenance"]["expr_hash"])
        if reference_expr_hash == candidate_expr_hash:
            raise SprintRunnerError(f"self pair survived engine screens for {name}/{operation}")
        pair_id = make_id(
            PAIR_PREFIX,
            {
                "root_id": root_id,
                "operation_id": operation,
                "reference_expr_hash": reference_expr_hash,
                "candidate_expr_hash": candidate_expr_hash,
            },
        )
        row = {
            "pair_id": pair_id,
            "root_id": root_id,
            "reference": reference_text,
            "candidate": candidate_text,
            "label": label,
            "operation_id": operation,
        }
        evidence_hash = hash_canonical(op_record.get("evidence"))
        row_hash = stable_row_hash(
            {
                "root_id": root_id,
                "operation_id": operation,
                "reference_expr_hash": reference_expr_hash,
                "candidate_expr_hash": candidate_expr_hash,
                "label": label,
                "evidence_hash": evidence_hash,
                "site_hash": hash_canonical(op_record.get("site")),
                "spec_hash": reference["spec_hash"],
                "implementation_identity": reference["implementation_identity"],
                "universe_profile_hash": reference["provenance"]["universe_profile_hash"],
                "render_context_hash": reference["provenance"]["render_context_hash"],
            }
        )
        pair_key = unordered_pair_key(
            str(reference["rendered_goal_hash"]), str(candidate["rendered_goal_hash"])
        )
        if str(reference["rendered_goal_hash"]) != render_hash(reference_text):
            raise SprintRunnerError("reference render hash disagrees with its text")
        sidecar = {
            "pair_id": pair_id,
            "root_id": root_id,
            "root_name": name,
            "module": root_record.get("module"),
            "pool_id": self.pools.get(name, "unknown"),
            "statement": self.statements.get(name),
            "operation_id": operation,
            "mechanism": engine_module.mechanism_of(operation),
            "label": label,
            "site": op_record.get("site"),
            "evidence": op_record.get("evidence"),
            "evidence_hash": evidence_hash,
            "candidate_truth": cast(dict[str, Any], op_record.get("evidence") or {}).get(
                "candidate_truth"
            ),
            "repr": {
                "reference": reference,
                "candidate": candidate,
                "reference_source_material": cast(dict[str, Any], render["reference"])[
                    "source_material"
                ],
                "candidate_source_material": cast(dict[str, Any], render["candidate"])[
                    "source_material"
                ],
            },
            "instance_dagger_counts": {
                "reference": instance_dagger_count(reference_text),
                "candidate": instance_dagger_count(candidate_text),
            },
            "project": self.pins.to_dict(),
            "engine": self.identity.to_dict(),
            "cache_key": self.op_key(str(root_record["reference_alpha_hash"]), operation, name),
            "lean_request_hashes": {
                "process": op_record.get("process_request_hash"),
                "render": render.get("request_hash"),
            },
            "root_binders": root_record.get("binders"),
            "level_params": root_record.get("level_params"),
            "implementation_commit": self.implementation_commit,
            "runner_source_sha256": self.runner_source_sha256,
            "cache_schema": CACHE_SCHEMA_CURRENT,
            "proof_check_time": "original_generation",
        }
        return {
            "row": row,
            "sidecar": sidecar,
            "row_hash": row_hash,
            "unordered_pair_key": pair_key,
            "label": label,
            "operation_id": operation,
            "root_name": name,
        }

    # -------------------------------------------------------------- status

    def write_run_manifest(self, *, order_size: int) -> None:
        if self.paths.run_manifest.is_file():
            recorded = read_json_object(self.paths.run_manifest)
            expected: dict[str, object] = {
                "sprint_id": self.config.sprint_id,
                "run_id": self.run_id,
                "config_semantic_hash": self.loaded.config_hash,
                "engine": self.identity.to_dict(),
                "project": self.pins.to_dict(),
                "gold_blocklist_sha256": self.gold.sha256,
                "explicit_roots_sha256": (
                    hash_canonical(self.explicit_roots) if self.explicit_roots is not None else None
                ),
                "roots_file_identity": (
                    self.roots_file.identity if self.roots_file is not None else None
                ),
                "operations": list(self.operations),
            }
            for field, value in expected.items():
                if recorded.get(field) != value:
                    raise SprintRunnerError(
                        f"run {self.run_id!r} manifest {field} changed; refusing mixed resume"
                    )
            if bool(recorded.get("allow_fixture_roots", False)) != self.allow_fixture_roots:
                raise SprintRunnerError(
                    f"run {self.run_id!r} fixture-root policy changed; refusing mixed resume"
                )
            recorded_runner = recorded.get("runner_source_sha256")
            if recorded_runner is not None and recorded_runner != self.runner_source_sha256:
                raise SprintRunnerError(
                    f"run {self.run_id!r} runner source changed; refusing mixed resume"
                )
            return
        manifest = {
            "schema_version": 1,
            "sprint_id": self.config.sprint_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "config_path": str(self.loaded.path),
            "config_file_sha256": hash_file(self.loaded.path),
            "config_semantic_hash": self.loaded.config_hash,
            "engine": self.identity.to_dict(),
            "project": self.pins.to_dict(),
            "gold_blocklist_sha256": self.gold.sha256,
            "implementation_commit": _git(self.repo_root, "rev-parse", "HEAD"),
            "implementation_dirty": bool(_git(self.repo_root, "status", "--porcelain")),
            "runner_source_sha256": self.runner_source_sha256,
            "max_roots": self.max_roots,
            "target_retained": self.target_retained,
            "explicit_roots": self.explicit_roots,
            "explicit_roots_sha256": (
                hash_canonical(self.explicit_roots) if self.explicit_roots is not None else None
            ),
            "roots_file_path": (
                self.roots_file.source_path if self.roots_file is not None else None
            ),
            "roots_file_identity": (
                self.roots_file.identity if self.roots_file is not None else None
            ),
            "operations": list(self.operations),
            "allow_fixture_roots": self.allow_fixture_roots,
            "engine_operation_set_version": engine_module.ENGINE_OPERATION_SET_VERSION,
            "root_order_size": order_size,
            "argv": sys.argv,
        }
        write_atomic(self.paths.run_manifest, canonical_json_bytes(manifest) + b"\n")

    def summary(self) -> dict[str, object]:
        wall = time.monotonic() - self.started
        roots = self.roots_lean + self.roots_cache
        new_retained = self.retained_count - self.retained_at_start
        new_roots = roots - self.roots_at_start
        rate = new_retained / wall * 60 if wall > 0 else 0.0
        eta = None
        if self.target_retained is not None and rate > 0:
            eta = max(0.0, (self.target_retained - self.retained_count) / rate * 60)
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "roots_considered": roots,
            "roots_lean": self.roots_lean,
            "roots_cache": self.roots_cache,
            "retained_total": self.retained_count,
            "retained_by_operation": dict(self.retained_by_op),
            "terminals_by_status": dict(self.counts),
            "terminals_by_operation_status": {k: dict(v) for k, v in self.op_status_counts.items()},
            "lean_requests": self.session.request_count if self.session else 0,
            "lean_elapsed_ms": self.session.lean_elapsed_ms if self.session else 0,
            "wall_seconds": round(wall, 3),
            "roots_per_minute": round(new_roots / wall * 60, 3) if wall > 0 else 0.0,
            "retained_per_minute": round(rate, 3),
            "retained_this_process": new_retained,
            "roots_this_process": new_roots,
            "eta_seconds": None if eta is None else round(eta),
            "target_retained": self.target_retained,
            "max_roots": self.max_roots,
            "peak_process_tree_rss_bytes": self.rss.peak,
            "batches": self.batches,
            "last_batch": self.last_batch,
        }

    def write_status(self, *, final: bool) -> dict[str, object]:
        summary = self.summary()
        summary["final"] = final
        summary["replay_mode"] = self.replay_mode
        # A replay never overwrites the generation run's measured throughput.
        target = (
            self.paths.run_dir / "replay_status.json" if self.replay_mode else self.paths.status
        )
        write_atomic(target, canonical_json_bytes(summary) + b"\n")
        return summary


# ------------------------------------------------------------------ compaction


def read_retained(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for line in path.read_bytes().split(b"\n"):
        if not line:
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def release_certificate_issues(record: Mapping[str, Any]) -> list[str]:
    """Return exact certificate-contract violations for one retained row."""

    issues: list[str] = []
    sidecar = record.get("sidecar")
    if not isinstance(sidecar, Mapping):
        return ["sidecar_missing"]
    operation = str(sidecar.get("operation_id", record.get("operation_id", "")))
    evidence = sidecar.get("evidence")
    if not isinstance(evidence, Mapping):
        return ["evidence_missing"]
    label = sidecar.get("label", record.get("label"))
    if operation in POSITIVE_OPERATIONS:
        equivalence = evidence.get("equivalence_proof")
        check = equivalence.get("check") if isinstance(equivalence, Mapping) else None
        if not _check_passed(check):
            issues.append("positive_equivalence_unchecked")
        if evidence.get("candidate_truth") != "proved_equivalent_to_reference":
            issues.append("positive_truth_evidence_missing")
        if label is not True:
            issues.append("positive_polarity_mismatch")
        return issues
    if operation not in NEGATIVE_OPERATIONS:
        return ["operation_polarity_unknown"]
    if label is not False:
        issues.append("negative_polarity_mismatch")
    if not _check_passed(evidence.get("source_proof_check")):
        issues.append("negative_source_proof_unchecked")
    refutation = evidence.get("refutation")
    if not isinstance(refutation, Mapping) or not _check_passed(refutation.get("check")):
        issues.append("negative_refutation_unchecked")
        return issues
    if evidence.get("candidate_truth") != "refuted":
        issues.append("negative_candidate_not_refuted")
    engine = sidecar.get("engine")
    semantic_version = (
        str(engine.get("semantic_version", "")) if isinstance(engine, Mapping) else ""
    )
    is_wave3_plus = semantic_version.startswith(("sft1_wave3_", "sft1_wave4_", "sft1_wave5_"))
    if is_wave3_plus and operation in {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    }:
        separator = refutation.get("separator")
        if not isinstance(separator, Mapping) or not _check_passed(separator.get("check")):
            issues.append("boundary_separator_unchecked")
    if is_wave3_plus and operation == "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1":
        witnesses = refutation.get("witnesses")
        checks = refutation.get("witness_checks")
        enumeration = refutation.get("enumeration")
        if not isinstance(witnesses, list) or len(witnesses) < 2:
            issues.append("n30_two_witnesses_missing")
        if (
            not isinstance(checks, list)
            or len(checks) < 3
            or not all(_check_passed(check) for check in checks)
        ):
            issues.append("n30_witness_checks_missing")
        if not isinstance(enumeration, str) or "complete" not in enumeration:
            issues.append("n30_complete_enumeration_missing")
    if is_wave3_plus and operation == "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1":
        checks = refutation.get("witness_checks")
        enumeration = refutation.get("enumeration")
        if (
            not isinstance(checks, list)
            or not checks
            or not all(_check_passed(check) for check in checks)
        ):
            issues.append("n29_matrix_checks_missing")
        if not isinstance(enumeration, str) or "complete_matrix" not in enumeration:
            issues.append("n29_complete_matrix_missing")
    return issues


def balanced_view(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-root label balancing: each root contributes equally many positive and
    negative pairs (chosen by stable row hash); roots lacking either polarity
    are dropped.  This makes the reference text uninformative about the label."""

    by_root: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    for record in records:
        root = str(record["root_name"])
        by_root.setdefault(root, {True: [], False: []})[bool(record["label"])].append(record)
    kept: list[dict[str, Any]] = []
    for root in sorted(by_root):
        polarity = by_root[root]
        count = min(len(polarity[True]), len(polarity[False]))
        if count == 0:
            continue
        for label in (True, False):
            kept.extend(sorted(polarity[label], key=lambda item: str(item["row_hash"]))[:count])
    return kept


def compact_run(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_id: str,
    shard_size: int | None = None,
    view: str = "raw",
) -> dict[str, object]:
    if view not in {"raw", "balanced"}:
        raise SprintRunnerError(f"unknown compaction view {view!r}")
    config = loaded.config
    paths = RunPaths(Path(config.output.staging_root), run_id)
    if view != "raw":
        paths.compacted = paths.compacted.parent / f"{run_id}_{view}"
    records = read_retained(paths.retained)
    gold = GoldBlocklist.load(
        repo_root / config.screens.gold_blocklist_path,
        expected_sha256=config.screens.gold_blocklist_sha256,
    )
    screened: list[dict[str, Any]] = []
    screen_rejections: dict[str, int] = {}
    for record in records:
        certificate_issues = release_certificate_issues(record)
        if certificate_issues:
            raise SprintRunnerError(
                "retained row has invalid release certificate: " + ",".join(certificate_issues)
            )
        row = record["row"]
        reasons = [
            residue_violation(str(row["reference"])),
            residue_violation(str(row["candidate"])),
            "self_pair_text" if row["reference"] == row["candidate"] else None,
            "gold_blocklist"
            if gold.hit(str(row["reference"])) or gold.hit(str(row["candidate"]))
            else None,
        ]
        reason = next((item for item in reasons if item), None)
        if reason is not None:
            screen_rejections[reason] = screen_rejections.get(reason, 0) + 1
            continue
        screened.append(record)
    outcome = deduplicate(screened)
    conflicting_rows = sum(
        1 for record in screened if str(record["unordered_pair_key"]) in set(outcome.conflict_keys)
    )
    size = shard_size or config.output.shard_size
    paths.compacted.mkdir(parents=True, exist_ok=True)
    selected = balanced_view(outcome.kept) if view == "balanced" else outcome.kept
    kept = group_by_ancestry(selected)
    provenance = derive_provenance(
        kept, repo_root=repo_root, cache_root=Path(config.output.staging_root) / "cache"
    )
    if not provenance["consistent"]:
        raise SprintRunnerError(
            "sidecar-derived provenance is inconsistent: " + "; ".join(provenance["issues"])
        )
    shards = ancestry_shards(kept, size)
    shard_manifests: list[dict[str, object]] = []
    for number, shard in enumerate(shards, start=1):
        shard_dir = paths.compacted / f"shard-{number:04d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in shard)
        sidecar_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in shard)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecar_bytes)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "shard": number,
            "row_count": len(shard),
            "complete": len(shard) >= size,
            "labels": {
                "positive": sum(1 for item in shard if item["label"]),
                "negative": sum(1 for item in shard if not item["label"]),
            },
            "operations": _count_by(shard, "operation_id"),
            "roots": len({item["root_name"] for item in shard}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecar_bytes),
            "first_row_hash": shard[0]["row_hash"],
            "last_row_hash": shard[-1]["row_hash"],
            "engine_source_sha256_set": sorted(
                {str(item["sidecar"]["engine"]["source_sha256"]) for item in shard}
            ),
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        shard_manifests.append(manifest)
    run_manifest = read_json_object(paths.run_manifest) if paths.run_manifest.is_file() else {}
    manifest = {
        "schema_version": 1,
        "sprint_id": config.sprint_id,
        "run_id": run_id,
        "view": view,
        "compacted_at": utc_now(),
        "input_records": len(records),
        "deduplicated_records": len(outcome.kept),
        "screen_rejections": screen_rejections,
        "duplicates_removed": outcome.duplicate_count,
        "conflicting_classes_rejected": outcome.conflict_count,
        "conflicting_rows_rejected": conflicting_rows,
        "conflict_keys": list(outcome.conflict_keys),
        "view_dropped": len(outcome.kept) - len(selected),
        "retained_rows": len(kept),
        "labels": {
            "positive": sum(1 for item in kept if item["label"]),
            "negative": sum(1 for item in kept if not item["label"]),
        },
        "operations": _count_by(kept, "operation_id"),
        "mechanisms": _count_by(kept, "mechanism"),
        "roots": len({item["root_name"] for item in kept}),
        "shard_size": size,
        "shards": shard_manifests,
        "config_semantic_hash": loaded.config_hash,
        "provenance": provenance,
        "first_launch_manifest": {
            "implementation_commit": run_manifest.get("implementation_commit"),
            "engine": run_manifest.get("engine"),
            "note": "identity of the first launch only; authoritative provenance is derived "
            "from sidecars above",
        },
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "artifact_status": "diagnostic_gate_evidence_not_a_training_release",
        "gold_blocklist_sha256": gold.sha256,
    }
    write_atomic(paths.compacted / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def group_by_ancestry(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows so every root's pairs are adjacent (root order by hash)."""

    return sorted(
        records,
        key=lambda item: (hash_canonical(str(item["row"]["root_id"])), str(item["row_hash"])),
    )


def ancestry_shards(records: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Cut ancestry-grouped rows into shards without splitting a root."""

    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_root: str | None = None
    for record in records:
        root_id = str(record["row"]["root_id"])
        if current and len(current) >= size and root_id != current_root:
            shards.append(current)
            current = []
        current.append(record)
        current_root = root_id
    if current:
        shards.append(current)
    return shards


def completed_roots(journal_path: Path, operation_count: int) -> set[str]:
    """Roots whose every operation has a journal terminal."""

    counts: dict[str, set[str]] = {}
    for record in Journal(journal_path).read():
        if record.get("kind") == "terminal":
            counts.setdefault(str(record["root"]), set()).add(str(record["operation_id"]))
    return {root for root, ops in counts.items() if len(ops) >= operation_count}


def compact_root_windows(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_id: str,
    roots_per_window: int,
) -> dict[str, object]:
    """Emit one shard per complete window of the deterministic root order.

    Windows are cut over the run's root order, so a shard's membership never
    changes as later roots arrive.  A window is compacted only when every root
    in it has terminals for all operations.  Duplicate unordered pairs are
    resolved within a window and against every previously compacted window
    through a persisted key set, so shards remain independently publishable.
    """

    runner = SprintRunner(repo_root, loaded, run_id=run_id)
    order = [name for name, _ in runner.root_order()]
    paths = runner.paths
    done = completed_roots(paths.journal, len(OPERATIONS))
    records = read_retained(paths.retained)
    by_root: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_root.setdefault(str(record["root_name"]), []).append(record)
    windows_dir = paths.compacted
    windows_dir.mkdir(parents=True, exist_ok=True)
    state_path = windows_dir / "dedup_state.json"
    state: dict[str, Any] = (
        read_json_object(state_path) if state_path.is_file() else {"seen_keys": [], "windows": []}
    )
    seen_keys = set(cast(list[str], state.get("seen_keys", [])))
    compacted_windows = cast(list[dict[str, Any]], state.get("windows", []))
    compacted_indices = {int(item["window"]) for item in compacted_windows}
    gold = runner.gold
    emitted: list[dict[str, Any]] = []
    for window_index, start in enumerate(range(0, len(order), roots_per_window)):
        window_number = window_index + 1
        window_roots = order[start : start + roots_per_window]
        if window_number in compacted_indices:
            continue
        if any(root not in done for root in window_roots):
            break
        window_records: list[dict[str, Any]] = []
        for root in window_roots:
            for record in by_root.get(root, []):
                row = record["row"]
                bad = (
                    residue_violation(str(row["reference"]))
                    or residue_violation(str(row["candidate"]))
                    or ("self_pair" if row["reference"] == row["candidate"] else None)
                    or (
                        "gold"
                        if gold.hit(str(row["reference"])) or gold.hit(str(row["candidate"]))
                        else None
                    )
                )
                if bad is None:
                    window_records.append(record)
        outcome = deduplicate(window_records)
        kept = [item for item in outcome.kept if str(item["unordered_pair_key"]) not in seen_keys]
        cross_duplicates = len(outcome.kept) - len(kept)
        kept = group_by_ancestry(kept)
        shard_dir = windows_dir / f"window-{window_number:05d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in kept)
        sidecar_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in kept)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecar_bytes)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "window": window_number,
            "root_order_start": start,
            "root_order_end": start + len(window_roots),
            "roots_in_window": len(window_roots),
            "row_count": len(kept),
            "complete": True,
            "labels": {
                "positive": sum(1 for item in kept if item["label"]),
                "negative": sum(1 for item in kept if not item["label"]),
            },
            "operations": _count_by(kept, "operation_id"),
            "duplicates_removed_in_window": outcome.duplicate_count,
            "conflicting_classes_rejected": outcome.conflict_count,
            "cross_window_duplicates_removed": cross_duplicates,
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecar_bytes),
            "compacted_at": utc_now(),
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        seen_keys.update(str(item["unordered_pair_key"]) for item in kept)
        compacted_windows.append(manifest)
        emitted.append(manifest)
        state = {"seen_keys": sorted(seen_keys), "windows": compacted_windows}
        write_atomic(state_path, canonical_json_bytes(state) + b"\n")
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "roots_per_window": roots_per_window,
        "root_order_size": len(order),
        "completed_roots": len(done),
        "windows_compacted_total": len(compacted_windows),
        "windows_compacted_now": [int(item["window"]) for item in emitted],
        "rows_total": sum(int(item["row_count"]) for item in compacted_windows),
        "config_semantic_hash": loaded.config_hash,
        "engine": runner.identity.to_dict(),
        "updated_at": utc_now(),
    }
    write_atomic(windows_dir / "windows_manifest.json", canonical_json_bytes(summary) + b"\n")
    return summary


def _count_by(records: Iterable[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(field)
        if value is None and field == "mechanism":
            value = str(record.get("operation_id", "")).split("_", 1)[0]
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


# ------------------------------------------------------------------ inspection


def inspection_sample(records: Sequence[Mapping[str, Any]], *, count: int) -> list[dict[str, Any]]:
    """Operation-stratified sample that always includes every N31 row."""

    by_op: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(records, key=lambda item: str(item["row_hash"])):
        by_op.setdefault(str(record["operation_id"]), []).append(dict(record))
    selected: list[dict[str, Any]] = list(by_op.get("N31_DROP_REQUIRED_GUARD_PROOF_V1", []))
    others = [op for op in OPERATIONS if op != "N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    positions = dict.fromkeys(others, 0)
    while len(selected) < count:
        progressed = False
        for op in others:
            if len(selected) >= count:
                break
            items = by_op.get(op, [])
            position = positions[op]
            if position < len(items):
                selected.append(items[position])
                positions[op] = position + 1
                progressed = True
        if not progressed:
            break
    return selected


def write_inspection(
    loaded: LoadedConfig[SprintConfig], *, run_id: str, count: int
) -> tuple[Path, int]:
    paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
    records = read_retained(paths.retained)
    sample = inspection_sample(records, count=count)
    paths.inspection.mkdir(parents=True, exist_ok=True)
    write_atomic(
        paths.inspection / "sample.jsonl",
        b"".join(canonical_json_bytes(item) + b"\n" for item in sample),
    )
    lines = [f"# Inspection sample for run `{run_id}` ({len(sample)} pairs)", ""]
    for index, item in enumerate(sample, start=1):
        row = item["row"]
        sidecar = item["sidecar"]
        evidence = sidecar.get("evidence") or {}
        refutation = evidence.get("refutation") or {}
        assignment = (refutation.get("grounding") or {}).get("assignment")
        lines.extend(
            [
                f"## {index}. {row['operation_id']} — label {int(bool(row['label']))} "
                f"— root `{sidecar['root_name']}`",
                "",
                "reference:",
                "```",
                str(row["reference"]),
                "```",
                "candidate:",
                "```",
                str(row["candidate"]),
                "```",
                f"site: `{json.dumps(sidecar.get('site'), ensure_ascii=False)}`",
                (
                    f"refutation: kind={refutation.get('kind')} assignment="
                    f"{json.dumps(assignment, ensure_ascii=False)} "
                    f"boundary={refutation.get('boundary')}"
                    if refutation
                    else "equivalence: kernel+meta checked Iff witness"
                ),
                "",
            ]
        )
    write_atomic(paths.inspection / "sample.md", "\n".join(lines).encode("utf-8"))
    return paths.inspection / "sample.md", len(sample)


# ------------------------------------------------------------------ gate


def gate_report(
    loaded: LoadedConfig[SprintConfig],
    *,
    run_id: str,
    fixtures_ok: bool | None,
    replay: Mapping[str, Any] | None,
) -> dict[str, object]:
    config = loaded.config
    paths = RunPaths(Path(config.output.staging_root), run_id)
    status = read_json_object(paths.status) if paths.status.is_file() else {}
    records = read_retained(paths.retained)
    by_mechanism: dict[str, int] = {}
    unchecked = 0
    residue = 0
    rubric_only = 0
    certificate_issue_counts: dict[str, int] = {}
    for record in records:
        op = str(record["operation_id"])
        by_mechanism[op] = by_mechanism.get(op, 0) + 1
        evidence = record["sidecar"].get("evidence") or {}
        check = (
            (evidence.get("equivalence_proof") or {}).get("check")
            if op in POSITIVE_OPERATIONS
            else (evidence.get("refutation") or {}).get("check")
        )
        if not check or not check.get("meta_checked") or not check.get("kernel_checked"):
            unchecked += 1
        if op in NEGATIVE_OPERATIONS and evidence.get("candidate_truth") != "refuted":
            rubric_only += 1
        for issue in release_certificate_issues(record):
            certificate_issue_counts[issue] = certificate_issue_counts.get(issue, 0) + 1
        row = record["row"]
        if residue_violation(str(row["reference"])) or residue_violation(str(row["candidate"])):
            residue += 1
    rich = {op: n for op, n in by_mechanism.items() if n >= 10}
    positives = [op for op in rich if op in POSITIVE_OPERATIONS]
    negatives = [op for op in rich if op in NEGATIVE_OPERATIONS]
    wall = float(status.get("wall_seconds", 0.0))
    evidence_path = paths.run_dir / "performance_evidence.json"
    performance_unavailable = (
        evidence_path.is_file() and read_json_object(evidence_path).get("status") == "unavailable"
    )
    dedup = deduplicate(records)
    checks = {
        "mechanisms_with_ten_pairs": len(rich) >= 5,
        "three_positive_mechanisms": len(positives) >= 3,
        "two_negative_mechanisms": len(negatives) >= 2,
        "all_rows_kernel_and_meta_checked": unchecked == 0,
        "all_release_certificates_complete": not certificate_issue_counts,
        "zero_rubric_only_negatives": rubric_only == 0,
        "zero_residue": residue == 0,
        "zero_duplicate_or_conflicting_pairs": dedup.duplicate_count == 0
        and dedup.conflict_count == 0,
        "fixtures_passed": fixtures_ok is True,
        "replay_zero_lean_calls": bool(
            replay and replay.get("lean_requests") == 0 and replay.get("duplicate_rows") == 0
        ),
        "under_one_hour": performance_unavailable or 0 < wall <= 3600,
    }
    return {
        "run_id": run_id,
        "retained_total": len(records),
        "retained_by_operation": dict(sorted(by_mechanism.items())),
        "rich_mechanisms": sorted(rich),
        "rich_positive": sorted(positives),
        "rich_negative": sorted(negatives),
        "unchecked_rows": unchecked,
        "rubric_only_negatives": rubric_only,
        "certificate_issue_counts": dict(sorted(certificate_issue_counts.items())),
        "residue_rows": residue,
        "duplicates": dedup.duplicate_count,
        "conflicts": dedup.conflict_count,
        "wall_seconds": None if performance_unavailable else wall,
        "lean_requests": None if performance_unavailable else status.get("lean_requests"),
        "peak_process_tree_rss_bytes": (
            None if performance_unavailable else status.get("peak_process_tree_rss_bytes")
        ),
        "performance_evidence": "unavailable_overwritten_status"
        if performance_unavailable
        else "status_json",
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "checks": checks,
        "passed": all(checks.values()),
        "manual_inspection_required": True,
    }


# ------------------------------------------------------------------ 10K release gate


def release_report(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_id: str,
    sprint_end_utc: str,
    minimum_negative_pairs: int = 100,
    view: str = "raw",
    minimum_rows: int = 10000,
) -> dict[str, object]:
    from leanfaith.sft1.sprint import shortcut

    config = loaded.config
    paths = RunPaths(Path(config.output.staging_root), run_id)
    manifest = compact_run(repo_root, loaded, run_id=run_id, view=view)
    if view != "raw":
        paths.compacted = paths.compacted.parent / f"{run_id}_{view}"
    records: list[dict[str, Any]] = []
    for shard in sorted(paths.compacted.glob("shard-*")):
        rows = read_retained(shard / "rows.jsonl")
        sidecars = read_retained(shard / "sidecars.jsonl")
        records.extend(
            {"row": row, "sidecar": sidecar, "row_hash": "", "label": row["label"]}
            for row, sidecar in zip(rows, sidecars, strict=True)
        )
    unchecked = 0
    for record in records:
        op = str(record["row"]["operation_id"])
        evidence = record["sidecar"].get("evidence") or {}
        check = (
            (evidence.get("equivalence_proof") or {}).get("check")
            if op in POSITIVE_OPERATIONS
            else (evidence.get("refutation") or {}).get("check")
        )
        if not check or not check.get("meta_checked") or not check.get("kernel_checked"):
            unchecked += 1
    screens = shortcut.run_screens(records) if records else {"passed": False, "screens": []}
    status = read_json_object(paths.status) if paths.status.is_file() else {}
    operations = cast(dict[str, int], manifest["operations"])
    negatives = {op: n for op, n in operations.items() if op in NEGATIVE_OPERATIONS}
    useful_negatives = [op for op, n in negatives.items() if n >= minimum_negative_pairs]
    inventory_size = None
    run_manifest = read_json_object(paths.run_manifest) if paths.run_manifest.is_file() else {}
    if isinstance(run_manifest.get("root_order_size"), int):
        inventory_size = int(run_manifest["root_order_size"])
    roots_per_minute = float(status.get("roots_per_minute") or 0.0)
    projection: dict[str, object] = {"roots_per_minute": roots_per_minute}
    if inventory_size and roots_per_minute > 0:
        remaining = inventory_size - int(status.get("roots_considered") or 0)
        hours = remaining / roots_per_minute / 60
        end = datetime.datetime.fromisoformat(sprint_end_utc)
        now = datetime.datetime.now(datetime.UTC)
        window_hours = (end - now).total_seconds() / 3600
        projection.update(
            {
                "inventory_roots": inventory_size,
                "remaining_roots": remaining,
                "projected_hours_full_wave": round(hours, 2),
                "remaining_sprint_hours": round(window_hours, 2),
                "fits_remaining_window": hours <= window_hours,
            }
        )
    retained_rows = cast(int, manifest["retained_rows"])
    conflicting = cast(int, manifest["conflicting_classes_rejected"])
    conflicting_rows = cast(int, manifest.get("conflicting_rows_rejected", 0))
    checks = {
        "retained_at_least_minimum": retained_rows >= minimum_rows,
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "zero_duplicate_or_conflicting_pairs": conflicting == 0 and conflicting_rows == 0,
        "two_useful_negative_mechanisms": len(useful_negatives) >= 2,
        "shortcut_screens": bool(screens["passed"]),
    }
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "view": view,
        "minimum_rows": minimum_rows,
        "generated_at": utc_now(),
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "compaction": {k: v for k, v in manifest.items() if k != "shards"},
        "shards": len(cast(list[object], manifest["shards"])),
        "unchecked_rows": unchecked,
        "useful_negative_mechanisms": sorted(useful_negatives),
        "shortcut": screens,
        "projection": projection,
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_atomic(paths.compacted / "release_report.json", canonical_json_bytes(report) + b"\n")
    return report


# ------------------------------------------------------------------ targeting


def conclusion_relation(statement: str) -> str:
    """Cheap text classification of a statement's conclusion relation."""

    import re

    match = re.search(r"\)\s*:\s*(.*)$", statement) or re.search(r"\s:\s(.*)$", statement)
    conclusion = match.group(1) if match else statement
    for token, name in (("↔", "iff"), ("≠", "ne"), (" < ", "lt"), (" ≤ ", "le"), (" = ", "eq")):
        if token in conclusion:
            return name
    return "other"


_FINITE_TYPE_HINTS: tuple[str, ...] = (
    "Bool",
    "Fin ",
    "Fin(",
    "Unit",
    "Option ",
    "Option(",
    "Prod ",
)


def _top_level_colon_index(statement: str) -> int | None:
    opening = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    closing = set(opening.values())
    depth = 0
    for index, character in enumerate(statement):
        if character in opening:
            depth += 1
        elif character in closing:
            depth = max(0, depth - 1)
        elif character == ":" and depth == 0:
            return index
    return None


def _top_level_result(statement: str) -> str:
    """Return a source declaration's result after its outer signature colon."""

    index = _top_level_colon_index(statement)
    return statement[index + 1 :].strip() if index is not None else ""


def _has_direct_finite_exists(statement: str) -> bool:
    """Whether the declaration's outer result is an existential.

    This deliberately avoids matching existentials nested under an iff, an
    implication, or another connective.  The Lean engine remains the source of
    truth for the witness type and finite-instance check.
    """

    result = _top_level_result(" ".join(statement.split()))
    return result.startswith("∃")


def _has_forall_exists_suffix(statement: str) -> bool:
    """Conservative text hint for a final ``forall/exists`` dependency.

    Source declarations normally spell their outer forall as argument binders
    before the signature colon, so a direct existential plus an explicit data
    binder is included.  A result that writes the forall explicitly is also
    included.  Exact applicability is still reconstructed from the typed Expr.
    """

    compact = " ".join(statement.split())
    result = _top_level_result(compact)
    if result.startswith("∀"):
        return "∃" in result
    if not result.startswith("∃"):
        return False
    signature = compact[: compact.find(result)].rstrip(" :")
    return bool(re.search(r"\([^()]*(?::=|:)[^()]+\)", signature))


def _finite_target_hint(statement: str) -> bool:
    compact = " ".join(statement.split())
    return any(token in compact for token in _FINITE_TYPE_HINTS) or any(
        token in compact
        for token in (
            "[Fintype ",
            "[Finite ",
            "[Nontrivial ",
            " card ",
            "Fintype.card",
        )
    )


_MULTI_EXISTS_BINDER = re.compile(
    r"^∃\s+(?:\([^)]*\)|[A-Za-z_][\w₀-₉']*)\s+"
    r"(?:\([^)]*\)|[A-Za-z_][\w₀-₉']*)\s*(?::|,)"
)


_N26_INDEX_NAME = r"[^\W\d]\w*"
_N26_BOUNDARY_PATTERN = re.compile(
    rf"(?:^∀\s+|,\s*)({_N26_INDEX_NAME})\s+"
    rf"(?:<\s*({_N26_INDEX_NAME})|∈\s*(?:Finset\.)?range\s+({_N26_INDEX_NAME}))"
    r"\s*(?:,|→)"
)
_N26_DIRECT_BOUNDARY_PATTERN = re.compile(
    rf"^({_N26_INDEX_NAME})\s+"
    rf"(?:<\s*({_N26_INDEX_NAME})|∈\s*(?:Finset\.)?range\s+({_N26_INDEX_NAME}))"
    r"\s*→"
)
_DECLARATION_BINDER = re.compile(r"[({⦃]([^(){}⦃⦄]+)[)}⦄]")


def _declaration_binder_names(statement: str) -> set[str]:
    """Conservative names from binder groups before the outer result colon."""

    result_colon = _top_level_colon_index(statement)
    if result_colon is None:
        return set()
    signature = statement[:result_colon]
    names: set[str] = set()
    for match in _DECLARATION_BINDER.finditer(signature):
        left = match.group(1).split(":=", 1)[0].split(":", 1)[0]
        names.update(re.findall(_N26_INDEX_NAME, left))
    return names


def target_family_matches(statement: str, family: str) -> bool:
    """Conservative Lean-free shape filter for one Wave 3 mechanism.

    This is only a targeting hint.  It never creates an applicability judgment:
    every selected root is still elaborated and either receives an exact checked
    certificate or a typed fail-closed terminal from the Lean engine.
    """

    compact = " ".join(statement.split())
    key = family.upper()
    if key in {"N26", "N26_INCREMENT_BOUND_PROOF_V1"}:
        result = _top_level_result(compact)
        match = (
            _N26_BOUNDARY_PATTERN.search(result)
            if result.startswith("∀")
            else _N26_DIRECT_BOUNDARY_PATTERN.match(result)
        )
        if match is None:
            return False
        value = match.group(1)
        bound = match.group(2) or match.group(3)
        if value == bound:
            return False
        if not result.startswith("∀"):
            declared = _declaration_binder_names(compact)
            return value in declared and bound in declared
        return True
    if key in {"N30", "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1"}:
        return "∃!" not in compact and _has_direct_finite_exists(compact)
    if key in {"N29", "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1"}:
        return _has_forall_exists_suffix(compact)
    if key in {"N32", "N32_SWAP_ROLE_ORDER_PROOF_V1"}:
        return conclusion_relation(compact) in {"lt", "le"}
    if key in {"N31", "N31_DROP_REQUIRED_GUARD_PROOF_V1"}:
        guard_tokens = (" ≠ 0", "0 < ", "0 ≤ ", " ∈ ", " < ")
        return "→" in compact and any(token in compact for token in guard_tokens)
    if key in {"PRESERVING", "POSITIVE"}:
        return any(
            token in compact for token in ("↔", " = ", " ≠ ", " ∧ ", "Finset.range", " ∈ ", " + ")
        )
    raise SprintRunnerError(f"unknown target family {family!r}")


def target_family_priority(statement: str, family: str) -> int:
    """Put likely supported finite Wave 3 roots first without excluding misses."""

    key = family.upper()
    if key in {"N30", "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1"}:
        result = _top_level_result(" ".join(statement.split()))
        multi = _MULTI_EXISTS_BINDER.search(result) is not None
        if ((multi and "≠" in result) or result.count("≠") >= 2) and _finite_target_hint(statement):
            return 0
        if multi:
            return 1
        return 2 if _finite_target_hint(statement) else 3
    if key in {"N29", "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1"}:
        result = _top_level_result(" ".join(statement.split()))
        dependency_shape = result.startswith("∃") and any(token in result for token in ("≠", " = "))
        if dependency_shape and "[Nontrivial " in statement:
            return 0
        return 1 if _finite_target_hint(statement) and dependency_shape else 2
    if key in {"N26", "N26_INCREMENT_BOUND_PROOF_V1"}:
        result = _top_level_result(" ".join(statement.split()))
        return 0 if "∈ Finset.range" in result or "∈ range" in result else 1
    return 0


MIXED_TARGET_FAMILY_ORDER: tuple[str, ...] = (
    "N26",
    "N29",
    "N30",
    "N31",
    "N32",
    "PRESERVING",
)
_MIXED_TARGET_FAMILY_ALIASES: dict[str, str] = {
    "N26": "N26",
    "N26_INCREMENT_BOUND_PROOF_V1": "N26",
    "N29": "N29",
    "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1": "N29",
    "N30": "N30",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1": "N30",
    "N31": "N31",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1": "N31",
    "N32": "N32",
    "N32_SWAP_ROLE_ORDER_PROOF_V1": "N32",
    "POSITIVE": "PRESERVING",
    "PRESERVING": "PRESERVING",
}


def _validate_mixed_target_payload(payload: Mapping[str, Any], entries: list[object]) -> None:
    """Validate the builder-specific quota and family-assignment bindings."""

    salt = payload.get("selection_salt")
    if not isinstance(salt, str) or not salt or salt != salt.strip():
        raise SprintRunnerError("mixed target file has an invalid selection salt")
    if payload.get("stable_family_order") != list(MIXED_TARGET_FAMILY_ORDER):
        raise SprintRunnerError("mixed target file has a conflicting stable family order")
    family_order = payload.get("family_order")
    if (
        not isinstance(family_order, list)
        or not family_order
        or not all(isinstance(family, str) for family in family_order)
    ):
        raise SprintRunnerError("mixed target file has malformed family order")
    canonical_order = [
        family for family in MIXED_TARGET_FAMILY_ORDER if family in set(family_order)
    ]
    if family_order != canonical_order:
        raise SprintRunnerError("mixed target file family order is not canonical")

    quotas_value = payload.get("family_quotas")
    if not isinstance(quotas_value, list) or len(quotas_value) != len(family_order):
        raise SprintRunnerError("mixed target file has malformed family quotas")
    quotas: dict[str, int] = {}
    for expected_family, item in zip(family_order, quotas_value, strict=True):
        if not isinstance(item, Mapping) or set(item) != {"target_family", "quota"}:
            raise SprintRunnerError("mixed target file has malformed family quota entry")
        family = item.get("target_family")
        quota = item.get("quota")
        if family != expected_family or type(quota) is not int or quota < 1:
            raise SprintRunnerError("mixed target file quota disagrees with family order")
        quotas[expected_family] = quota

    assignments: list[dict[str, str]] = []
    assignment_families: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != {"name", "target_family"}:
            raise SprintRunnerError(
                f"mixed target roots[{index}] must contain exactly name and target_family"
            )
        family = entry.get("target_family")
        if not isinstance(family, str) or family not in quotas:
            raise SprintRunnerError(f"mixed target roots[{index}] has an unrequested family")
        name = _root_entry_name(entry, index=index)
        assignments.append({"name": name, "target_family": family})
        assignment_families.append(family)
    expected_families = [family for family in family_order for _ in range(quotas[family])]
    if assignment_families != expected_families:
        raise SprintRunnerError("mixed target root assignments do not match exact ordered quotas")
    if payload.get("assignments_sha256") != hash_canonical(assignments):
        raise SprintRunnerError("mixed target assignment hash mismatch")


def parse_family_quotas(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """Parse repeated ``FAMILY=COUNT`` values into the fixed selection order."""

    if not values:
        raise SprintRunnerError("mixed-targets requires at least one --family-quota")
    quotas: dict[str, int] = {}
    for value in values:
        family_text, separator, quota_text = value.partition("=")
        if not separator or not family_text or not quota_text:
            raise SprintRunnerError(f"invalid family quota {value!r}; expected FAMILY=COUNT")
        family = _MIXED_TARGET_FAMILY_ALIASES.get(family_text.upper())
        if family is None:
            raise SprintRunnerError(f"unsupported mixed target family {family_text!r}")
        if family in quotas:
            raise SprintRunnerError(f"duplicate mixed target family quota for {family}")
        try:
            quota = int(quota_text)
        except ValueError as exc:
            raise SprintRunnerError(f"family quota is not an integer: {value!r}") from exc
        if quota < 1 or str(quota) != quota_text:
            raise SprintRunnerError(f"family quota must be a canonical positive integer: {value!r}")
        quotas[family] = quota
    return tuple(
        (family, quotas[family]) for family in MIXED_TARGET_FAMILY_ORDER if family in quotas
    )


def write_mixed_family_targets(
    loaded: LoadedConfig[SprintConfig],
    *,
    family_quotas: Sequence[tuple[str, int]],
    selection_salt: str,
    out: Path,
) -> dict[str, object]:
    """Write deterministic, outcome-independent targets from overlapping family queues."""

    if not selection_salt or selection_salt != selection_salt.strip():
        raise SprintRunnerError("mixed target selection salt must be nonempty and unpadded")
    quota_by_family: dict[str, int] = {}
    for family, quota in family_quotas:
        if family not in MIXED_TARGET_FAMILY_ORDER:
            raise SprintRunnerError(f"unsupported mixed target family {family!r}")
        if family in quota_by_family:
            raise SprintRunnerError(f"duplicate mixed target family quota for {family}")
        if type(quota) is not int or quota < 1:
            raise SprintRunnerError(f"mixed target quota for {family} must be positive")
        quota_by_family[family] = quota
    if not quota_by_family:
        raise SprintRunnerError("mixed targets require at least one family quota")
    ordered_quotas = tuple(
        (family, quota_by_family[family])
        for family in MIXED_TARGET_FAMILY_ORDER
        if family in quota_by_family
    )

    config = loaded.config
    inventory_path, inventory_sha256 = _pinned_inventory_identity(loaded)
    statements: dict[str, str] = {}
    for row in load_inventory(inventory_path):
        name = str(row["name"])
        statements.setdefault(name, str(row["statement"]))

    used: set[str] = set()
    assignments: list[dict[str, str]] = []
    eligible_counts: dict[str, int] = {}
    available_after_dedup: dict[str, int] = {}
    for family, quota in ordered_quotas:
        queue = [
            name
            for name, statement in statements.items()
            if target_family_matches(statement, family)
        ]
        queue.sort(
            key=lambda name: (
                target_family_priority(statements[name], family),
                hash_canonical(
                    [
                        selection_salt,
                        config.project.project_id,
                        config.project.project_revision,
                        family,
                        name,
                    ]
                ),
                name,
            )
        )
        eligible_counts[family] = len(queue)
        available = [name for name in queue if name not in used]
        available_after_dedup[family] = len(available)
        if len(available) < quota:
            raise SprintRunnerError(
                f"mixed target quota {family}={quota} cannot be met after deterministic "
                f"cross-family dedup; only {len(available)} roots remain"
            )
        for name in available[:quota]:
            assignments.append({"name": name, "target_family": family})
            used.add(name)

    names = [assignment["name"] for assignment in assignments]
    family_order = [family for family, _ in ordered_quotas]
    quota_payload = [{"target_family": family, "quota": quota} for family, quota in ordered_quotas]
    payload = {
        "schema_version": 1,
        "target_kind": "lean_free_mixed_family_candidate_only",
        "project_id": config.project.project_id,
        "project_revision": config.project.project_revision,
        "inventory_sha256": inventory_sha256,
        "selection_salt": selection_salt,
        "stable_family_order": list(MIXED_TARGET_FAMILY_ORDER),
        "family_order": family_order,
        "family_quotas": quota_payload,
        "eligible_counts_before_dedup": eligible_counts,
        "available_counts_after_prior_family_dedup": available_after_dedup,
        "count": len(assignments),
        "roots_sha256": hash_canonical(names),
        "assignments_sha256": hash_canonical(assignments),
        "roots": assignments,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    selection = read_roots_file(out, loaded, expected_file_sha256=hash_file(out))
    if selection.roots != tuple(names):
        raise SprintRunnerError("mixed target roots failed strict roots-file round-trip")
    return {key: value for key, value in payload.items() if key != "roots"}


def write_family_targets(
    loaded: LoadedConfig[SprintConfig], *, family: str, out: Path, limit: int | None = None
) -> dict[str, object]:
    """Write a content-bound, deterministic real-root candidate set."""

    config = loaded.config
    inventory_dir = Path(config.inventory.root) / config.project.project_revision
    inventory_path = inventory_dir / "inventory.jsonl"
    rows = load_inventory(inventory_path)
    by_name: dict[str, str] = {}
    for row in rows:
        name = str(row["name"])
        statement = str(row["statement"])
        if name not in by_name and target_family_matches(statement, family):
            by_name[name] = statement
    names = sorted(
        by_name,
        key=lambda name: (
            target_family_priority(by_name[name], family),
            hash_canonical([config.inventory.order_salt, family, name]),
        ),
    )
    eligible_count = len(names)
    if limit is not None:
        names = names[:limit]
    payload = {
        "schema_version": 1,
        "target_kind": "lean_free_shape_candidate_only",
        "family": family,
        "project_id": config.project.project_id,
        "project_revision": config.project.project_revision,
        "inventory_sha256": hash_file(inventory_path),
        "eligible_count": eligible_count,
        "count": len(names),
        "roots_sha256": hash_canonical(names),
        "roots": names,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


def _check_passed(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("meta_checked") is True
        and value.get("kernel_checked") is True
    )


def write_certified_targets(
    *,
    compacted_dir: Path,
    operation_id: str,
    out: Path,
    selection_salt: str,
    limit: int | None = None,
) -> dict[str, object]:
    """Extract roots from an immutable proof-certified compacted SFT1 artifact.

    The source manifest and every sidecar shard are hash-checked before roots are
    admitted.  This is used to start Wave 3 N31 from the exact 346 previously
    certified roots, without rerunning or mutating that historical release.
    """

    manifest_path = compacted_dir / "manifest.json"
    manifest = read_json_object(manifest_path)
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise SprintRunnerError("certified target source has no manifest shards")
    roots: set[str] = set()
    matching_records = 0
    for shard in shards:
        if not isinstance(shard, Mapping) or shard.get("complete") is not True:
            raise SprintRunnerError("certified target source contains an incomplete shard")
        number = shard.get("shard")
        expected = shard.get("sidecars_sha256")
        if type(number) is not int or not isinstance(expected, str):
            raise SprintRunnerError("certified target shard metadata is malformed")
        sidecars_path = compacted_dir / f"shard-{number:04d}" / "sidecars.jsonl"
        if hash_file(sidecars_path) != expected:
            raise SprintRunnerError(f"certified target sidecar hash mismatch: {sidecars_path}")
        with sidecars_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                sidecar = json.loads(line)
                if sidecar.get("operation_id") != operation_id:
                    continue
                evidence = sidecar.get("evidence")
                if sidecar.get("label") is not False or not isinstance(evidence, Mapping):
                    raise SprintRunnerError("certified negative source has inconsistent polarity")
                refutation = evidence.get("refutation")
                source_check = evidence.get("source_proof_check")
                if not isinstance(refutation, Mapping) or not _check_passed(
                    refutation.get("check")
                ):
                    raise SprintRunnerError(
                        "certified negative source lacks an exact checked refutation"
                    )
                if not _check_passed(source_check):
                    raise SprintRunnerError(
                        "certified negative source lacks a checked source proof"
                    )
                root_name = sidecar.get("root_name")
                if not isinstance(root_name, str) or not root_name:
                    raise SprintRunnerError("certified negative source lacks a root name")
                roots.add(root_name)
                matching_records += 1
    names = sorted(roots, key=lambda name: hash_canonical([selection_salt, operation_id, name]))
    eligible_count = len(names)
    if limit is not None:
        names = names[:limit]
    payload = {
        "schema_version": 1,
        "target_kind": "prior_exact_proof_certified_roots",
        "operation_id": operation_id,
        "source_compacted_dir": str(compacted_dir),
        "source_manifest_sha256": hash_file(manifest_path),
        "source_run_id": manifest.get("run_id"),
        "matching_records": matching_records,
        "eligible_count": eligible_count,
        "selection_salt": selection_salt,
        "count": len(names),
        "roots_sha256": hash_canonical(names),
        "roots": names,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


def write_targets(
    loaded: LoadedConfig[SprintConfig], *, relation: str, out: Path, limit: int | None = None
) -> dict[str, object]:
    config = loaded.config
    inventory_dir = Path(config.inventory.root) / config.project.project_revision
    rows = load_inventory(inventory_dir / "inventory.jsonl")
    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        name = str(row["name"])
        if name in seen:
            continue
        seen.add(name)
        if conclusion_relation(str(row["statement"])) == relation:
            names.append(name)
    names.sort(key=lambda name: hash_canonical([config.inventory.order_salt, relation, name]))
    if limit is not None:
        names = names[:limit]
    payload = {
        "schema_version": 1,
        "relation": relation,
        "inventory_sha256": hash_file(inventory_dir / "inventory.jsonl"),
        "count": len(names),
        "roots_sha256": hash_canonical(names),
        "roots": names,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


def roots_from_run(loaded: LoadedConfig[SprintConfig], run_id: str) -> list[str]:
    paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
    names: list[str] = []
    seen: set[str] = set()
    for record in Journal(paths.journal).read():
        if record.get("kind") == "root" and str(record["root"]) not in seen:
            seen.add(str(record["root"]))
            names.append(str(record["root"]))
    return names


# ------------------------------------------------------- Wave 3 mixed gate


_MIXED_PROJECTS = frozenset({"mathlib", "physlib", "cslib"})
_TERMINAL_STATUSES = frozenset({"retained", "not_applicable", "rejected", "error"})


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SprintRunnerError(f"cannot read mixed-gate input {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SprintRunnerError(f"malformed JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise SprintRunnerError(
                f"mixed-gate JSONL row is not an object at {path}:{line_number}"
            )
        records.append(value)
    return records


def _mixed_run_receipt(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    required = {
        "manifest": run_dir / "run.json",
        "status": run_dir / "status.json",
        "journal": run_dir / "journal.jsonl",
        "retained": run_dir / "retained.jsonl",
        "replay": run_dir / "replay_report.json",
    }
    missing_files = [str(path) for path in required.values() if not path.is_file()]
    if missing_files:
        raise SprintRunnerError("mixed-gate run receipt is incomplete: " + ", ".join(missing_files))
    manifest = read_json_object(required["manifest"])
    status = read_json_object(required["status"])
    replay = read_json_object(required["replay"])
    journal = _read_jsonl_objects(required["journal"])
    retained = _read_jsonl_objects(required["retained"])

    project = manifest.get("project")
    if not isinstance(project, Mapping):
        raise SprintRunnerError(f"mixed-gate run {run_dir} lacks project identity")
    project_id = project.get("project_id")
    project_revision = project.get("project_revision")
    run_id = manifest.get("run_id")
    if not isinstance(project_id, str) or not isinstance(project_revision, str):
        raise SprintRunnerError(f"mixed-gate run {run_dir} has malformed project identity")
    if not isinstance(run_id, str) or not run_id:
        raise SprintRunnerError(f"mixed-gate run {run_dir} has no run id")
    operations_value = manifest.get("operations")
    if not isinstance(operations_value, list) or not operations_value:
        raise SprintRunnerError(f"mixed-gate run {run_id!r} has no selected operations")
    operations = tuple(operations_value)
    if not all(isinstance(operation, str) for operation in operations):
        raise SprintRunnerError(f"mixed-gate run {run_id!r} has malformed operations")
    if len(operations) != len(set(operations)) or any(op not in OPERATIONS for op in operations):
        raise SprintRunnerError(f"mixed-gate run {run_id!r} has invalid selected operations")
    roots_value = manifest.get("explicit_roots")
    if not isinstance(roots_value, list) or not all(isinstance(root, str) for root in roots_value):
        raise SprintRunnerError(f"mixed-gate run {run_id!r} lacks explicit string roots")
    roots = tuple(roots_value)
    if not roots or len(roots) != len(set(roots)):
        raise SprintRunnerError(f"mixed-gate run {run_id!r} has empty or duplicate roots")

    roots_file = manifest.get("roots_file_identity")
    roots_file_bound = isinstance(roots_file, Mapping) and all(
        roots_file.get(field) == expected
        for field, expected in {
            "project_id": project_id,
            "project_revision": project_revision,
            "count": len(roots),
            "roots_sha256": hash_canonical(roots),
        }.items()
    )
    if roots_file_bound and isinstance(roots_file, Mapping):
        roots_file_bound = all(
            isinstance(roots_file.get(field), str)
            and _SHA256.fullmatch(str(roots_file[field])) is not None
            for field in ("file_sha256", "metadata_sha256", "inventory_sha256")
        )
    explicit_hash_ok = manifest.get("explicit_roots_sha256") == hash_canonical(roots)

    terminal_records = [record for record in journal if record.get("kind") == "terminal"]
    terminal_counts: dict[tuple[str, str], int] = {}
    terminal_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    unexpected_terminals = 0
    invalid_terminal_statuses = 0
    error_terminals = 0
    expected_cells = {(root, operation) for root in roots for operation in operations}
    for terminal in terminal_records:
        cell = (str(terminal.get("root", "")), str(terminal.get("operation_id", "")))
        terminal_counts[cell] = terminal_counts.get(cell, 0) + 1
        terminal_by_cell.setdefault(cell, terminal)
        if cell not in expected_cells:
            unexpected_terminals += 1
        status_value = terminal.get("status")
        if status_value not in _TERMINAL_STATUSES:
            invalid_terminal_statuses += 1
        elif status_value == "error":
            error_terminals += 1
    missing_cells = sum(1 for cell in expected_cells if terminal_counts.get(cell, 0) == 0)
    duplicate_cells = sum(max(0, count - 1) for count in terminal_counts.values())
    qualified_roots = sum(
        all(
            terminal_counts.get((root, operation)) == 1
            and terminal_by_cell[(root, operation)].get("status")
            in (_TERMINAL_STATUSES - {"error"})
            for operation in operations
        )
        for root in roots
    )

    record_cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    record_issues = 0
    for record in retained:
        sidecar = record.get("sidecar")
        row = record.get("row")
        if not isinstance(sidecar, Mapping) or not isinstance(row, Mapping):
            record_issues += 1
            continue
        cell = (str(sidecar.get("root_name", "")), str(sidecar.get("operation_id", "")))
        record_cells.setdefault(cell, []).append(record)
        labels = (row.get("label"), sidecar.get("label"), record.get("label"))
        pair_ids = (row.get("pair_id"), sidecar.get("pair_id"))
        if cell not in expected_cells or (
            row.get("operation_id") != cell[1]
            or record.get("operation_id") != cell[1]
            or record.get("root_name") != cell[0]
            or not isinstance(row.get("root_id"), str)
            or row.get("root_id") != sidecar.get("root_id")
            or not all(type(label) is bool for label in labels)
            or labels[0] != labels[1]
            or labels[0] != labels[2]
            or not all(isinstance(pair_id, str) and pair_id for pair_id in pair_ids)
            or pair_ids[0] != pair_ids[1]
        ):
            record_issues += 1
        sidecar_project = sidecar.get("project")
        if not isinstance(sidecar_project, Mapping) or (
            sidecar_project.get("project_id") != project_id
            or sidecar_project.get("project_revision") != project_revision
        ):
            record_issues += 1
    retained_cells = {
        cell for cell, terminal in terminal_by_cell.items() if terminal.get("status") == "retained"
    }
    joined_cells = {
        cell
        for cell, records in record_cells.items()
        if len(records) == 1 and cell in retained_cells
    }
    record_join_defects = (
        len(retained_cells.difference(joined_cells))
        + len(set(record_cells).difference(retained_cells))
        + sum(max(0, len(records) - 1) for records in record_cells.values())
    )
    for cell in joined_cells:
        terminal = terminal_by_cell[cell]
        record = record_cells[cell][0]
        row = record.get("row")
        if not isinstance(row, Mapping) or terminal.get("pair_id") != row.get("pair_id"):
            record_join_defects += 1
        if terminal.get("unordered_pair_key") != record.get("unordered_pair_key"):
            record_join_defects += 1
        if terminal.get("row_hash") is not None and terminal.get("row_hash") != record.get(
            "row_hash"
        ):
            record_join_defects += 1

    replay_ok = (
        replay.get("run_id") == run_id
        and replay.get("lean_requests") == 0
        and replay.get("duplicate_rows") == 0
        and replay.get("retained_before") == len(retained)
        and replay.get("retained_after") == len(retained)
        and replay.get("roots_considered") == len(roots)
    )
    status_ok = (
        status.get("run_id") == run_id
        and status.get("final") is True
        and status.get("roots_considered") == len(roots)
        and status.get("retained_total") == len(retained)
    )
    roots_this_process = status.get("roots_this_process")
    resume_observed = type(roots_this_process) is int and 0 <= roots_this_process < len(roots)
    receipt = {
        "run_dir": str(run_dir),
        "run_id": run_id,
        "project_id": project_id,
        "project_revision": project_revision,
        "operations": list(operations),
        "selected_roots": len(roots),
        "qualified_roots": qualified_roots,
        "retained_rows": len(retained),
        "terminal_counts": {
            "expected": len(expected_cells),
            "observed": len(terminal_records),
            "missing": missing_cells,
            "duplicate": duplicate_cells,
            "unexpected": unexpected_terminals,
            "invalid_status": invalid_terminal_statuses,
            "error": error_terminals,
        },
        "record_issues": record_issues,
        "record_join_defects": record_join_defects,
        "roots_file_bound": roots_file_bound,
        "explicit_roots_hash_matches": explicit_hash_ok,
        "generation_status_complete": status_ok,
        "resume_observed": resume_observed,
        "replay_zero_call": replay_ok,
        "input_sha256": {name: hash_file(path) for name, path in required.items()},
    }
    return receipt, retained


def _wave3_shortcut_screens(records: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """Run the existing v3 release screens on single-hop Wave 3 families.

    V3 calls the polarity-paired grouping field ``core_family``.  Single-hop
    records predate that name and carry the same grouping as ``mechanism``;
    normalize a copy so the existing thresholds and classifiers are reused
    unchanged.
    """

    if not records:
        return {"rows": 0, "screens": [], "passed": False, "reason": "no_valid_records"}
    normalized: list[dict[str, Any]] = []
    for record in records:
        sidecar = dict(cast(Mapping[str, Any], record["sidecar"]))
        family = sidecar.get("core_family") or sidecar.get("mechanism")
        if not isinstance(family, str) or not family:
            family = str(sidecar.get("operation_id", "unassigned"))
        sidecar["core_family"] = family
        normalized.append({**record, "sidecar": sidecar})
    try:
        return run_screens_v3(normalized)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        return {
            "rows": len(normalized),
            "screens": [],
            "passed": False,
            "reason": f"screen_input_invalid:{type(exc).__name__}:{str(exc)[:200]}",
        }


def wave3_mixed_gate_report(
    run_dirs: Sequence[Path], *, expected_roots: int = 200
) -> dict[str, Any]:
    """Validate a three-project Wave 3 gate entirely from durable receipts."""

    if expected_roots < 1:
        raise SprintRunnerError("mixed-gate expected roots must be positive")
    if len(run_dirs) != 3 or len({path.resolve() for path in run_dirs}) != 3:
        raise SprintRunnerError("mixed-gate requires three distinct project run directories")
    receipts: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        receipt, run_records = _mixed_run_receipt(run_dir.resolve())
        receipts.append(receipt)
        records.extend(run_records)

    project_ids = [str(receipt["project_id"]) for receipt in receipts]
    selected_roots = sum(int(receipt["selected_roots"]) for receipt in receipts)
    qualified_roots = sum(int(receipt["qualified_roots"]) for receipt in receipts)
    terminal_totals = {
        key: sum(int(receipt["terminal_counts"][key]) for receipt in receipts)
        for key in (
            "expected",
            "observed",
            "missing",
            "duplicate",
            "unexpected",
            "invalid_status",
            "error",
        )
    }

    pair_ids: list[str] = []
    row_hashes: list[str] = []
    self_pairs = 0
    residue_rows = 0
    malformed_stable_ids = 0
    certificate_issues: dict[str, int] = {}
    negative_yields: dict[str, int] = {}
    n25_rows = 0
    diagnostic_records: list[dict[str, Any]] = []
    for record in records:
        row = record.get("row")
        sidecar = record.get("sidecar")
        if not isinstance(row, Mapping) or not isinstance(sidecar, Mapping):
            malformed_stable_ids += 1
            continue
        pair_id = row.get("pair_id")
        sidecar_pair_id = sidecar.get("pair_id")
        row_hash = record.get("row_hash")
        unordered_key = record.get("unordered_pair_key")
        label = record.get("label")
        reference_value = row.get("reference")
        candidate_value = row.get("candidate")
        root_id = row.get("root_id")
        sidecar_root_id = sidecar.get("root_id")
        if (
            not isinstance(pair_id, str)
            or _PAIR_ID.fullmatch(pair_id) is None
            or pair_id != sidecar_pair_id
        ):
            malformed_stable_ids += 1
        else:
            pair_ids.append(pair_id)
        if not isinstance(row_hash, str) or _SHA256.fullmatch(row_hash) is None:
            malformed_stable_ids += 1
        else:
            row_hashes.append(row_hash)
        if not isinstance(unordered_key, str) or _SHA256.fullmatch(unordered_key) is None:
            malformed_stable_ids += 1
        if type(label) is not bool or row.get("label") is not label:
            malformed_stable_ids += 1
        if not isinstance(root_id, str) or not root_id or root_id != sidecar_root_id:
            malformed_stable_ids += 1
        if not isinstance(reference_value, str) or not isinstance(candidate_value, str):
            malformed_stable_ids += 1
            reference = ""
            candidate = ""
        else:
            reference = reference_value
            candidate = candidate_value
        if (
            isinstance(pair_id, str)
            and _PAIR_ID.fullmatch(pair_id) is not None
            and pair_id == sidecar_pair_id
            and isinstance(row_hash, str)
            and _SHA256.fullmatch(row_hash) is not None
            and isinstance(unordered_key, str)
            and _SHA256.fullmatch(unordered_key) is not None
            and type(label) is bool
            and row.get("label") is label
            and isinstance(root_id, str)
            and bool(root_id)
            and root_id == sidecar_root_id
            and isinstance(reference_value, str)
            and isinstance(candidate_value, str)
        ):
            diagnostic_records.append(record)
        repr_payload = sidecar.get("repr")
        same_render_hash = False
        if isinstance(repr_payload, Mapping):
            reference_repr = repr_payload.get("reference")
            candidate_repr = repr_payload.get("candidate")
            if isinstance(reference_repr, Mapping) and isinstance(candidate_repr, Mapping):
                reference_record = reference_repr.get("record")
                candidate_record = candidate_repr.get("record")
                if isinstance(reference_record, Mapping) and isinstance(candidate_record, Mapping):
                    reference_render_hash = reference_record.get("rendered_goal_hash")
                    candidate_render_hash = candidate_record.get("rendered_goal_hash")
                    same_render_hash = (
                        isinstance(reference_render_hash, str)
                        and bool(reference_render_hash)
                        and reference_render_hash == candidate_render_hash
                    )
        if reference == candidate or same_render_hash:
            self_pairs += 1
        if residue_violation(reference) is not None or residue_violation(candidate) is not None:
            residue_rows += 1
        for issue in release_certificate_issues(record):
            certificate_issues[issue] = certificate_issues.get(issue, 0) + 1
        operation = str(sidecar.get("operation_id", record.get("operation_id", "")))
        if operation in NEGATIVE_OPERATIONS:
            negative_yields[operation] = negative_yields.get(operation, 0) + 1
        if operation == "N25_TOGGLE_EQ_NE_PROOF_V1":
            n25_rows += 1

    duplicate_pair_ids = len(pair_ids) - len(set(pair_ids))
    duplicate_row_hashes = len(row_hashes) - len(set(row_hashes))
    dedup = deduplicate(diagnostic_records)
    available_negatives = sorted(
        {
            operation
            for receipt in receipts
            for operation in receipt["operations"]
            if operation in NEGATIVE_OPERATIONS
        }
    )
    useful_negatives = sorted(operation for operation, count in negative_yields.items() if count)
    required_useful_negatives = min(3, len(available_negatives))
    n25_share = n25_rows / len(records) if records else 0.0
    pair_delta = pairwise_shortcut_diagnostics(diagnostic_records)
    shortcut_screens = _wave3_shortcut_screens(diagnostic_records)
    shortcut_results = {
        str(screen.get("name")): screen
        for screen in cast(list[dict[str, Any]], shortcut_screens.get("screens", []))
        if isinstance(screen, Mapping)
    }
    checks = {
        "exact_three_pinned_projects": set(project_ids) == _MIXED_PROJECTS
        and len(project_ids) == len(set(project_ids)),
        "exactly_expected_selected_roots": selected_roots == expected_roots,
        "exactly_expected_qualified_roots": qualified_roots == expected_roots,
        "roots_files_content_bound": all(receipt["roots_file_bound"] for receipt in receipts),
        "explicit_root_hashes_match": all(
            receipt["explicit_roots_hash_matches"] for receipt in receipts
        ),
        "selected_operation_terminals_complete": terminal_totals["missing"] == 0
        and terminal_totals["duplicate"] == 0
        and terminal_totals["unexpected"] == 0
        and terminal_totals["invalid_status"] == 0
        and terminal_totals["observed"] == terminal_totals["expected"],
        "zero_error_terminals": terminal_totals["error"] == 0,
        "generation_receipts_complete": all(
            receipt["generation_status_complete"] for receipt in receipts
        ),
        "retained_terminal_record_join_complete": all(
            receipt["record_join_defects"] == 0 and receipt["record_issues"] == 0
            for receipt in receipts
        ),
        "zero_certificate_defects": not certificate_issues,
        "zero_self_pairs": self_pairs == 0,
        "zero_residue": residue_rows == 0,
        "zero_duplicate_stable_ids": malformed_stable_ids == 0
        and duplicate_pair_ids == 0
        and duplicate_row_hashes == 0,
        "zero_duplicate_or_conflicting_pairs": dedup.duplicate_count == 0
        and dedup.conflict_count == 0,
        "three_useful_negative_families_when_available": len(useful_negatives)
        >= required_useful_negatives,
        "n25_share_at_most_25_percent": n25_share <= 0.25,
        "forced_resume_observed": any(receipt["resume_observed"] for receipt in receipts),
        "all_replays_zero_call": all(receipt["replay_zero_call"] for receipt in receipts),
        "pair_delta_diagnostics_recorded": pair_delta.get("rows") == len(records)
        and bool(pair_delta.get("rules")),
        "candidate_only_shortcut_screen_passed": shortcut_results.get("candidate_only", {}).get(
            "passed"
        )
        is True,
        "reference_only_shortcut_screen_passed": shortcut_results.get("reference_only", {}).get(
            "passed"
        )
        is True,
        "existing_shortcut_screen_contract_passed": shortcut_screens.get("passed") is True,
    }
    return {
        "schema_version": 1,
        "gate": "sft1_wave3_mixed_200_v1",
        "expected_roots": expected_roots,
        "selected_roots": selected_roots,
        "qualified_roots": qualified_roots,
        "retained_rows": len(records),
        "projects": sorted(project_ids),
        "receipts": sorted(receipts, key=lambda receipt: str(receipt["project_id"])),
        "terminal_counts": terminal_totals,
        "negative_family_yields": dict(sorted(negative_yields.items())),
        "available_negative_families": available_negatives,
        "useful_negative_families": useful_negatives,
        "required_useful_negative_families": required_useful_negatives,
        "n25_rows": n25_rows,
        "n25_share": round(n25_share, 6),
        "certificate_issue_counts": dict(sorted(certificate_issues.items())),
        "self_pairs": self_pairs,
        "residue_rows": residue_rows,
        "malformed_stable_ids": malformed_stable_ids,
        "duplicate_pair_ids": duplicate_pair_ids,
        "duplicate_row_hashes": duplicate_row_hashes,
        "duplicate_pairs": dedup.duplicate_count,
        "conflicting_pairs": dedup.conflict_count,
        "pair_delta_diagnostics": pair_delta,
        "shortcut_screens": shortcut_screens,
        "input_receipts_sha256": hash_canonical(
            [
                receipt["input_sha256"]
                for receipt in sorted(receipts, key=lambda item: str(item["project_id"]))
            ]
        ),
        "checks": checks,
        "passed": all(checks.values()),
    }


# ------------------------------------------------------------------ fixtures


def latest_fixtures_report(loaded: LoadedConfig[SprintConfig]) -> Path:
    runs = Path(loaded.config.output.staging_root) / "runs"
    candidates = sorted(
        runs.glob("fixtures-*/fixtures_report.json"), key=lambda path: path.stat().st_mtime
    )
    return candidates[-1] if candidates else runs / "fixtures-none" / "fixtures_report.json"


def run_fixtures(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    owner_session: str = "claude-sft1-sprint",
) -> dict[str, object]:
    config = loaded.config
    roots = sorted({fixture.root for fixture in config.fixtures})
    pins = project_pins(config)
    context = engine_module.build_compile_context(repo_root, pins)
    identity = engine_module.engine_identity(repo_root, pins, context)
    fixture_identity = hash_canonical(
        [identity.source_sha256, loaded.config_hash, hash_file(Path(__file__))]
    )
    run_id = f"fixtures-{fixture_identity[:12]}"
    runner = SprintRunner(
        repo_root,
        loaded,
        run_id=run_id,
        explicit_roots=roots,
        owner_session=owner_session,
        allow_fixture_roots=True,
    )
    runner.run()
    terminals: dict[tuple[str, str], dict[str, Any]] = {}
    for record in runner.journal.read():
        if record.get("kind") == "terminal":
            terminals[(str(record["root"]), str(record["operation_id"]))] = record
    results = []
    for fixture in config.fixtures:
        terminal = terminals.get((fixture.root, fixture.operation_id))
        observed_status = None if terminal is None else str(terminal.get("status"))
        observed_reason = "" if terminal is None else str(terminal.get("reason", ""))
        passed = observed_status == fixture.expect_status and observed_reason.startswith(
            fixture.expect_reason_prefix
        )
        results.append(
            {
                "root": fixture.root,
                "operation_id": fixture.operation_id,
                "expect_status": fixture.expect_status,
                "expect_reason_prefix": fixture.expect_reason_prefix,
                "observed_status": observed_status,
                "observed_reason": observed_reason,
                "passed": passed,
            }
        )
    covered_success = {
        str(r["operation_id"]) for r in results if r["expect_status"] == "retained" and r["passed"]
    }
    covered_rejection = {
        str(r["operation_id"]) for r in results if r["expect_status"] != "retained" and r["passed"]
    }
    waived = {waiver.operation_id: waiver.reason for waiver in config.fixtures_success_waivers}
    required_operations = set(config.engine.operations)
    required_success = required_operations - set(waived)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "results": results,
        "success_covered": sorted(covered_success),
        "rejection_covered": sorted(covered_rejection),
        "success_waived": waived,
        "all_operations_covered": covered_success >= required_success
        and covered_rejection == required_operations,
        "passed": all(r["passed"] for r in results)
        and covered_success >= required_success
        and covered_rejection == required_operations,
        "status": runner.summary(),
    }
    write_atomic(
        runner.paths.run_dir / "fixtures_report.json", canonical_json_bytes(report) + b"\n"
    )
    return report


# ------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "fixtures",
            "run",
            "replay",
            "status",
            "compact",
            "inspect",
            "gate100",
            "gate10k",
            "compact-windows",
            "targets",
            "mixed-targets",
            "certified-targets",
            "mixed-gate",
        ),
    )
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id", default="roots100")
    parser.add_argument("--max-roots", type=int)
    parser.add_argument("--target-retained", type=int)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--shard-size", type=int)
    parser.add_argument("--owner-session", default="claude-sft1-sprint")
    parser.add_argument("--sprint-end-utc", default="2026-09-04T21:30:00+00:00")
    parser.add_argument("--roots-per-window", type=int, default=2000)
    parser.add_argument("--view", choices=("raw", "balanced"), default="raw")
    parser.add_argument("--operations", help="comma-separated operation IDs for this run")
    parser.add_argument("--roots-file", type=Path, help="JSON targets file with a roots list")
    parser.add_argument("--roots-file-sha256", help="expected exact SHA-256 of --roots-file")
    parser.add_argument("--roots-from-run", help="reuse the root list of an existing run")
    parser.add_argument(
        "--project-run",
        action="append",
        type=Path,
        help="mixed-gate: one project run directory (repeat for Mathlib, Physlib, CSLib)",
    )
    parser.add_argument("--expected-roots", type=int, default=200)
    parser.add_argument("--relation", default="ne", help="targets: conclusion relation")
    parser.add_argument("--family", help="targets: Wave 3 mechanism family shape")
    parser.add_argument(
        "--family-quota",
        action="append",
        default=[],
        help="mixed-targets: deterministic FAMILY=COUNT quota (repeat per family)",
    )
    parser.add_argument(
        "--source-compacted", type=Path, help="certified-targets: immutable compacted source"
    )
    parser.add_argument("--operation", help="certified-targets: exact negative operation ID")
    parser.add_argument("--selection-salt", default="sft1-wave3-certified-targets-v1")
    parser.add_argument("--out", type=Path, help="targets: output JSON path")
    parser.add_argument("--limit", type=int, help="targets: maximum roots")
    parser.add_argument("--minimum-rows", type=int, default=10000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.command == "mixed-gate":
        if len(args.project_run or []) != 3:
            raise SprintRunnerError("mixed-gate requires exactly three --project-run directories")
        report = wave3_mixed_gate_report(
            [path.resolve() for path in args.project_run], expected_roots=args.expected_roots
        )
        if args.out is not None:
            write_atomic(args.out, canonical_json_bytes(report) + b"\n")
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["passed"] else 1
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    if args.command == "validate":
        pins = project_pins(loaded.config)
        context = engine_module.build_compile_context(repo_root, pins)
        identity = engine_module.engine_identity(repo_root, pins, context)
        print(json.dumps({"config_hash": loaded.config_hash, "engine": identity.to_dict()}))
        return 0
    if args.command == "fixtures":
        report = run_fixtures(repo_root, loaded, owner_session=args.owner_session)
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "status"}, ensure_ascii=False, indent=1
            )
        )
        return 0 if report["passed"] else 1
    if args.command == "targets":
        out = (
            args.out
            or Path(loaded.config.output.staging_root)
            / "targets"
            / f"{args.family or args.relation}.json"
        )
        report = (
            write_family_targets(loaded, family=args.family, out=out, limit=args.limit)
            if args.family
            else write_targets(loaded, relation=args.relation, out=out, limit=args.limit)
        )
        print(json.dumps(report))
        return 0
    if args.command == "mixed-targets":
        if args.out is None:
            raise SprintRunnerError("mixed-targets requires --out")
        report = write_mixed_family_targets(
            loaded,
            family_quotas=parse_family_quotas(args.family_quota),
            selection_salt=args.selection_salt,
            out=args.out.resolve(),
        )
        print(json.dumps(report))
        return 0
    if args.command == "certified-targets":
        if args.source_compacted is None or args.operation is None or args.out is None:
            raise SprintRunnerError(
                "certified-targets requires --source-compacted, --operation, and --out"
            )
        print(
            json.dumps(
                write_certified_targets(
                    compacted_dir=args.source_compacted.resolve(),
                    operation_id=args.operation,
                    out=args.out,
                    selection_salt=args.selection_salt,
                    limit=args.limit,
                )
            )
        )
        return 0
    if args.command in {"run", "replay"}:
        max_roots = args.max_roots
        target_retained = args.target_retained
        explicit: list[str] | None = None
        roots_file: RootsFileSelection | None = None
        if args.roots_file is not None:
            roots_file = read_roots_file(
                args.roots_file.resolve(),
                loaded,
                expected_file_sha256=args.roots_file_sha256,
            )
            explicit = list(roots_file.roots)
        elif args.roots_file_sha256 is not None:
            raise SprintRunnerError("--roots-file-sha256 requires --roots-file")
        elif args.roots_from_run:
            explicit = roots_from_run(loaded, args.roots_from_run)
        operations = args.operations.split(",") if args.operations else None
        run_manifest_path = RunPaths(
            Path(loaded.config.output.staging_root), args.run_id
        ).run_manifest
        if args.command == "replay" and run_manifest_path.is_file():
            recorded = read_json_object(run_manifest_path)
            if max_roots is None and isinstance(recorded.get("max_roots"), int):
                max_roots = int(recorded["max_roots"])
            if target_retained is None and isinstance(recorded.get("target_retained"), int):
                target_retained = int(recorded["target_retained"])
            if explicit is None and isinstance(recorded.get("explicit_roots"), list):
                explicit = [str(name) for name in recorded["explicit_roots"]]
            recorded_roots_file = recorded.get("roots_file_identity")
            if roots_file is None and isinstance(recorded_roots_file, Mapping):
                if explicit is None:
                    raise SprintRunnerError(
                        "recorded roots-file identity lacks recorded explicit roots"
                    )
                roots_file = RootsFileSelection(
                    roots=tuple(explicit),
                    source_path=(
                        str(recorded["roots_file_path"])
                        if isinstance(recorded.get("roots_file_path"), str)
                        else None
                    ),
                    identity=dict(recorded_roots_file),
                )
        runner = SprintRunner(
            repo_root,
            loaded,
            run_id=args.run_id,
            max_roots=max_roots,
            target_retained=target_retained,
            owner_session=args.owner_session,
            explicit_roots=explicit,
            operations=operations,
            roots_file=roots_file,
        )
        before = len(read_retained(runner.paths.retained))
        summary = runner.run(require_zero_lean=args.command == "replay")
        if args.command == "replay":
            report = {
                "run_id": args.run_id,
                "lean_requests": summary["lean_requests"],
                "duplicate_rows": len(read_retained(runner.paths.retained)) - before,
                "retained_before": before,
                "retained_after": runner.retained_count,
                "roots_considered": summary["roots_considered"],
            }
            paths = RunPaths(Path(loaded.config.output.staging_root), args.run_id)
            write_atomic(paths.run_dir / "replay_report.json", canonical_json_bytes(report) + b"\n")
            print(json.dumps(report))
            return 0
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    paths = RunPaths(Path(loaded.config.output.staging_root), args.run_id)
    if args.command == "status":
        print(json.dumps(read_json_object(paths.status), ensure_ascii=False, indent=1))
        return 0
    if args.command == "compact":
        manifest = compact_run(
            repo_root, loaded, run_id=args.run_id, shard_size=args.shard_size, view=args.view
        )
        print(
            json.dumps(
                {k: v for k, v in manifest.items() if k != "shards"}, ensure_ascii=False, indent=1
            )
        )
        return 0
    if args.command == "inspect":
        path, count = write_inspection(loaded, run_id=args.run_id, count=args.count)
        print(json.dumps({"path": str(path), "count": count}))
        return 0
    if args.command == "compact-windows":
        summary = compact_root_windows(
            repo_root, loaded, run_id=args.run_id, roots_per_window=args.roots_per_window
        )
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    if args.command == "gate10k":
        release = release_report(
            repo_root,
            loaded,
            run_id=args.run_id,
            sprint_end_utc=args.sprint_end_utc,
            view=args.view,
            minimum_rows=args.minimum_rows,
        )
        print(json.dumps(release, ensure_ascii=False, indent=1))
        return 0 if release["passed"] else 1
    fixtures_path = latest_fixtures_report(loaded)
    fixtures_ok = (
        read_json_object(fixtures_path).get("passed") is True if fixtures_path.is_file() else None
    )
    replay_path = paths.run_dir / "replay_report.json"
    replay = read_json_object(replay_path) if replay_path.is_file() else None
    report = gate_report(loaded, run_id=args.run_id, fixtures_ok=fixtures_ok, replay=replay)
    write_atomic(paths.run_dir / "gate_report.json", canonical_json_bytes(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

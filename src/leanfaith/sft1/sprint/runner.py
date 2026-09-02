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
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
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
    operation_mask,
    operations_in_mask,
    render_scope_id,
)
from leanfaith.sft1.sprint.inventory import Pool, load_inventory, ordered_roots
from leanfaith.sft1.sprint.screens import (
    GoldBlocklist,
    deduplicate,
    instance_dagger_count,
    render_hash,
    residue_violation,
    stable_row_hash,
    unordered_pair_key,
)
from leanfaith.sft1.sprint.store import Journal, SemanticCache, read_json_object, write_atomic

DEFAULT_CONFIG = Path("configs/transformations/sft1_value_first_v1/sprint_v1.yaml")
Sha256 = str
NonEmpty = str
TerminalStatus = Literal["retained", "not_applicable", "rejected", "error"]


class SprintRunnerError(RuntimeError):
    """Fail-closed runner error."""


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
    project_id: Literal["mathlib"]
    project_dir: NonEmpty
    project_revision: NonEmpty
    lean_version: NonEmpty
    lean_interact_version: NonEmpty
    repl_revision: NonEmpty
    import_header: Literal["import Mathlib"]
    options: dict[str, bool]

    @model_validator(mode="after")
    def _options(self) -> ProjectConfig:
        if self.options.get("Elab.async") is not False:
            raise ValueError("sprint runs must disable Elab.async")
        return self


class EngineConfig(StrictModel):
    path: Literal["LeanFaith/Meta/SFT1/Sprint.lean"]
    operations: tuple[str, ...]

    @model_validator(mode="after")
    def _operations(self) -> EngineConfig:
        if tuple(self.operations) != OPERATIONS:
            raise ValueError("sprint config must list exactly the seven sprint operations in order")
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


class SprintConfig(StrictModel):
    schema_version: Literal[1]
    sprint_id: NonEmpty
    project: ProjectConfig
    engine: EngineConfig
    inventory: InventoryConfig
    screens: ScreensConfig
    execution: ExecutionConfig
    output: OutputConfig
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
    ) -> None:
        self.repo_root = repo_root
        self.loaded = loaded
        self.config = loaded.config
        self.run_id = run_id
        self.max_roots = max_roots
        self.target_retained = target_retained
        self.explicit_roots = list(explicit_roots) if explicit_roots is not None else None
        self.owner_session = owner_session
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
        self.mask = operation_mask(self.config.engine.operations)
        self.scope = render_scope_id(self.identity.semantic_version)
        self.session: SprintSession | None = None
        self.backend: LeanInteractBackend | None = None
        self.rss = PeakRss()
        self.started = time.monotonic()
        self.started_at = utc_now()
        self.statements: dict[str, str] = {}
        self.modules: dict[str, str] = {}
        self.pools: dict[str, str] = {}
        # durable state
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
                f"Mathlib revision mismatch: {head} != {self.pins.project_revision}"
            )

    def load_inventory_rows(self) -> list[dict[str, object]]:
        inventory_dir = Path(self.config.inventory.root) / self.pins.project_revision
        manifest = read_json_object(inventory_dir / "manifest.json")
        if manifest.get("mathlib_revision") != self.pins.project_revision:
            raise SprintRunnerError("inventory manifest revision mismatch")
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
            kind = record.get("kind")
            if kind == "terminal":
                key = (str(record["root"]), str(record["operation_id"]))
                status = str(record["status"])
                if key in self.done:
                    continue
                self.done[key] = status
                self._count(str(record["operation_id"]), status)
                if status == "retained":
                    self.retained_count += 1
                    self.retained_by_op[str(record["operation_id"])] += 1
                    self.retained_keys.add(str(record["unordered_pair_key"]))
            elif kind == "root":
                self.roots_seen.add(str(record["root"]))
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
        )

    def root_id(self, name: str) -> str:
        return "root:" + hash_canonical([self.pins.project_id, self.pins.project_revision, name])

    # ------------------------------------------------------------- main loop

    def run(self, *, require_zero_lean: bool = False) -> dict[str, object]:
        self.replay_mode = require_zero_lean
        self.verify_pins()
        self.load_state()
        order = self.root_order()
        self.write_run_manifest(order_size=len(order))
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
                if self.try_cache(name, missing):
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

    def try_cache(self, name: str, missing: int) -> bool:
        root_record = self.cache.get_root(self.root_key(name))
        if root_record is None:
            return False
        if root_record.get("root_status") != "ok":
            self.finalize_root_failure(name, root_record, source="cache")
            return True
        op_keys = root_record.get("ops")
        if not isinstance(op_keys, dict):
            return False
        op_records: dict[str, dict[str, Any]] = {}
        for operation in operations_in_mask(missing):
            key = op_keys.get(operation)
            if not isinstance(key, str):
                return False
            record = self.cache.get_op(key)
            if record is None:
                return False
            if record.get("status") == "retained" and not isinstance(record.get("render"), dict):
                # Rendered evidence is missing (an earlier render failure); redo the root.
                return False
            op_records[operation] = record
        self.journal.append(
            {
                "kind": "root",
                "root": name,
                "root_status": "ok",
                "reason": "",
                "source": "cache",
                "batch": self.batches,
            }
        )
        self.roots_seen.add(name)
        self.roots_cache += 1
        for operation, record in op_records.items():
            self.finalize_from_op_record(name, root_record, operation, record, source="cache")
        return True

    def finalize_root_failure(
        self, name: str, root_payload: Mapping[str, Any], *, source: str
    ) -> None:
        status = str(root_payload.get("root_status", "error"))
        reason = str(root_payload.get("reason", ""))
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

    def process_batch(self, batch: list[tuple[str, int]]) -> None:
        session = self.open_session()
        self.batches += 1
        batch_started = time.monotonic()
        request_id = f"{self.run_id}:process:{self.batches}:" + hash_canonical(batch)[:16]
        result = session.run_process(batch, request_id=request_id)
        missing = [name for name, _ in batch if name not in result.roots]
        if result.status not in {LeanStatus.VALID.value, LeanStatus.INVALID.value} or missing:
            if len(batch) > 1:
                half = len(batch) // 2
                self.batches -= 1
                self.process_batch(batch[:half])
                self.process_batch(batch[half:])
                return
            name, mask = batch[0]
            detail = "; ".join(result.errors[:2]) or result.status
            self.finalize_root_failure(
                name,
                {"root_status": "error", "reason": f"request_{result.status}:{detail[:300]}"},
                source="lean",
            )
            return
        pending_render: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for name, mask in batch:
            payload = result.roots[name]
            if payload.get("root_status") != "ok":
                self.finalize_root_failure(name, payload, source="lean")
                self.cache.put_root(self.root_key(name), self.root_cache_record(name, payload, {}))
                continue
            self.journal.append(
                {
                    "kind": "root",
                    "root": name,
                    "root_status": "ok",
                    "reason": "",
                    "source": "lean",
                    "batch": self.batches,
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
    ) -> bool:
        session = self.open_session()
        pairs = [(name, operation) for name, operation, _, _ in chunk]
        request_id = f"{self.run_id}:render:{self.batches}:" + hash_canonical(pairs)[:16]
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
            "mechanism": operation.split("_", 1)[0],
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
            "max_roots": self.max_roots,
            "target_retained": self.target_retained,
            "explicit_roots": self.explicit_roots,
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
    size = shard_size or config.output.shard_size
    paths.compacted.mkdir(parents=True, exist_ok=True)
    selected = balanced_view(outcome.kept) if view == "balanced" else outcome.kept
    kept = group_by_ancestry(selected)
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
        "conflict_keys": list(outcome.conflict_keys),
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
        "engine": run_manifest.get("engine"),
        "implementation_commit": run_manifest.get("implementation_commit"),
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
        row = record["row"]
        if residue_violation(str(row["reference"])) or residue_violation(str(row["candidate"])):
            residue += 1
    rich = {op: n for op, n in by_mechanism.items() if n >= 10}
    positives = [op for op in rich if op in POSITIVE_OPERATIONS]
    negatives = [op for op in rich if op in NEGATIVE_OPERATIONS]
    wall = float(status.get("wall_seconds", 0.0))
    dedup = deduplicate(records)
    checks = {
        "mechanisms_with_ten_pairs": len(rich) >= 5,
        "three_positive_mechanisms": len(positives) >= 3,
        "two_negative_mechanisms": len(negatives) >= 2,
        "all_rows_kernel_and_meta_checked": unchecked == 0,
        "zero_rubric_only_negatives": rubric_only == 0,
        "zero_residue": residue == 0,
        "zero_duplicate_or_conflicting_pairs": dedup.duplicate_count == 0
        and dedup.conflict_count == 0,
        "fixtures_passed": fixtures_ok is True,
        "replay_zero_lean_calls": bool(
            replay and replay.get("lean_requests") == 0 and replay.get("duplicate_rows") == 0
        ),
        "under_one_hour": 0 < wall <= 3600,
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
        "residue_rows": residue,
        "duplicates": dedup.duplicate_count,
        "conflicts": dedup.conflict_count,
        "wall_seconds": wall,
        "lean_requests": status.get("lean_requests"),
        "peak_process_tree_rss_bytes": status.get("peak_process_tree_rss_bytes"),
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
    checks = {
        "retained_at_least_minimum": retained_rows >= minimum_rows,
        "all_rows_kernel_and_meta_checked": unchecked == 0,
        "zero_duplicate_or_conflicting_pairs": conflicting == 0,
        "two_useful_negative_mechanisms": len(useful_negatives) >= 2,
        "shortcut_screens": bool(screens["passed"]),
    }
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "view": view,
        "minimum_rows": minimum_rows,
        "generated_at": utc_now(),
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


# ------------------------------------------------------------------ fixtures


def latest_fixtures_report(loaded: LoadedConfig[SprintConfig]) -> Path:
    runs = Path(loaded.config.output.staging_root) / "runs"
    candidates = sorted(
        runs.glob("fixtures-*/fixtures_report.json"), key=lambda path: path.stat().st_mtime
    )
    return candidates[-1] if candidates else runs / "fixtures-none" / "fixtures_report.json"


def run_fixtures(repo_root: Path, loaded: LoadedConfig[SprintConfig]) -> dict[str, object]:
    config = loaded.config
    roots = sorted({fixture.root for fixture in config.fixtures})
    pins = project_pins(config)
    context = engine_module.build_compile_context(repo_root, pins)
    identity = engine_module.engine_identity(repo_root, pins, context)
    run_id = f"fixtures-{identity.source_sha256[:12]}"
    runner = SprintRunner(repo_root, loaded, run_id=run_id, explicit_roots=roots)
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
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "results": results,
        "success_covered": sorted(covered_success),
        "rejection_covered": sorted(covered_rejection),
        "all_operations_covered": covered_success == set(OPERATIONS)
        and covered_rejection == set(OPERATIONS),
        "passed": all(r["passed"] for r in results)
        and covered_success == set(OPERATIONS)
        and covered_rejection == set(OPERATIONS),
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
    parser.add_argument("--minimum-rows", type=int, default=10000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    if args.command == "validate":
        pins = project_pins(loaded.config)
        context = engine_module.build_compile_context(repo_root, pins)
        identity = engine_module.engine_identity(repo_root, pins, context)
        print(json.dumps({"config_hash": loaded.config_hash, "engine": identity.to_dict()}))
        return 0
    if args.command == "fixtures":
        report = run_fixtures(repo_root, loaded)
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "status"}, ensure_ascii=False, indent=1
            )
        )
        return 0 if report["passed"] else 1
    if args.command in {"run", "replay"}:
        max_roots = args.max_roots
        target_retained = args.target_retained
        run_manifest_path = RunPaths(
            Path(loaded.config.output.staging_root), args.run_id
        ).run_manifest
        if args.command == "replay" and run_manifest_path.is_file():
            recorded = read_json_object(run_manifest_path)
            if max_roots is None and isinstance(recorded.get("max_roots"), int):
                max_roots = int(recorded["max_roots"])
            if target_retained is None and isinstance(recorded.get("target_retained"), int):
                target_retained = int(recorded["target_retained"])
        runner = SprintRunner(
            repo_root,
            loaded,
            run_id=args.run_id,
            max_roots=max_roots,
            target_retained=target_retained,
            owner_session=args.owner_session,
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

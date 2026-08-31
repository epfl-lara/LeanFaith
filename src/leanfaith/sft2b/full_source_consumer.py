"""Gated, resumable consumption of the corrected SFT2B full-source release.

This module owns orchestration contracts, not model or Lean implementation.  A
frozen source release is split into the corrected 50K core and the remaining
legacy tail.  Each source is paired with all four frozen ReForm slots, durable
terminals are stored by content identity, and an append-only journal permits
safe resume and deterministic compaction.

The detached launcher is deliberately fail-closed.  It cannot create a tmux
session or claim a GPU until the config is ``scale_authorized`` and an exact,
passing matched-500 runtime-and-quality receipt is present.  The checked-in
config remains ``waiting_matched_500_report``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.sft2b.durable import atomic_write, immutable_write
from leanfaith.sft2b.schemas import CandidateSlot, Sha256, SourceRecord, StableId, stable_id

CONFIG_SCHEMA = "sft2b_reform_diverse_full_consumer_v1"
RECEIPT_SCHEMA = "sft2b_matched_500_runtime_quality_receipt_v1"
type ShardId = Literal["corrected_core_50000", "legacy_tail"]
CORE_SHARD: Literal["corrected_core_50000"] = "corrected_core_50000"
TAIL_SHARD: Literal["legacy_tail"] = "legacy_tail"
SHARD_IDS = (CORE_SHARD, TAIL_SHARD)
EXPECTED_SLOTS = tuple(CandidateSlot)
EXPECTED_SEEDS = (0, 1, 2, 3)

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_ID_RE = re.compile(r"^sft2b_source:[0-9a-f]{64}$")
_TMUX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


class FullSourceConsumerError(RuntimeError):
    """A source, resume, compaction, or launch contract drifted."""


class ReleaseFilePin(StrictModel):
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256 | None


class SourceShardSpec(StrictModel):
    shard_id: ShardId
    id_view_path: Annotated[str, Field(min_length=1)]
    id_view_sha256: Sha256 | None
    expected_rows: Annotated[int, Field(ge=1)] | None


class FullSourceInputSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    path_prefix: Literal["source_inputs/reform_diverse_full_v2"]
    files: tuple[ReleaseFilePin, ...]
    expected_source_rows: Annotated[int, Field(ge=50000)] | None
    shards: tuple[SourceShardSpec, SourceShardSpec]


class SlotSeedSpec(StrictModel):
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]


class ModelSpec(StrictModel):
    model_id: Literal["GuoxinChen/ReForm-32B"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    placement_config_path: Annotated[str, Field(min_length=1)]
    placement_config_sha256: Sha256
    prompt_path: Annotated[str, Field(min_length=1)]
    prompt_sha256: Sha256
    tokenizer_sha256: Sha256
    slots: tuple[SlotSeedSpec, SlotSeedSpec, SlotSeedSpec, SlotSeedSpec]


class Matched500GateSpec(StrictModel):
    receipt_path: Annotated[str, Field(min_length=1)] | None
    receipt_sha256: Sha256 | None
    decision: Literal["pending", "pass"]
    expected_sources: Literal[500]
    expected_requests: Literal[2000]


class ExecutorSpec(StrictModel):
    """Pinned worker command filled only after the matched-500 gate passes.

    Supported placeholders are replaced without a shell: ``{repo_root}``,
    ``{config_path}``, ``{bundle_root}``, ``{run_root}``, and ``{shard_id}``.
    """

    argv: tuple[Annotated[str, Field(min_length=1)], ...] | None


class RuntimeSpec(StrictModel):
    cache_root: Path
    run_root: Path
    reservation_root: Path
    reservation_task: Literal["SFT2B"]
    tmux_session_prefix: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
    startup_health_timeout_seconds: Annotated[int, Field(ge=5, le=300)]
    owner_session: Annotated[str, Field(min_length=1)]


class FullSourceConsumerSpec(StrictModel):
    schema_version: Literal["sft2b_reform_diverse_full_consumer_v1"]
    status: Literal["waiting_matched_500_report", "scale_authorized"]
    input: FullSourceInputSpec
    model: ModelSpec
    matched_500_gate: Matched500GateSpec
    executor: ExecutorSpec
    runtime: RuntimeSpec

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if tuple(item.shard_id for item in self.input.shards) != SHARD_IDS:
            raise ValueError("source shards must be corrected core followed by legacy tail")
        core, tail = self.input.shards
        if core.expected_rows != 50000:
            raise ValueError("corrected core must contain exactly 50,000 sources")
        if (
            tail.expected_rows is not None
            and self.input.expected_source_rows is not None
            and core.expected_rows + tail.expected_rows != self.input.expected_source_rows
        ):
            raise ValueError("core and tail counts do not cover the full release")
        if tuple(item.slot for item in self.model.slots) != EXPECTED_SLOTS:
            raise ValueError("all four candidate slots must appear in frozen order")
        if tuple(item.seed for item in self.model.slots) != EXPECTED_SEEDS:
            raise ValueError("candidate slot seeds must be 0, 1, 2, and 3")
        required_paths = {
            "SHA256SUMS",
            "sources.jsonl",
            "source_manifest.json",
            core.id_view_path,
            tail.id_view_path,
        }
        file_paths = [item.path for item in self.input.files]
        if len(file_paths) != len(set(file_paths)) or not required_paths.issubset(file_paths):
            raise ValueError("input file pins omit a required consumer artifact or repeat a path")
        if self.status == "scale_authorized":
            if self.input.revision is None or self.input.expected_source_rows is None:
                raise ValueError("scale authorization requires an immutable full-release pin")
            if any(item.sha256 is None for item in self.input.files):
                raise ValueError("scale authorization requires every input file hash")
            if any(item.id_view_sha256 is None for item in self.input.shards):
                raise ValueError("scale authorization requires both ID-view hashes")
            if (
                self.matched_500_gate.decision != "pass"
                or self.matched_500_gate.receipt_path is None
                or self.matched_500_gate.receipt_sha256 is None
            ):
                raise ValueError("scale authorization requires the matched-500 passing receipt")
            if not self.executor.argv:
                raise ValueError("scale authorization requires a pinned executor command")
        return self


class Matched500Receipt(StrictModel):
    schema_version: Literal["sft2b_matched_500_runtime_quality_receipt_v1"]
    source_count: Literal[500]
    request_count: Literal[2000]
    complete_cartesian_product: Literal[True]
    runtime_complete: Literal[True]
    quality_review_complete: Literal[True]
    quality_decision: Literal["pass"]
    output_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    runtime_report_sha256: Sha256
    quality_report_sha256: Sha256


@dataclass(frozen=True, slots=True)
class VerifiedSourceViews:
    rows: tuple[SourceRecord, ...]
    source_ids: tuple[str, ...]
    shard_source_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class WorkCell:
    ordinal: int
    shard_id: ShardId
    source_id: str
    slot: CandidateSlot
    seed: int
    cell_id: str


@dataclass(frozen=True, slots=True)
class FullSourceRunPlan:
    run_id: str
    shard_id: ShardId
    source_ids: tuple[str, ...]
    cells: tuple[WorkCell, ...]
    input_binding_sha256: str


class FullSourceTerminal(StrictModel):
    schema_version: Literal["sft2b_full_source_terminal_v1"] = "sft2b_full_source_terminal_v1"
    run_id: StableId
    cell_id: StableId
    shard_id: ShardId
    source_id: StableId
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]
    payload: dict[str, Any]


class FullSourceJournalEvent(StrictModel):
    schema_version: Literal["sft2b_full_source_journal_event_v1"] = (
        "sft2b_full_source_journal_event_v1"
    )
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    cell_id: StableId
    shard_id: ShardId
    source_id: StableId
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]
    terminal_path: Annotated[str, Field(min_length=1)]
    terminal_sha256: Sha256


@dataclass(frozen=True, slots=True)
class CompactionResult:
    path: Path
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DetachedLaunch:
    session_name: str
    command: tuple[str, ...]
    status_path: Path
    log_path: Path


@dataclass(frozen=True, slots=True)
class DetachedHealth:
    session_name: str
    pane_pid: int | None
    state: str
    healthy: bool


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FullSourceConsumerError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FullSourceConsumerError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def load_consumer_spec(config_path: Path) -> tuple[FullSourceConsumerSpec, str]:
    """Load the strict consumer config and return its exact content hash."""

    try:
        spec = FullSourceConsumerSpec.model_validate(_json_object(config_path))
    except Exception as exc:
        raise FullSourceConsumerError(f"invalid full-source consumer config: {exc}") from exc
    return spec, hash_file(config_path)


def _file_pin(spec: FullSourceConsumerSpec, relative_path: str) -> ReleaseFilePin:
    by_path = {item.path: item for item in spec.input.files}
    try:
        return by_path[relative_path]
    except KeyError as exc:
        raise FullSourceConsumerError(f"release file is not pinned: {relative_path}") from exc


def _require_pinned_input(spec: FullSourceConsumerSpec) -> None:
    if spec.input.revision is None or spec.input.expected_source_rows is None:
        raise FullSourceConsumerError("corrected v2 input revision/count are still pending")
    if any(item.sha256 is None for item in spec.input.files):
        raise FullSourceConsumerError("corrected v2 input file hashes are still pending")
    if any(item.id_view_sha256 is None for item in spec.input.shards):
        raise FullSourceConsumerError("corrected v2 ID-view hashes are still pending")


def _read_source_rows(path: Path) -> tuple[SourceRecord, ...]:
    rows: list[SourceRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(SourceRecord.model_validate_json(line))
            except Exception as exc:
                raise FullSourceConsumerError(
                    f"invalid SourceRecord at {path}:{line_number}: {exc}"
                ) from exc
    return tuple(rows)


def _read_id_view(path: Path, *, expected_rows: int | None) -> tuple[str, ...]:
    value = _json_object(path)
    raw_ids = value.get("source_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise FullSourceConsumerError(f"ID view has invalid source_ids: {path}")
    source_ids = tuple(cast(list[str], raw_ids))
    if any(_SOURCE_ID_RE.fullmatch(item) is None for item in source_ids):
        raise FullSourceConsumerError(f"ID view contains an invalid source ID: {path}")
    if len(source_ids) != len(set(source_ids)):
        raise FullSourceConsumerError(f"ID view contains duplicate source IDs: {path}")
    if value.get("source_count") != len(source_ids):
        raise FullSourceConsumerError(f"ID view source_count drifted: {path}")
    if expected_rows is not None and len(source_ids) != expected_rows:
        raise FullSourceConsumerError(
            f"ID view row count drifted: expected {expected_rows}, observed {len(source_ids)}"
        )
    return source_ids


def verify_source_views(spec: FullSourceConsumerSpec, *, bundle_root: Path) -> VerifiedSourceViews:
    """Verify local release bytes and the exact core/tail partition."""

    _require_pinned_input(spec)
    for pin in spec.input.files:
        path = bundle_root / pin.path
        if not path.is_file() or pin.sha256 is None or hash_file(path) != pin.sha256:
            raise FullSourceConsumerError(f"full-source release hash mismatch: {pin.path}")

    checksums: dict[str, str] = {}
    for line in (bundle_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise FullSourceConsumerError("full-source SHA256SUMS is malformed")
        checksums[parts[1]] = parts[0]
    for pin in spec.input.files:
        if pin.path == "SHA256SUMS":
            continue
        if checksums.get(pin.path) != pin.sha256:
            raise FullSourceConsumerError(f"SHA256SUMS binding drifted: {pin.path}")

    rows = _read_source_rows(bundle_root / "sources.jsonl")
    source_ids = tuple(row.source_id for row in rows)
    if len(rows) != spec.input.expected_source_rows or len(source_ids) != len(set(source_ids)):
        raise FullSourceConsumerError("full source rows are not the exact unique pinned count")
    by_shard: dict[str, tuple[str, ...]] = {}
    for shard in spec.input.shards:
        pin = _file_pin(spec, shard.id_view_path)
        if pin.sha256 != shard.id_view_sha256:
            raise FullSourceConsumerError(f"ID-view pin is inconsistent: {shard.shard_id}")
        by_shard[shard.shard_id] = _read_id_view(
            bundle_root / shard.id_view_path, expected_rows=shard.expected_rows
        )

    core_ids = by_shard[CORE_SHARD]
    tail_ids = by_shard[TAIL_SHARD]
    core_set = set(core_ids)
    tail_set = set(tail_ids)
    source_set = set(source_ids)
    if core_set & tail_set:
        raise FullSourceConsumerError("corrected core and legacy tail overlap")
    if core_set | tail_set != source_set:
        raise FullSourceConsumerError("corrected core and legacy tail do not cover sources exactly")
    if tail_ids != tuple(item for item in source_ids if item not in core_set):
        raise FullSourceConsumerError("legacy tail is not the ordered full-release remainder")
    return VerifiedSourceViews(rows=rows, source_ids=source_ids, shard_source_ids=by_shard)


def _input_binding(spec: FullSourceConsumerSpec) -> str:
    _require_pinned_input(spec)
    return hash_canonical(
        {
            "schema_version": "sft2b_full_source_input_binding_v1",
            "repo_id": spec.input.repo_id,
            "revision": spec.input.revision,
            "path_prefix": spec.input.path_prefix,
            "files": {item.path: item.sha256 for item in spec.input.files},
            "shards": [item.model_dump(mode="json") for item in spec.input.shards],
        }
    )


def build_run_plan(
    spec: FullSourceConsumerSpec,
    *,
    config_sha256: str,
    shard_id: str,
    source_ids: Sequence[str],
) -> FullSourceRunPlan:
    """Expand one frozen ID view into its complete four-slot Cartesian product."""

    if shard_id not in SHARD_IDS:
        raise FullSourceConsumerError(f"unknown full-source shard: {shard_id}")
    typed_shard_id = shard_id
    if re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None:
        raise FullSourceConsumerError("consumer config hash is invalid")
    input_binding = _input_binding(spec)
    shard = next(item for item in spec.input.shards if item.shard_id == typed_shard_id)
    ordered_ids = tuple(source_ids)
    if len(ordered_ids) != len(set(ordered_ids)) or any(
        _SOURCE_ID_RE.fullmatch(item) is None for item in ordered_ids
    ):
        raise FullSourceConsumerError("run plan source IDs are invalid or duplicated")
    if shard.expected_rows is not None and len(ordered_ids) != shard.expected_rows:
        raise FullSourceConsumerError("run plan row count differs from its pinned ID view")

    run_id = stable_id(
        "sft2b_full_reform_run",
        {
            "schema_version": "sft2b_full_reform_run_identity_v1",
            "consumer_config_sha256": config_sha256,
            "input_binding_sha256": input_binding,
            "shard_id": typed_shard_id,
            "source_ids_sha256": hash_canonical(ordered_ids),
            "model_id": spec.model.model_id,
            "model_revision": spec.model.revision,
            "placement_config_sha256": spec.model.placement_config_sha256,
            "prompt_sha256": spec.model.prompt_sha256,
            "tokenizer_sha256": spec.model.tokenizer_sha256,
            "slots": [item.model_dump(mode="json") for item in spec.model.slots],
        },
    )
    cells: list[WorkCell] = []
    for source_id in ordered_ids:
        for slot_spec in spec.model.slots:
            cell_id = stable_id(
                "sft2b_full_reform_cell",
                {
                    "run_id": run_id,
                    "source_id": source_id,
                    "slot": slot_spec.slot,
                    "seed": slot_spec.seed,
                },
            )
            cells.append(
                WorkCell(
                    ordinal=len(cells),
                    shard_id=typed_shard_id,
                    source_id=source_id,
                    slot=slot_spec.slot,
                    seed=slot_spec.seed,
                    cell_id=cell_id,
                )
            )
    if len(cells) != len(ordered_ids) * 4 or len({item.cell_id for item in cells}) != len(cells):
        raise FullSourceConsumerError("four-slot Cartesian-product construction failed")
    return FullSourceRunPlan(
        run_id=run_id,
        shard_id=typed_shard_id,
        source_ids=ordered_ids,
        cells=tuple(cells),
        input_binding_sha256=input_binding,
    )


def terminal_cache_path(cache_root: Path, plan: FullSourceRunPlan, cell: WorkCell) -> Path:
    """Return the content-addressed immutable terminal location for one cell."""

    if cell not in plan.cells:
        raise FullSourceConsumerError("cell does not belong to the supplied run plan")
    digest = cell.cell_id.split(":", 1)[1]
    return (
        cache_root
        / "generation"
        / "reform_full_v1"
        / plan.run_id
        / plan.shard_id
        / "requests"
        / digest[:2]
        / digest
        / "terminal.json"
    )


def write_cached_terminal(
    cache_root: Path,
    plan: FullSourceRunPlan,
    cell: WorkCell,
    *,
    payload: Mapping[str, Any],
) -> Path:
    """Write or verify one immutable terminal envelope."""

    terminal = FullSourceTerminal(
        run_id=plan.run_id,
        cell_id=cell.cell_id,
        shard_id=cell.shard_id,
        source_id=cell.source_id,
        slot=cell.slot,
        seed=cell.seed,
        payload=dict(payload),
    )
    path = terminal_cache_path(cache_root, plan, cell)
    immutable_write(path, canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n")
    return path


class FullSourceJournal:
    """Locked append-only terminal journal with exact-cell duplicate suppression."""

    def __init__(self, path: Path, *, plan: FullSourceRunPlan, cache_root: Path) -> None:
        self.path = path
        self.plan = plan
        self.cache_root = cache_root
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._cells = {item.cell_id: item for item in plan.cells}

    def _events(self) -> list[FullSourceJournalEvent]:
        if not self.path.exists():
            return []
        events: list[FullSourceJournalEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(FullSourceJournalEvent.model_validate_json(line))
                except Exception as exc:
                    raise FullSourceConsumerError(
                        f"invalid full-source journal event at line {line_number}: {exc}"
                    ) from exc
        if [item.sequence for item in events] != list(range(len(events))):
            raise FullSourceConsumerError("full-source journal sequence is not contiguous")
        if any(item.run_id != self.plan.run_id for item in events):
            raise FullSourceConsumerError("full-source journal run identity drifted")
        event_ids = [item.cell_id for item in events]
        if len(event_ids) != len(set(event_ids)):
            raise FullSourceConsumerError("full-source journal contains duplicate terminal cells")
        for event in events:
            cell = self._cells.get(event.cell_id)
            if cell is None or (
                event.shard_id != cell.shard_id
                or event.source_id != cell.source_id
                or event.slot != cell.slot
                or event.seed != cell.seed
            ):
                raise FullSourceConsumerError("full-source journal contains a foreign cell")
            expected = terminal_cache_path(self.cache_root, self.plan, cell)
            if Path(event.terminal_path) != expected:
                raise FullSourceConsumerError("journal terminal is outside its content cache cell")
        return events

    def append_terminal(self, cell: WorkCell, terminal_path: Path) -> bool:
        """Append one verified terminal; return False for an identical replay."""

        expected_path = terminal_cache_path(self.cache_root, self.plan, cell)
        if terminal_path != expected_path or not terminal_path.is_file():
            raise FullSourceConsumerError("terminal is absent or outside its content cache cell")
        terminal = FullSourceTerminal.model_validate_json(terminal_path.read_text(encoding="utf-8"))
        if (
            terminal.run_id != self.plan.run_id
            or terminal.cell_id != cell.cell_id
            or terminal.shard_id != cell.shard_id
            or terminal.source_id != cell.source_id
            or terminal.slot != cell.slot
            or terminal.seed != cell.seed
        ):
            raise FullSourceConsumerError("terminal envelope differs from its planned cell")
        terminal_hash = hash_file(terminal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                events = self._events()
                prior = {item.cell_id: item for item in events}.get(cell.cell_id)
                if prior is not None:
                    if prior.terminal_sha256 != terminal_hash:
                        raise FullSourceConsumerError("terminal replay changed immutable content")
                    return False
                event = FullSourceJournalEvent(
                    sequence=len(events),
                    run_id=self.plan.run_id,
                    cell_id=cell.cell_id,
                    shard_id=cell.shard_id,
                    source_id=cell.source_id,
                    slot=cell.slot,
                    seed=cell.seed,
                    terminal_path=str(terminal_path),
                    terminal_sha256=terminal_hash,
                )
                with self.path.open("ab") as handle:
                    handle.write(canonical_json_bytes(event.model_dump(mode="json")) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def events(self) -> tuple[FullSourceJournalEvent, ...]:
        return tuple(self._events())

    def missing_cells(self) -> tuple[WorkCell, ...]:
        completed = {item.cell_id for item in self._events()}
        return tuple(item for item in self.plan.cells if item.cell_id not in completed)


def compact_completed(journal: FullSourceJournal, output_path: Path) -> CompactionResult:
    """Require all cells, then compact terminals in deterministic plan order."""

    events = {item.cell_id: item for item in journal.events()}
    expected_ids = {item.cell_id for item in journal.plan.cells}
    if set(events) != expected_ids:
        missing = len(expected_ids.difference(events))
        extra = len(set(events).difference(expected_ids))
        raise FullSourceConsumerError(
            f"cannot compact incomplete Cartesian product: missing={missing}, extra={extra}"
        )
    payload = bytearray()
    for cell in journal.plan.cells:
        event = events[cell.cell_id]
        terminal_path = Path(event.terminal_path)
        if not terminal_path.is_file() or hash_file(terminal_path) != event.terminal_sha256:
            raise FullSourceConsumerError(
                f"terminal content drifted before compaction: {cell.cell_id}"
            )
        terminal = FullSourceTerminal.model_validate_json(terminal_path.read_text(encoding="utf-8"))
        if terminal.cell_id != cell.cell_id:
            raise FullSourceConsumerError("terminal ordering identity drifted before compaction")
        payload.extend(canonical_json_bytes(terminal.model_dump(mode="json")))
        payload.extend(b"\n")
    immutable_write(output_path, bytes(payload))
    result = CompactionResult(
        path=output_path,
        rows=len(journal.plan.cells),
        sha256=hash_file(output_path),
    )
    verify_compaction(journal.plan, output_path, expected_sha256=result.sha256)
    return result


def verify_compaction(
    plan: FullSourceRunPlan, path: Path, *, expected_sha256: str
) -> tuple[FullSourceTerminal, ...]:
    """Replay the deterministic compacted ordering and completeness contract."""

    if not path.is_file() or hash_file(path) != expected_sha256:
        raise FullSourceConsumerError("compacted output hash mismatch")
    rows = tuple(
        FullSourceTerminal.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len(rows) != len(plan.cells):
        raise FullSourceConsumerError("compacted output is not the complete Cartesian product")
    for terminal, cell in zip(rows, plan.cells, strict=True):
        if (
            terminal.run_id != plan.run_id
            or terminal.cell_id != cell.cell_id
            or terminal.source_id != cell.source_id
            or terminal.slot != cell.slot
            or terminal.seed != cell.seed
        ):
            raise FullSourceConsumerError("compacted output order or cell identity drifted")
    return rows


def verify_matched_500_gate(repo_root: Path, spec: FullSourceConsumerSpec) -> Matched500Receipt:
    """Require the exact passing matched-500 runtime-and-quality receipt."""

    gate = spec.matched_500_gate
    if spec.status != "scale_authorized":
        raise FullSourceConsumerError("full generation is not scale_authorized")
    if gate.decision != "pass" or gate.receipt_path is None or gate.receipt_sha256 is None:
        raise FullSourceConsumerError("matched-500 runtime and quality gate is still pending")
    path = Path(gate.receipt_path)
    if not path.is_absolute():
        path = repo_root / path
    if not path.is_file() or hash_file(path) != gate.receipt_sha256:
        raise FullSourceConsumerError("matched-500 receipt is absent or hash-mismatched")
    try:
        receipt = Matched500Receipt.model_validate(_json_object(path))
    except Exception as exc:
        raise FullSourceConsumerError(f"matched-500 receipt failed validation: {exc}") from exc
    if (
        receipt.source_count != gate.expected_sources
        or receipt.request_count != gate.expected_requests
    ):
        raise FullSourceConsumerError("matched-500 receipt count drifted")
    return receipt


def _session_name(spec: FullSourceConsumerSpec, plan: FullSourceRunPlan) -> str:
    suffix = plan.run_id.split(":", 1)[1][:12]
    name = f"{spec.runtime.tmux_session_prefix}-{plan.shard_id}-{suffix}"
    if _TMUX_NAME_RE.fullmatch(name) is None:
        raise FullSourceConsumerError(f"invalid deterministic tmux session name: {name}")
    return name


def build_detached_launch(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    bundle_root: Path,
    plan: FullSourceRunPlan,
    run_root: Path,
) -> DetachedLaunch:
    """Build the named tmux command only after the hard launch gate passes."""

    verify_matched_500_gate(repo_root, spec)
    if not spec.executor.argv:
        raise FullSourceConsumerError("scale-authorized config has no executor command")
    session_name = _session_name(spec, plan)
    shard_root = run_root / plan.shard_id / plan.run_id
    status_path = shard_root / "launch_status.json"
    log_path = shard_root / "consumer.log"
    supervisor_argv = (
        "uv",
        "run",
        "python",
        "-m",
        "leanfaith.sft2b.full_source_consumer",
        "supervise",
        "--repo-root",
        str(repo_root),
        "--config",
        str(config_path),
        "--bundle-root",
        str(bundle_root),
        "--shard",
        plan.shard_id,
        "--run-root",
        str(run_root),
    )
    shell_command = f"{shlex.join(supervisor_argv)} >> {shlex.quote(str(log_path))} 2>&1 </dev/null"
    command = (
        "tmux",
        "new-session",
        "-d",
        "-s",
        session_name,
        "-c",
        str(repo_root),
        shell_command,
    )
    return DetachedLaunch(
        session_name=session_name,
        command=command,
        status_path=status_path,
        log_path=log_path,
    )


def inspect_detached_health(launch: DetachedLaunch) -> DetachedHealth:
    """Inspect tmux liveness and the supervisor's durable startup state."""

    pane = subprocess.run(
        (
            "tmux",
            "display-message",
            "-p",
            "-t",
            f"={launch.session_name}",
            "#{pane_pid}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    pane_pid: int | None = None
    if pane.returncode == 0 and pane.stdout.strip().isdigit():
        pane_pid = int(pane.stdout.strip())
    state = "not_started"
    if launch.status_path.is_file():
        value = _json_object(launch.status_path)
        raw_state = value.get("state")
        if isinstance(raw_state, str):
            state = raw_state
    healthy_states = {"resource_claimed", "worker_started", "completed"}
    healthy = state in healthy_states and (pane_pid is not None or state == "completed")
    return DetachedHealth(
        session_name=launch.session_name,
        pane_pid=pane_pid,
        state=state,
        healthy=healthy,
    )


def launch_detached(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    bundle_root: Path,
    plan: FullSourceRunPlan,
    run_root: Path,
) -> DetachedHealth:
    """Start and health-check the authorized detached supervisor.

    The checked-in waiting config always raises before filesystem mutation,
    tmux, or resource claiming.
    """

    launch = build_detached_launch(
        repo_root,
        spec=spec,
        config_path=config_path,
        bundle_root=bundle_root,
        plan=plan,
        run_root=run_root,
    )
    existing = subprocess.run(
        ("tmux", "has-session", "-t", f"={launch.session_name}"),
        check=False,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        raise FullSourceConsumerError(f"tmux session already exists: {launch.session_name}")
    launch.log_path.parent.mkdir(parents=True, exist_ok=True)
    started = subprocess.run(launch.command, check=False, capture_output=True, text=True)
    if started.returncode != 0:
        raise FullSourceConsumerError(f"tmux launch failed: {started.stderr.strip()}")
    deadline = time.monotonic() + spec.runtime.startup_health_timeout_seconds
    last = DetachedHealth(launch.session_name, None, "not_started", False)
    while time.monotonic() < deadline:
        last = inspect_detached_health(launch)
        if last.healthy:
            return last
        if last.state == "failed":
            break
        time.sleep(0.5)
    raise FullSourceConsumerError(
        f"detached launch failed health contract: session={launch.session_name}, state={last.state}"
    )


def _status_payload(plan: FullSourceRunPlan, *, state: str, **extra: object) -> bytes:
    value = {
        "schema_version": "sft2b_full_source_launch_status_v1",
        "run_id": plan.run_id,
        "shard_id": plan.shard_id,
        "state": state,
        **extra,
    }
    return canonical_json_bytes(value) + b"\n"


def _executor_argv(
    spec: FullSourceConsumerSpec,
    *,
    repo_root: Path,
    config_path: Path,
    bundle_root: Path,
    run_root: Path,
    shard_id: str,
) -> tuple[str, ...]:
    if not spec.executor.argv:
        raise FullSourceConsumerError("executor command is not pinned")
    replacements = {
        "{repo_root}": str(repo_root),
        "{config_path}": str(config_path),
        "{bundle_root}": str(bundle_root),
        "{run_root}": str(run_root),
        "{shard_id}": shard_id,
    }
    result: list[str] = []
    for value in spec.executor.argv:
        expanded = value
        for marker, replacement in replacements.items():
            expanded = expanded.replace(marker, replacement)
        if "{" in expanded or "}" in expanded:
            raise FullSourceConsumerError(f"unknown executor placeholder: {value}")
        result.append(expanded)
    return tuple(result)


def supervise_shard(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    config_sha256: str,
    bundle_root: Path,
    shard_id: str,
    run_root: Path,
) -> int:
    """Claim the GPU around one authorized shard executor and release it reliably."""

    verify_matched_500_gate(repo_root, spec)
    verified = verify_source_views(spec, bundle_root=bundle_root)
    source_ids = verified.shard_source_ids[shard_id]
    plan = build_run_plan(
        spec, config_sha256=config_sha256, shard_id=shard_id, source_ids=source_ids
    )
    shard_root = run_root / shard_id / plan.run_id
    status_path = shard_root / "launch_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(status_path, _status_payload(plan, state="starting", supervisor_pid=os.getpid()))
    claimed = False
    try:
        claim_resources(
            root=spec.runtime.reservation_root,
            task=spec.runtime.reservation_task,
            lean_workers=0,
            lean_rss_gib=0.0,
            gpu=True,
            pid=os.getpid(),
            owner_session=spec.runtime.owner_session,
            worktree=repo_root,
        )
        claimed = True
        atomic_write(
            status_path,
            _status_payload(plan, state="resource_claimed", supervisor_pid=os.getpid()),
        )
        command = _executor_argv(
            spec,
            repo_root=repo_root,
            config_path=config_path,
            bundle_root=bundle_root,
            run_root=run_root,
            shard_id=shard_id,
        )
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            start_new_session=False,
        )
        atomic_write(
            status_path,
            _status_payload(
                plan,
                state="worker_started",
                supervisor_pid=os.getpid(),
                worker_pid=process.pid,
            ),
        )
        return_code = process.wait()
        state = "completed" if return_code == 0 else "failed"
        atomic_write(
            status_path,
            _status_payload(
                plan,
                state=state,
                supervisor_pid=os.getpid(),
                worker_pid=process.pid,
                return_code=return_code,
            ),
        )
        return return_code
    except Exception:
        atomic_write(
            status_path,
            _status_payload(plan, state="failed", supervisor_pid=os.getpid()),
        )
        raise
    finally:
        if claimed:
            release_resources(
                root=spec.runtime.reservation_root, task=spec.runtime.reservation_task
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "launch", "supervise"):
        child = subparsers.add_parser(name)
        child.add_argument("--repo-root", type=Path, default=Path.cwd())
        child.add_argument(
            "--config",
            type=Path,
            default=Path("configs/sft2b/reform_diverse_full_consumer_v1.json"),
        )
        child.add_argument("--bundle-root", type=Path, required=True)
        child.add_argument("--shard", choices=SHARD_IDS, default=CORE_SHARD)
        child.add_argument("--run-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    spec, config_sha256 = load_consumer_spec(config_path.resolve())
    bundle_root = args.bundle_root.resolve()
    verified = verify_source_views(spec, bundle_root=bundle_root)
    plan = build_run_plan(
        spec,
        config_sha256=config_sha256,
        shard_id=args.shard,
        source_ids=verified.shard_source_ids[args.shard],
    )
    run_root = (args.run_root or spec.runtime.run_root).resolve()
    if args.command == "preflight":
        result = {
            "schema_version": "sft2b_full_source_preflight_v1",
            "status": spec.status,
            "run_id": plan.run_id,
            "shard_id": plan.shard_id,
            "sources": len(plan.source_ids),
            "requests": len(plan.cells),
            "launch_authorized": spec.status == "scale_authorized",
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "launch":
        health = launch_detached(
            repo_root,
            spec=spec,
            config_path=config_path.resolve(),
            bundle_root=bundle_root,
            plan=plan,
            run_root=run_root,
        )
        print(json.dumps(asdict(health), sort_keys=True))
        return 0
    return supervise_shard(
        repo_root,
        spec=spec,
        config_path=config_path.resolve(),
        config_sha256=config_sha256,
        bundle_root=bundle_root,
        shard_id=args.shard,
        run_root=run_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())

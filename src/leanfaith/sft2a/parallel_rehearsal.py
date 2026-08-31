"""Prepared, unexecuted bounded-parallel coordination for SFT2A v5.2."""

from __future__ import annotations

import fcntl
import json
import os
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import ExecutionCeilings, SFT2AV52Config
from leanfaith.sft2a.reference_certification import (
    ReferenceCertificationPhaseError,
    verify_global_reference_preflight,
)

ProviderKind = Literal["proposer", "opus", "lemex"]


class ParallelRehearsalError(RuntimeError):
    """A parallel budget, resume, deduplication, or launch invariant failed."""


@contextmanager
def parallel_launch_lock(path: Path) -> Iterator[None]:
    """Refuse a second process before any provider or Lean worker is constructed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ParallelRehearsalError("parallel launch lock is unsafe")
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ParallelRehearsalError("duplicate parallel rehearsal launch refused") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    result: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParallelRehearsalError(f"invalid parallel journal line {number}") from exc
        if not isinstance(value, dict):
            raise ParallelRehearsalError("parallel journal row is not an object")
        result.append(value)
    return result


def _append_locked(path: Path, event: Mapping[str, object]) -> None:
    record = {"event_id": "sft2a-parallel:" + hash_canonical(event), **event}
    payload = canonical_json_bytes(record) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            prior = handle.read().splitlines()
            if payload.rstrip() in prior:
                return
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ParallelRehearsalError(f"parallel {field} is not numeric")
    return float(value)


class AtomicProviderBudget:
    """Reserve before a call and finalize afterward under one cross-process file lock."""

    def __init__(self, path: Path, ceilings: ExecutionCeilings) -> None:
        self.path = path
        self.ceilings = ceilings
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _states(self, events: Iterable[Mapping[str, object]]) -> dict[str, dict[str, object]]:
        states: dict[str, dict[str, object]] = {}
        for event in events:
            call_key = event.get("call_key")
            phase = event.get("phase")
            if not isinstance(call_key, str) or phase not in {"reserved", "finalized"}:
                raise ParallelRehearsalError("parallel budget event is malformed")
            current = states.get(call_key)
            if phase == "reserved":
                if current is not None:
                    if current.get("reservation_id") != event.get("reservation_id"):
                        raise ParallelRehearsalError("parallel call key was reserved twice")
                    continue
                states[call_key] = dict(event)
            else:
                if current is None or current.get("phase") == "finalized":
                    raise ParallelRehearsalError("parallel finalization lacks one reservation")
                if current.get("reservation_id") != event.get("reservation_id"):
                    raise ParallelRehearsalError(
                        "parallel reservation/finalization identity differs"
                    )
                states[call_key] = dict(event)
        return states

    def snapshot(self) -> dict[str, object]:
        states = self._states(_events(self.path))
        counts = Counter(str(state["kind"]) for state in states.values())
        finalized = sum(state.get("phase") == "finalized" for state in states.values())
        spend = sum(
            _number(state.get("reported_cost_usd"), field="reported cost")
            for state in states.values()
            if state.get("kind") == "opus" and state.get("phase") == "finalized"
        )
        outstanding = sum(
            _number(state.get("maximum_charge_usd"), field="maximum charge")
            for state in states.values()
            if state.get("kind") == "opus" and state.get("phase") == "reserved"
        )
        return {
            "unique_reserved_calls": len(states),
            "finalized_calls": finalized,
            "outstanding_calls": len(states) - finalized,
            "proposer_calls": counts["proposer"],
            "opus_calls": counts["opus"],
            "lemex_calls": counts["lemex"],
            "reported_opus_spend_usd": spend,
            "reserved_opus_maximum_charge_usd": outstanding,
            "ledger_hash": hash_canonical(list(states.values())),
        }

    def reserve(
        self,
        *,
        call_key: str,
        kind: ProviderKind,
        worker_id: str,
        maximum_charge_usd: float = 0.0,
    ) -> str:
        if not call_key or not worker_id or maximum_charge_usd < 0:
            raise ParallelRehearsalError("invalid provider reservation")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw_events = [json.loads(line) for line in handle.read().splitlines() if line]
                if not all(isinstance(event, dict) for event in raw_events):
                    raise ParallelRehearsalError("parallel budget journal is malformed")
                states = self._states(raw_events)
                existing = states.get(call_key)
                reservation_id = hash_canonical(
                    {"call_key": call_key, "kind": kind, "worker_id": worker_id}
                )
                if existing is not None:
                    if existing.get("reservation_id") != reservation_id:
                        raise ParallelRehearsalError(
                            "provider call key belongs to another reservation"
                        )
                    return reservation_id
                counts = Counter(str(state["kind"]) for state in states.values())
                maxima = {
                    "proposer": self.ceilings.maximum_proposer_calls,
                    "opus": self.ceilings.maximum_opus_calls,
                    "lemex": self.ceilings.maximum_lemex_calls,
                }
                if (
                    len(states) >= self.ceilings.maximum_provider_calls
                    or counts[kind] >= maxima[kind]
                ):
                    raise ParallelRehearsalError("atomic provider call ceiling reached")
                spent_or_held = sum(
                    _number(
                        state.get(
                            "reported_cost_usd"
                            if state.get("phase") == "finalized"
                            else "maximum_charge_usd"
                        ),
                        field="Opus cost",
                    )
                    for state in states.values()
                    if state.get("kind") == "opus"
                )
                if kind == "opus" and (
                    spent_or_held + maximum_charge_usd
                    > self.ceilings.maximum_reported_opus_spend_usd
                ):
                    raise ParallelRehearsalError("atomic reported Opus spend ceiling reached")
                event = {
                    "version": "leanfaith_sft2a_parallel_provider_budget_v5_2",
                    "phase": "reserved",
                    "call_key": call_key,
                    "reservation_id": reservation_id,
                    "kind": kind,
                    "worker_id": worker_id,
                    "maximum_charge_usd": maximum_charge_usd,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_json_bytes(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                return reservation_id
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def finalize(
        self,
        *,
        call_key: str,
        reservation_id: str,
        response_sha256: str,
        reported_cost_usd: float | None,
    ) -> dict[str, object]:
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw_events = [json.loads(line) for line in handle.read().splitlines() if line]
                if not all(isinstance(event, dict) for event in raw_events):
                    raise ParallelRehearsalError("parallel budget journal is malformed")
                states = self._states(raw_events)
                state = states.get(call_key)
                if state is None or state.get("reservation_id") != reservation_id:
                    raise ParallelRehearsalError("provider finalization lacks its reservation")
                if state.get("phase") == "finalized":
                    if state.get("response_sha256") != response_sha256:
                        raise ParallelRehearsalError("provider finalization replay differs")
                    return self.snapshot()
                kind = state.get("kind")
                if kind == "opus" and reported_cost_usd is None:
                    raise ParallelRehearsalError("Opus finalization lacks reported cost")
                charge = 0.0 if reported_cost_usd is None else reported_cost_usd
                if charge < 0 or charge > _number(
                    state.get("maximum_charge_usd"), field="maximum charge"
                ):
                    raise ParallelRehearsalError("reported cost exceeds atomic reservation")
                event = {
                    "version": "leanfaith_sft2a_parallel_provider_budget_v5_2",
                    "phase": "finalized",
                    "call_key": call_key,
                    "reservation_id": reservation_id,
                    "kind": kind,
                    "worker_id": state["worker_id"],
                    "maximum_charge_usd": state["maximum_charge_usd"],
                    "response_sha256": response_sha256,
                    "reported_cost_usd": charge,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_json_bytes(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return self.snapshot()


class ParallelRootJournal:
    """Atomic root claims and slot checkpoints supporting both resume boundaries."""

    def __init__(self, path: Path, *, maximum_workers: int = 2) -> None:
        if maximum_workers != 2:
            raise ParallelRehearsalError("v5.2 root concurrency must be exactly capped at two")
        self.path = path
        self.maximum_workers = maximum_workers

    def claim(self, *, root_id: str, worker_id: str) -> Literal["claimed", "replay_complete"]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"phase": "claimed", "root_id": root_id, "worker_id": worker_id}
        record = {"event_id": "sft2a-parallel:" + hash_canonical(event), **event}
        payload = canonical_json_bytes(record) + b"\n"
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                prior = handle.read().splitlines()
                events = [json.loads(line) for line in prior if line]
                if not all(isinstance(item, dict) for item in events):
                    raise ParallelRehearsalError("parallel root journal is malformed")
                completed = {
                    str(item["root_id"]) for item in events if item.get("phase") == "complete"
                }
                if root_id in completed:
                    return "replay_complete"
                active = {
                    str(item["worker_id"]): str(item["root_id"])
                    for item in events
                    if item.get("phase") == "claimed" and str(item["root_id"]) not in completed
                }
                owners = [owner for owner, root in active.items() if root == root_id]
                if owners and owners != [worker_id]:
                    raise ParallelRehearsalError("root is already claimed by another worker")
                if worker_id not in active and len(active) >= self.maximum_workers:
                    raise ParallelRehearsalError("parallel root-worker ceiling reached")
                if payload.rstrip() not in prior:
                    handle.seek(0, os.SEEK_END)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                return "claimed"
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def checkpoint(self, *, root_id: str, worker_id: str, slot_id: str, artifact_hash: str) -> None:
        _append_locked(
            self.path,
            {
                "phase": "slot_checkpoint",
                "root_id": root_id,
                "worker_id": worker_id,
                "slot_id": slot_id,
                "artifact_hash": artifact_hash,
            },
        )

    def complete(self, *, root_id: str, worker_id: str, manifest_hash: str) -> None:
        _append_locked(
            self.path,
            {
                "phase": "complete",
                "root_id": root_id,
                "worker_id": worker_id,
                "manifest_hash": manifest_hash,
            },
        )


def deterministic_parallel_compaction(
    rows: Iterable[Mapping[str, object]], *, output: Path
) -> dict[str, object]:
    """Merge worker rows by stable ID while rejecting all cross-worker duplicates."""

    by_id: dict[str, dict[str, object]] = {}
    goals: set[str] = set()
    exprs: set[str] = set()
    planned: Counter[tuple[str, str]] = Counter()
    accepted: Counter[tuple[str, str]] = Counter()
    for raw in rows:
        row = dict(raw)
        row_id = row.get("row_id")
        goal_hash = row.get("candidate_rendered_goal_hash")
        expr_hash = row.get("candidate_closed_expr_hash")
        if not all(isinstance(value, str) and value for value in (row_id, goal_hash, expr_hash)):
            raise ParallelRehearsalError("parallel compacted row lacks stable hashes")
        assert isinstance(row_id, str) and isinstance(goal_hash, str) and isinstance(expr_hash, str)
        if row_id in by_id or goal_hash in goals or expr_hash in exprs:
            raise ParallelRehearsalError("cross-worker duplicate reached compaction")
        for key, target_counter in (
            ("planned_mechanism", planned),
            ("accepted_mechanism", accepted),
        ):
            mechanism = row.get(key)
            if isinstance(mechanism, dict):
                target_counter[(str(mechanism.get("polarity")), str(mechanism.get("family")))] += 1
        by_id[row_id] = row
        goals.add(goal_hash)
        exprs.add(expr_hash)
    ordered = [by_id[row_id] for row_id in sorted(by_id)]
    _atomic_exact(output / "rows.jsonl", b"".join(canonical_json_bytes(r) + b"\n" for r in ordered))

    def histogram(counter: Counter[tuple[str, str]]) -> dict[str, dict[str, int]]:
        return {
            polarity: {
                family: counter[(polarity, family)]
                for seen, family in sorted(counter)
                if seen == polarity
            }
            for polarity in ("preserving", "breaking")
        }

    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_parallel_compaction_v5_2",
        "rows": len(ordered),
        "rows_sha256": hash_file(output / "rows.jsonl"),
        "planned_mechanism_histogram": histogram(planned),
        "accepted_mechanism_evidence_histogram": histogram(accepted),
        "cross_worker_duplicates": 0,
        "deterministic": True,
    }
    _atomic_exact(output / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def prepare_parallel_rehearsal_path(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Verify the cache gate and persist an unexecuted, provider-disabled runner receipt."""

    config = loaded.config
    if not isinstance(config, SFT2AV52Config):
        raise ParallelRehearsalError("parallel rehearsal preparation requires v5.2")
    try:
        preflight = verify_global_reference_preflight(loaded)
    except ReferenceCertificationPhaseError as exc:
        raise ParallelRehearsalError(str(exc)) from exc
    output = Path(config.staging_root) / config.rehearsal.output_subdir
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_parallel_rehearsal_prepared_v5_2",
        "config_hash": loaded.config_hash,
        "sample_sha256": preflight["sample_sha256"],
        "global_reference_preflight_sha256": hash_file(
            Path(config.staging_root)
            / config.reference_certification.output_subdir
            / "global_100_preflight_receipt.json"
        ),
        "maximum_root_workers": config.parallel_rehearsal.maximum_root_workers,
        "maximum_total_lean_workers": config.parallel_rehearsal.maximum_total_lean_workers,
        "maximum_measured_rss_gib": config.parallel_rehearsal.maximum_measured_rss_gib,
        "provider_budget_protocol": config.parallel_rehearsal.provider_budget_protocol,
        "cross_worker_deduplication": True,
        "mid_root_resume": True,
        "between_root_resume": True,
        "duplicate_launch_refusal": True,
        "deterministic_compaction": True,
        "zero_call_replay": True,
        "planned_vs_accepted_mechanisms_distinct": True,
        "execution_authorized": False,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
    }
    _atomic_exact(output / "parallel_runner_readiness.json", canonical_json_bytes(receipt) + b"\n")
    return receipt


__all__ = [
    "AtomicProviderBudget",
    "ParallelRehearsalError",
    "ParallelRootJournal",
    "deterministic_parallel_compaction",
    "parallel_launch_lock",
    "prepare_parallel_rehearsal_path",
]

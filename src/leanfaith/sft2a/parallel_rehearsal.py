"""Prepared, unexecuted bounded-parallel coordination for SFT2A v5.2."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import ExecutionCeilings, SFT2AV52Config
from leanfaith.sft2a.providers import ProviderCallResult
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
    """Reserve before a call and finalize afterward under one cross-process file lock.

    The journal is physically read at most once per process. Every later operation uses the
    synchronized in-memory event list plus an incrementally maintained per-call state map; new
    events are appended under a short threading lock plus an fcntl append for cross-process
    durability. ``journal_reads`` counts physical journal loads so tests can prove load-once.
    """

    def __init__(self, path: Path, ceilings: ExecutionCeilings) -> None:
        self.path = path
        self.ceilings = ceilings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cached_events: list[dict[str, object]] | None = None
        self._cached_states: dict[str, dict[str, object]] | None = None
        self._thread_lock = threading.RLock()
        self.journal_reads = 0

    def _events_locked(self) -> list[dict[str, object]]:
        if self._cached_events is None:
            self.journal_reads += 1
            events = _events(self.path)
            self._cached_states = self._states(events)
            self._cached_events = events
        return self._cached_events

    def _states_locked(self) -> dict[str, dict[str, object]]:
        self._events_locked()
        assert self._cached_states is not None
        return self._cached_states

    def _append_event_locked(self, event: Mapping[str, object]) -> None:
        record = {"event_id": "sft2a-parallel:" + hash_canonical(event), **event}
        payload = canonical_json_bytes(record) + b"\n"
        states = self._states_locked()
        trial = dict(states)
        self._apply_event(trial, record)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        assert self._cached_events is not None
        self._cached_events.append(dict(record))
        call_key = str(record["call_key"])
        states[call_key] = trial[call_key]

    @staticmethod
    def _apply_event(states: dict[str, dict[str, object]], event: Mapping[str, object]) -> None:
        call_key = event.get("call_key")
        phase = event.get("phase")
        if not isinstance(call_key, str) or phase not in {
            "reserved",
            "reclaimed",
            "finalized",
        }:
            raise ParallelRehearsalError("parallel budget event is malformed")
        current = states.get(call_key)
        if phase == "reserved":
            if current is not None:
                if current.get("reservation_id") != event.get("reservation_id"):
                    raise ParallelRehearsalError("parallel call key was reserved twice")
                return
            states[call_key] = dict(event)
        elif phase == "reclaimed":
            if current is None or current.get("phase") == "finalized":
                raise ParallelRehearsalError("parallel reclaim lacks an unfinished reservation")
            if event.get("prior_reservation_id") != current.get("reservation_id"):
                raise ParallelRehearsalError("parallel reclaim predecessor differs")
            states[call_key] = dict(event)
        else:
            if current is None or current.get("phase") == "finalized":
                raise ParallelRehearsalError("parallel finalization lacks one reservation")
            if current.get("reservation_id") != event.get("reservation_id"):
                raise ParallelRehearsalError("parallel reservation/finalization identity differs")
            states[call_key] = dict(event)

    def _states(self, events: Iterable[Mapping[str, object]]) -> dict[str, dict[str, object]]:
        states: dict[str, dict[str, object]] = {}
        for event in events:
            self._apply_event(states, event)
        return states

    def state_of(self, call_key: str) -> dict[str, object] | None:
        """Return a copy of one call's current ledger state from the cached journal."""

        with self._thread_lock:
            state = self._states_locked().get(call_key)
            return None if state is None else dict(state)

    def snapshot(self) -> dict[str, object]:
        with self._thread_lock:
            states = dict(self._states_locked())
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
            if state.get("kind") == "opus" and state.get("phase") != "finalized"
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
        with self._thread_lock:
            states = self._states_locked()
            existing = states.get(call_key)
            reservation_id = hash_canonical(
                {"call_key": call_key, "kind": kind, "worker_id": worker_id}
            )
            if existing is not None:
                if existing.get("reservation_id") != reservation_id:
                    raise ParallelRehearsalError("provider call key belongs to another reservation")
                return reservation_id
            counts = Counter(str(state["kind"]) for state in states.values())
            maxima = {
                "proposer": self.ceilings.maximum_proposer_calls,
                "opus": self.ceilings.maximum_opus_calls,
                "lemex": self.ceilings.maximum_lemex_calls,
            }
            if len(states) >= self.ceilings.maximum_provider_calls or counts[kind] >= maxima[kind]:
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
                spent_or_held + maximum_charge_usd > self.ceilings.maximum_reported_opus_spend_usd
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
            self._append_event_locked(event)
            return reservation_id

    def finalize(
        self,
        *,
        call_key: str,
        reservation_id: str,
        response_sha256: str,
        reported_cost_usd: float | None,
    ) -> dict[str, object]:
        with self._thread_lock:
            states = self._states_locked()
            state = states.get(call_key)
            if state is None or state.get("reservation_id") != reservation_id:
                raise ParallelRehearsalError("provider finalization lacks its reservation")
            if state.get("phase") == "finalized":
                if state.get("response_sha256") != response_sha256:
                    raise ParallelRehearsalError("provider finalization replay differs")
            else:
                kind = state.get("kind")
                if kind == "opus" and reported_cost_usd is None:
                    raise ParallelRehearsalError("Opus finalization lacks reported cost")
                charge = 0.0 if reported_cost_usd is None else reported_cost_usd
                if charge < 0:
                    raise ParallelRehearsalError("reported provider cost is negative")
                other_opus = sum(
                    _number(
                        other.get(
                            "reported_cost_usd"
                            if other.get("phase") == "finalized"
                            else "maximum_charge_usd"
                        ),
                        field="Opus cost",
                    )
                    for key, other in states.items()
                    if key != call_key and other.get("kind") == "opus"
                )
                if (
                    kind == "opus"
                    and other_opus + charge > self.ceilings.maximum_reported_opus_spend_usd
                ):
                    raise ParallelRehearsalError("reported cost exceeds atomic Opus spend ceiling")
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
                self._append_event_locked(event)
        return self.snapshot()

    def reclaim_missing_terminal(
        self,
        *,
        call_key: str,
        prior_worker_id: str,
        new_worker_id: str,
        terminal_path: Path,
    ) -> str:
        """Explicitly transfer a crashed pre-terminal reservation to a new worker."""

        if terminal_path.exists():
            raise ParallelRehearsalError("provider terminal exists; reconcile instead of reclaim")
        with self._thread_lock:
            states = self._states_locked()
            state = states.get(call_key)
            if state is None or state.get("phase") == "finalized":
                raise ParallelRehearsalError("provider reclaim lacks unfinished reservation")
            if state.get("worker_id") != prior_worker_id:
                raise ParallelRehearsalError("provider reclaim prior owner differs")
            reservation_id = hash_canonical(
                {"call_key": call_key, "kind": state["kind"], "worker_id": new_worker_id}
            )
            event = {
                "version": "leanfaith_sft2a_parallel_provider_budget_v5_2",
                "phase": "reclaimed",
                "call_key": call_key,
                "reservation_id": reservation_id,
                "prior_reservation_id": state["reservation_id"],
                "kind": state["kind"],
                "worker_id": new_worker_id,
                "maximum_charge_usd": state["maximum_charge_usd"],
            }
            self._append_event_locked(event)
            return reservation_id

    def reconcile_terminal(self, *, call_key: str, terminal_path: Path) -> dict[str, object]:
        """Finalize a crash-left terminal without issuing another provider call."""

        if not terminal_path.is_file():
            raise ParallelRehearsalError("provider terminal is absent during reconciliation")
        terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
        if not isinstance(terminal, dict) or terminal.get("call_key") != call_key:
            raise ParallelRehearsalError("provider terminal identity differs during reconciliation")
        state = self.state_of(call_key)
        if state is None:
            raise ParallelRehearsalError("provider terminal has no atomic reservation")
        return self.finalize(
            call_key=call_key,
            reservation_id=str(state["reservation_id"]),
            response_sha256=hash_file(terminal_path),
            reported_cost_usd=(
                float(terminal["cost_usd"])
                if isinstance(terminal.get("cost_usd"), int | float)
                and not isinstance(terminal.get("cost_usd"), bool)
                else None
            ),
        )


class AtomicBudgetedProvider:
    """Connect one provider to the shared reserve/call/finalize ledger."""

    def __init__(
        self,
        provider: object,
        *,
        ledger: AtomicProviderBudget,
        kind: ProviderKind,
        worker_id: str,
        maximum_charge_usd: float = 0.0,
        reclaim_from_worker: str | None = None,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.kind = kind
        self.worker_id = worker_id
        self.maximum_charge_usd = maximum_charge_usd
        self.reclaim_from_worker = reclaim_from_worker

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        preview = getattr(self.provider, "preview_call", None)
        invoke = getattr(self.provider, "call", None)
        if not callable(preview) or not callable(invoke):
            raise ParallelRehearsalError("budgeted provider lacks preview/call protocol")
        call_key, terminal_path, _request = preview(prompt=prompt, input_ids=input_ids)
        if not isinstance(call_key, str) or not isinstance(terminal_path, Path):
            raise ParallelRehearsalError("budgeted provider preview is malformed")
        prior = self.ledger.state_of(call_key)
        if prior is not None and terminal_path.is_file():
            self.ledger.reconcile_terminal(call_key=call_key, terminal_path=terminal_path)
            result = invoke(prompt=prompt, input_ids=input_ids)
            if not isinstance(result, ProviderCallResult) or not result.cache_hit:
                raise ParallelRehearsalError("terminal reconciliation executed a provider call")
            return result
        if (
            prior is not None
            and prior.get("worker_id") != self.worker_id
            and self.reclaim_from_worker == prior.get("worker_id")
        ):
            assert self.reclaim_from_worker is not None
            self.ledger.reclaim_missing_terminal(
                call_key=call_key,
                prior_worker_id=self.reclaim_from_worker,
                new_worker_id=self.worker_id,
                terminal_path=terminal_path,
            )
        reservation_id = self.ledger.reserve(
            call_key=call_key,
            kind=self.kind,
            worker_id=self.worker_id,
            maximum_charge_usd=self.maximum_charge_usd,
        )
        result = invoke(prompt=prompt, input_ids=input_ids)
        if not isinstance(result, ProviderCallResult) or result.call_key != call_key:
            raise ParallelRehearsalError("provider result differs from reserved call")
        self.ledger.finalize(
            call_key=call_key,
            reservation_id=reservation_id,
            response_sha256=hash_file(result.terminal_path),
            reported_cost_usd=result.cost_usd,
        )
        return result


class ParallelRootStateMachine:
    """Validated root ownership, checkpoints, crashes, and explicit reclamation."""

    def __init__(self, path: Path, *, maximum_workers: int = 2) -> None:
        if maximum_workers < 1:
            raise ParallelRehearsalError("root concurrency must be at least one")
        self.path = path
        self.maximum_workers = maximum_workers

    def _validate(
        self, events: Iterable[Mapping[str, object]]
    ) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        roots: dict[str, dict[str, object]] = {}
        workers: dict[str, str] = {}
        for event in events:
            phase = event.get("phase")
            root_id = event.get("root_id")
            worker_id = event.get("worker_id")
            if (
                phase not in {"claimed", "slot_checkpoint", "crashed", "reclaimed", "complete"}
                or not isinstance(root_id, str)
                or not isinstance(worker_id, str)
            ):
                raise ParallelRehearsalError("parallel root state event is malformed")
            current = roots.get(root_id)
            if phase == "claimed":
                if current is not None:
                    raise ParallelRehearsalError("root has a second initial claim")
                if worker_id in workers:
                    raise ParallelRehearsalError("worker owns more than one unfinished root")
                roots[root_id] = {
                    "status": "active",
                    "owner": worker_id,
                    "generation": 0,
                    "checkpoints": {},
                }
                workers[worker_id] = root_id
                continue
            if current is None:
                raise ParallelRehearsalError("root transition lacks an initial claim")
            owner = current.get("owner")
            if phase == "slot_checkpoint":
                if current.get("status") != "active" or owner != worker_id:
                    raise ParallelRehearsalError("slot checkpoint is not owned by active worker")
                slot_id = event.get("slot_id")
                artifact_hash = event.get("artifact_hash")
                if not isinstance(slot_id, str) or not isinstance(artifact_hash, str):
                    raise ParallelRehearsalError("slot checkpoint is malformed")
                checkpoints = current["checkpoints"]
                assert isinstance(checkpoints, dict)
                prior = checkpoints.get(slot_id)
                if prior is not None and prior != artifact_hash:
                    raise ParallelRehearsalError("conflicting slot checkpoint")
                checkpoints[slot_id] = artifact_hash
            elif phase == "crashed":
                if current.get("status") != "active" or owner != worker_id:
                    raise ParallelRehearsalError("only the active owner may record a crash")
                current["status"] = "crashed"
                workers.pop(worker_id, None)
            elif phase == "reclaimed":
                if current.get("status") != "crashed":
                    raise ParallelRehearsalError("only a crashed root may be reclaimed")
                if event.get("prior_owner") != owner or worker_id in workers:
                    raise ParallelRehearsalError("root reclaim owner or worker conflicts")
                current["status"] = "active"
                current["owner"] = worker_id
                generation = current.get("generation")
                if not isinstance(generation, int) or isinstance(generation, bool):
                    raise ParallelRehearsalError("root generation is malformed")
                current["generation"] = generation + 1
                workers[worker_id] = root_id
            else:
                if current.get("status") != "active" or owner != worker_id:
                    raise ParallelRehearsalError("only the active owner may complete a root")
                manifest_hash = event.get("manifest_hash")
                if not isinstance(manifest_hash, str) or not manifest_hash:
                    raise ParallelRehearsalError("root completion lacks manifest hash")
                current["status"] = "complete"
                current["manifest_hash"] = manifest_hash
                workers.pop(worker_id, None)
        if len(workers) > self.maximum_workers:
            raise ParallelRehearsalError("parallel root-worker ceiling exceeded")
        return roots, workers

    def _transition(
        self,
        event: Mapping[str, object],
        *,
        replay_result: Literal["claimed", "replay_complete"] | None = None,
    ) -> Literal["claimed", "replay_complete"] | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                prior = [json.loads(line) for line in handle.read().splitlines() if line]
                roots, workers = self._validate(prior)
                root_id = str(event["root_id"])
                worker_id = str(event["worker_id"])
                phase = event["phase"]
                current = roots.get(root_id)
                if phase == "claimed" and current is not None:
                    if current.get("status") == "complete":
                        return "replay_complete"
                    if current.get("status") == "active" and current.get("owner") == worker_id:
                        return "claimed"
                    if current.get("status") == "crashed":
                        raise ParallelRehearsalError("crashed root requires explicit reclaim")
                    raise ParallelRehearsalError("root is already claimed by another worker")
                if phase == "claimed" and worker_id in workers:
                    raise ParallelRehearsalError("worker already owns another unfinished root")
                record = {"event_id": "sft2a-parallel:" + hash_canonical(event), **event}
                if any(item.get("event_id") == record["event_id"] for item in prior):
                    return replay_result
                self._validate([*prior, record])
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_json_bytes(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                return replay_result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def claim(self, *, root_id: str, worker_id: str) -> Literal["claimed", "replay_complete"]:
        result = self._transition(
            {"phase": "claimed", "root_id": root_id, "worker_id": worker_id},
            replay_result="claimed",
        )
        assert result is not None
        return result

    def checkpoint(self, *, root_id: str, worker_id: str, slot_id: str, artifact_hash: str) -> None:
        self._transition(
            {
                "phase": "slot_checkpoint",
                "root_id": root_id,
                "worker_id": worker_id,
                "slot_id": slot_id,
                "artifact_hash": artifact_hash,
            }
        )

    def crash(self, *, root_id: str, worker_id: str, reason: str) -> None:
        if not reason:
            raise ParallelRehearsalError("root crash reason is empty")
        self._transition(
            {
                "phase": "crashed",
                "root_id": root_id,
                "worker_id": worker_id,
                "reason": reason,
            }
        )

    def reclaim(self, *, root_id: str, prior_worker_id: str, worker_id: str) -> None:
        self._transition(
            {
                "phase": "reclaimed",
                "root_id": root_id,
                "worker_id": worker_id,
                "prior_owner": prior_worker_id,
            }
        )

    def complete(self, *, root_id: str, worker_id: str, manifest_hash: str) -> None:
        self._transition(
            {
                "phase": "complete",
                "root_id": root_id,
                "worker_id": worker_id,
                "manifest_hash": manifest_hash,
            }
        )

    def snapshot(self) -> dict[str, object]:
        roots, workers = self._validate(_events(self.path))
        return {"roots": roots, "unfinished_workers": workers}


# Compatibility name for historical tests and callers; all behavior is the validated state machine.
ParallelRootJournal = ParallelRootStateMachine


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
    "AtomicBudgetedProvider",
    "AtomicProviderBudget",
    "ParallelRehearsalError",
    "ParallelRootJournal",
    "ParallelRootStateMachine",
    "deterministic_parallel_compaction",
    "parallel_launch_lock",
    "prepare_parallel_rehearsal_path",
]

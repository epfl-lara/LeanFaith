# ruff: noqa: RUF001
"""Sprint-track SFT2A v5.2 execution paths.

This module owns the additive 72-hour sprint surfaces: the zero-Lean sprint sample verifier,
the synthetic malformed-provider injection check, the objective pilot thresholds, the controlled
stop/resume receipt, the detached 20-root/80-slot pilot worker/launch/health path, the
audit-only Kimi telemetry path over the completed historical run, and the bounded oracle-v2
live Lean gate. Generation, replay, Kimi telemetry, and evaluation each write their own durable
terminal; only the evaluation terminal decides ``scale_10k_authorized``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import (
    MAX_LEAN_RSS_GIB,
    MAX_LEAN_WORKERS,
    ReservationError,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.sft2a.certified_sample_v52 import verify_sprint_pilot_sample
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import (
    ORACLE_METHOD_VERSION_V2,
    SignatureOracle,
    SignatureOracleResult,
)
from leanfaith.sft2a.mechanisms import (
    BREAKING_MECHANISMS,
    PRESERVING_MECHANISMS,
    MechanismAssignment,
)
from leanfaith.sft2a.models import OneRootConfig, SFT2AOpusConfig
from leanfaith.sft2a.parallel_rehearsal import (
    AtomicProviderBudget,
    ParallelRehearsalError,
    ParallelRootStateMachine,
    parallel_launch_lock,
)
from leanfaith.sft2a.pipeline import _gold_blocklist, run_one_root
from leanfaith.sft2a.provider_rehearsal_v52 import (
    LoadedProviderAuthorizationV52,
    LoadedProviderRehearsalV52,
    ProviderRehearsalV52Error,
    _atomic_replace_json,
    _object,
    _root_output,
    _sample_rows,
    compact_provider_rehearsal_v52,
    load_provider_rehearsal_v52,
    run_provider_kimi_audit_v52,
    run_two_provider_workers_v52,
    verify_provider_replay_v52,
)
from leanfaith.sft2a.providers import ProviderCallResult

SPRINT_PILOT_VERSION = "leanfaith_sft2a_provider_rehearsal_v5_2_sprint_pilot_v1"
AUDIT_ONLY_VERSION = "leanfaith_sft2a_audit_only_kimi_v5_2_v1"
ORACLE_V2_GATE_VERSION = "leanfaith_sft2a_oracle_v2_live_gate_v1"
PILOT_MINIMUM_ACCEPTED_FRACTION = 0.7
PILOT_MAXIMUM_LEAN_INVALID_FRACTION = 0.25
PILOT_MAXIMUM_INFRASTRUCTURE_FAILURE_FRACTION = 0.02
PILOT_MAXIMUM_WALL_SECONDS = 1800.0


class SprintPilotError(RuntimeError):
    """A sprint sample, threshold, launch, audit-only, or live-gate invariant failed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _int_field(document: Mapping[str, object], key: str, default: int) -> int:
    value = document.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SprintPilotError(f"sprint config field {key} must be an integer")
    return value


def _append_stage(path: Path, event: Mapping[str, object]) -> dict[str, object]:
    """Append one timestamped stage event to the durable per-stage journal."""

    record: dict[str, object] = {"at": _now(), "pid": os.getpid(), **event}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(canonical_json_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def _stage_events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    return events


def _redirect_stdio(log_path: Path) -> int:
    """Send stdout/stderr to the persistent log while keeping one non-I/O PTY reference."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_descriptor = os.open(os.devnull, os.O_RDONLY)
    # Redirecting every descriptor away from the tmux PTY makes tmux observe EOF and deliver
    # SIGHUP before the worker can claim its Lean resource; keep one dormant reference.
    keepalive = os.dup(sys.stdout.fileno())
    os.set_inheritable(keepalive, False)
    try:
        os.dup2(log_descriptor, sys.stdout.fileno())
        os.dup2(log_descriptor, sys.stderr.fileno())
        os.dup2(null_descriptor, sys.stdin.fileno())
    finally:
        os.close(log_descriptor)
        os.close(null_descriptor)
    return keepalive


def _tmux_session_exists(session: str) -> bool:
    completed = subprocess.run(
        ("tmux", "has-session", "-t", session), check=False, capture_output=True
    )
    return completed.returncode == 0


def _tmux_pane_pid(session: str) -> int | None:
    completed = subprocess.run(
        ("tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"),
        check=False,
        capture_output=True,
        text=True,
    )
    pane = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    return int(pane) if completed.returncode == 0 and pane.isdigit() else None


def _process_tree(pane_pid: int) -> str:
    completed = subprocess.run(
        ("ps", "-o", "pid=,ppid=,stat=,etime=,rss=,cmd=", "--forest", "--ppid", str(pane_pid)),
        check=False,
        capture_output=True,
        text=True,
    )
    own = subprocess.run(
        ("ps", "-o", "pid=,ppid=,stat=,etime=,rss=,cmd=", "-p", str(pane_pid)),
        check=False,
        capture_output=True,
        text=True,
    )
    return (own.stdout + completed.stdout).strip()[:8000]


def _start_tmux(session: str, command: Sequence[str], cwd: Path) -> None:
    completed = subprocess.run(
        ("tmux", "new-session", "-d", "-s", session, "-c", str(cwd), shlex.join(command)),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SprintPilotError(
            f"tmux launch failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )


# --------------------------------------------------------------------------------------------
# Sample verifier and capacity checks
# --------------------------------------------------------------------------------------------


def verify_sprint_pilot_sample_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Zero-Lean sprint sample verifier pinned to the declared SHA (no 100-root preflight)."""

    if loaded.kind != "sprint":
        raise SprintPilotError("sprint sample verification requires a sprint pilot config")
    receipt_path = loaded.output_root / "preflight/sprint_sample_verification.json"
    if receipt_path.is_file():
        existing = _object(receipt_path)
        if (
            existing.get("verified") is True
            and existing.get("sample_sha256") == loaded.document["sample_sha256"]
            and existing.get("provider_config_sha256") == loaded.sha256
            and hash_file(loaded.sample_path) == loaded.document["sample_sha256"]
        ):
            return existing
    completed_paths = [
        Path(str(item))
        for item in cast(list[object], loaded.document.get("completed_root_sample_paths", []))
    ]
    _groups, blocked_hashes = _gold_blocklist(loaded.base)
    mix = loaded.document.get("expected_source_mix")
    receipt = verify_sprint_pilot_sample(
        loaded.sample_path,
        expected_sha256=str(loaded.document["sample_sha256"]),
        expected_source_mix=cast(dict[str, int], mix) if isinstance(mix, dict) else None,
        completed_sample_paths=completed_paths,
        blocked_signature_hashes=blocked_hashes,
        verify_certificates=True,
    )
    receipt["provider_config_sha256"] = loaded.sha256
    _atomic_replace_json(receipt_path, receipt)
    return receipt


def sprint_capacity_check(*, lean_workers: int, lean_rss_gib: float) -> dict[str, object]:
    """Recheck the atomic host reservation ledger for the requested Lean capacity."""

    claims = list_reservations()
    used_workers = sum(item.lean_workers for item in claims)
    used_rss = sum(item.lean_rss_gib for item in claims)
    return {
        "checked_at": _now(),
        "claims": [
            {
                "task": item.task,
                "lean_workers": item.lean_workers,
                "lean_rss_gib": item.lean_rss_gib,
                "pid": item.pid,
                "owner_session": item.owner_session,
            }
            for item in claims
        ],
        "used_workers": used_workers,
        "used_rss_gib": used_rss,
        "requested_workers": lean_workers,
        "requested_rss_gib": lean_rss_gib,
        "cap_workers": MAX_LEAN_WORKERS,
        "cap_rss_gib": MAX_LEAN_RSS_GIB,
        "capacity_available_now": (
            used_workers + lean_workers <= MAX_LEAN_WORKERS
            and used_rss + lean_rss_gib <= MAX_LEAN_RSS_GIB
        ),
    }


# --------------------------------------------------------------------------------------------
# Synthetic malformed-provider injection through the executable one-root path
# --------------------------------------------------------------------------------------------


class _ScriptedProvider:
    def __init__(self, provider_id: str, responses: Sequence[dict[str, object]]) -> None:
        self.provider_id = provider_id
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt
        index = len(self.calls)
        if index >= len(self.responses):
            raise SprintPilotError(f"scripted provider {self.provider_id} exhausted its script")
        self.calls.append(tuple(input_ids))
        return ProviderCallResult(
            call_key=f"synthetic:{self.provider_id}:{index}",
            provider_id=self.provider_id,
            structured=dict(self.responses[index]),
            usage={"input_tokens": 1, "output_tokens": 1},
            cost_usd=0.0,
            elapsed_seconds=0.0,
            cache_hit=False,
            terminal_path=Path(f"/nonexistent/synthetic-{self.provider_id}-{index}.json"),
        )


class _SyntheticOracle:
    method_version = ORACLE_METHOD_VERSION_V2
    cache_version = "v2"

    def __init__(self, expected_reference_goal: str) -> None:
        self.expected_reference_goal = expected_reference_goal
        self.calls: list[tuple[str, str]] = []

    def elaborate(
        self, signature: str, *, endpoint_role: Literal["reference", "candidate"]
    ) -> SignatureOracleResult:
        self.calls.append((signature, endpoint_role))
        goal = self.expected_reference_goal if endpoint_role == "reference" else f"⊢ {signature}"
        digest = hash_canonical({"synthetic_signature": signature})
        return SignatureOracleResult(
            status="valid",
            cache_key=f"synthetic:{digest}",
            cache_hit=False,
            signature_sha256=digest,
            goal_v1=goal,
            sidecar={"record": {"goal_v1": goal, "provenance": {"expr_hash": digest}}},
            lean_status="valid",
            request_hash=None,
            elapsed_ms=1,
            raw_response_path=None,
            detail="synthetic elaboration; no Lean process",
        )

    def close(self) -> None:
        return None


def _closure_checks() -> dict[str, object]:
    return {
        "entire_universally_closed_proposition": True,
        "argument_swapping": "not_applicable",
        "symmetry": "not_applicable",
        "antisymmetry": "not_applicable",
        "extensionality": "not_applicable",
        "recoverable_boundary_cases": "checked_no_effect",
    }


def _judge(
    verdict: str, *, error_type: str = "none", confidence: str = "high"
) -> dict[str, object]:
    rationale = (
        "Both closed propositions express the same claim."
        if verdict == "equivalent"
        else "The second closed proposition changes the conclusion."
    )
    return {
        "schema_version": 5,
        "verdict": verdict,
        "confidence": confidence,
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": error_type,
        "rationale": rationale,
        "closure_checks": _closure_checks(),
    }


def _proposal(polarity: str, family: str, signature: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "requested_polarity": polarity,
        "mechanism": family,
        "applicability_reason": "synthetic injection check",
        "candidate_signature": signature,
        "change_summary": "synthetic candidate for the malformed-output injection check",
        "judge_trap": "none; synthetic",
        "informative": True,
        "substantive_change": True,
        "proof_free": True,
    }


def run_malformed_injection_check(
    base: LoadedSFT2AConfig, *, output_root: Path
) -> dict[str, object]:
    """Prove that injected malformed proposer and judge output never crashes the root path.

    The check drives the real ``run_one_root`` with synthetic providers and a synthetic oracle:
    one judge answer is semantically malformed then repaired on the single retry, one slot's
    judge answers stay malformed twice and route to a slot retry, and one proposer answer is
    schema-invalid. It executes zero real provider calls and zero Lean requests.
    """

    staging = str(output_root / "staging")
    update: dict[str, object] = {"staging_root": staging}
    if isinstance(base.config, SFT2AOpusConfig):
        update["run_layout"] = base.config.run_layout.model_copy(
            update={"shared_cache_root": staging}
        )
    config = base.config.model_copy(update=update)
    loaded = replace(
        base, config=config, config_hash=hash_canonical(config.model_dump(mode="json"))
    )
    preserving = [spec for spec in PRESERVING_MECHANISMS if spec.applicability == "general"]
    breaking = [spec for spec in BREAKING_MECHANISMS if spec.applicability == "general"]
    if not preserving or not breaking:
        preserving, breaking = list(PRESERVING_MECHANISMS), list(BREAKING_MECHANISMS)
    plan = {
        "preserve_0": MechanismAssignment(
            preserving[0].family, "preserving", preserving[0].instruction, "general", "synthetic"
        ),
        "preserve_1": MechanismAssignment(
            preserving[-1].family, "preserving", preserving[-1].instruction, "general", "synthetic"
        ),
        "break_0": MechanismAssignment(
            breaking[0].family, "breaking", breaking[0].instruction, "general", "synthetic"
        ),
        "break_1": MechanismAssignment(
            breaking[-1].family, "breaking", breaking[-1].instruction, "general", "synthetic"
        ),
    }
    binders = "∀ {α : Type} [inst : Preorder α] {a b c : α}, "
    proposer = _ScriptedProvider(
        loaded.config.proposer.provider_id,
        [
            _proposal("preserving", plan["preserve_0"].family, binders + "b ≤ c → a ≤ b → a ≤ c"),
            _proposal("preserving", plan["preserve_1"].family, binders + "a ≤ b ∧ b ≤ c → a ≤ c"),
            _proposal("preserving", plan["preserve_1"].family, binders + "a ≤ b → (b ≤ c → a ≤ c)"),
            _proposal("breaking", plan["break_0"].family, binders + "a ≤ b → b ≤ c → c ≤ a"),
            {"schema_version": 5, "requested_polarity": "breaking"},
            _proposal("breaking", plan["break_1"].family, binders + "a ≤ b → a ≤ c"),
        ],
    )
    judge = _ScriptedProvider(
        loaded.config.claude_judge.provider_id,
        [
            _judge("equivalent", error_type="insufficient_confidence"),
            _judge("equivalent"),
            _judge("equivalent", confidence="low"),
            _judge("equivalent", error_type="ambiguous"),
            _judge("equivalent"),
            _judge("non_equivalent"),
            _judge("non_equivalent"),
        ],
    )
    oracle = _SyntheticOracle(loaded.config.root.expected_reference_goal_v1)
    result = run_one_root(
        loaded,
        proposer=proposer,
        claude_judge=judge,
        oracle=oracle,
        output_root=output_root / "one_root",
        enforce_expected_reference_goal=True,
        enforce_smoke_ceilings=False,
        mechanism_plan=plan,
        enforce_closure_canaries=False,
    )
    counts = cast(dict[str, object], result.manifest["counts"])
    expected = {
        "accepted": 4,
        "judge_malformed_exhausted": 1,
        "judge_malformed_attempts": 3,
        "proposer_schema_rejections": 1,
        "retry_slots": 2,
    }
    observed = {key: counts.get(key) for key in expected}
    passed = observed == expected and not result.replayed
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_malformed_injection_check_v1",
        "passed": passed,
        "expected_counts": expected,
        "observed_counts": observed,
        "proposer_calls": len(proposer.calls),
        "judge_calls": len(judge.calls),
        "synthetic_lean_elaborations": len(oracle.calls),
        "real_provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "crashed": False,
        "one_root_manifest_sha256": hash_file(result.output_root / "manifest.json"),
    }
    _atomic_replace_json(output_root / "malformed_injection_receipt.json", receipt)
    if not passed:
        raise SprintPilotError(f"malformed injection check failed: {observed} != {expected}")
    return receipt


# --------------------------------------------------------------------------------------------
# Objective thresholds
# --------------------------------------------------------------------------------------------


def _root_manifests(loaded: LoadedProviderRehearsalV52) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for row in _sample_rows(loaded.sample_path):
        root = OneRootConfig.model_validate(row["root"])
        manifest_path = _root_output(loaded, root.root_id) / "manifest.json"
        if manifest_path.is_file():
            manifests.append(_object(manifest_path))
    return manifests


def _provider_id_for_kind(loaded: LoadedProviderRehearsalV52, kind: str) -> str:
    config = loaded.base.config
    if kind == "proposer":
        return config.proposer.provider_id
    if kind == "opus":
        return config.claude_judge.provider_id
    return config.lemex_auditor.provider_id


def measure_infrastructure_failures(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Count provider infrastructure attempts, Lean infrastructure attempts, and root crashes."""

    ledger_path = loaded.output_root / "provider_budget.jsonl"
    states: dict[str, dict[str, object]] = {}
    if ledger_path.is_file():
        ledger = AtomicProviderBudget(ledger_path, loaded.ceilings)
        with ledger._thread_lock:
            states = dict(ledger._states_locked())
    staging = Path(loaded.base.config.staging_root) / "provider_calls"
    provider_failures = 0
    schema_invalid_attempts = 0
    for call_key, state in states.items():
        provider_dir = (
            staging / _provider_id_for_kind(loaded, str(state["kind"])) / call_key[:2] / call_key
        )
        attempts_dir = provider_dir / "attempts"
        if not attempts_dir.is_dir():
            continue
        for attempt in sorted(attempts_dir.iterdir()):
            failure = attempt / "failure.json"
            if not failure.is_file():
                continue
            detail = str(_object(failure).get("detail", ""))
            if detail.startswith("provider output schema violation:"):
                schema_invalid_attempts += 1
            else:
                provider_failures += 1
    lean_failures = 0
    for row in _sample_rows(loaded.sample_path):
        root = OneRootConfig.model_validate(row["root"])
        attempts_path = _root_output(loaded, root.root_id) / "attempts/terminal_attempts.jsonl"
        if attempts_path.is_file():
            for line in attempts_path.read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("status") == "lean_infrastructure":
                    lean_failures += 1
    crashes = 0
    state_path = loaded.output_root / "root_state.jsonl"
    if state_path.is_file():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("phase") == "crashed":
                crashes += 1
    finalized_calls = sum(state.get("phase") == "finalized" for state in states.values())
    failures = provider_failures + lean_failures + crashes
    denominator = finalized_calls + provider_failures
    return {
        "provider_infrastructure_failures": provider_failures,
        "provider_schema_invalid_attempts": schema_invalid_attempts,
        "lean_infrastructure_attempts": lean_failures,
        "root_crashes": crashes,
        "finalized_provider_calls": finalized_calls,
        "infrastructure_failures": failures,
        "infrastructure_failure_rate": (failures / denominator) if denominator else 0.0,
    }


def evaluate_sprint_pilot_thresholds(
    loaded: LoadedProviderRehearsalV52,
    *,
    compaction: Mapping[str, object],
    replay: Mapping[str, object],
    generation_wall_seconds: float,
    malformed_injection: Mapping[str, object],
    resume_check: Mapping[str, object],
    root_manifests: Sequence[Mapping[str, object]] | None = None,
    infrastructure: Mapping[str, object] | None = None,
    role: Literal["pilot", "shard"] = "pilot",
    minimum_accepted_rows_per_minute: float = 8.0,
) -> dict[str, object]:
    """Evaluate the objective pilot thresholds from durable artifacts only.

    A shard replaces the 30-minute wall bound with the long-run throughput bound (accepted rows
    per minute of generation time) while keeping every other check.

    The pilot passes only when at least 70% of planned slots are accepted (56/80), fewer than
    25% of elaborated candidates are Lean-invalid (fewer than 20/80 planned slots), zero
    accepted self-pairs or duplicates exist, the injected malformed provider output check
    passed, infrastructure failures are below 2%, completed-root resume executed zero new
    provider and Lean calls, and generation wall time is at most 30 minutes.
    """

    sample_rows = _sample_rows(loaded.sample_path)
    planned_slots = len(sample_rows) * 4
    manifests = list(root_manifests) if root_manifests is not None else _root_manifests(loaded)
    lean_invalid_attempts = 0
    candidate_requests = 0
    candidate_attempts = 0
    for manifest in manifests:
        counts = cast(Mapping[str, object], manifest.get("counts", {}))
        lean = cast(Mapping[str, object], manifest.get("lean", {}))
        lean_invalid_attempts += int(cast(int, counts.get("lean_invalid_attempts", 0)))
        candidate_requests += int(cast(int, lean.get("candidate_requests", 0)))
        candidate_attempts += int(cast(int, counts.get("candidate_attempts", 0)))
    infra = (
        dict(infrastructure)
        if infrastructure is not None
        else measure_infrastructure_failures(loaded)
    )
    accepted = int(cast(int, compaction.get("accepted_rows", 0)))
    self_pairs = int(cast(int, compaction.get("self_pairs", 0)))
    duplicates = int(cast(int, compaction.get("candidate_duplicates", 0)))
    replay_calls = int(cast(int, replay.get("provider_calls_executed", -1)))
    replay_lean = int(cast(int, replay.get("lean_requests_executed", -1)))
    minimum_accepted = -(-planned_slots * 7 // 10)
    lean_invalid_rate = lean_invalid_attempts / candidate_requests if candidate_requests else 0.0
    infra_rate = float(cast(float, infra.get("infrastructure_failure_rate", 0.0)))
    resume_calls = int(
        cast(int, resume_check.get("provider_calls_for_completed_roots_after_resume", -1))
    )
    resume_lean = int(
        cast(int, resume_check.get("lean_requests_for_completed_roots_after_resume", -1))
    )
    checks: dict[str, bool] = {
        "accepted_at_least_70pct": accepted >= minimum_accepted,
        "lean_invalid_below_25pct": (
            lean_invalid_rate < PILOT_MAXIMUM_LEAN_INVALID_FRACTION
            and lean_invalid_attempts < planned_slots * PILOT_MAXIMUM_LEAN_INVALID_FRACTION
        ),
        "zero_accepted_self_pairs": self_pairs == 0,
        "zero_accepted_duplicates": duplicates == 0,
        "injected_malformed_output_did_not_crash": bool(malformed_injection.get("passed")),
        "infrastructure_failures_below_2pct": (
            infra_rate < PILOT_MAXIMUM_INFRASTRUCTURE_FAILURE_FRACTION
        ),
        "completed_root_resume_zero_calls": (
            replay_calls == 0
            and replay_lean == 0
            and bool(replay.get("reproducible"))
            and resume_calls == 0
            and resume_lean == 0
            and bool(resume_check.get("manifests_unchanged"))
        ),
        "all_roots_have_manifests": len(manifests) == len(sample_rows),
    }
    throughput = accepted / (generation_wall_seconds / 60.0) if generation_wall_seconds > 0 else 0.0
    if role == "pilot":
        checks["wall_time_at_most_30min"] = generation_wall_seconds <= PILOT_MAXIMUM_WALL_SECONDS
    else:
        checks["accepted_throughput_at_least_minimum"] = (
            throughput >= minimum_accepted_rows_per_minute
        )
    passed = all(checks.values())
    return {
        "version": "leanfaith_sft2a_sprint_pilot_thresholds_v1",
        "role": role,
        "accepted_rows_per_minute": throughput,
        "minimum_accepted_rows_per_minute": (
            minimum_accepted_rows_per_minute if role == "shard" else None
        ),
        "roots": len(sample_rows),
        "planned_slots": planned_slots,
        "accepted_rows": accepted,
        "accepted_fraction": accepted / planned_slots if planned_slots else 0.0,
        "minimum_accepted": minimum_accepted,
        "lean_invalid_attempts": lean_invalid_attempts,
        "candidate_lean_requests": candidate_requests,
        "candidate_attempts": candidate_attempts,
        "lean_invalid_rate": lean_invalid_rate,
        "lean_invalid_per_planned_slot": (
            lean_invalid_attempts / planned_slots if planned_slots else 0.0
        ),
        "self_pairs": self_pairs,
        "candidate_duplicates": duplicates,
        "infrastructure": infra,
        "replay_provider_calls": replay_calls,
        "replay_lean_requests": replay_lean,
        "resume_check": dict(resume_check),
        "malformed_injection_passed": bool(malformed_injection.get("passed")),
        "generation_wall_seconds": generation_wall_seconds,
        "maximum_wall_seconds": PILOT_MAXIMUM_WALL_SECONDS,
        "checks": checks,
        "failed_checks": sorted(name for name, ok in checks.items() if not ok),
        "passed": passed,
        "scale_10k_authorized": passed,
        "scale_50k_authorized": False,
    }


# --------------------------------------------------------------------------------------------
# Controlled stop/resume
# --------------------------------------------------------------------------------------------


def _completed_root_ids(loaded: LoadedProviderRehearsalV52) -> list[str]:
    state_path = loaded.output_root / "root_state.jsonl"
    if not state_path.is_file():
        return []
    roots = ParallelRootStateMachine(state_path).snapshot()["roots"]
    assert isinstance(roots, dict)
    return sorted(
        root_id
        for root_id, state in roots.items()
        if isinstance(state, dict) and state.get("status") == "complete"
    )


def snapshot_completed_roots(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Record completed roots, their manifest hashes, their call keys, and the ledger state."""

    completed = _completed_root_ids(loaded)
    manifest_hashes: dict[str, str] = {}
    call_keys: dict[str, list[str]] = {}
    for root_id in completed:
        root_output = _root_output(loaded, root_id)
        manifest_path = root_output / "manifest.json"
        manifest_hashes[root_id] = hash_file(manifest_path)
        usage = cast(Mapping[str, object], _object(manifest_path).get("llm", {})).get("usage", [])
        call_keys[root_id] = sorted(
            str(cast(Mapping[str, object], item)["call_key"])
            for item in cast(list[object], usage)
            if isinstance(item, Mapping)
        )
    ledger_path = loaded.output_root / "provider_budget.jsonl"
    finalized: set[str] = set()
    if ledger_path.is_file():
        ledger = AtomicProviderBudget(ledger_path, loaded.ceilings)
        with ledger._thread_lock:
            finalized = {
                key
                for key, state in ledger._states_locked().items()
                if state.get("phase") == "finalized"
            }
    lean_cache_root = Path(loaded.base.config.staging_root) / "lean_cache"
    lean_cache_files = (
        sum(1 for path in lean_cache_root.rglob("*.json")) if lean_cache_root.is_dir() else 0
    )
    return {
        "taken_at": _now(),
        "completed_root_ids": completed,
        "manifest_hashes": manifest_hashes,
        "call_keys": call_keys,
        "finalized_call_keys_count": len(finalized),
        "finalized_call_keys_hash": hash_canonical(sorted(finalized)),
        "unfinalized_completed_root_call_keys": sorted(
            key for keys in call_keys.values() for key in keys if key not in finalized
        ),
        "lean_cache_files": lean_cache_files,
    }


def controlled_resume_receipt(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object]:
    """Prove that roots completed before a resume received zero new provider or Lean calls."""

    completed_before = cast(list[str], before.get("completed_root_ids", []))
    hashes_before = cast(Mapping[str, str], before.get("manifest_hashes", {}))
    hashes_after = cast(Mapping[str, str], after.get("manifest_hashes", {}))
    manifests_unchanged = all(
        hashes_after.get(root_id) == hashes_before.get(root_id) for root_id in completed_before
    )
    unfinalized_before = cast(list[str], before.get("unfinalized_completed_root_call_keys", []))
    keys_before = cast(Mapping[str, list[str]], before.get("call_keys", {}))
    keys_after = cast(Mapping[str, list[str]], after.get("call_keys", {}))
    same_keys = all(
        keys_after.get(root_id) == keys_before.get(root_id) for root_id in completed_before
    )
    return {
        "version": "leanfaith_sft2a_sprint_controlled_resume_v1",
        "completed_before_resume": completed_before,
        "completed_after_resume": cast(list[str], after.get("completed_root_ids", [])),
        "manifests_unchanged": manifests_unchanged and same_keys,
        "provider_calls_for_completed_roots_after_resume": len(unfinalized_before),
        "lean_requests_for_completed_roots_after_resume": 0 if manifests_unchanged else -1,
        "completed_roots_before_resume": len(completed_before),
    }


# --------------------------------------------------------------------------------------------
# Detached pilot worker, launch, and health
# --------------------------------------------------------------------------------------------


def _sprint_authorization(loaded: LoadedProviderRehearsalV52) -> LoadedProviderAuthorizationV52:
    document: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_pilot_authority_v1",
        "sprint_authority": "plans/72h_sft_data_sprint_2026-09-01.md",
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": loaded.document["sample_sha256"],
        "authorized": True,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
    }
    path = loaded.output_root / "detached/sprint_authority.json"
    _atomic_replace_json(path, document)
    return LoadedProviderAuthorizationV52(path=path, document=document, sha256=hash_file(path))


def _claim_with_wait(
    loaded: LoadedProviderRehearsalV52,
    *,
    stage_path: Path,
    wait_seconds: float,
    poll_seconds: float,
) -> dict[str, object]:
    resource_task = str(loaded.document["resource_task"])
    lean_workers = _int_field(loaded.document, "maximum_total_lean_workers", 2)
    lean_rss = float(cast(float, loaded.document.get("maximum_measured_rss_gib", 40.0)))
    if lean_workers != 2 or lean_rss != 40.0:
        raise SprintPilotError("sprint pilot requires exactly two persistent Lean workers/40 GiB")
    deadline = time.monotonic() + wait_seconds
    waits = 0
    while True:
        try:
            reservation = claim_resources(
                task=resource_task,
                lean_workers=lean_workers,
                lean_rss_gib=lean_rss,
                gpu=False,
                pid=os.getpid(),
                owner_session=str(loaded.document["tmux_session"]),
                worktree=loaded.base.repo_root,
            )
        except ReservationError as exc:
            if "cap exceeded" not in str(exc) and "already reserved" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise SprintPilotError(f"Lean capacity unavailable after waiting: {exc}") from exc
            if waits % 10 == 0:
                _append_stage(
                    stage_path,
                    {
                        "event": "waiting_for_lean_capacity",
                        "detail": str(exc),
                        "capacity": sprint_capacity_check(
                            lean_workers=lean_workers, lean_rss_gib=lean_rss
                        ),
                    },
                )
            waits += 1
            time.sleep(poll_seconds)
            continue
        return {
            "task": reservation.task,
            "lean_workers": reservation.lean_workers,
            "lean_rss_gib": reservation.lean_rss_gib,
            "pid": reservation.pid,
            "worktree": reservation.worktree,
            "created_at": reservation.created_at,
            "waits": waits,
        }


def run_detached_sprint_pilot_v52(
    loaded: LoadedProviderRehearsalV52,
    *,
    wait_for_capacity_seconds: float = 12 * 3600.0,
    capacity_poll_seconds: float = 60.0,
    redirect_stdio: bool = True,
) -> dict[str, object]:
    """Detached 20-root/80-slot pilot with separate per-stage durable terminals.

    Order: launch receipt, run lock, malformed injection check (no Lean), truthful two-worker/
    40 GiB claim (waiting for capacity when another task holds it), controlled generation stop
    after the configured number of completed roots plus resume, remaining generation,
    compaction, zero-call replay, claim release, evaluation, then Kimi telemetry outside the
    Lean reservation. Only the evaluation terminal sets ``scale_10k_authorized``.
    """

    if loaded.kind != "sprint":
        raise SprintPilotError("detached sprint pilot requires a sprint pilot config")
    output_root = loaded.output_root
    detached = output_root / "detached"
    detached.mkdir(parents=True, exist_ok=True)
    log_path = detached / "combined.log"
    stage_path = detached / "stage_journal.jsonl"
    terminal_path = detached / "terminal_status.json"
    keepalive: int | None = _redirect_stdio(log_path) if redirect_stdio else None
    started_at = _now()
    invocation = hash_canonical({"started_at": started_at, "pid": os.getpid()})[:12]
    print(json.dumps({"event": "worker_stdio_ready", "pid": os.getpid()}), flush=True)
    _append_stage(stage_path, {"event": "worker_started", "invocation": invocation})
    terminal: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_pilot_terminal_v1",
        "status": "started",
        "invocation": invocation,
    }
    _atomic_replace_json(terminal_path, terminal)
    resource_task = str(loaded.document["resource_task"])
    claimed = False
    try:
        with parallel_launch_lock(detached / "run.lock"):
            prior = _object(terminal_path) if terminal_path.is_file() else {}
            if prior.get("status") in {"complete", "threshold_failed"}:
                raise SprintPilotError("finished sprint pilot cannot be restarted")
            authorization = _sprint_authorization(loaded)
            command = (
                sys.executable,
                "-m",
                "leanfaith.sft2a",
                "--provider-rehearsal-config",
                str(loaded.path),
                "detached-sprint-pilot-v5-2-worker",
            )
            launch_receipt: dict[str, object] = {
                "version": "leanfaith_sft2a_sprint_pilot_launch_receipt_v1",
                "invocation": invocation,
                "started_at": started_at,
                "pid": os.getpid(),
                "session_name": loaded.document["tmux_session"],
                "sanitized_command": shlex.join(command),
                "provider_config_path": str(loaded.path),
                "provider_config_sha256": loaded.sha256,
                "base_config_hash": loaded.base.config_hash,
                "sample_sha256": loaded.document["sample_sha256"],
                "implementation_commit": _git_head(loaded.base.repo_root),
                "output_root": str(output_root),
                "shared_cache_root": loaded.base.config.staging_root,
                "provider_budget_ledger": str(output_root / "provider_budget.jsonl"),
                "root_journal": str(output_root / "root_state.jsonl"),
                "stage_journal": str(stage_path),
                "combined_log": str(log_path),
                "ceilings": loaded.ceilings.model_dump(mode="json"),
                "resource_task": resource_task,
                "maximum_root_workers": 2,
                "maximum_total_lean_workers": 2,
                "maximum_measured_rss_gib": 40.0,
                "provider_concurrency": _int_field(loaded.document, "provider_concurrency", 8),
                "kimi_audit_rows": _int_field(loaded.document, "kimi_audit_rows", 8),
                "resume_command": (
                    f"uv run python -m leanfaith.sft2a --provider-rehearsal-config {loaded.path} "
                    "resume-sprint-pilot-v5-2"
                ),
                "health_command": (
                    f"uv run python -m leanfaith.sft2a --provider-rehearsal-config {loaded.path} "
                    "sprint-pilot-v5-2-health"
                ),
                "stop_conditions": [
                    "any objective threshold fails (terminal status threshold_failed)",
                    "a root crashes deterministically twice (crash recorded in root_state)",
                    "provider ceilings reached (atomic ledger refuses reservation)",
                    "Lean capacity unavailable for 12 hours",
                ],
            }
            with (detached / "launch_history.jsonl").open("ab") as handle:
                handle.write(canonical_json_bytes(launch_receipt) + b"\n")
            _atomic_replace_json(detached / "launch_receipt.json", launch_receipt)
            prerequisites = require_sprint_prerequisite_receipts(loaded)
            _append_stage(stage_path, {"event": "prerequisite_receipts", **prerequisites})
            injection = run_malformed_injection_check(
                loaded.base, output_root=output_root / "checks/malformed_injection"
            )
            _append_stage(stage_path, {"event": "malformed_injection_check", "passed": True})
            claim = _claim_with_wait(
                loaded,
                stage_path=stage_path,
                wait_seconds=wait_for_capacity_seconds,
                poll_seconds=capacity_poll_seconds,
            )
            claimed = True
            _append_stage(stage_path, {"event": "resource_claimed", "claim": claim})
            generation_started = time.monotonic()
            role = str(loaded.document.get("sprint_role", "pilot"))
            concurrency = _effective_concurrency(loaded, detached)
            _append_stage(stage_path, {"event": "effective_concurrency", "value": concurrency})
            stop_after = _int_field(
                loaded.document,
                "controlled_stop_after_completed_roots",
                1 if role == "pilot" else 0,
            )
            resume_check: dict[str, object]
            try:
                already_complete = _completed_root_ids(loaded)
                phase_one: list[dict[str, object]] = []
                if stop_after > 0 and len(already_complete) < len(_sample_rows(loaded.sample_path)):
                    _append_stage(
                        stage_path,
                        {"event": "generation_phase_started", "phase": 1, "stop_after": stop_after},
                    )
                    phase_one = run_two_provider_workers_v52(
                        loaded,
                        authorization,
                        provider_concurrency=concurrency,
                        stop_after_completed_roots=stop_after,
                        stop_request_path=detached / "stop_requested",
                    )
                    _atomic_replace_json(
                        detached / "generation_stopped_terminal.json",
                        {
                            "version": "leanfaith_sft2a_sprint_generation_stage_v1",
                            "status": "stopped_controlled",
                            "workers": phase_one,
                        },
                    )
                    _append_stage(
                        stage_path,
                        {"event": "generation_phase_stopped", "phase": 1, "workers": phase_one},
                    )
                before = snapshot_completed_roots(loaded)
                _atomic_replace_json(detached / "resume_snapshot_before.json", before)
                _append_stage(stage_path, {"event": "generation_phase_started", "phase": 2})
                phase_two = run_two_provider_workers_v52(
                    loaded,
                    authorization,
                    provider_concurrency=concurrency,
                    stop_request_path=detached / "stop_requested",
                )
                after = snapshot_completed_roots(loaded)
                _atomic_replace_json(detached / "resume_snapshot_after.json", after)
                resume_check = controlled_resume_receipt(before, after)
                _atomic_replace_json(detached / "controlled_resume_receipt.json", resume_check)
                if phase_two and bool(phase_two[0].get("stopped")):
                    generation_terminal: dict[str, object] = {
                        "version": "leanfaith_sft2a_sprint_generation_stage_v1",
                        "status": "stopped",
                        "workers": [*phase_one, *phase_two],
                    }
                    _atomic_replace_json(detached / "generation_terminal.json", generation_terminal)
                    raise SprintPilotError("generation stopped by request before completion")
                generation_terminal = {
                    "version": "leanfaith_sft2a_sprint_generation_stage_v1",
                    "status": "complete",
                    "workers": [*phase_one, *phase_two],
                }
                _atomic_replace_json(detached / "generation_terminal.json", generation_terminal)
                _append_stage(stage_path, {"event": "generation_complete"})
                compacted = compact_provider_rehearsal_v52(loaded)
                _append_stage(
                    stage_path,
                    {"event": "compaction_complete", "accepted_rows": compacted["accepted_rows"]},
                )
                replay = verify_provider_replay_v52(loaded)
                _atomic_replace_json(
                    detached / "replay_terminal.json",
                    {
                        "version": "leanfaith_sft2a_sprint_replay_stage_v1",
                        "status": "complete",
                        "replay": replay,
                    },
                )
                _append_stage(stage_path, {"event": "replay_complete"})
            finally:
                generation_wall = time.monotonic() - generation_started
                release_resources(task=resource_task)
                claimed = False
                _append_stage(
                    stage_path,
                    {"event": "resource_released", "generation_wall_seconds": generation_wall},
                )
            total_generation_wall = sum(
                float(cast(float, event.get("generation_wall_seconds", 0.0)))
                for event in _stage_events(stage_path)
                if event.get("event") == "resource_released"
            )
            evaluation = evaluate_sprint_pilot_thresholds(
                loaded,
                compaction=compacted,
                replay=replay,
                generation_wall_seconds=total_generation_wall,
                malformed_injection=injection,
                resume_check=resume_check,
                role="shard" if role == "shard" else "pilot",
                minimum_accepted_rows_per_minute=float(
                    cast(float, loaded.document.get("minimum_accepted_rows_per_minute", 8.0))
                ),
            )
            evaluation["effective_provider_concurrency"] = concurrency
            _atomic_replace_json(detached / "evaluation_terminal.json", evaluation)
            _append_stage(
                stage_path,
                {"event": "evaluation_complete", "passed": evaluation["passed"]},
            )
            if role == "shard":
                fraction = float(cast(float, loaded.document.get("kimi_audit_fraction", 0.1)))
                kimi_rows = min(
                    _int_field(loaded.document, "kimi_audit_rows_maximum", 80),
                    -(-int(cast(int, compacted["accepted_rows"])) * int(fraction * 1000) // 1000),
                )
            else:
                kimi_rows = _int_field(loaded.document, "kimi_audit_rows", 8)
            audit_terminal: dict[str, object]
            try:
                audit = run_provider_kimi_audit_v52(
                    loaded, authorization, kimi_count=kimi_rows, concurrency=8
                )
                audit_terminal = {
                    "version": "leanfaith_sft2a_sprint_kimi_telemetry_stage_v1",
                    "status": "complete",
                    "held_lean_reservation": False,
                    "audit": audit,
                }
            except (ProviderRehearsalV52Error, ParallelRehearsalError) as exc:
                audit_terminal = {
                    "version": "leanfaith_sft2a_sprint_kimi_telemetry_stage_v1",
                    "status": "failed_resumable",
                    "held_lean_reservation": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:2000],
                }
            _atomic_replace_json(detached / "audit_terminal.json", audit_terminal)
            _append_stage(
                stage_path, {"event": "kimi_telemetry", "status": audit_terminal["status"]}
            )
            terminal = {
                "version": "leanfaith_sft2a_sprint_pilot_terminal_v1",
                "status": "complete" if evaluation["passed"] else "threshold_failed",
                "invocation": invocation,
                "generation_terminal_sha256": hash_file(detached / "generation_terminal.json"),
                "replay_terminal_sha256": hash_file(detached / "replay_terminal.json"),
                "evaluation_terminal_sha256": hash_file(detached / "evaluation_terminal.json"),
                "audit_terminal_sha256": hash_file(detached / "audit_terminal.json"),
                "accepted_rows": compacted["accepted_rows"],
                "generation_wall_seconds": generation_wall,
                "failed_checks": evaluation["failed_checks"],
                "kimi_telemetry_status": audit_terminal["status"],
                "scale_10k_authorized": bool(evaluation["passed"]),
                "scale_50k_authorized": False,
                "completed_at": _now(),
            }
    except Exception as exc:
        if claimed:
            with contextlib.suppress(ReservationError):
                release_resources(task=resource_task)
        terminal = {
            "version": "leanfaith_sft2a_sprint_pilot_terminal_v1",
            "status": "failed",
            "invocation": invocation,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "scale_10k_authorized": False,
            "scale_50k_authorized": False,
            "failed_at": _now(),
        }
        _atomic_replace_json(terminal_path, terminal)
        _append_stage(stage_path, {"event": "worker_failed", "error_type": type(exc).__name__})
        print(json.dumps(terminal, sort_keys=True), flush=True)
        if keepalive is not None:
            os.close(keepalive)
        raise
    _atomic_replace_json(terminal_path, terminal)
    _append_stage(stage_path, {"event": "worker_finished", "status": terminal["status"]})
    try:
        chain = chain_next_stage(loaded, terminal=terminal, evaluation=evaluation)
    except Exception as exc:  # the finished stage stays durable even if chaining fails
        chain = {"launched": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]}
    _atomic_replace_json(detached / "chain_receipt.json", chain)
    _append_stage(stage_path, {"event": "chain_next_stage", **chain})
    print(json.dumps(terminal, sort_keys=True), flush=True)
    if keepalive is not None:
        os.close(keepalive)
    return terminal


def _effective_concurrency(loaded: LoadedProviderRehearsalV52, detached: Path) -> int:
    """Configured concurrency, or the durable override written by a throttling fallback."""

    override_path = detached / "concurrency_override.json"
    if override_path.is_file():
        override = _object(override_path)
        value = override.get("provider_concurrency")
        if not isinstance(value, bool) and isinstance(value, int) and value >= 1:
            return value
    return _int_field(loaded.document, "provider_concurrency", 8)


def chain_decision(
    loaded: LoadedProviderRehearsalV52,
    *,
    terminal: Mapping[str, object],
    evaluation: Mapping[str, object],
) -> dict[str, object]:
    """Decide the automatic next stage without launching anything.

    A passing pilot chains to the 12K reference certification; a passing shard chains to the
    next shard at the same concurrency; a shard whose only failed check is the infrastructure
    bound chains to the next shard at the fallback concurrency once (sustained throttling);
    any other failure stops the chain and is reported.
    """

    role = str(loaded.document.get("sprint_role", "pilot"))
    failed = list(cast(list[str], evaluation.get("failed_checks", [])))
    status = str(terminal.get("status"))
    if role == "pilot":
        target = loaded.document.get("next_stage_config_path")
        if status == "complete" and isinstance(target, str):
            return {
                "action": "launch_pool_certification",
                "target": target,
                "reason": "pilot_passed",
            }
        return {"action": "stop", "reason": f"pilot_{status}", "failed_checks": failed}
    target = loaded.document.get("next_shard_config_path")
    if target is None:
        return {"action": "stop", "reason": "last_shard", "failed_checks": failed}
    if status == "complete":
        return {"action": "launch_next_shard", "target": str(target), "reason": "shard_passed"}
    if failed == ["infrastructure_failures_below_2pct"]:
        current = int(cast(int, evaluation.get("effective_provider_concurrency", 16)))
        fallback = _int_field(loaded.document, "fallback_provider_concurrency", 8)
        if current > fallback:
            return {
                "action": "launch_next_shard",
                "target": str(target),
                "reason": "throttling_fallback",
                "provider_concurrency_override": fallback,
            }
        return {"action": "stop", "reason": "repeated_infrastructure_fault_at_fallback"}
    return {"action": "stop", "reason": f"shard_{status}", "failed_checks": failed}


def chain_next_stage(
    loaded: LoadedProviderRehearsalV52,
    *,
    terminal: Mapping[str, object],
    evaluation: Mapping[str, object],
) -> dict[str, object]:
    """Launch the automatic next stage decided by ``chain_decision``."""

    decision = chain_decision(loaded, terminal=terminal, evaluation=evaluation)
    if decision["action"] == "stop":
        return {**decision, "launched": False}
    if decision["action"] == "launch_pool_certification":
        from leanfaith.sft2a.sprint_scale_v52 import (
            launch_sprint_pool_certification,
            load_sprint_pool_config,
        )

        pool = load_sprint_pool_config(loaded.base.repo_root / str(decision["target"]))
        return {**decision, "launched": True, "launch": launch_sprint_pool_certification(pool)}
    next_loaded = load_provider_rehearsal_v52(Path(str(decision["target"])))
    override = decision.get("provider_concurrency_override")
    if isinstance(override, int):
        _atomic_replace_json(
            next_loaded.output_root / "detached/concurrency_override.json",
            {
                "provider_concurrency": override,
                "reason": "throttling_fallback",
                "from_shard": str(loaded.path),
            },
        )
    return {**decision, "launched": True, "launch": launch_sprint_pilot_v52(next_loaded)}


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo_root, check=False, capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def require_sprint_prerequisite_receipts(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Require the passed sprint-prompt closure canaries and the passed oracle-v2 live gate."""

    canary_path = run_paths(loaded.base).one_root / "closure_canaries_v5/manifest.json"
    if not canary_path.is_file() or _object(canary_path).get("all_passed") is not True:
        raise SprintPilotError(
            "sprint pilot requires the passed closure canary manifest for the sprint judge prompt"
        )
    gate_path = loaded.output_root / "checks/oracle_v2_live_gate/oracle_v2_live_gate_receipt.json"
    if not gate_path.is_file() or _object(gate_path).get("all_passed") is not True:
        raise SprintPilotError("sprint pilot requires the passed oracle-v2 live gate receipt")
    return {
        "closure_canaries_sha256": hash_file(canary_path),
        "oracle_v2_live_gate_sha256": hash_file(gate_path),
    }


def launch_sprint_pilot_v52(
    loaded: LoadedProviderRehearsalV52, *, resume: bool = False, startup_timeout: float = 90.0
) -> dict[str, object]:
    """Verify the sample, recheck the atomic resource ledger, then start the named tmux worker."""

    if loaded.kind != "sprint":
        raise SprintPilotError("sprint pilot launch requires a sprint pilot config")
    verification = verify_sprint_pilot_sample_v52(loaded)
    prerequisites = require_sprint_prerequisite_receipts(loaded)
    session = str(loaded.document["tmux_session"])
    if _tmux_session_exists(session):
        raise SprintPilotError(f"sprint pilot tmux session already exists: {session}")
    resource_task = str(loaded.document["resource_task"])
    if any(item.task == resource_task for item in list_reservations()):
        raise SprintPilotError("sprint pilot resource task is already claimed")
    detached = loaded.output_root / "detached"
    try:
        with parallel_launch_lock(detached / "run.lock"):
            pass
    except ParallelRehearsalError as exc:
        raise SprintPilotError("sprint pilot run lock is held") from exc
    terminal_path = detached / "terminal_status.json"
    if terminal_path.is_file():
        status = _object(terminal_path).get("status")
        if status in {"complete", "threshold_failed"}:
            raise SprintPilotError(f"finished sprint pilot cannot be relaunched: {status}")
        if not resume:
            raise SprintPilotError("sprint pilot output exists; use resume-sprint-pilot-v5-2")
    capacity = sprint_capacity_check(lean_workers=2, lean_rss_gib=40.0)
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--provider-rehearsal-config",
        str(loaded.path),
        "detached-sprint-pilot-v5-2-worker",
    )
    _start_tmux(session, command, loaded.base.repo_root)
    deadline = time.monotonic() + startup_timeout
    health = sprint_pilot_health_v52(loaded)
    while time.monotonic() < deadline and not bool(health["worker_started"]):
        time.sleep(1.0)
        health = sprint_pilot_health_v52(loaded)
        status = cast(Mapping[str, object], health["terminal_status"]).get("status")
        if status == "failed":
            break
    if not bool(health["worker_started"]):
        raise SprintPilotError(f"sprint pilot worker did not start: {health}")
    return {
        "version": "leanfaith_sft2a_sprint_pilot_launch_v1",
        "session_started": True,
        "resume": resume,
        "sample_verification": verification,
        "prerequisite_receipts": prerequisites,
        "capacity_recheck": capacity,
        "worker_will_wait_for_capacity": not bool(capacity["capacity_available_now"]),
        "health": health,
    }


def sprint_pilot_health_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Read-only tmux, process, claim, journal, ledger, and terminal evidence for the pilot."""

    session = str(loaded.document["tmux_session"])
    resource_task = str(loaded.document["resource_task"])
    output_root = loaded.output_root
    detached = output_root / "detached"
    alive = _tmux_session_exists(session)
    pane_pid = _tmux_pane_pid(session) if alive else None
    terminal_path = detached / "terminal_status.json"
    terminal = _object(terminal_path) if terminal_path.is_file() else {}
    events = _stage_events(detached / "stage_journal.jsonl")
    reservation = next((item for item in list_reservations() if item.task == resource_task), None)
    state_path = output_root / "root_state.jsonl"
    budget_path = output_root / "provider_budget.jsonl"
    roots_complete = len(_completed_root_ids(loaded))
    budget = (
        AtomicProviderBudget(budget_path, loaded.ceilings).snapshot()
        if budget_path.is_file()
        else {}
    )
    log_path = detached / "combined.log"
    log_tail = (
        log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        if log_path.is_file()
        else []
    )
    return {
        "version": "leanfaith_sft2a_sprint_pilot_health_v1",
        "checked_at": _now(),
        "tmux_session": session,
        "tmux_alive": alive,
        "pane_pid": pane_pid,
        "process_tree": _process_tree(pane_pid) if pane_pid is not None else "",
        "worker_started": any(event.get("event") == "worker_started" for event in events),
        "last_stage_event": events[-1] if events else None,
        "stage_event_count": len(events),
        "resource_claim": (
            None
            if reservation is None
            else {
                "task": reservation.task,
                "pid": reservation.pid,
                "lean_workers": reservation.lean_workers,
                "lean_rss_gib": reservation.lean_rss_gib,
                "worktree": reservation.worktree,
            }
        ),
        "terminal_status": terminal,
        "roots_total": len(_sample_rows(loaded.sample_path)),
        "roots_complete": roots_complete,
        "root_state_present": state_path.is_file(),
        "provider_budget": budget,
        "first_durable_advancement": roots_complete > 0
        or bool(budget and int(cast(int, budget.get("finalized_calls", 0))) > 0),
        "combined_log_tail": log_tail,
        "launch_receipt_present": (detached / "launch_receipt.json").is_file(),
    }


# --------------------------------------------------------------------------------------------
# Audit-only Kimi telemetry over the completed historical run
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedAuditOnlyKimiV52:
    path: Path
    document: dict[str, object]
    sha256: str
    source: LoadedProviderRehearsalV52
    authorization: LoadedProviderAuthorizationV52
    kimi_rows: int
    concurrency: int
    tmux_session: str


def load_audit_only_kimi_v52(path: Path) -> LoadedAuditOnlyKimiV52:
    """Load the additive audit-only config bound to the historical run's own receipt and ledger."""

    resolved = path.resolve()
    document = _object(resolved)
    if document.get("version") != AUDIT_ONLY_VERSION:
        raise SprintPilotError("audit-only config version differs")
    repo_root = Path(__file__).resolve().parents[3]
    source_path = repo_root / str(document.get("source_provider_config_path"))
    if hash_file(source_path) != document.get("source_provider_config_sha256"):
        raise SprintPilotError("audit-only source provider config hash differs")
    source = load_provider_rehearsal_v52(source_path)
    receipt_path = Path(str(document.get("authorization_receipt_path")))
    if receipt_path != source.output_root / "authorization/authorization_receipt.json" or hash_file(
        receipt_path
    ) != document.get("authorization_receipt_sha256"):
        raise SprintPilotError("audit-only authorization receipt path or hash differs")
    receipt = _object(receipt_path)
    if (
        receipt.get("authorized") is not True
        or receipt.get("provider_config_sha256") != source.sha256
    ):
        raise SprintPilotError("historical authorization receipt does not bind the source run")
    if (
        document.get("terra_calls_allowed") != 0
        or document.get("opus_calls_allowed") != 0
        or document.get("lean_requests_allowed") != 0
    ):
        raise SprintPilotError("audit-only config must allow zero Terra, Opus, and Lean calls")
    kimi_rows = _int_field(document, "kimi_audit_rows", 40)
    concurrency = _int_field(document, "kimi_concurrency", 8)
    if not 1 <= kimi_rows <= source.ceilings.maximum_lemex_calls or not 1 <= concurrency <= 16:
        raise SprintPilotError("audit-only Kimi rows/concurrency are outside the ledger ceiling")
    session = document.get("tmux_session")
    if not isinstance(session, str) or not session:
        raise SprintPilotError("audit-only config lacks a tmux session name")
    return LoadedAuditOnlyKimiV52(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
        source=source,
        authorization=LoadedProviderAuthorizationV52(
            path=receipt_path, document=receipt, sha256=hash_file(receipt_path)
        ),
        kimi_rows=kimi_rows,
        concurrency=concurrency,
        tmux_session=session,
    )


def run_audit_only_kimi_v52(loaded: LoadedAuditOnlyKimiV52) -> dict[str, object]:
    """Complete the historical Kimi audit with zero Terra, zero Opus, and zero Lean calls."""

    source = loaded.source
    ledger_path = source.output_root / "provider_budget.jsonl"
    before = AtomicProviderBudget(ledger_path, source.ceilings).snapshot()
    manifest = run_provider_kimi_audit_v52(
        source, loaded.authorization, kimi_count=loaded.kimi_rows, concurrency=loaded.concurrency
    )
    after = AtomicProviderBudget(ledger_path, source.ceilings).snapshot()
    if (
        after["proposer_calls"] != before["proposer_calls"]
        or after["opus_calls"] != before["opus_calls"]
        or after["reported_opus_spend_usd"] != before["reported_opus_spend_usd"]
    ):
        raise SprintPilotError("audit-only path changed Terra/Opus ledger counts")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_audit_only_kimi_receipt_v1",
        "status": "complete",
        "audit_only_config_sha256": loaded.sha256,
        "source_provider_config_sha256": source.sha256,
        "authorization_receipt_sha256": loaded.authorization.sha256,
        "kimi_rows": loaded.kimi_rows,
        "concurrency": loaded.concurrency,
        "ledger_before": before,
        "ledger_after": after,
        "terra_calls_executed": 0,
        "opus_calls_executed": 0,
        "lean_requests_executed": 0,
        "kimi_calls_added": int(cast(int, after["lemex_calls"]))
        - int(cast(int, before["lemex_calls"])),
        "outstanding_calls_after": after["outstanding_calls"],
        "audit_manifest_sha256": hash_file(source.output_root / "audit_kimi/manifest.json"),
        "agreement_rate": manifest.get("agreement_rate"),
        "agreements": manifest.get("agreements"),
        "selected_rows": manifest.get("selected_rows"),
        "malformed_exhausted": manifest.get("malformed_exhausted"),
        "unknown_review_rows": manifest.get("unknown_review_rows"),
        "released_rows": manifest.get("released_rows"),
        "systematic_disagreement": manifest.get("systematic_disagreement"),
        "completed_at": _now(),
    }
    _atomic_replace_json(source.output_root / "audit_kimi/audit_only_terminal.json", receipt)
    return receipt


def run_detached_audit_only_kimi_v52(
    loaded: LoadedAuditOnlyKimiV52, *, redirect_stdio: bool = True
) -> dict[str, object]:
    """Provider-only detached worker for the historical Kimi audit."""

    audit_root = loaded.source.output_root / "audit_kimi"
    audit_root.mkdir(parents=True, exist_ok=True)
    log_path = audit_root / "audit_only.log"
    stage_path = audit_root / "audit_only_stage_journal.jsonl"
    keepalive = _redirect_stdio(log_path) if redirect_stdio else None
    print(json.dumps({"event": "audit_only_stdio_ready", "pid": os.getpid()}), flush=True)
    _append_stage(stage_path, {"event": "audit_only_started", "kimi_rows": loaded.kimi_rows})
    try:
        with parallel_launch_lock(audit_root / "audit_only.lock"):
            receipt = run_audit_only_kimi_v52(loaded)
    except Exception as exc:
        failure = {
            "version": "leanfaith_sft2a_audit_only_kimi_receipt_v1",
            "status": "failed_resumable",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "terra_calls_executed": 0,
            "opus_calls_executed": 0,
            "lean_requests_executed": 0,
            "failed_at": _now(),
        }
        _atomic_replace_json(audit_root / "audit_only_terminal.json", failure)
        _append_stage(stage_path, {"event": "audit_only_failed", "error_type": type(exc).__name__})
        print(json.dumps(failure, sort_keys=True), flush=True)
        if keepalive is not None:
            os.close(keepalive)
        raise
    _append_stage(stage_path, {"event": "audit_only_complete", "agreements": receipt["agreements"]})
    print(json.dumps(receipt, sort_keys=True), flush=True)
    if keepalive is not None:
        os.close(keepalive)
    return receipt


def launch_audit_only_kimi_v52(
    loaded: LoadedAuditOnlyKimiV52, *, startup_timeout: float = 60.0
) -> dict[str, object]:
    """Start the provider-only historical audit in its own named tmux session."""

    if _tmux_session_exists(loaded.tmux_session):
        raise SprintPilotError(f"audit-only tmux session already exists: {loaded.tmux_session}")
    audit_root = loaded.source.output_root / "audit_kimi"
    if (audit_root / "manifest.json").is_file():
        raise SprintPilotError("historical Kimi audit manifest already exists; nothing to launch")
    try:
        with parallel_launch_lock(audit_root / "audit_only.lock"):
            pass
    except ParallelRehearsalError as exc:
        raise SprintPilotError("audit-only run lock is held") from exc
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--audit-only-config",
        str(loaded.path),
        "detached-audit-only-kimi-v5-2-worker",
    )
    _start_tmux(loaded.tmux_session, command, loaded.source.base.repo_root)
    deadline = time.monotonic() + startup_timeout
    health = audit_only_kimi_health_v52(loaded)
    while time.monotonic() < deadline and not bool(health["worker_started"]):
        time.sleep(1.0)
        health = audit_only_kimi_health_v52(loaded)
    if not bool(health["worker_started"]):
        raise SprintPilotError(f"audit-only worker did not start: {health}")
    return {
        "version": "leanfaith_sft2a_audit_only_kimi_launch_v1",
        "session_started": True,
        "sanitized_command": shlex.join(command),
        "health": health,
    }


def audit_only_kimi_health_v52(loaded: LoadedAuditOnlyKimiV52) -> dict[str, object]:
    """Read-only health for the audit-only job: tmux, pid, checkpoints, ledger, terminal."""

    audit_root = loaded.source.output_root / "audit_kimi"
    alive = _tmux_session_exists(loaded.tmux_session)
    pane_pid = _tmux_pane_pid(loaded.tmux_session) if alive else None
    events = _stage_events(audit_root / "audit_only_stage_journal.jsonl")
    checkpoints = audit_root / "checkpoints"
    checkpoint_count = (
        sum(1 for path in checkpoints.iterdir() if path.suffix == ".json")
        if checkpoints.is_dir()
        else 0
    )
    terminal_path = audit_root / "audit_only_terminal.json"
    ledger_path = loaded.source.output_root / "provider_budget.jsonl"
    return {
        "version": "leanfaith_sft2a_audit_only_kimi_health_v1",
        "checked_at": _now(),
        "tmux_session": loaded.tmux_session,
        "tmux_alive": alive,
        "pane_pid": pane_pid,
        "process_tree": _process_tree(pane_pid) if pane_pid is not None else "",
        "worker_started": any(event.get("event") == "audit_only_started" for event in events),
        "last_stage_event": events[-1] if events else None,
        "checkpointed_rows": checkpoint_count,
        "kimi_rows_requested": loaded.kimi_rows,
        "provider_budget": (
            AtomicProviderBudget(ledger_path, loaded.source.ceilings).snapshot()
            if ledger_path.is_file()
            else {}
        ),
        "manifest_present": (audit_root / "manifest.json").is_file(),
        "terminal_status": _object(terminal_path) if terminal_path.is_file() else {},
        "lean_reservation_held": False,
    }


# --------------------------------------------------------------------------------------------
# Bounded oracle-v2 live Lean gate
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OracleV2Fixture:
    fixture_id: str
    signature: str
    expected_status: Literal["valid", "invalid"]
    minimum_distinct_universes: int = 0
    open_context: tuple[str, ...] = ()


ORACLE_V2_FIXTURES: tuple[OracleV2Fixture, ...] = (
    OracleV2Fixture(
        "type_star_universe",
        "∀ {α : Type*} [inst : Preorder α] {a b : α}, a ≤ b → a ≤ b",
        "valid",
        minimum_distinct_universes=1,
    ),
    OracleV2Fixture(
        "explicit_declared_universes",
        "∀ {α : Type u_3} {β : Type u_5} (f : α → β) (a : α), f a = f a",
        "valid",
        minimum_distinct_universes=2,
    ),
    OracleV2Fixture(
        "non_prop_function_type_is_invalid",
        "∀ {α : Type u_3} {β : Type u_5}, (α → β) → α → β",
        "invalid",
    ),
    OracleV2Fixture(
        "explicit_undeclared_universe",
        "∀ {α : Type v}, α → α",
        "invalid",
    ),
    OracleV2Fixture(
        "two_universe_metavariables_stay_distinct",
        "∀ {α : Type _} {β : Type _} (f : α → β) (a : α), f a = f a",
        "valid",
        minimum_distinct_universes=2,
    ),
    OracleV2Fixture(
        "sort_metavariable",
        "∀ {α : Sort _} (a : α), a = a",
        "valid",
        minimum_distinct_universes=1,
    ),
    OracleV2Fixture(
        "dependent_binders",
        "∀ (n : ℕ) (v : Fin n → ℕ) (i : Fin n), v i ≤ v i + n",
        "valid",
    ),
    OracleV2Fixture(
        "section_variable_unbound",
        "∀ (a b : α), a ≤ b → a ≤ b",
        "invalid",
    ),
    OracleV2Fixture(
        "section_variable_closed",
        "∀ {α : Type*} [inst : Preorder α] (a b : α), a ≤ b → a ≤ b",
        "valid",
        minimum_distinct_universes=1,
    ),
    OracleV2Fixture(
        "rebound_open_context",
        "∀ (n : ℕ), succ n = n + 1",
        "valid",
        open_context=("Nat",),
    ),
)


def _distinct_universes(goal: str) -> int:
    import re

    return len(set(re.findall(r"\b(?:Type|Sort) (u_\d+)\b", goal)))


def run_oracle_v2_live_gate(
    base: LoadedSFT2AConfig,
    *,
    output_root: Path,
    resource_task: str = "SFT2A-SPRINT-ORACLE-V2-GATE",
    claim: bool = True,
    fixtures: Sequence[OracleV2Fixture] = ORACLE_V2_FIXTURES,
    wait_for_capacity_seconds: float = 12 * 3600.0,
    capacity_poll_seconds: float = 60.0,
) -> dict[str, object]:
    """Run the bounded live Lean fixtures through one persistent v2 oracle with rebind.

    Covers ``Type*``, explicit declared/undeclared universes, distinct universe metavariables,
    ``Sort _``, dependent binders, section-variable closure, and a rebound open-context root on
    the same backend. Claims one Lean worker/16 GiB when ``claim`` is set and releases it.
    """

    reservation: dict[str, object] | None = None
    if claim:
        waits = 0
        deadline = time.monotonic() + wait_for_capacity_seconds
        while True:
            try:
                claimed = claim_resources(
                    task=resource_task,
                    lean_workers=1,
                    lean_rss_gib=16.0,
                    gpu=False,
                    pid=os.getpid(),
                    owner_session="oracle-v2-live-gate",
                    worktree=base.repo_root,
                )
            except ReservationError as exc:
                if "cap exceeded" not in str(exc) or time.monotonic() >= deadline:
                    raise
                if waits % 10 == 0:
                    _append_stage(
                        output_root / "oracle_v2_gate_stage_journal.jsonl",
                        {
                            "event": "waiting_for_lean_capacity",
                            "detail": str(exc),
                            "capacity": sprint_capacity_check(lean_workers=1, lean_rss_gib=16.0),
                        },
                    )
                waits += 1
                time.sleep(capacity_poll_seconds)
                continue
            break
        reservation = {
            "task": claimed.task,
            "lean_workers": 1,
            "lean_rss_gib": 16.0,
            "waits": waits,
        }
        _append_stage(
            output_root / "oracle_v2_gate_stage_journal.jsonl",
            {"event": "resource_claimed", "claim": reservation},
        )
    rows: list[dict[str, object]] = []
    started = time.monotonic()
    oracle: SignatureOracle | None = None
    try:
        oracle = SignatureOracle(base, cache_version="v2")
        for fixture in fixtures:
            if fixture.open_context:
                context = base.config.root.compile_context.model_copy(
                    update={"open_context": list(fixture.open_context)}
                )
                root = base.config.root.model_copy(update={"compile_context": context})
                rebound = replace(base, config=base.config.model_copy(update={"root": root}))
                oracle.rebind(rebound)
            else:
                oracle.rebind(base)
            result = oracle.elaborate(fixture.signature, endpoint_role="candidate")
            distinct = _distinct_universes(result.goal_v1 or "")
            passed = result.status == fixture.expected_status and (
                distinct >= fixture.minimum_distinct_universes
            )
            rows.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "signature": fixture.signature,
                    "expected_status": fixture.expected_status,
                    "observed_status": result.status,
                    "goal_v1": result.goal_v1,
                    "distinct_universes": distinct,
                    "minimum_distinct_universes": fixture.minimum_distinct_universes,
                    "cache_hit": result.cache_hit,
                    "cache_key": result.cache_key,
                    "elapsed_ms": result.elapsed_ms,
                    "detail": result.detail[:500],
                    "open_context": list(fixture.open_context),
                    "passed": passed,
                }
            )
    finally:
        if oracle is not None:
            oracle.close()
        if claim:
            release_resources(task=resource_task)
    receipt: dict[str, object] = {
        "version": ORACLE_V2_GATE_VERSION,
        "method_version": ORACLE_METHOD_VERSION_V2,
        "cache_version": "v2",
        "base_config_hash": base.config_hash,
        "project_id": base.config.root.compile_context.project_id,
        "backend_context_fingerprint": (
            oracle.backend_context.fingerprint if oracle is not None else None
        ),
        "fixtures": rows,
        "fixture_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "all_passed": all(bool(row["passed"]) for row in rows),
        "lean_requests_executed": sum(not bool(row["cache_hit"]) for row in rows),
        "cache_hits": sum(bool(row["cache_hit"]) for row in rows),
        "persistent_backends_created": 1,
        "provider_calls_executed": 0,
        "resource_claim": reservation,
        "elapsed_seconds": time.monotonic() - started,
        "status_counts": dict(Counter(str(row["observed_status"]) for row in rows)),
    }
    _atomic_replace_json(output_root / "oracle_v2_live_gate_receipt.json", receipt)
    if not bool(receipt["all_passed"]):
        raise SprintPilotError("oracle-v2 live gate failed one or more fixtures")
    return receipt


__all__ = [
    "AUDIT_ONLY_VERSION",
    "ORACLE_V2_FIXTURES",
    "SPRINT_PILOT_VERSION",
    "LoadedAuditOnlyKimiV52",
    "OracleV2Fixture",
    "SprintPilotError",
    "audit_only_kimi_health_v52",
    "chain_decision",
    "chain_next_stage",
    "controlled_resume_receipt",
    "evaluate_sprint_pilot_thresholds",
    "launch_audit_only_kimi_v52",
    "launch_sprint_pilot_v52",
    "load_audit_only_kimi_v52",
    "measure_infrastructure_failures",
    "require_sprint_prerequisite_receipts",
    "run_audit_only_kimi_v52",
    "run_detached_audit_only_kimi_v52",
    "run_detached_sprint_pilot_v52",
    "run_malformed_injection_check",
    "run_oracle_v2_live_gate",
    "snapshot_completed_roots",
    "sprint_capacity_check",
    "sprint_pilot_health_v52",
    "verify_sprint_pilot_sample_v52",
]

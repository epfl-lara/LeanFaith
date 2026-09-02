"""Executable-path tests for the sprint pilot: controlled stop/resume through the dynamic queue,
per-stage durable terminals, the malformed injection check, objective thresholds, resource-claim
argument handling with capacity waiting, the audit-only loader, and CLI dispatch."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Literal, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import ReservationError
from leanfaith.sft2a import __main__ as sft2a_main
from leanfaith.sft2a import provider_rehearsal_v52, sprint_pilot_v52
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.lean_oracle import (
    COMMAND_TEMPLATE_VERSION_V2,
    ORACLE_METHOD_VERSION_V2,
    SignatureOracleResult,
    elaborator_sha256,
)
from leanfaith.sft2a.mechanisms import BREAKING_MECHANISMS, PRESERVING_MECHANISMS
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.provider_rehearsal_v52 import (
    LoadedProviderAuthorizationV52,
    LoadedProviderRehearsalV52,
    compact_provider_rehearsal_v52,
    run_two_provider_workers_v52,
    verify_provider_replay_v52,
)
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.sprint_pilot_v52 import (
    SprintPilotError,
    controlled_resume_receipt,
    evaluate_sprint_pilot_thresholds,
    load_audit_only_kimi_v52,
    run_malformed_injection_check,
    snapshot_completed_roots,
)

_SPRINT_BASE = Path("configs/sft2a/closure_aware_v5_2_sprint_v1.yaml")
_AUDIT_ONLY_CONFIG = Path("configs/sft2a/audit_only_kimi_recovery_v5.json")
_BINDERS = "∀ {α : Type} [inst : Preorder α] {a b c : α}"


def _closure_checks() -> dict[str, object]:
    return {
        "entire_universally_closed_proposition": True,
        "argument_swapping": "not_applicable",
        "symmetry": "not_applicable",
        "antisymmetry": "not_applicable",
        "extensionality": "not_applicable",
        "recoverable_boundary_cases": "checked_no_effect",
    }


def _judgment(verdict: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": (
            "Both closed propositions express the same claim."
            if verdict == "equivalent"
            else "The second proposition changes the conclusion."
        ),
        "closure_checks": _closure_checks(),
    }


def _proposal(polarity: str, family: str, signature: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "requested_polarity": polarity,
        "mechanism": family,
        "applicability_reason": "unit test",
        "candidate_signature": signature,
        "change_summary": "unit test candidate",
        "judge_trap": "none",
        "informative": True,
        "substantive_change": True,
        "proof_free": True,
    }


class _RecordingProvider:
    """Immutable-terminal fake provider whose outputs are chosen from the request identity."""

    def __init__(self, root: Path, provider_id: str, kind: str) -> None:
        self.root = root
        self.provider_id = provider_id
        self.kind = kind
        self.executed: list[tuple[str, ...]] = []
        self.lock = threading.Lock()

    def preview_call(
        self, *, prompt: str, input_ids: Sequence[str]
    ) -> tuple[str, Path, dict[str, object]]:
        request: dict[str, object] = {
            "provider_id": self.provider_id,
            "prompt_sha256": hash_canonical({"prompt": prompt}),
            "input_ids": list(input_ids),
        }
        call_key = hash_canonical(request)
        return call_key, self.root / call_key / "terminal.json", request

    def _structured(self, prompt: str, input_ids: Sequence[str]) -> dict[str, object]:
        if self.kind == "proposer":
            root_id, slot_id, _attempt = input_ids[0], input_ids[1], input_ids[2]
            tag = root_id.rsplit(":", maxsplit=1)[-1]
            polarity = "preserving" if slot_id.startswith("preserve") else "breaking"
            specs = PRESERVING_MECHANISMS if polarity == "preserving" else BREAKING_MECHANISMS
            family = specs[0].family if slot_id.endswith("0") else specs[1].family
            bodies = {
                "preserve_0": "b ≤ c → a ≤ b → a ≤ c",
                "preserve_1": "a ≤ b ∧ b ≤ c → a ≤ c",
                "break_0": "a ≤ b → b ≤ c → c ≤ a",
                "break_1": "a ≤ b → b ≤ c → b ≤ a",
            }
            signature = f"{_BINDERS} (w{tag} : α), {bodies[slot_id]}"
            return _proposal(polarity, family, signature)
        breaking = "c ≤ a" in prompt or "b ≤ a" in prompt
        return _judgment("non_equivalent" if breaking else "equivalent")

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        call_key, terminal_path, request = self.preview_call(prompt=prompt, input_ids=input_ids)
        with self.lock:
            cache_hit = terminal_path.is_file()
            if cache_hit:
                structured = json.loads(terminal_path.read_text())["structured"]
            else:
                self.executed.append(tuple(input_ids))
                structured = self._structured(prompt, input_ids)
                terminal_path.parent.mkdir(parents=True, exist_ok=True)
                terminal_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "call_key": call_key,
                            "request": request,
                            "structured": structured,
                            "usage": {},
                            "cost_usd": 0.01 if self.kind == "opus" else None,
                            "elapsed_seconds": 0.0,
                        }
                    )
                    + b"\n"
                )
        return ProviderCallResult(
            call_key=call_key,
            provider_id=self.provider_id,
            structured=dict(structured),
            usage={},
            cost_usd=0.01 if self.kind == "opus" else None,
            elapsed_seconds=0.0,
            cache_hit=cache_hit,
            terminal_path=terminal_path,
        )


class _PoolFakeOracle:
    instances: ClassVar[list[_PoolFakeOracle]] = []
    lock: ClassVar[threading.Lock] = threading.Lock()
    method_version = ORACLE_METHOD_VERSION_V2
    cache_version = "v2"

    def __init__(self, loaded: LoadedSFT2AConfig, *, cache_version: str = "v2") -> None:
        self.loaded = loaded
        self.project_id = loaded.config.root.compile_context.project_id
        self.elaborations: list[str] = []
        self.rebinds = 0
        self.closed = False
        with type(self).lock:
            type(self).instances.append(self)

    def rebind(self, loaded: LoadedSFT2AConfig) -> None:
        assert loaded.config.root.compile_context.project_id == self.project_id
        self.loaded = loaded
        self.rebinds += 1

    def close(self) -> None:
        self.closed = True

    def elaborate(
        self, signature: str, *, endpoint_role: Literal["reference", "candidate"]
    ) -> SignatureOracleResult:
        with type(self).lock:
            self.elaborations.append(signature)
        goal = f"⊢ {signature}"
        digest = hash_canonical({"signature": signature})
        return _valid_result(goal, digest)


def _valid_result(goal: str, digest: str) -> SignatureOracleResult:
    return SignatureOracleResult(
        status="valid",
        cache_key=f"fake:{digest}",
        cache_hit=True,
        signature_sha256=digest,
        goal_v1=goal,
        sidecar={"record": {"goal_v1": goal, "provenance": {"expr_hash": digest}}},
        lean_status="valid",
        request_hash=None,
        elapsed_ms=1,
        raw_response_path=None,
        detail="fake",
    )


def _mechanism_plan() -> dict[str, dict[str, object]]:
    plan: dict[str, dict[str, object]] = {}
    for slot, spec in (
        ("preserve_0", PRESERVING_MECHANISMS[0]),
        ("preserve_1", PRESERVING_MECHANISMS[1]),
        ("break_0", BREAKING_MECHANISMS[0]),
        ("break_1", BREAKING_MECHANISMS[1]),
    ):
        plan[slot] = {
            "family": spec.family,
            "polarity": spec.polarity,
            "instruction": spec.instruction,
            "applicability": "general",
            "shape_id": "unit",
        }
    return plan


def _sprint_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roots: int = 3
) -> tuple[
    LoadedProviderRehearsalV52,
    LoadedProviderAuthorizationV52,
    _RecordingProvider,
    _RecordingProvider,
]:
    base = load_sft2a_config(_SPRINT_BASE)
    staging = str(tmp_path / "staging")
    config = base.config.model_copy(
        update={
            "staging_root": staging,
            "run_layout": base.config.run_layout.model_copy(update={"shared_cache_root": staging}),
        }
    )
    base = replace(base, config=config, config_hash=hash_canonical(config.model_dump(mode="json")))
    root_document = base.config.root.model_dump(mode="json")
    rows: list[dict[str, object]] = []
    for index in range(roots):
        root = {**root_document, "root_id": f"mathlib:unit:r{index}"}
        rows.append(
            {
                "root": root,
                "certified_reference": {"goal_v1": root_document["expected_reference_goal_v1"]},
                "mechanism_plan": _mechanism_plan(),
            }
        )
    sample = tmp_path / "sample.jsonl"
    sample.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    output_root = tmp_path / "run"
    document: dict[str, object] = {
        "version": sprint_pilot_v52.SPRINT_PILOT_VERSION,
        "sample_sha256": hash_file(sample),
        "provider_output_root": str(output_root),
        "tmux_session": "unit-sprint",
        "resource_task": "SFT2A-UNIT-SPRINT",
        "maximum_root_workers": 2,
        "maximum_total_lean_workers": 2,
        "maximum_measured_rss_gib": 40.0,
        "provider_concurrency": 2,
        "kimi_audit_rows": 2,
        "controlled_stop_after_completed_roots": 1,
        "completed_root_sample_paths": [],
    }
    ceilings = ExecutionCeilings.model_validate(
        {
            "maximum_roots": roots,
            "maximum_provider_calls": 400,
            "maximum_proposer_calls": 150,
            "maximum_opus_calls": 200,
            "maximum_lemex_calls": 50,
            "maximum_attempts_per_slot": 3,
            "maximum_reported_opus_spend_usd": 50.0,
            "codex_cost_status": "unavailable",
            "lemex_cost_status": "unavailable",
        }
    )
    loaded = LoadedProviderRehearsalV52(
        path=tmp_path / "sprint.json",
        document=document,
        sha256="b" * 64,
        base=base,
        sample_path=sample,
        output_root=output_root,
        ceilings=ceilings,
        recovery_source=None,
        kind="sprint",
    )
    authorization = LoadedProviderAuthorizationV52(
        path=tmp_path / "auth.json", document={"authorized": True}, sha256="c" * 64
    )
    proposer = _RecordingProvider(
        tmp_path / "providers/terra", config.proposer.provider_id, "proposer"
    )
    judge = _RecordingProvider(tmp_path / "providers/opus", config.claude_judge.provider_id, "opus")
    _PoolFakeOracle.instances = []
    monkeypatch.setattr(
        provider_rehearsal_v52, "preflight_sample_v52", lambda _loaded: {"ok": True}
    )
    monkeypatch.setattr(provider_rehearsal_v52, "proposer_provider", lambda _base: proposer)
    monkeypatch.setattr(provider_rehearsal_v52, "claude_judge_provider", lambda _base: judge)
    monkeypatch.setattr(provider_rehearsal_v52, "SignatureOracle", _PoolFakeOracle)
    monkeypatch.setattr(
        provider_rehearsal_v52,
        "certified_reference_result_v52",
        lambda row: _valid_result(
            str(cast(dict[str, object], row["certified_reference"])["goal_v1"]),
            hash_canonical({"reference": str(cast(dict[str, object], row["root"])["root_id"])}),
        ),
    )
    return loaded, authorization, proposer, judge


def test_controlled_stop_then_resume_executes_zero_calls_for_completed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, authorization, proposer, judge = _sprint_fixture(tmp_path, monkeypatch, roots=3)

    phase_one = run_two_provider_workers_v52(
        loaded,
        authorization,
        provider_concurrency=2,
        stop_after_completed_roots=1,
        enforce_closure_canaries=False,
    )
    summary = phase_one[0]
    assert summary["stopped"] is True
    assert summary["stop_reasons"] == ["controlled_stop_after_completed_roots"]
    assert 1 <= int(cast(int, summary["roots_complete"])) < 3
    assert summary["lean_workers"] == 2
    pool_stats = cast(dict[str, int], summary["oracle_pool"])
    assert pool_stats["max_active_backends"] <= 2
    before = snapshot_completed_roots(loaded)
    completed_before = cast(list[str], before["completed_root_ids"])
    assert len(completed_before) == summary["roots_complete"]
    executed_before = (len(proposer.executed), len(judge.executed))
    elaborations_before = sum(len(o.elaborations) for o in _PoolFakeOracle.instances)
    assert before["unfinalized_completed_root_call_keys"] == []

    phase_two = run_two_provider_workers_v52(
        loaded, authorization, provider_concurrency=2, enforce_closure_canaries=False
    )
    resumed = phase_two[0]
    assert resumed["stopped"] is False
    assert resumed["replayed_at_start"] == len(completed_before)
    assert resumed["roots_complete"] == 3
    after = snapshot_completed_roots(loaded)
    receipt = controlled_resume_receipt(before, after)
    assert receipt["manifests_unchanged"] is True
    assert receipt["provider_calls_for_completed_roots_after_resume"] == 0
    assert receipt["lean_requests_for_completed_roots_after_resume"] == 0
    assert receipt["completed_before_resume"] == completed_before
    # Exactly 4 proposer + 4 judge calls per root, none repeated for completed roots.
    assert len(proposer.executed) == 12
    assert len(judge.executed) == 12
    assert len(proposer.executed) - executed_before[0] == 4 * (3 - len(completed_before))
    assert sum(len(o.elaborations) for o in _PoolFakeOracle.instances) - elaborations_before == (
        4 * (3 - len(completed_before))
    )
    assert len(_PoolFakeOracle.instances) <= 4
    assert all(oracle.closed for oracle in _PoolFakeOracle.instances)

    third = run_two_provider_workers_v52(
        loaded, authorization, provider_concurrency=2, enforce_closure_canaries=False
    )[0]
    assert third["replayed_at_start"] == 3
    assert third["completed_in_invocation"] == 0
    assert len(proposer.executed) == 12 and len(judge.executed) == 12

    compacted = compact_provider_rehearsal_v52(loaded)
    assert compacted["accepted_rows"] == 12
    assert compacted["planned_slots"] == 12
    replay = verify_provider_replay_v52(loaded)
    assert replay["reproducible"] is True
    assert replay["provider_calls_executed"] == 0 and replay["lean_requests_executed"] == 0
    assert len(proposer.executed) == 12 and len(judge.executed) == 12

    core_rows = [
        json.loads(line)
        for line in (loaded.output_root / "compacted/new_core/core.jsonl").read_text().splitlines()
    ]
    sidecars = [
        json.loads(line)
        for line in (loaded.output_root / "compacted/new_core/sidecar.jsonl")
        .read_text()
        .splitlines()
    ]
    for core, sidecar in zip(core_rows, sidecars, strict=True):
        assert core["label"] == (sidecar["claude_judge"]["verdict"] == "equivalent")
    manifests = [
        json.loads(
            (provider_rehearsal_v52._root_output(loaded, root_id) / "manifest.json").read_text()
        )
        for root_id in cast(list[str], after["completed_root_ids"])
    ]
    assert all(m["lean"]["method_version"] == ORACLE_METHOD_VERSION_V2 for m in manifests)
    assert all(m["lean"]["cache_version"] == "v2" for m in manifests)
    assert all(m["lean"]["oracle_source_sha256"] is None for m in manifests)
    assert all(m["lean"]["candidate_requests"] == 4 for m in manifests)

    evaluation = evaluate_sprint_pilot_thresholds(
        loaded,
        compaction=compacted,
        replay=replay,
        generation_wall_seconds=12.0,
        malformed_injection={"passed": True},
        resume_check=receipt,
    )
    assert evaluation["passed"] is True
    assert evaluation["scale_10k_authorized"] is True
    assert evaluation["lean_invalid_attempts"] == 0
    assert evaluation["candidate_lean_requests"] == 12
    infra = cast(dict[str, object], evaluation["infrastructure"])
    assert infra["infrastructure_failure_rate"] == 0.0
    assert infra["finalized_provider_calls"] == 24


def test_stop_request_file_halts_new_roots_and_leaves_resumable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, authorization, proposer, _judge = _sprint_fixture(tmp_path, monkeypatch, roots=3)
    stop_file = tmp_path / "stop_requested"
    stop_file.write_text("stop\n")
    summary = run_two_provider_workers_v52(
        loaded,
        authorization,
        provider_concurrency=2,
        stop_request_path=stop_file,
        enforce_closure_canaries=False,
    )[0]
    assert summary["stopped"] is True
    assert summary["stop_reasons"] == ["stop_request_file"]
    assert summary["roots_complete"] == 0
    assert proposer.executed == []
    stop_file.unlink()
    resumed = run_two_provider_workers_v52(
        loaded, authorization, provider_concurrency=2, enforce_closure_canaries=False
    )[0]
    assert resumed["roots_complete"] == 3


def test_root_crash_is_recorded_and_re_raised_after_in_flight_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, authorization, _proposer, _judge = _sprint_fixture(tmp_path, monkeypatch, roots=2)

    def broken(row: dict[str, object]) -> SignatureOracleResult:
        raise RuntimeError("synthetic certified reference failure")

    monkeypatch.setattr(provider_rehearsal_v52, "certified_reference_result_v52", broken)
    with pytest.raises(RuntimeError, match="synthetic certified reference failure"):
        run_two_provider_workers_v52(
            loaded, authorization, provider_concurrency=1, enforce_closure_canaries=False
        )
    state = [
        json.loads(line)
        for line in (loaded.output_root / "root_state.jsonl").read_text().splitlines()
    ]
    assert any(event["phase"] == "crashed" for event in state)
    infra = sprint_pilot_v52.measure_infrastructure_failures(loaded)
    assert infra["root_crashes"] >= 1


def test_malformed_injection_check_runs_real_root_path_without_crash(tmp_path: Path) -> None:
    base = load_sft2a_config(_SPRINT_BASE)
    receipt = run_malformed_injection_check(base, output_root=tmp_path / "injection")
    assert receipt["passed"] is True
    assert receipt["crashed"] is False
    assert receipt["observed_counts"] == receipt["expected_counts"]
    assert receipt["proposer_calls"] == 6
    assert receipt["judge_calls"] == 7
    assert receipt["real_provider_calls_executed"] == 0
    assert receipt["lean_requests_executed"] == 0
    assert (tmp_path / "injection/malformed_injection_receipt.json").is_file()
    manifest = json.loads((tmp_path / "injection/one_root/manifest.json").read_text())
    assert manifest["counts"]["accepted"] == 4
    assert manifest["counts"]["judge_malformed_exhausted"] == 1
    core = [
        json.loads(line)
        for line in (tmp_path / "injection/one_root/new_core/core.jsonl").read_text().splitlines()
    ]
    assert sorted(row["label"] for row in core) == [False, False, True, True]


def _thresholds_loaded(tmp_path: Path, roots: int) -> LoadedProviderRehearsalV52:
    sample = tmp_path / "sample.jsonl"
    sample.write_bytes(
        b"".join(
            canonical_json_bytes({"root": {"root_id": f"mathlib:t:{i}"}}) + b"\n"
            for i in range(roots)
        )
    )
    return LoadedProviderRehearsalV52(
        path=tmp_path / "c.json",
        document={},
        sha256="d" * 64,
        base=cast(Any, SimpleNamespace(config=SimpleNamespace(staging_root=str(tmp_path)))),
        sample_path=sample,
        output_root=tmp_path / "run",
        ceilings=ExecutionCeilings.model_validate(
            {
                "maximum_roots": roots,
                "maximum_provider_calls": 10,
                "maximum_proposer_calls": 5,
                "maximum_opus_calls": 5,
                "maximum_lemex_calls": 0,
                "maximum_attempts_per_slot": 3,
                "maximum_reported_opus_spend_usd": 1.0,
                "codex_cost_status": "unavailable",
                "lemex_cost_status": "unavailable",
            }
        ),
        recovery_source=None,
        kind="sprint",
    )


def _manifest(lean_invalid: int, requests: int) -> dict[str, object]:
    return {
        "counts": {"lean_invalid_attempts": lean_invalid, "candidate_attempts": requests},
        "lean": {"candidate_requests": requests},
    }


def _evaluate(tmp_path: Path, **overrides: Any) -> dict[str, object]:
    loaded = _thresholds_loaded(tmp_path, 20)
    values: dict[str, Any] = {
        "compaction": {"accepted_rows": 60, "self_pairs": 0, "candidate_duplicates": 0},
        "replay": {"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        "generation_wall_seconds": 900.0,
        "malformed_injection": {"passed": True},
        "resume_check": {
            "manifests_unchanged": True,
            "provider_calls_for_completed_roots_after_resume": 0,
            "lean_requests_for_completed_roots_after_resume": 0,
        },
        "root_manifests": [_manifest(1, 5)] * 19 + [_manifest(0, 5)],
        "infrastructure": {"infrastructure_failure_rate": 0.0},
    }
    values.update(overrides)
    return evaluate_sprint_pilot_thresholds(loaded, **values)


def test_thresholds_pass_only_when_every_objective_check_holds(tmp_path: Path) -> None:
    passed = _evaluate(tmp_path)
    assert passed["passed"] is True and passed["scale_10k_authorized"] is True
    assert passed["minimum_accepted"] == 56
    assert passed["lean_invalid_attempts"] == 19 and passed["candidate_lean_requests"] == 100
    assert passed["failed_checks"] == []
    failures = {
        "accepted_at_least_70pct": {
            "compaction": {"accepted_rows": 55, "self_pairs": 0, "candidate_duplicates": 0}
        },
        "lean_invalid_below_25pct": {"root_manifests": [_manifest(2, 5)] * 20},
        "zero_accepted_self_pairs": {
            "compaction": {"accepted_rows": 70, "self_pairs": 1, "candidate_duplicates": 0}
        },
        "zero_accepted_duplicates": {
            "compaction": {"accepted_rows": 70, "self_pairs": 0, "candidate_duplicates": 1}
        },
        "injected_malformed_output_did_not_crash": {"malformed_injection": {"passed": False}},
        "infrastructure_failures_below_2pct": {
            "infrastructure": {"infrastructure_failure_rate": 0.02}
        },
        "completed_root_resume_zero_calls": {
            "resume_check": {
                "manifests_unchanged": True,
                "provider_calls_for_completed_roots_after_resume": 1,
                "lean_requests_for_completed_roots_after_resume": 0,
            }
        },
        "wall_time_at_most_30min": {"generation_wall_seconds": 1801.0},
        "all_roots_have_manifests": {"root_manifests": [_manifest(0, 5)] * 19},
    }
    for check, override in failures.items():
        result = _evaluate(tmp_path, **override)
        assert result["passed"] is False, check
        assert result["scale_10k_authorized"] is False, check
        assert check in cast(list[str], result["failed_checks"]), check


def test_lean_invalid_threshold_is_rate_only(tmp_path: Path) -> None:
    # 19 or 20 invalid of 100 elaborations both pass the rate-only bound (below 25%).
    assert _evaluate(tmp_path, root_manifests=[_manifest(1, 5)] * 19 + [_manifest(0, 5)])["passed"]
    exact = _evaluate(tmp_path, root_manifests=[_manifest(1, 5)] * 20)
    assert exact["passed"] is True
    assert exact["lean_invalid_rate"] == 0.2
    assert exact["lean_invalid_per_planned_slot"] == 0.25  # telemetry only
    # 19 invalid of 60 elaborations passes the count bound but fails the 25% rate bound.
    rate = _evaluate(tmp_path, root_manifests=[_manifest(1, 3)] * 19 + [_manifest(0, 3)])
    assert rate["passed"] is False
    assert rate["lean_invalid_attempts"] == 19 and rate["candidate_lean_requests"] == 60
    assert "lean_invalid_below_25pct" in cast(list[str], rate["failed_checks"])


def test_claim_with_wait_uses_real_argument_names_and_waits_for_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    attempts = {"count": 0}

    def fake_claim(**kwargs: object) -> Any:
        attempts["count"] += 1
        calls.append(dict(kwargs))
        if attempts["count"] < 3:
            raise ReservationError("Lean worker cap exceeded: requested total 3, cap 2")
        return SimpleNamespace(
            task=kwargs["task"],
            lean_workers=kwargs["lean_workers"],
            lean_rss_gib=kwargs["lean_rss_gib"],
            pid=kwargs["pid"],
            worktree=str(kwargs["worktree"]),
            created_at="now",
        )

    monkeypatch.setattr(sprint_pilot_v52, "claim_resources", fake_claim)
    monkeypatch.setattr(sprint_pilot_v52, "list_reservations", lambda: [])
    monkeypatch.setattr(sprint_pilot_v52.time, "sleep", lambda _seconds: None)
    loaded = SimpleNamespace(
        document={
            "resource_task": "SFT2A-UNIT",
            "maximum_total_lean_workers": 2,
            "maximum_measured_rss_gib": 40.0,
            "tmux_session": "unit",
        },
        base=SimpleNamespace(repo_root=tmp_path),
    )
    claim = sprint_pilot_v52._claim_with_wait(
        cast(Any, loaded), stage_path=tmp_path / "stage.jsonl", wait_seconds=10.0, poll_seconds=0.0
    )
    assert claim["waits"] == 2
    assert claim["lean_workers"] == 2 and claim["lean_rss_gib"] == 40.0
    assert calls[0]["lean_workers"] == 2 and "workers" not in calls[0]
    assert calls[0]["worktree"] == tmp_path
    assert calls[0]["gpu"] is False
    events = [json.loads(line) for line in (tmp_path / "stage.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "waiting_for_lean_capacity"
    one_worker = SimpleNamespace(
        document={**loaded.document, "maximum_total_lean_workers": 1}, base=loaded.base
    )
    with pytest.raises(SprintPilotError, match="exactly two persistent Lean workers"):
        sprint_pilot_v52._claim_with_wait(
            cast(Any, one_worker),
            stage_path=tmp_path / "s2.jsonl",
            wait_seconds=1.0,
            poll_seconds=0.0,
        )


def test_audit_only_config_binds_historical_receipt_and_zero_call_contract() -> None:
    loaded = load_audit_only_kimi_v52(_AUDIT_ONLY_CONFIG)
    assert loaded.kimi_rows == 40
    assert loaded.concurrency == 8
    assert loaded.source.kind == "recovery"
    assert loaded.authorization.document["authorized"] is True
    assert loaded.authorization.document["provider_config_sha256"] == loaded.source.sha256
    assert loaded.document["terra_calls_allowed"] == 0
    assert loaded.document["opus_calls_allowed"] == 0
    assert loaded.document["lean_requests_allowed"] == 0
    assert loaded.tmux_session == "leanfaith-sft2a-audit-kimi-recovery-v5"


def test_audit_only_config_rejects_nonzero_call_allowances(tmp_path: Path) -> None:
    document = json.loads(_AUDIT_ONLY_CONFIG.read_text())
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**document, "opus_calls_allowed": 1}))
    with pytest.raises(SprintPilotError, match="zero Terra, Opus, and Lean"):
        load_audit_only_kimi_v52(path)
    path.write_text(json.dumps({**document, "kimi_audit_rows": 81}))
    with pytest.raises(SprintPilotError, match="ledger ceiling"):
        load_audit_only_kimi_v52(path)


# ---------------------------------------------------------------------------
# CLI dispatch.
# ---------------------------------------------------------------------------


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    *,
    target: str,
    loader: str,
) -> tuple[list[tuple[tuple[object, ...], dict[str, object]]], dict[str, object]]:
    recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []
    sentinel = SimpleNamespace(
        base=SimpleNamespace(),
        output_root=Path("/nonexistent/output"),
        document={},
    )
    monkeypatch.setattr(sft2a_main, "load_sft2a_config", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sft2a_main, loader, lambda _path: sentinel)

    def fake(*args: object, **kwargs: object) -> dict[str, object]:
        recorded.append((args, dict(kwargs)))
        return {"dispatched": target, "argument_is_loaded": args[0] is sentinel}

    monkeypatch.setattr(sft2a_main, target, fake)
    assert sft2a_main.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    return recorded, printed


@pytest.mark.parametrize(
    ("command", "target", "kwargs"),
    [
        ("verify-sprint-pilot-sample", "verify_sprint_pilot_sample_v52", {}),
        ("launch-sprint-pilot-v5-2", "launch_sprint_pilot_v52", {"resume": False}),
        ("resume-sprint-pilot-v5-2", "launch_sprint_pilot_v52", {"resume": True}),
        ("detached-sprint-pilot-v5-2-worker", "run_detached_sprint_pilot_v52", {}),
        ("sprint-pilot-v5-2-health", "sprint_pilot_health_v52", {}),
    ],
)
def test_cli_dispatches_sprint_pilot_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    target: str,
    kwargs: dict[str, object],
) -> None:
    recorded, printed = _dispatch(
        monkeypatch,
        capsys,
        ["--provider-rehearsal-config", "unit.json", command],
        target=target,
        loader="load_provider_rehearsal_v52",
    )
    assert printed == {"dispatched": target, "argument_is_loaded": True}
    assert recorded[0][1] == kwargs


def test_cli_dispatches_check_commands_with_base_and_output_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for command, target in (
        ("run-malformed-injection-check", "run_malformed_injection_check"),
        ("run-oracle-v2-live-gate", "run_oracle_v2_live_gate"),
    ):
        recorded, printed = _dispatch(
            monkeypatch,
            capsys,
            ["--provider-rehearsal-config", "unit.json", command],
            target=target,
            loader="load_provider_rehearsal_v52",
        )
        assert printed["dispatched"] == target
        assert "output_root" in recorded[0][1]
        assert str(recorded[0][1]["output_root"]).startswith("/nonexistent/output/checks/")


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("run-audit-only-kimi-v5-2", "run_audit_only_kimi_v52"),
        ("launch-audit-only-kimi-v5-2", "launch_audit_only_kimi_v52"),
        ("detached-audit-only-kimi-v5-2-worker", "run_detached_audit_only_kimi_v52"),
        ("audit-only-kimi-v5-2-health", "audit_only_kimi_health_v52"),
    ],
)
def test_cli_dispatches_audit_only_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, target: str
) -> None:
    recorded, printed = _dispatch(
        monkeypatch,
        capsys,
        ["--audit-only-config", "audit.json", command],
        target=target,
        loader="load_audit_only_kimi_v52",
    )
    assert printed == {"dispatched": target, "argument_is_loaded": True}
    assert recorded[0][1] == {}


def test_cli_requires_the_matching_config_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sft2a_main, "load_sft2a_config", lambda *a, **k: SimpleNamespace())
    with pytest.raises(SystemExit) as missing_audit:
        sft2a_main.main(["audit-only-kimi-v5-2-health"])
    assert missing_audit.value.code == 2
    with pytest.raises(SystemExit) as missing_provider:
        sft2a_main.main(["sprint-pilot-v5-2-health"])
    assert missing_provider.value.code == 2


def test_launch_requires_passed_canary_and_gate_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, _authorization, _proposer, _judge = _sprint_fixture(tmp_path, monkeypatch, roots=1)
    monkeypatch.setattr(sprint_pilot_v52, "verify_sprint_pilot_sample_v52", lambda _l: {"ok": 1})
    monkeypatch.setattr(sprint_pilot_v52, "_tmux_session_exists", lambda _s: False)
    monkeypatch.setattr(sprint_pilot_v52, "list_reservations", lambda: [])
    with pytest.raises(SprintPilotError, match="closure canary manifest"):
        sprint_pilot_v52.launch_sprint_pilot_v52(loaded)
    canary = sprint_pilot_v52.run_paths(loaded.base).one_root / "closure_canaries_v5/manifest.json"
    canary.parent.mkdir(parents=True)
    canary.write_text(json.dumps({"all_passed": True, "config_hash": loaded.base.config_hash}))
    with pytest.raises(SprintPilotError, match="oracle-v2 live gate receipt"):
        sprint_pilot_v52.launch_sprint_pilot_v52(loaded)
    gate = loaded.output_root / "checks/oracle_v2_live_gate/oracle_v2_live_gate_receipt.json"
    gate.parent.mkdir(parents=True)
    gate.write_text(json.dumps({"all_passed": False}))
    with pytest.raises(SprintPilotError, match="oracle-v2 live gate receipt"):
        sprint_pilot_v52.launch_sprint_pilot_v52(loaded)
    identity = {
        "all_passed": True,
        "method_version": ORACLE_METHOD_VERSION_V2,
        "cache_version": "v2",
        "elaborator_sha256": elaborator_sha256("v2"),
        "command_template_version": COMMAND_TEMPLATE_VERSION_V2,
        "base_config_hash": loaded.base.config_hash,
    }
    gate.write_text(json.dumps(identity))
    receipts = sprint_pilot_v52.require_sprint_prerequisite_receipts(loaded)
    assert set(receipts) == {
        "closure_canaries_sha256",
        "closure_canaries_config_hash",
        "oracle_v2_live_gate_sha256",
        "oracle_v2_live_gate_identity",
    }
    # A shard names the pilot's gate receipt explicitly instead of its own output root.
    shared_gate = tmp_path / "pilot_gate_receipt.json"
    shared_gate.write_text(json.dumps(identity))
    shard = replace(
        loaded,
        document={**loaded.document, "oracle_v2_gate_receipt_path": str(shared_gate)},
        output_root=tmp_path / "shard_run",
    )
    assert sprint_pilot_v52.require_sprint_prerequisite_receipts(shard)[
        "oracle_v2_live_gate_sha256"
    ] == hash_file(shared_gate)


def test_oracle_gate_waits_for_capacity_before_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"count": 0}

    def fake_claim(**kwargs: object) -> Any:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ReservationError("Lean worker cap exceeded: requested total 3, cap 2")
        return SimpleNamespace(task=kwargs["task"])

    released: list[str] = []
    monkeypatch.setattr(sprint_pilot_v52, "claim_resources", fake_claim)
    monkeypatch.setattr(sprint_pilot_v52, "list_reservations", lambda: [])
    monkeypatch.setattr(
        sprint_pilot_v52, "release_resources", lambda **kw: released.append(str(kw["task"]))
    )
    monkeypatch.setattr(sprint_pilot_v52.time, "sleep", lambda _s: None)

    class _Oracle:
        backend_context = SimpleNamespace(fingerprint="fp")

        def __init__(self, base: Any, *, cache_version: str) -> None:
            self.calls = 0

        def rebind(self, loaded: Any) -> None:
            return None

        def elaborate(self, signature: str, *, endpoint_role: str) -> SignatureOracleResult:
            self.calls += 1
            return _valid_result("⊢ ∀ {α : Type u_0}, True", hash_canonical({"s": signature}))

        def close(self) -> None:
            return None

    monkeypatch.setattr(sprint_pilot_v52, "SignatureOracle", _Oracle)
    base = load_sft2a_config(_SPRINT_BASE)
    fixtures = [
        sprint_pilot_v52.OracleV2Fixture("only", "True", "valid", minimum_distinct_universes=1)
    ]
    receipt = sprint_pilot_v52.run_oracle_v2_live_gate(
        base, output_root=tmp_path, fixtures=fixtures, capacity_poll_seconds=0.0
    )
    assert receipt["all_passed"] is True
    assert cast(dict[str, object], receipt["resource_claim"])["waits"] == 1
    assert released == ["SFT2A-SPRINT-ORACLE-V2-GATE"]
    events = [
        json.loads(line)
        for line in (tmp_path / "oracle_v2_gate_stage_journal.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["waiting_for_lean_capacity", "resource_claimed"]

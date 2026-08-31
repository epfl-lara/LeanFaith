from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.certified_sample_v52 import verify_corrected_global_preflight
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.mechanisms import (
    plan_structured_mechanism_rotation,
    structured_signature_shape,
)
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.parallel_rehearsal import (
    AtomicBudgetedProvider,
    AtomicProviderBudget,
    ParallelRehearsalError,
    ParallelRootStateMachine,
    deterministic_parallel_compaction,
    parallel_launch_lock,
)
from leanfaith.sft2a.provider_rehearsal_v52 import (
    certified_reference_result_v52,
    load_provider_rehearsal_v52,
    preflight_provider_launch_v52,
)
from leanfaith.sft2a.providers import ProviderCallResult

_PROVIDER_CONFIG = Path("configs/sft2a/provider_rehearsal_v5_2_corrected.json")


class _FakeTerminalProvider:
    def __init__(self, root: Path, provider_id: str, *, cost_usd: float | None = None) -> None:
        self.root = root
        self.provider_id = provider_id
        self.cost_usd = cost_usd
        self.executed = 0

    def preview_call(
        self, *, prompt: str, input_ids: tuple[str, ...]
    ) -> tuple[str, Path, dict[str, object]]:
        request: dict[str, object] = {
            "provider_id": self.provider_id,
            "prompt": prompt,
            "input_ids": list(input_ids),
        }
        call_key = hash_canonical(request)
        return call_key, self.root / call_key / "terminal.json", request

    def call(self, *, prompt: str, input_ids: tuple[str, ...]) -> ProviderCallResult:
        call_key, terminal_path, request = self.preview_call(prompt=prompt, input_ids=input_ids)
        cache_hit = terminal_path.is_file()
        if not cache_hit:
            self.executed += 1
            terminal_path.parent.mkdir(parents=True, exist_ok=True)
            terminal = {
                "version": "synthetic_terminal_v1",
                "call_key": call_key,
                "request": request,
                "structured": {"ok": True},
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "cost_usd": self.cost_usd,
                "elapsed_seconds": 0.01,
            }
            terminal_path.write_bytes(canonical_json_bytes(terminal) + b"\n")
        return ProviderCallResult(
            call_key=call_key,
            provider_id=self.provider_id,
            structured={"ok": True},
            usage={"input_tokens": 1, "output_tokens": 1},
            cost_usd=self.cost_usd,
            elapsed_seconds=0.01,
            cache_hit=cache_hit,
            terminal_path=terminal_path,
        )


def _ceilings() -> ExecutionCeilings:
    return ExecutionCeilings(
        maximum_roots=2,
        maximum_provider_calls=3,
        maximum_proposer_calls=1,
        maximum_opus_calls=1,
        maximum_lemex_calls=1,
        maximum_attempts_per_slot=3,
        maximum_reported_opus_spend_usd=0.15,
        codex_cost_status="unavailable",
        lemex_cost_status="unavailable",
    )


def test_structured_goal_regressions_and_corrected_cache_preflight() -> None:
    loaded = load_provider_rehearsal_v52(_PROVIDER_CONFIG)
    rows = [json.loads(line) for line in loaded.sample_path.read_text().splitlines()]
    assert len(rows) == 100
    assert all(
        marker not in str(row["certified_reference"]["goal_v1"])
        for row in rows
        for marker in ("[anonymous]", "⋯", "...")
    )
    assert all(
        row["root"]["declaration_name"] != "Composition.orderEmbOfFin_boundaries" for row in rows
    )
    with pytest.raises(ValueError, match="forbidden placeholder"):
        structured_signature_shape(
            "n : Nat\n⊢ Composition.orderEmbOfFin_boundaries ⋯",
            {"k": "const", "name": "True"},
        )

    lambda_shape = structured_signature_shape(
        "f : Nat → Nat\n⊢ (fun x => x) True",
        {"k": "const", "name": "True"},
    )
    assert lambda_shape.binder_count == 1
    assert lambda_shape.premise_count == 0
    assert not lambda_shape.has_equality
    assert not lambda_shape.has_order

    one_binder = [
        (str(row["root"]["root_id"]), row["structured_goal"]["shape"])
        for row in rows
        if row["structured_goal"]["shape"]["binder_count"] == 1
    ]
    assert len(one_binder) == 13
    for row in rows:
        if row["structured_goal"]["shape"]["binder_count"] != 1:
            continue
        assert all(
            assignment["applicability"] != "two_binders"
            for assignment in row["mechanism_plan"].values()
        )
    receipt = verify_corrected_global_preflight(loaded.sample_path.parent)
    assert receipt["global_certificate"] == "100/100"
    assert receipt["lean_requests_executed"] == 0
    assert receipt["provider_calls_executed"] == 0
    certified = certified_reference_result_v52(rows[0])
    assert certified.cache_hit
    assert certified.goal_v1 == rows[0]["root"]["expected_reference_goal_v1"]


def test_provider_free_two_worker_crash_resume_and_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceilings = _ceilings()
    ledger = AtomicProviderBudget(tmp_path / "provider_budget.jsonl", ceilings)

    # Crash before a terminal: the reservation is explicitly reclaimed, then called once.
    terra = _FakeTerminalProvider(tmp_path / "providers", "terra")
    terra_key, terra_terminal, _request = terra.preview_call(prompt="p", input_ids=("terra",))
    ledger.reserve(call_key=terra_key, kind="proposer", worker_id="worker-0")
    ledger.reclaim_missing_terminal(
        call_key=terra_key,
        prior_worker_id="worker-0",
        new_worker_id="worker-1",
        terminal_path=terra_terminal,
    )
    terra_wrapped = AtomicBudgetedProvider(
        terra, ledger=ledger, kind="proposer", worker_id="worker-1"
    )
    terra_wrapped.call(prompt="p", input_ids=("terra",))
    assert terra.executed == 1

    # Crash after provider terminal but before finalization: resume reconciles and cache-hits.
    opus = _FakeTerminalProvider(tmp_path / "providers", "opus", cost_usd=0.12)
    opus_key, opus_terminal, _request = opus.preview_call(prompt="j", input_ids=("opus",))
    ledger.reserve(
        call_key=opus_key,
        kind="opus",
        worker_id="worker-0",
        maximum_charge_usd=0.10,
    )
    first = opus.call(prompt="j", input_ids=("opus",))
    assert not first.cache_hit and opus_terminal.is_file()
    resumed_opus = AtomicBudgetedProvider(
        opus,
        ledger=ledger,
        kind="opus",
        worker_id="worker-1",
        maximum_charge_usd=0.10,
    ).call(prompt="j", input_ids=("opus",))
    assert resumed_opus.cache_hit and opus.executed == 1
    assert ledger.snapshot()["reported_opus_spend_usd"] == pytest.approx(0.12)

    # Kimi uses the same near-ceiling ledger. No fourth unique call is permitted.
    kimi = _FakeTerminalProvider(tmp_path / "providers", "kimi")
    AtomicBudgetedProvider(kimi, ledger=ledger, kind="lemex", worker_id="worker-1").call(
        prompt="a", input_ids=("kimi",)
    )
    with pytest.raises(ParallelRehearsalError, match="call ceiling"):
        AtomicBudgetedProvider(
            _FakeTerminalProvider(tmp_path / "providers", "terra-2"),
            ledger=ledger,
            kind="proposer",
            worker_id="worker-1",
        ).call(prompt="new", input_ids=("fourth",))

    states = ParallelRootStateMachine(tmp_path / "root_state.jsonl")
    assert states.claim(root_id="root-a", worker_id="worker-0") == "claimed"
    states.checkpoint(
        root_id="root-a", worker_id="worker-0", slot_id="preserve_0", artifact_hash="a" * 64
    )
    with pytest.raises(ParallelRehearsalError, match="another unfinished root"):
        states.claim(root_id="root-b", worker_id="worker-0")
    states.crash(root_id="root-a", worker_id="worker-0", reason="synthetic_pre_checkpoint")
    states.reclaim(root_id="root-a", prior_worker_id="worker-0", worker_id="worker-1")
    states.checkpoint(
        root_id="root-a", worker_id="worker-1", slot_id="preserve_0", artifact_hash="a" * 64
    )
    with pytest.raises(ParallelRehearsalError, match="conflicting slot checkpoint"):
        states.checkpoint(
            root_id="root-a",
            worker_id="worker-1",
            slot_id="preserve_0",
            artifact_hash="b" * 64,
        )
    with pytest.raises(ParallelRehearsalError, match="active owner"):
        states.complete(root_id="root-a", worker_id="worker-0", manifest_hash="c" * 64)
    states.complete(root_id="root-a", worker_id="worker-1", manifest_hash="c" * 64)
    assert states.claim(root_id="root-a", worker_id="worker-0") == "replay_complete"

    registry = PersistentCandidateRegistry(tmp_path / "candidates.jsonl")
    outcomes: list[bool] = []

    def claim(owner: str) -> None:
        outcomes.append(
            registry.claim(
                raw_signature="Nat.succ n = n + 1",
                rendered_goal="n : Nat\n⊢ Nat.succ n = n + 1",
                closed_expr_hash="d" * 64,
                owner=owner,
            )
        )

    threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [False, True]

    compact_rows = [
        {
            "row_id": "row-1",
            "candidate_rendered_goal_hash": "e" * 64,
            "candidate_closed_expr_hash": "f" * 64,
            "planned_mechanism": {"polarity": "preserving", "family": "definition"},
            "accepted_mechanism": {"polarity": "preserving", "family": "definition"},
        }
    ]
    compacted = deterministic_parallel_compaction(compact_rows, output=tmp_path / "compacted")
    first_hash = hash_file(tmp_path / "compacted/rows.jsonl")
    replayed = deterministic_parallel_compaction(compact_rows, output=tmp_path / "compacted")
    assert compacted == replayed
    assert hash_file(tmp_path / "compacted/rows.jsonl") == first_hash

    lock = tmp_path / "launch.lock"
    with (
        parallel_launch_lock(lock),
        pytest.raises(ParallelRehearsalError, match=r"duplicate.*launch refused"),
        parallel_launch_lock(lock),
    ):
        pass

    loaded = load_provider_rehearsal_v52(_PROVIDER_CONFIG)

    def no_tmux(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(
        "leanfaith.sft2a.provider_rehearsal_v52.subprocess.run",
        no_tmux,
    )
    preflight = preflight_provider_launch_v52(loaded, None)
    assert preflight["boundary"] == "tmux_start_not_executed"
    assert preflight["maximum_total_lean_workers"] == 2
    assert preflight["maximum_measured_rss_gib"] == 40.0
    assert preflight["provider_calls_executed"] == 0
    assert preflight["lean_requests_executed"] == 0


def test_structured_planner_never_assigns_two_binders_to_one_binder() -> None:
    shape = structured_signature_shape(
        "n : Nat\n⊢ n = n",
        {
            "k": "app",
            "fn": {"k": "const", "name": "Eq"},
            "arg": {"k": "bvar", "index": 0},
        },
    )
    plan = plan_structured_mechanism_rotation(
        [("one-binder", shape)], salt="test", maximum_family_fraction_per_polarity=1.0
    )
    assert all(
        assignment.applicability != "two_binders" for assignment in plan["one-binder"].values()
    )

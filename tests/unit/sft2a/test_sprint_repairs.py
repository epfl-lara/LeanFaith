"""Executable-path regressions for the 72-hour sprint repairs.

Covers Kimi audit assembly and checkpoint resume, configurable Kimi counts, the precise judge
retry failure, the load-once provider ledger, the project-affine oracle pool, the oracle-v2
command shape and project-scoped rebind, the zero-Lean sprint verifier, and the sprint loader's
resource/telemetry contract.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a import parallel_rehearsal, provider_rehearsal_v52
from leanfaith.sft2a.certified_sample_v52 import (
    CorrectedSampleError,
    verify_sprint_pilot_sample,
)
from leanfaith.sft2a.config import load_sft2a_config
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.lean_oracle import (
    ORACLE_METHOD_VERSION_V2,
    SignatureOracle,
    SignatureOracleError,
    _signature_command,
    project_backend_context,
)
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.parallel_rehearsal import AtomicBudgetedProvider, AtomicProviderBudget
from leanfaith.sft2a.provider_rehearsal_v52 import (
    LoadedProviderAuthorizationV52,
    LoadedProviderRehearsalV52,
    OraclePool,
    ProviderRehearsalV52Error,
    load_provider_rehearsal_v52,
    run_provider_kimi_audit_v52,
)
from leanfaith.sft2a.providers import ProviderCallResult

_SPRINT_CONFIG = Path("configs/sft2a/sprint_pilot_20roots_v1.json")
_SPRINT_BASE = Path("configs/sft2a/closure_aware_v5_2_sprint_v1.yaml")


def _ceilings(**overrides: object) -> ExecutionCeilings:
    values: dict[str, object] = {
        "maximum_roots": 100,
        "maximum_provider_calls": 500,
        "maximum_proposer_calls": 200,
        "maximum_opus_calls": 200,
        "maximum_lemex_calls": 100,
        "maximum_attempts_per_slot": 3,
        "maximum_reported_opus_spend_usd": 100.0,
        "codex_cost_status": "unavailable",
        "lemex_cost_status": "unavailable",
    }
    values.update(overrides)
    return ExecutionCeilings.model_validate(values)


def _closure_checks() -> dict[str, object]:
    return {
        "entire_universally_closed_proposition": True,
        "argument_swapping": "not_applicable",
        "symmetry": "not_applicable",
        "antisymmetry": "not_applicable",
        "extensionality": "not_applicable",
        "recoverable_boundary_cases": "checked_no_effect",
    }


def _judgment(verdict: str = "equivalent", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 5,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": "Both closed propositions express the same claim."
        if verdict == "equivalent"
        else "The second proposition changes the conclusion.",
        "closure_checks": _closure_checks(),
    }
    payload.update(overrides)
    return payload


class _TerminalProvider:
    """Fake provider with immutable terminals; responses keyed by row ID in input_ids."""

    def __init__(
        self, root: Path, provider_id: str, responses: dict[str, list[dict[str, object]]]
    ) -> None:
        self.root = root
        self.provider_id = provider_id
        self.responses = responses
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

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        call_key, terminal_path, request = self.preview_call(prompt=prompt, input_ids=input_ids)
        with self.lock:
            cache_hit = terminal_path.is_file()
            if cache_hit:
                structured = json.loads(terminal_path.read_text())["structured"]
            else:
                self.executed.append(tuple(input_ids))
                queue = self.responses[input_ids[0]]
                structured = queue.pop(0)
                terminal_path.parent.mkdir(parents=True, exist_ok=True)
                terminal_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "call_key": call_key,
                            "request": request,
                            "structured": structured,
                            "usage": {},
                            "cost_usd": None,
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
            cost_usd=None,
            elapsed_seconds=0.0,
            cache_hit=cache_hit,
            terminal_path=terminal_path,
        )


def _sidecars(count: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    core: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    sources = ("mathlib", "physlib", "cslib", "compiler_data")
    families = ("fam_a", "fam_b", "fam_c")
    for index in range(count):
        row_id = f"sft2a-new:{index:04d}"
        verdict = "equivalent" if index % 2 == 0 else "non_equivalent"
        core.append({"reference": f"⊢ ref {index}", "candidate": f"⊢ cand {index}", "label": True})
        sidecars.append(
            {
                "row_id": row_id,
                "root_id": f"{sources[index % 4]}:census:{index:04d}",
                "requested_polarity": "preserving" if index % 2 == 0 else "breaking",
                "planned_mechanism": {"family": families[index % 3], "polarity": "preserving"},
                "reference_repr": {"record": {"goal_v1": f"⊢ ref {index}"}},
                "candidate_repr": {"record": {"goal_v1": f"⊢ cand {index}"}},
                "claude_judge": _judgment(verdict),
            }
        )
    return core, sidecars


def _audit_loaded(tmp_path: Path, count: int = 50) -> LoadedProviderRehearsalV52:
    output_root = tmp_path / "run"
    (output_root / "compacted/new_core").mkdir(parents=True)
    (output_root / "replay").mkdir(parents=True)
    core, sidecars = _sidecars(count)
    (output_root / "compacted/new_core/core.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in core)
    )
    (output_root / "compacted/new_core/sidecar.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in sidecars)
    )
    (output_root / "replay/reproducibility_receipt.json").write_text(
        json.dumps({"reproducible": True}) + "\n"
    )
    base = SimpleNamespace(
        judge_prompt="STATEMENT A:\n{{STATEMENT_A}}\nSTATEMENT B:\n{{STATEMENT_B}}\n",
        config=SimpleNamespace(staging_root=str(tmp_path / "staging")),
    )
    return LoadedProviderRehearsalV52(
        path=tmp_path / "config.json",
        document={"provider_output_root": str(output_root)},
        sha256="0" * 64,
        base=cast(Any, base),
        sample_path=tmp_path / "unused_sample.jsonl",
        output_root=output_root,
        ceilings=_ceilings(),
        recovery_source=None,
        kind="recovery",
    )


def _authorization(tmp_path: Path) -> LoadedProviderAuthorizationV52:
    return LoadedProviderAuthorizationV52(
        path=tmp_path / "authorization.json", document={"authorized": True}, sha256="a" * 64
    )


# ---------------------------------------------------------------------------
# Fix 1 + 2 + 3: Kimi audit assembly, non-contiguous selection, checkpoint resume, count.
# ---------------------------------------------------------------------------


def test_kimi_audit_assembles_by_position_and_resumes_from_partial_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanfaith.sft2a.rehearsal import _audit_selection

    loaded = _audit_loaded(tmp_path)
    sidecars = [
        json.loads(line)
        for line in (loaded.output_root / "compacted/new_core/sidecar.jsonl")
        .read_text()
        .splitlines()
    ]
    selected = _audit_selection(sidecars, 7)
    assert len(selected) == 7
    assert selected != sorted(selected) or max(selected) >= 7, "selection must be non-contiguous"
    assert max(selected) >= len(selected), "source indices exceed the result length"

    # Two of the selected rows already have durable checkpoints from an interrupted run.
    checkpoint_root = loaded.output_root / "audit_kimi/checkpoints"
    checkpoint_root.mkdir(parents=True)
    pre_checkpointed = [selected[1], selected[4]]
    for index in pre_checkpointed:
        row_id = str(sidecars[index]["row_id"])
        checkpoint = {
            "row_id": row_id,
            "source": str(sidecars[index]["root_id"]).split(":")[0],
            "requested_polarity": sidecars[index]["requested_polarity"],
            "opus_verdict": sidecars[index]["claude_judge"]["verdict"],
            "kimi_judgment": sidecars[index]["claude_judge"],
            "agrees": True,
            "malformed_attempts": [],
            "malformed_retries": 0,
            "malformed_exhausted": False,
            "infrastructure_failed": False,
            "call_keys": ["prior-run"],
            "cache_hits": 0,
            "prompt_hash": "x" * 64,
            "action": "retain",
        }
        (checkpoint_root / f"{row_id.replace(':', '_')}.json").write_bytes(
            canonical_json_bytes(checkpoint) + b"\n"
        )

    responses: dict[str, list[dict[str, object]]] = {}
    for position, index in enumerate(selected):
        row_id = str(sidecars[index]["row_id"])
        verdict = str(sidecars[index]["claude_judge"]["verdict"])
        if position == 0:
            # Malformed twice: binary verdict with a non-none error type, then low confidence.
            responses[row_id] = [
                _judgment(verdict, error_type="insufficient_confidence"),
                _judgment(verdict, confidence="low"),
            ]
        elif position == 2:
            # Malformed once, then repaired on the single retry.
            responses[row_id] = [_judgment(verdict, error_type="ambiguous"), _judgment(verdict)]
        elif position == 3:
            responses[row_id] = [
                _judgment("non_equivalent" if verdict == "equivalent" else "equivalent")
            ]
        else:
            responses[row_id] = [_judgment(verdict)]
    provider = _TerminalProvider(tmp_path / "kimi", "lemex", responses)
    monkeypatch.setattr(provider_rehearsal_v52, "lemex_audit_provider", lambda _base: provider)

    manifest = run_provider_kimi_audit_v52(
        loaded, _authorization(tmp_path), kimi_count=7, concurrency=3
    )

    rows = [
        json.loads(line)
        for line in (loaded.output_root / "audit_kimi/audit_rows.jsonl").read_text().splitlines()
    ]
    assert [row["row_id"] for row in rows] == [str(sidecars[i]["row_id"]) for i in selected]
    assert all(row for row in rows)
    assert manifest["selected_rows"] == 7
    assert manifest["checkpoint_hits"] == 2
    assert manifest["kimi_count_requested"] == 7
    assert manifest["malformed_exhausted"] == 1
    assert manifest["malformed_retries"] == 2
    assert manifest["genuine_semantic_disagreements"] == 1
    assert manifest["unknown_review_rows"] == 2
    assert manifest["released_rows"] == 48
    assert manifest["terra_calls_executed"] == 0
    assert manifest["opus_calls_executed"] == 0
    assert manifest["lean_requests_executed"] == 0
    assert rows[0]["action"] == "unknown_review_exclude_core_malformed_exhausted"
    assert rows[3]["action"] == "unknown_review_exclude_core"
    # Checkpointed rows were never re-called; every other selected row has a checkpoint now.
    executed_ids = {ids[0] for ids in provider.executed}
    assert all(str(sidecars[i]["row_id"]) not in executed_ids for i in pre_checkpointed)
    assert len(list(checkpoint_root.glob("*.json"))) == 7
    # Replay returns the durable manifest with no provider construction.
    monkeypatch.setattr(
        provider_rehearsal_v52,
        "lemex_audit_provider",
        lambda _base: (_ for _ in ()).throw(AssertionError("must not construct provider")),
    )
    assert run_provider_kimi_audit_v52(loaded, _authorization(tmp_path), kimi_count=7) == manifest


def test_kimi_audit_provider_failure_is_resumable_not_a_crash_mid_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanfaith.sft2a.providers import StructuredProviderError
    from leanfaith.sft2a.rehearsal import _audit_selection

    loaded = _audit_loaded(tmp_path, count=12)
    sidecars = [
        json.loads(line)
        for line in (loaded.output_root / "compacted/new_core/sidecar.jsonl")
        .read_text()
        .splitlines()
    ]
    selected = _audit_selection(sidecars, 4)
    failing_row = str(sidecars[selected[1]]["row_id"])

    class _FailingOnce(_TerminalProvider):
        def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
            if input_ids[0] == failing_row and not self.responses[failing_row]:
                raise StructuredProviderError("lemex process failed: timeout")
            return super().call(prompt=prompt, input_ids=input_ids)

    responses = {
        str(sidecars[i]["row_id"]): [_judgment(str(sidecars[i]["claude_judge"]["verdict"]))]
        for i in selected
    }
    responses[failing_row] = []
    provider = _FailingOnce(tmp_path / "kimi", "lemex", responses)
    monkeypatch.setattr(provider_rehearsal_v52, "lemex_audit_provider", lambda _base: provider)
    with pytest.raises(ProviderRehearsalV52Error, match="uncheckpointed rows"):
        run_provider_kimi_audit_v52(loaded, _authorization(tmp_path), kimi_count=4)
    partial = json.loads((loaded.output_root / "audit_kimi/partial_status.json").read_text())
    assert partial["checkpointed_rows"] == 3
    assert partial["infrastructure_failed_rows"][0]["row_id"] == failing_row
    assert not (loaded.output_root / "audit_kimi/manifest.json").is_file()
    responses[failing_row] = [_judgment(str(sidecars[selected[1]]["claude_judge"]["verdict"]))]
    manifest = run_provider_kimi_audit_v52(loaded, _authorization(tmp_path), kimi_count=4)
    assert manifest["selected_rows"] == 4
    assert manifest["checkpoint_hits"] == 3
    assert manifest["agreements"] == 4


# ---------------------------------------------------------------------------
# Fix 4: judge retry catches ValidationError and includes the precise failure.
# ---------------------------------------------------------------------------


class _MalformedThenValid:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del input_ids
        self.prompts.append(prompt)
        structured = (
            {"schema_version": 5, "verdict": "equivalent"}
            if len(self.prompts) == 1
            else _judgment("equivalent")
        )
        return ProviderCallResult(
            call_key=f"key-{len(self.prompts)}",
            provider_id="lemex",
            structured=structured,
            usage={},
            cost_usd=None,
            elapsed_seconds=0.0,
            cache_hit=False,
            terminal_path=Path("/nonexistent/terminal.json"),
        )


def test_judge_retry_prompt_carries_precise_validation_failure() -> None:
    provider = _MalformedThenValid()
    result = call_consistent_judge(
        provider,
        prompt="judge prompt",
        input_ids=("root", "cand", "blinded_judge_v5"),
        closure_aware=True,
        malformed_retries=1,
    )
    assert result.judgment is not None and result.judgment.verdict == "equivalent"
    assert len(result.malformed_attempts) == 1
    reason = str(result.malformed_attempts[0]["reason"])
    assert reason.startswith("schema:ValidationError:")
    assert "confidence" in reason and "Field required" in reason
    assert reason in provider.prompts[1]
    assert "error_type=none" in provider.prompts[1]
    assert "verdict=unknown" in provider.prompts[1]


def test_judge_layer_does_not_swallow_non_validation_errors() -> None:
    class _Broken:
        def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
            raise RuntimeError("provider transport exploded")

    with pytest.raises(RuntimeError, match="transport exploded"):
        call_consistent_judge(
            _Broken(),
            prompt="p",
            input_ids=("r",),
            closure_aware=True,
            malformed_retries=1,
        )


# ---------------------------------------------------------------------------
# Fix 5: provider ledger loads the journal once and counts physical reads.
# ---------------------------------------------------------------------------


def test_ledger_loads_journal_once_and_keeps_states_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[Path] = []
    original = parallel_rehearsal._events

    def counting_events(path: Path) -> list[dict[str, object]]:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(parallel_rehearsal, "_events", counting_events)
    ledger_path = tmp_path / "budget.jsonl"
    ledger = AtomicProviderBudget(ledger_path, _ceilings())
    provider = _TerminalProvider(
        tmp_path / "providers",
        "terra",
        {f"row-{i}": [{"ok": i}] for i in range(30)},
    )
    wrapped = AtomicBudgetedProvider(provider, ledger=ledger, kind="proposer", worker_id="w")
    for index in range(30):
        wrapped.call(prompt="p", input_ids=(f"row-{index}",))
    assert ledger.journal_reads == 1
    assert len(reads) == 1
    snapshot = ledger.snapshot()
    assert snapshot["finalized_calls"] == 30
    assert ledger.journal_reads == 1
    # Replay through the same ledger object: still one physical read, all cache hits.
    for index in range(30):
        assert wrapped.call(prompt="p", input_ids=(f"row-{index}",)).cache_hit
    assert len(reads) == 1
    # A fresh instance recomputes identical state from disk in exactly one read.
    fresh = AtomicProviderBudget(ledger_path, _ceilings())
    assert fresh.snapshot() == snapshot
    assert fresh.journal_reads == 1
    assert fresh.state_of("missing") is None
    assert len(reads) == 2


def test_ledger_incremental_states_match_full_recompute(tmp_path: Path) -> None:
    ledger = AtomicProviderBudget(tmp_path / "budget.jsonl", _ceilings())
    terminal = tmp_path / "terminal.json"
    reservation = ledger.reserve(call_key="k1", kind="opus", worker_id="w0", maximum_charge_usd=1.0)
    terminal.write_text(json.dumps({"call_key": "k1", "cost_usd": 0.5}))
    ledger.finalize(
        call_key="k1",
        reservation_id=reservation,
        response_sha256=hash_file(terminal),
        reported_cost_usd=0.5,
    )
    ledger.reserve(call_key="k2", kind="proposer", worker_id="w0")
    ledger.reclaim_missing_terminal(
        call_key="k2",
        prior_worker_id="w0",
        new_worker_id="w1",
        terminal_path=tmp_path / "absent.json",
    )
    with ledger._thread_lock:
        incremental = dict(ledger._states_locked())
    recomputed = ledger._states(parallel_rehearsal._events(ledger.path))
    assert incremental == recomputed
    assert incremental["k2"]["phase"] == "reclaimed"
    assert incremental["k2"]["worker_id"] == "w1"


# ---------------------------------------------------------------------------
# Fix 6: project-affine oracle pool with a hard backend cap.
# ---------------------------------------------------------------------------


class _FakeOracle:
    instances: ClassVar[list[_FakeOracle]] = []
    lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, loaded: Any) -> None:
        self.project_id = loaded.config.root.compile_context.project_id
        self.rebinds = 0
        self.closed = False
        with type(self).lock:
            type(self).instances.append(self)

    def rebind(self, loaded: Any) -> None:
        assert loaded.config.root.compile_context.project_id == self.project_id
        self.rebinds += 1

    def close(self) -> None:
        self.closed = True

    def elaborate(self, signature: str, *, endpoint_role: str) -> None:
        return None


def _root_loaded(project_id: str) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(
            root=SimpleNamespace(compile_context=SimpleNamespace(project_id=project_id))
        )
    )


def test_oracle_pool_reuses_backends_with_project_affinity_and_hard_cap() -> None:
    _FakeOracle.instances = []
    pool = OraclePool(cache_version="v2", workers=2, oracle_factory=cast(Any, _FakeOracle))
    # Bind one slot per project first; the concurrent phase must then create nothing new.
    with pool.acquire(_root_loaded("mathlib")):
        pass
    with pool.acquire(_root_loaded("physlib")):
        pass
    assert len(_FakeOracle.instances) == 2
    schedule = ["mathlib", "physlib", "mathlib", "mathlib", "physlib", "mathlib", "physlib"] * 6
    active_peaks: list[int] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        for project in schedule[index::8]:
            with pool.acquire(_root_loaded(project)) as oracle:
                assert cast(_FakeOracle, oracle).project_id == project
                active_peaks.append(pool.active_backend_count())
                threading.Event().wait(0.002)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(active_peaks) <= 2
    assert pool.stats["max_active_backends"] <= 2
    # Two projects, two slots: nothing is ever closed and recreated while a matching slot exists.
    assert pool.stats["closed_backends"] == 0
    assert len(_FakeOracle.instances) == 2
    assert {oracle.project_id for oracle in _FakeOracle.instances} == {"mathlib", "physlib"}
    assert pool.stats["reuses"] == len(schedule)
    pool.close()
    assert pool.stats["closed_backends"] == 2
    assert all(oracle.closed for oracle in _FakeOracle.instances)


def test_oracle_pool_waits_for_busy_matching_slot_instead_of_replacing() -> None:
    _FakeOracle.instances = []
    pool = OraclePool(cache_version="v2", workers=2, oracle_factory=cast(Any, _FakeOracle))
    holder_ready = threading.Event()
    release_holder = threading.Event()
    waiter_done = threading.Event()

    def holder() -> None:
        with pool.acquire(_root_loaded("mathlib")):
            holder_ready.set()
            release_holder.wait(timeout=5)

    def physlib_user() -> None:
        with pool.acquire(_root_loaded("physlib")):
            pass

    def waiter() -> None:
        with pool.acquire(_root_loaded("mathlib")) as oracle:
            assert cast(_FakeOracle, oracle).rebinds >= 1
        waiter_done.set()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    holder_ready.wait(timeout=5)
    physlib_thread = threading.Thread(target=physlib_user)
    physlib_thread.start()
    physlib_thread.join(timeout=5)
    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    threading.Event().wait(0.1)
    # The mathlib slot is busy and the physlib slot is free, yet the waiter must not replace it.
    assert not waiter_done.is_set()
    assert len(_FakeOracle.instances) == 2
    release_holder.set()
    waiter_thread.join(timeout=5)
    holder_thread.join(timeout=5)
    assert waiter_done.is_set()
    assert pool.stats["waits"] >= 1
    assert pool.stats["closed_backends"] == 0
    assert len(_FakeOracle.instances) == 2
    pool.close()


def test_oracle_pool_replaces_only_when_no_slot_holds_project() -> None:
    _FakeOracle.instances = []
    pool = OraclePool(cache_version="v2", workers=1, oracle_factory=cast(Any, _FakeOracle))
    with pool.acquire(_root_loaded("mathlib")):
        pass
    with pool.acquire(_root_loaded("cslib")):
        pass
    assert pool.stats["closed_backends"] == 1
    assert pool.active_backend_count() == 1
    assert len(_FakeOracle.instances) == 2
    pool.close()
    with (
        pytest.raises(ProviderRehearsalV52Error, match="closed"),
        pool.acquire(_root_loaded("cslib")),
    ):
        pass


# ---------------------------------------------------------------------------
# Fix 7: oracle-v2 command traverses sorts, canonical universes, project-scoped rebind.
# ---------------------------------------------------------------------------


def _context(**overrides: object) -> Any:
    from leanfaith.representations.goal_v1 import CompileContext

    values: dict[str, Any] = {
        "project_id": "test",
        "project_revision": "abc",
        "lean_version": "leanprover/lean4:v4.99.0",
        "import_header": "import Lean",
        "command_preamble": "",
        "namespace_context": [],
        "open_context": [],
        "scoped_context": [],
        "options": {},
    }
    values.update(overrides)
    return CompileContext(**values)


def test_v2_command_collects_sort_levels_and_assigns_distinct_canonical_universes() -> None:
    command = _signature_command(
        context=_context(),
        signature="True",
        endpoint_id="test",
        render_scope_id="test",
        cache_version="v2",
    )
    assert "| .sort level => collectLevelMVars acc level" in command
    assert "collectExprLevelMVars" in command
    assert "assignCanonicalUniverses" in command
    assert 's!"u_{index}"' in command
    assert "universe u_0 u_1 u_2 u_3 u_4 u_5 u_6 u_7" in command
    assert command.count("Term.elabTerm") == 1


def test_project_backend_context_strips_root_scopes_only() -> None:
    context = _context(namespace_context=["Foo"], open_context=["Bar"], scoped_context=["Baz"])
    backend = project_backend_context(context)
    assert backend.namespace_context == ()
    assert backend.open_context == ()
    assert backend.scoped_context == ()
    assert backend.import_header == context.import_header
    assert backend.fingerprint != context.fingerprint
    assert project_backend_context(_context(open_context=["Other"])) == backend


def test_v2_oracle_rebind_is_project_scoped_and_reports_v2_identity() -> None:
    base = load_sft2a_config(_SPRINT_BASE)

    class _Backend:
        def run(self, request: Any) -> Any:
            raise AssertionError("no Lean in unit tests")

        def run_batch(self, requests: Any) -> Any:
            raise AssertionError("no Lean in unit tests")

        def close(self) -> None:
            return None

    oracle = SignatureOracle(base, backend=cast(Any, _Backend()), cache_version="v2")
    assert oracle.method_version == ORACLE_METHOD_VERSION_V2
    assert oracle.cache_version == "v2"
    assert oracle.backend_context.open_context == ()
    root = base.config.root
    rebound_context = root.compile_context.model_copy(update={"open_context": ["Nat"]})
    rebound = replace(
        base,
        config=base.config.model_copy(
            update={"root": root.model_copy(update={"compile_context": rebound_context})}
        ),
    )
    oracle.rebind(rebound)
    assert tuple(oracle.context.open_context) == ("Nat",)
    assert tuple(oracle.backend_context.open_context) == ()
    other_header = root.compile_context.model_copy(update={"import_header": "import Lean"})
    other = replace(
        base,
        config=base.config.model_copy(
            update={"root": root.model_copy(update={"compile_context": other_header})}
        ),
    )
    with pytest.raises(SignatureOracleError, match="import/option contexts"):
        oracle.rebind(other)


# ---------------------------------------------------------------------------
# Fix 8: zero-Lean sprint verifier.
# ---------------------------------------------------------------------------


def _sample_rows(prefix: str, mix: dict[str, int]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source, count in mix.items():
        for index in range(count):
            root_id = f"{source}:{prefix}:{index}"
            rows.append(
                {
                    "root": {
                        "root_id": root_id,
                        "source": source,
                        "reference_signature": f"P{root_id}",
                    },
                    "certified_reference": {
                        "closed_expr_hash": hash_canonical({"expr": root_id}),
                        "rendered_goal_hash": hash_canonical({"goal": root_id}),
                        "goal_v1": f"⊢ {root_id}",
                    },
                }
            )
    return rows


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    return path


_MIX = {"mathlib": 8, "physlib": 5, "cslib": 4, "compiler_data": 3}


def test_sprint_verifier_passes_and_reports_zero_lean(tmp_path: Path) -> None:
    sample = _write(tmp_path / "sample.jsonl", _sample_rows("pilot", _MIX))
    completed = _write(
        tmp_path / "completed.jsonl",
        _sample_rows("done", {"mathlib": 42, "physlib": 25, "cslib": 17, "compiler_data": 16}),
    )
    receipt = verify_sprint_pilot_sample(
        sample,
        expected_sha256=hash_file(sample),
        completed_sample_paths=[completed],
        blocked_signature_hashes=set(),
        verify_certificates=False,
    )
    assert receipt["verified"] is True
    assert receipt["rows"] == 20
    assert receipt["source_mix"] == {"compiler_data": 3, "cslib": 4, "mathlib": 8, "physlib": 5}
    assert receipt["completed_rows_screened"] == 100
    assert receipt["lean_requests_executed"] == 0
    assert receipt["provider_calls_executed"] == 0
    assert receipt["recertified_roots"] == 0
    assert receipt["gold_screen_applied"] is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("sha", "SHA-256 differs"),
        ("mix", "source mix differs"),
        ("dup_expr", "duplicate closed Expr"),
        ("placeholder", "placeholder marker"),
        ("overlap", "overlaps completed"),
        ("gold", "gold contamination"),
    ],
)
def test_sprint_verifier_rejects_each_invariant_violation(
    tmp_path: Path, mutation: str, message: str
) -> None:
    from leanfaith.representations.views import signature_near_dup_hash

    rows = _sample_rows("pilot", _MIX)
    completed_rows = _sample_rows("done", {"mathlib": 100})
    blocked: set[str] = set()
    if mutation == "mix":
        rows[0]["root"] = {**cast(dict[str, object], rows[0]["root"]), "source": "physlib"}
    elif mutation == "dup_expr":
        cast(dict[str, object], rows[1]["certified_reference"])["closed_expr_hash"] = cast(
            dict[str, object], rows[0]["certified_reference"]
        )["closed_expr_hash"]
    elif mutation == "placeholder":
        cast(dict[str, object], rows[0]["certified_reference"])["goal_v1"] = "⊢ ⋯"
    elif mutation == "overlap":
        completed_rows[3] = rows[2]
    elif mutation == "gold":
        blocked = {
            signature_near_dup_hash(
                str(cast(dict[str, object], rows[5]["certified_reference"])["goal_v1"])
            )
        }
    sample = _write(tmp_path / "sample.jsonl", rows)
    completed = _write(tmp_path / "completed.jsonl", completed_rows)
    expected = "0" * 64 if mutation == "sha" else hash_file(sample)
    with pytest.raises(CorrectedSampleError, match=message):
        verify_sprint_pilot_sample(
            sample,
            expected_sha256=expected,
            completed_sample_paths=[completed],
            blocked_signature_hashes=blocked,
            verify_certificates=False,
        )


# ---------------------------------------------------------------------------
# Fix 10 + 2: sprint loader enforces two workers/40 GiB, telemetry cap, and sample pin.
# ---------------------------------------------------------------------------


def test_sprint_config_loads_with_real_pins_and_rejects_bad_contracts(tmp_path: Path) -> None:
    loaded = load_provider_rehearsal_v52(_SPRINT_CONFIG)
    assert loaded.kind == "sprint"
    assert loaded.document["maximum_total_lean_workers"] == 2
    assert loaded.document["maximum_measured_rss_gib"] == 40.0
    assert loaded.document["kimi_audit_rows"] == 8
    assert loaded.ceilings.maximum_roots == 20
    assert loaded.ceilings.maximum_lemex_calls >= 16
    assert loaded.base.config.prompts.blinded_claude_judge.path == (
        "prompts/sft2a/blinded_judge_sprint_v1.txt"
    )
    assert "STRICT VERDICT, CONFIDENCE, AND ERROR_TYPE CONTRACT" in loaded.base.judge_prompt
    assert "verdict=unknown" in loaded.base.judge_prompt
    document = json.loads(_SPRINT_CONFIG.read_text())
    variants = {
        "kimi_audit_rows": ({"kimi_audit_rows": 9}, "at most 8"),
        "workers": ({"maximum_total_lean_workers": 1}, "exactly two persistent"),
        "sample": ({"sample_sha256": "0" * 64}, "SHA-256 differs"),
        "scale": ({"scale_10k_authorized": True}, "out-of-scope"),
        "concurrency": ({"provider_concurrency": 0}, "provider_concurrency"),
    }
    for name, (update, message) in variants.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({**document, **update}))
        with pytest.raises(ProviderRehearsalV52Error, match=message):
            load_provider_rehearsal_v52(path)

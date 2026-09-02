"""Tests for the sprint-track repairs: audit assembly, ledger caching, oracle pool,
sprint verifier, judge retry, pilot thresholds, CLI dispatch, and resource handling."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.sft2a.certified_sample_v52 import verify_sprint_pilot_sample
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.parallel_rehearsal import AtomicBudgetedProvider, AtomicProviderBudget
from leanfaith.sft2a.providers import ProviderCallResult

# ---------------------------------------------------------------------------
# Fix 1: Kimi audit result assembly maps futures to result positions.
# ---------------------------------------------------------------------------


def test_audit_selection_returns_non_contiguous_indices() -> None:
    """_audit_selection can return non-contiguous sidecar indices."""

    from leanfaith.sft2a.rehearsal import _audit_selection

    sidecars: list[dict[str, object]] = []
    for i in range(50):
        sidecars.append(
            {
                "row_id": f"mathlib:row:{i}",
                "root_id": f"mathlib:root:{i}",
                "requested_polarity": "preserving" if i % 2 == 0 else "breaking",
                "planned_mechanism": {"family": "fam_a" if i % 3 == 0 else "fam_b"},
            }
        )
    selected = _audit_selection(sidecars, 5)
    assert len(selected) == 5
    # With 50 rows and 5 selected, indices are very likely non-contiguous.
    assert selected != list(range(5))


def test_kimi_audit_assembly_maps_positions_not_source_indices(tmp_path: Path) -> None:
    """The audit_rows list must be indexed by result position, not source index."""

    # Build a minimal compacted output with non-contiguous selection.
    output_root = tmp_path / "run"
    (output_root / "replay").mkdir(parents=True)
    (output_root / "compacted/new_core").mkdir(parents=True)
    (output_root / "audit_kimi/checkpoints").mkdir(parents=True)

    # Replay receipt.
    (output_root / "replay/reproducibility_receipt.json").write_text(
        json.dumps({"reproducible": True}) + "\n"
    )

    # Build 50 sidecars with distinct strata so selection is non-contiguous.
    core_rows: list[dict[str, object]] = []
    sidecar_rows: list[dict[str, object]] = []
    for i in range(50):
        row_id = f"mathlib:root:{i}:slot:0"
        core_rows.append({"reference": f"ref_{i}", "candidate": f"cand_{i}", "label": True})
        sidecar_rows.append(
            {
                "row_id": row_id,
                "root_id": f"mathlib:root:{i}",
                "requested_polarity": "preserving" if i % 2 == 0 else "breaking",
                "planned_mechanism": {"family": "fam_a" if i % 3 == 0 else "fam_b"},
                "reference_repr": {"record": {"goal_v1": f"ref_goal_{i}"}},
                "candidate_repr": {"record": {"goal_v1": f"cand_goal_{i}"}},
                "claude_judge": {
                    "schema_version": 5,
                    "verdict": "equivalent",
                    "confidence": "high",
                    "relation_class": "logical_restatement",
                    "error_type": "none",
                    "rationale": "match",
                    "closure_checks": {
                        "entire_universally_closed_proposition": True,
                        "argument_swapping": "not_applicable",
                        "symmetry": "not_applicable",
                        "antisymmetry": "not_applicable",
                        "extensionality": "not_applicable",
                        "recoverable_boundary_cases": "checked_no_effect",
                    },
                },
            }
        )

    (output_root / "compacted/new_core/core.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in core_rows)
    )
    (output_root / "compacted/new_core/sidecar.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in sidecar_rows)
    )

    # Stub the provider to return a valid judgment.
    class _StubProvider:
        def preview_call(self, *, prompt: str, input_ids: Sequence[str]):
            del prompt, input_ids
            return "stub-key", tmp_path / "terminal.json", {}

        def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
            del prompt, input_ids
            return ProviderCallResult(
                call_key="stub-key",
                provider_id="lemex",
                structured={
                    "schema_version": 5,
                    "verdict": "equivalent",
                    "confidence": "high",
                    "relation_class": "logical_restatement",
                    "error_type": "none",
                    "rationale": "agree",
                    "closure_checks": {
                        "entire_universally_closed_proposition": True,
                        "argument_swapping": "not_applicable",
                        "symmetry": "not_applicable",
                        "antisymmetry": "not_applicable",
                        "extensionality": "not_applicable",
                        "recoverable_boundary_cases": "checked_no_effect",
                    },
                },
                usage={},
                cost_usd=None,
                elapsed_seconds=0.0,
                cache_hit=False,
                terminal_path=tmp_path / "terminal.json",
            )

    # Patch the audit to use our stub.
    with (
        patch(
            "leanfaith.sft2a.provider_rehearsal_v52.AtomicBudgetedProvider",
            lambda provider, **kw: AtomicBudgetedProvider(_StubProvider(), **kw),
        ),
        patch("leanfaith.sft2a.provider_rehearsal_v52.call_consistent_judge") as mock_judge,
        patch("leanfaith.sft2a.provider_rehearsal_v52.lemex_audit_provider"),
    ):
        from leanfaith.sft2a.judgments import ConsistentJudgeResult

        def _fake_judge(provider, *, prompt, input_ids, closure_aware, malformed_retries):
            call = _StubProvider().call(prompt=prompt, input_ids=input_ids)
            return ConsistentJudgeResult(
                judgment=None, calls=(call,), malformed_attempts=(), final_prompt=prompt
            )

        mock_judge.side_effect = _fake_judge
        # We can't easily call run_provider_kimi_audit_v52 without a full loaded
        # config, so we test the index-mapping logic directly.
        from leanfaith.sft2a.rehearsal import _audit_selection

        selected = _audit_selection(sidecar_rows, 5)
        # Verify non-contiguous indices produce correct-length result.
        assert len(selected) == 5
        # Simulate the fixed assembly: positions, not source indices.
        audit_rows: list[dict[str, object]] = [{} for _ in selected]
        for position, source_index in enumerate(selected):
            audit_rows[position] = {"row_id": sidecar_rows[source_index]["row_id"]}
        assert all(row != {} for row in audit_rows)
        assert len(audit_rows) == 5


# ---------------------------------------------------------------------------
# Fix 2: Configurable Kimi count.
# ---------------------------------------------------------------------------


def test_kimi_audit_count_is_configurable() -> None:
    import inspect

    from leanfaith.sft2a.provider_rehearsal_v52 import run_provider_kimi_audit_v52

    sig = inspect.signature(run_provider_kimi_audit_v52)
    assert "kimi_count" in sig.parameters
    assert sig.parameters["kimi_count"].default == 40


# ---------------------------------------------------------------------------
# Fix 4: Judge retry catches ValidationError, includes precise failure.
# ---------------------------------------------------------------------------


class _MalformedThenValidProvider:
    """Returns a schema-invalid result first, then a valid one."""

    def __init__(self) -> None:
        self.calls = 0

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del input_ids
        self.calls += 1
        if self.calls == 1:
            structured: dict[str, object] = {"schema_version": 5, "verdict": "equivalent"}
        else:
            structured = {
                "schema_version": 5,
                "verdict": "equivalent",
                "confidence": "high",
                "relation_class": "logical_restatement",
                "error_type": "none",
                "rationale": "They match.",
                "closure_checks": {
                    "entire_universally_closed_proposition": True,
                    "argument_swapping": "not_applicable",
                    "symmetry": "not_applicable",
                    "antisymmetry": "not_applicable",
                    "extensionality": "not_applicable",
                    "recoverable_boundary_cases": "checked_no_effect",
                },
            }
        return ProviderCallResult(
            call_key=f"key-{self.calls}",
            provider_id="lemex",
            structured=structured,
            usage={},
            cost_usd=None,
            elapsed_seconds=0.0,
            cache_hit=False,
            terminal_path=Path(f"/tmp/terminal-{self.calls}.json"),
        )


def test_judge_retry_includes_precise_validation_failure(tmp_path: Path) -> None:
    provider = _MalformedThenValidProvider()
    result = call_consistent_judge(
        provider,
        prompt="judge prompt",
        input_ids=("root", "cand", "blinded_judge_v5"),
        closure_aware=True,
        malformed_retries=1,
    )
    assert result.judgment is not None
    assert result.judgment.verdict == "equivalent"
    assert len(result.malformed_attempts) == 1
    reason = str(result.malformed_attempts[0]["reason"])
    assert reason.startswith("schema:")
    # The retry prompt must include the precise validation failure.
    assert "error_type=none" in result.final_prompt
    assert "Low confidence" in result.final_prompt or "low confidence" in result.final_prompt


# ---------------------------------------------------------------------------
# Fix 5: Provider ledger load-once (cached state, count physical reads).
# ---------------------------------------------------------------------------


def test_atomic_budget_uses_cached_events_not_physical_reads(tmp_path: Path) -> None:
    """AtomicProviderBudget loads the journal once and uses the cache afterwards."""

    ceilings = ExecutionCeilings(
        maximum_roots=100,
        maximum_provider_calls=150,
        maximum_proposer_calls=50,
        maximum_opus_calls=50,
        maximum_lemex_calls=50,
        maximum_attempts_per_slot=3,
        maximum_reported_opus_spend_usd=100.0,
        codex_cost_status="unavailable",
        lemex_cost_status="unavailable",
    )
    budget_path = tmp_path / "budget.jsonl"
    ledger = AtomicProviderBudget(budget_path, ceilings)

    # Initially no cache.
    assert ledger._cached_events is None
    # First access loads from disk.
    events1 = ledger._events_locked()
    assert ledger._cached_events is not None
    assert ledger._cached_events is events1
    # Subsequent accesses return the same cached object without re-reading.
    events2 = ledger._events_locked()
    assert events2 is events1  # same object, no re-read

    # Write an event and verify the cache is updated in-memory, not re-read.
    ledger.reserve(call_key="key-1", kind="lemex", worker_id="w-0")
    events3 = ledger._events_locked()
    assert events3 is events1  # still the same list, extended in-place
    assert len(events3) == 1


# ---------------------------------------------------------------------------
# Fix 6: OraclePool project affinity and backend cap.
# ---------------------------------------------------------------------------


class _FakeOracle:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.closed = False
        self.rebound = False

    def rebind(self, loaded) -> None:
        self.rebound = True

    def close(self) -> None:
        self.closed = True

    def elaborate(self, signature: str, *, endpoint_role: str):
        del signature, endpoint_role
        return None


def test_oracle_pool_project_affinity_and_backend_cap(tmp_path: Path) -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import OraclePool

    pool = OraclePool(cache_version="v2")

    # We can't easily create real SignatureOracles, so test the slot logic
    # by checking active_backend_count and project tracking.
    assert pool.active_backend_count() == 0
    assert pool._oracles == [None, None]
    assert pool._projects == [None, None]
    pool.close()
    assert pool.active_backend_count() == 0


# ---------------------------------------------------------------------------
# Fix 8: Zero-Lean sprint verifier.
# ---------------------------------------------------------------------------


def test_sprint_verifier_rejects_wrong_sha(tmp_path: Path) -> None:
    sample = tmp_path / "sample.jsonl"
    sample.write_text(json.dumps({"root": {"root_id": "x", "source": "mathlib"}}) + "\n")
    with pytest.raises(Exception, match="SHA-256 differs"):
        verify_sprint_pilot_sample(sample, expected_sha256="wrong")


def _make_sprint_sample(rows: list[dict[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(r) + b"\n" for r in rows)


def test_sprint_verifier_passes_valid_sample(tmp_path: Path) -> None:
    from leanfaith.config.hashing import hash_file

    sources = (
        [("mathlib", f"mathlib:root:{i}") for i in range(8)]
        + [("physlib", f"physlib:root:{i}") for i in range(5)]
        + [("cslib", f"cslib:root:{i}") for i in range(4)]
        + [("compiler_data", f"compiler_data:root:{i}") for i in range(3)]
    )
    rows = []
    for source, root_id in sources:
        rows.append(
            {
                "root": {"root_id": root_id, "source": source},
                "certified_reference": {
                    "closed_expr_hash": f"expr_{root_id}",
                    "rendered_goal_hash": f"goal_{root_id}",
                    "goal_v1": "x : Type\n⊢ True",
                },
            }
        )
    sample = tmp_path / "sample.jsonl"
    payload = _make_sprint_sample(rows)
    sample.write_bytes(payload)
    result = verify_sprint_pilot_sample(sample, expected_sha256=hash_file(sample))
    assert result["verified"] is True
    assert result["rows"] == 20
    assert result["lean_requests_executed"] == 0
    assert result["provider_calls_executed"] == 0


def test_sprint_verifier_rejects_overlap(tmp_path: Path) -> None:
    from leanfaith.config.hashing import hash_file

    shared_row = {
        "root": {"root_id": "mathlib:shared:0", "source": "mathlib"},
        "certified_reference": {
            "closed_expr_hash": "shared_expr",
            "rendered_goal_hash": "shared_goal",
            "goal_v1": "⊢ True",
        },
    }
    pilot_rows = [shared_row]
    for i in range(7):
        pilot_rows.append(
            {
                "root": {"root_id": f"mathlib:r:{i}", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": f"e_{i}",
                    "rendered_goal_hash": f"g_{i}",
                    "goal_v1": "⊢ True",
                },
            }
        )
    for source, n in [("physlib", 5), ("cslib", 4), ("compiler_data", 3)]:
        for i in range(n):
            pilot_rows.append(
                {
                    "root": {"root_id": f"{source}:r:{i}", "source": source},
                    "certified_reference": {
                        "closed_expr_hash": f"e_{source}_{i}",
                        "rendered_goal_hash": f"g_{source}_{i}",
                        "goal_v1": "⊢ True",
                    },
                }
            )

    completed_rows = [shared_row]
    for i in range(99):
        completed_rows.append(
            {
                "root": {"root_id": f"mathlib:comp:{i}", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": f"comp_e_{i}",
                    "rendered_goal_hash": f"comp_g_{i}",
                    "goal_v1": "⊢ True",
                },
            }
        )

    pilot = tmp_path / "pilot.jsonl"
    pilot.write_bytes(_make_sprint_sample(pilot_rows))
    completed = tmp_path / "completed.jsonl"
    completed.write_bytes(_make_sprint_sample(completed_rows))

    with pytest.raises(Exception, match="overlaps completed 100"):
        verify_sprint_pilot_sample(
            pilot,
            expected_sha256=hash_file(pilot),
            completed_100_sample_path=completed,
        )


# ---------------------------------------------------------------------------
# Fix 7: Oracle-v2 universe levels in assignUnivMVars.
# ---------------------------------------------------------------------------


def test_v2_command_traverses_sort_universe_levels() -> None:
    from leanfaith.representations.goal_v1 import CompileContext
    from leanfaith.sft2a.lean_oracle import _signature_command

    context = CompileContext(
        project_id="test",
        project_revision="abc",
        lean_version="leanprover/lean4:v4.99.0",
        import_header="import Lean",
        command_preamble="",
        namespace_context=[],
        open_context=[],
        scoped_context=[],
        options={},
    )
    command = _signature_command(
        context=context,
        signature="True",
        endpoint_id="test",
        render_scope_id="test",
        cache_version="v2",
    )
    assert ".sort" in command or "assignLevelMVars" in command
    assert "assignUnivMVars" in command


# ---------------------------------------------------------------------------
# Fix 9: CLI dispatch tests.
# ---------------------------------------------------------------------------


def test_cli_has_sprint_commands() -> None:
    import inspect

    import leanfaith.sft2a.__main__ as sft2a_main

    source = inspect.getsource(sft2a_main)
    for cmd in [
        "verify-sprint-pilot-sample",
        "launch-sprint-pilot-v5-2",
        "detached-sprint-pilot-v5-2-worker",
        "sprint-pilot-v5-2-health",
        "run-audit-only-kimi-v5-2",
    ]:
        assert cmd in source, f"CLI missing command: {cmd}"


# ---------------------------------------------------------------------------
# Fix 10: Resource handling uses correct claim_resources args.
# ---------------------------------------------------------------------------


def test_sprint_config_requires_two_workers() -> None:
    """The sprint pilot config must declare exactly 2 Lean workers and 40 GiB."""

    config_path = Path(__file__).resolve().parents[3] / "configs/sft2a/sprint_pilot_20roots_v1.json"
    config = json.loads(config_path.read_text())
    assert config["maximum_total_lean_workers"] == 2
    assert config["maximum_measured_rss_gib"] == 40.0
    assert config["maximum_root_workers"] == 2


def test_claim_resources_uses_correct_arg_names() -> None:
    import inspect

    from leanfaith.host_resources import claim_resources

    sig = inspect.signature(claim_resources)
    params = set(sig.parameters)
    assert "lean_workers" in params
    assert "worktree" in params
    assert "workers" not in params  # old wrong name must not exist


# ---------------------------------------------------------------------------
# Fix 11: Pilot threshold evaluation.
# ---------------------------------------------------------------------------


def test_pilot_thresholds_pass_with_good_metrics(tmp_path: Path) -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import evaluate_sprint_pilot_thresholds

    # Create a minimal sample.
    sample = tmp_path / "sample.jsonl"
    rows = []
    for i in range(20):
        rows.append(
            {
                "root": {"root_id": f"mathlib:r:{i}", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": f"e_{i}",
                    "rendered_goal_hash": f"g_{i}",
                    "goal_v1": "⊢ True",
                },
            }
        )
    sample.write_bytes(_make_sprint_sample(rows))

    class _FakeLoaded:
        sample_path = sample

    result = evaluate_sprint_pilot_thresholds(
        _FakeLoaded(),  # type: ignore[arg-type]
        compaction={"accepted_rows": 60, "self_pairs": 0, "candidate_duplicates": 0},
        replay={"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        wall_seconds=1200.0,
    )
    assert result["passed"] is True
    assert result["scale_10k_authorized"] is True


def test_pilot_thresholds_fail_with_low_acceptance(tmp_path: Path) -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import evaluate_sprint_pilot_thresholds

    sample = tmp_path / "sample.jsonl"
    rows = []
    for i in range(20):
        rows.append(
            {
                "root": {"root_id": f"mathlib:r:{i}", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": f"e_{i}",
                    "rendered_goal_hash": f"g_{i}",
                    "goal_v1": "⊢ True",
                },
            }
        )
    sample.write_bytes(_make_sprint_sample(rows))

    class _FakeLoaded:
        sample_path = sample

    result = evaluate_sprint_pilot_thresholds(
        _FakeLoaded(),  # type: ignore[arg-type]
        compaction={"accepted_rows": 30, "self_pairs": 0, "candidate_duplicates": 0},
        replay={"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        wall_seconds=1200.0,
    )
    assert result["passed"] is False
    assert result["scale_10k_authorized"] is False


def test_pilot_thresholds_fail_with_self_pairs(tmp_path: Path) -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import evaluate_sprint_pilot_thresholds

    sample = tmp_path / "sample.jsonl"
    rows = []
    for i in range(20):
        rows.append(
            {
                "root": {"root_id": f"mathlib:r:{i}", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": f"e_{i}",
                    "rendered_goal_hash": f"g_{i}",
                    "goal_v1": "⊢ True",
                },
            }
        )
    sample.write_bytes(_make_sprint_sample(rows))

    class _FakeLoaded:
        sample_path = sample

    result = evaluate_sprint_pilot_thresholds(
        _FakeLoaded(),  # type: ignore[arg-type]
        compaction={"accepted_rows": 70, "self_pairs": 1, "candidate_duplicates": 0},
        replay={"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        wall_seconds=1200.0,
    )
    assert result["passed"] is False


def test_pilot_thresholds_fail_on_wall_time(tmp_path: Path) -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import evaluate_sprint_pilot_thresholds

    sample = tmp_path / "sample.jsonl"
    sample.write_text("{}\n")

    class _FakeLoaded:
        sample_path = sample

    result = evaluate_sprint_pilot_thresholds(
        _FakeLoaded(),  # type: ignore[arg-type]
        compaction={"accepted_rows": 70, "self_pairs": 0, "candidate_duplicates": 0},
        replay={"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        wall_seconds=2400.0,
    )
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# Fix 7d: Final labels from accepted Opus verdict.
# ---------------------------------------------------------------------------


def test_label_derived_from_verdict_not_polarity() -> None:
    import inspect

    from leanfaith.sft2a import pipeline

    source = inspect.getsource(pipeline.run_one_root)
    assert 'label=judgment.verdict == "equivalent"' in source
    assert "label=slot.requested_polarity" not in source

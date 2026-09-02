"""Corrections requested after pilot v1: the preserving-slot universe guard with its known
regression row, cell-balanced Kimi sampling, schema-only judge retries, the rate-only Lean gate,
receipt identity binding, sprint-window projection, and combined cross-shard compaction."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a import sprint_pilot_v52, sprint_scale_v52
from leanfaith.sft2a.judgments import call_consistent_judge, verdict_rationale_contradiction
from leanfaith.sft2a.lean_oracle import (
    COMMAND_TEMPLATE_VERSION_V2,
    ORACLE_METHOD_VERSION_V2,
    SignatureOracleResult,
    elaborator_sha256,
)
from leanfaith.sft2a.models import JudgeOutputV5
from leanfaith.sft2a.pipeline import universe_profile_mismatch
from leanfaith.sft2a.provider_rehearsal_v52 import (
    ProviderRehearsalV52Error,
    load_provider_rehearsal_v52,
    sprint_audit_selection,
)
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.sprint_pilot_v52 import (
    SprintPilotError,
    chain_decision,
    evaluate_sprint_pilot_thresholds,
    load_audit_only_kimi_v52,
    quarantined_row_ids,
    sprint_window_projection,
)

_PILOT_V1_SIDECAR = Path(
    "/storage/milikic/leanfaith/value_first/sft2_llm_transforms_v1/runs/"
    "sprint_pilot_20roots_run/compacted/new_core/sidecar.jsonl"
)
_REGRESSION_ROW = "sft2a-new:35707756dbe6d253f3eb500adf71e1d56435308a86cbe836f03eb8fea19b153d"


def _result(levels: Sequence[str] | None, goal: str = "⊢ True") -> SignatureOracleResult:
    provenance: dict[str, object] = {"expr_hash": hash_canonical({"goal": goal})}
    if levels is not None:
        provenance["canonical_level_params"] = list(levels)
    return SignatureOracleResult(
        status="valid",
        cache_key="k:" + hash_canonical({"goal": goal})[:16],
        cache_hit=True,
        signature_sha256=hash_canonical({"signature": goal}),
        goal_v1=goal,
        sidecar={"record": {"goal_v1": goal, "provenance": provenance}},
        lean_status="valid",
        request_hash=None,
        elapsed_ms=1,
        raw_response_path=None,
        detail="",
    )


# ---------------------------------------------------------------------------
# 1. Universe guard.
# ---------------------------------------------------------------------------


def test_universe_guard_rejects_specialization_and_generalization_only() -> None:
    assert universe_profile_mismatch(_result(["u_0"]), _result(["u_0"])) is None
    narrowed = universe_profile_mismatch(_result(["u_0"]), _result([]))
    assert narrowed is not None and "reference=['u_0'] candidate=[]" in narrowed
    generalized = universe_profile_mismatch(_result([]), _result(["u_0"]))
    assert generalized is not None
    reordered = universe_profile_mismatch(_result(["u_0", "u_1"]), _result(["u_1", "u_0"]))
    assert reordered is not None
    assert universe_profile_mismatch(_result(None), _result(None)) is None


def test_universe_guard_regression_row_from_pilot_v1_is_rejected_and_quarantined() -> None:
    assert _REGRESSION_ROW in quarantined_row_ids(Path.cwd())
    if not _PILOT_V1_SIDECAR.is_file():
        pytest.skip("pilot v1 evidence is not mounted")
    row = next(
        json.loads(line)
        for line in _PILOT_V1_SIDECAR.read_text().splitlines()
        if json.loads(line)["row_id"] == _REGRESSION_ROW
    )
    assert row["requested_polarity"] == "preserving"
    assert row["claude_judge"]["verdict"] == "equivalent"
    reference = _result(row["reference_repr"]["record"]["provenance"]["canonical_level_params"])
    candidate = _result(row["candidate_repr"]["record"]["provenance"]["canonical_level_params"])
    detail = universe_profile_mismatch(reference, candidate)
    assert detail is not None and "reference=['u_0'] candidate=[]" in detail


def test_universe_guard_runs_inside_the_root_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanfaith.sft2a.config import load_sft2a_config
    from leanfaith.sft2a.mechanisms import (
        BREAKING_MECHANISMS,
        PRESERVING_MECHANISMS,
        MechanismAssignment,
    )
    from leanfaith.sft2a.pipeline import run_one_root

    base = load_sft2a_config(Path("configs/sft2a/closure_aware_v5_2_sprint_v3.yaml"))
    from dataclasses import replace

    staging = str(tmp_path / "staging")
    config = base.config.model_copy(
        update={
            "staging_root": staging,
            "run_layout": base.config.run_layout.model_copy(update={"shared_cache_root": staging}),
        }
    )  # type: ignore[union-attr]
    loaded = replace(
        base, config=config, config_hash=hash_canonical(config.model_dump(mode="json"))
    )
    plan = {
        "preserve_0": MechanismAssignment(
            PRESERVING_MECHANISMS[0].family, "preserving", "i", "general", "s"
        ),
        "preserve_1": MechanismAssignment(
            PRESERVING_MECHANISMS[1].family, "preserving", "i", "general", "s"
        ),
        "break_0": MechanismAssignment(
            BREAKING_MECHANISMS[0].family, "breaking", "i", "general", "s"
        ),
        "break_1": MechanismAssignment(
            BREAKING_MECHANISMS[1].family, "breaking", "i", "general", "s"
        ),
    }
    binders = "∀ {α : Type u_0} [inst : Preorder α] {a b c : α}, "
    narrow = "∀ {α : Type} [inst : Preorder α] {a b c : α}, "

    def proposal(polarity: str, family: str, signature: str) -> dict[str, object]:
        return {
            "schema_version": 5,
            "requested_polarity": polarity,
            "mechanism": family,
            "applicability_reason": "t",
            "candidate_signature": signature,
            "change_summary": "t",
            "judge_trap": "t",
            "informative": True,
            "substantive_change": True,
            "proof_free": True,
        }

    class _Proposer:
        def __init__(self) -> None:
            self.provider_id = loaded.config.proposer.provider_id
            self.calls: list[tuple[str, ...]] = []

        script: ClassVar[list[dict[str, object]]] = [
            proposal("preserving", plan["preserve_0"].family, narrow + "b ≤ c → a ≤ b → a ≤ c"),
            proposal("preserving", plan["preserve_0"].family, binders + "b ≤ c → a ≤ b → a ≤ c"),
            proposal("preserving", plan["preserve_1"].family, binders + "a ≤ b ∧ b ≤ c → a ≤ c"),
            proposal("breaking", plan["break_0"].family, narrow + "a ≤ b → b ≤ c → c ≤ a"),
            proposal("breaking", plan["break_1"].family, binders + "a ≤ b → b ≤ c → b ≤ a"),
        ]

        def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
            index = len(self.calls)
            self.calls.append(tuple(input_ids))
            return ProviderCallResult(
                call_key=f"p{index}",
                provider_id=self.provider_id,
                structured=self.script[index],
                usage={},
                cost_usd=0.0,
                elapsed_seconds=0.0,
                cache_hit=False,
                terminal_path=Path("/nonexistent"),
            )

    class _Judge:
        def __init__(self) -> None:
            self.provider_id = loaded.config.claude_judge.provider_id
            self.calls: list[str] = []

        def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
            self.calls.append(prompt)
            verdict = "non_equivalent" if ("c ≤ a" in prompt or "b ≤ a" in prompt) else "equivalent"
            structured = {
                "schema_version": 5,
                "verdict": verdict,
                "confidence": "high",
                "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
                "error_type": "none",
                "rationale": "t",
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
                call_key=f"j{len(self.calls)}",
                provider_id=self.provider_id,
                structured=structured,
                usage={},
                cost_usd=0.0,
                elapsed_seconds=0.0,
                cache_hit=False,
                terminal_path=Path("/nonexistent"),
            )

    class _Oracle:
        method_version = ORACLE_METHOD_VERSION_V2
        cache_version = "v2"

        def elaborate(self, signature: str, *, endpoint_role: str) -> SignatureOracleResult:
            levels = ["u_0"] if "Type u_0" in signature else []
            return _result(levels, goal=f"⊢ {signature}")

        def close(self) -> None:
            return None

    reference = _result(["u_0"], goal=loaded.config.root.expected_reference_goal_v1)
    result = run_one_root(
        loaded,
        proposer=_Proposer(),
        claude_judge=_Judge(),
        oracle=_Oracle(),
        output_root=tmp_path / "root",
        enforce_expected_reference_goal=True,
        enforce_smoke_ceilings=False,
        mechanism_plan=plan,
        enforce_closure_canaries=False,
        certified_reference=reference,
    )
    counts = cast(dict[str, object], result.manifest["counts"])
    # The narrowed preserving candidate is rejected before any judge call and only that slot retries;
    # the narrowed breaking candidate is not subject to the guard.
    assert counts["universe_mismatch_rejections"] == 1
    assert counts["accepted"] == 4
    assert counts["retry_slots"] == 1
    attempts = [
        json.loads(line)
        for line in (tmp_path / "root/attempts/terminal_attempts.jsonl").read_text().splitlines()
    ]
    rejected = [a for a in attempts if a["status"] == "universe_mismatch_rejected"]
    assert len(rejected) == 1 and rejected[0]["slot_id"] == "preserve_0"
    assert rejected[0]["judge_call_key"] is None
    assert rejected[0]["reference_canonical_level_params"] == ["u_0"]
    assert rejected[0]["candidate_canonical_level_params"] == []
    assert counts["judge_lexical_contradictions"] == 0


# ---------------------------------------------------------------------------
# 2. Cell-balanced Kimi sampling.
# ---------------------------------------------------------------------------


def _sidecars(cells: dict[tuple[str, str], int], families: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (source, polarity), count in cells.items():
        for index in range(count):
            rows.append(
                {
                    "row_id": f"sft2a-new:{source}:{polarity}:{index}",
                    "root_id": f"{source}:census:{index}",
                    "requested_polarity": polarity,
                    "planned_mechanism": {"family": f"fam_{index % families}"},
                }
            )
    return rows


def _cell_counts(
    sidecars: list[dict[str, object]], selected: list[int]
) -> Counter[tuple[str, str]]:
    return Counter(
        (str(sidecars[i]["root_id"]).split(":")[0], str(sidecars[i]["requested_polarity"]))
        for i in selected
    )


def test_eight_row_audit_selects_exactly_one_row_per_source_polarity_cell() -> None:
    cells = {
        (s, p): n
        for s, n in (("mathlib", 15), ("physlib", 9), ("cslib", 5), ("compiler_data", 6))
        for p in ("preserving", "breaking")
    }
    sidecars = _sidecars(cells)
    selected = sprint_audit_selection(sidecars, 8)
    assert len(selected) == 8 and len(set(selected)) == 8
    assert _cell_counts(sidecars, selected) == dict.fromkeys(cells, 1)
    assert sprint_audit_selection(sidecars, 8) == selected  # deterministic
    sixteen = sprint_audit_selection(sidecars, 16)
    assert _cell_counts(sidecars, sixteen) == dict.fromkeys(cells, 2)
    # Inside a cell the second pick diversifies by mechanism family.
    by_cell: dict[tuple[str, str], list[str]] = {}
    for i in sixteen:
        row = sidecars[i]
        key = (str(row["root_id"]).split(":")[0], str(row["requested_polarity"]))
        by_cell.setdefault(key, []).append(
            str(cast(dict[str, object], row["planned_mechanism"])["family"])
        )
    assert all(len(set(families)) == 2 for families in by_cell.values())
    with pytest.raises(ProviderRehearsalV52Error, match="cannot fill"):
        sprint_audit_selection(sidecars, 200)


def test_cell_sampler_skips_empty_cells_but_keeps_balance() -> None:
    cells = {("mathlib", "preserving"): 4, ("mathlib", "breaking"): 4, ("physlib", "preserving"): 2}
    sidecars = _sidecars(cells)
    selected = sprint_audit_selection(sidecars, 6)
    assert _cell_counts(sidecars, selected) == {
        ("mathlib", "preserving"): 2,
        ("mathlib", "breaking"): 2,
        ("physlib", "preserving"): 2,
    }


def test_pilot_v1_cell_audit_config_loads_with_cell_sampler() -> None:
    loaded = load_audit_only_kimi_v52(
        Path("configs/sft2a/audit_only_kimi_sprint_pilot_v1_cells.json")
    )
    assert loaded.kimi_rows == 8 and loaded.sampler == "source_polarity_cells"
    assert loaded.audit_subdir == "audit_kimi_cells_v1"
    assert loaded.source.kind == "sprint"
    assert loaded.ceilings.maximum_lemex_calls == 40
    assert loaded.ceilings.maximum_proposer_calls == loaded.source.ceilings.maximum_proposer_calls


# ---------------------------------------------------------------------------
# 3. Schema-only judge retries; lexical contradiction is telemetry.
# ---------------------------------------------------------------------------


def _judge_payload(verdict: str, rationale: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "conclusion_strength",
        "error_type": "none",
        "rationale": rationale,
        "closure_checks": {
            "entire_universally_closed_proposition": True,
            "argument_swapping": "not_applicable",
            "symmetry": "not_applicable",
            "antisymmetry": "not_applicable",
            "extensionality": "not_applicable",
            "recoverable_boundary_cases": "checked_no_effect",
        },
    }


class _Scripted:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        structured = self.responses[self.calls]
        self.calls += 1
        return ProviderCallResult(
            call_key=f"c{self.calls}",
            provider_id="opus",
            structured=structured,
            usage={},
            cost_usd=0.03,
            elapsed_seconds=1.0,
            cache_hit=False,
            terminal_path=Path("/nonexistent"),
        )


def test_do_not_express_the_same_claim_no_longer_triggers_a_paid_retry() -> None:
    payload = _judge_payload(
        "non_equivalent", "The two closed propositions do not express the same claim."
    )
    judgment = JudgeOutputV5.model_validate(payload)
    assert (
        verdict_rationale_contradiction(judgment)
        == "non_equivalent_verdict_with_equivalent_rationale"
    )
    provider = _Scripted([payload, payload])
    result = call_consistent_judge(
        provider, prompt="p", input_ids=("r",), closure_aware=True, malformed_retries=1
    )
    assert provider.calls == 1
    assert result.judgment is not None and result.judgment.verdict == "non_equivalent"
    assert result.malformed_attempts == ()
    assert result.lexical_contradiction == "non_equivalent_verdict_with_equivalent_rationale"


def test_schema_invalid_output_still_retries_exactly_once() -> None:
    invalid = {"schema_version": 5, "verdict": "equivalent"}
    valid = _judge_payload("equivalent", "Both express the same claim.")
    provider = _Scripted([invalid, valid])
    result = call_consistent_judge(
        provider, prompt="p", input_ids=("r",), closure_aware=True, malformed_retries=1
    )
    assert provider.calls == 2 and result.judgment is not None
    assert len(result.malformed_attempts) == 1
    assert result.lexical_contradiction is None
    exhausted = call_consistent_judge(
        _Scripted([invalid, invalid]),
        prompt="p",
        input_ids=("r",),
        closure_aware=True,
        malformed_retries=1,
    )
    assert exhausted.judgment is None and len(exhausted.malformed_attempts) == 2


# ---------------------------------------------------------------------------
# 4. Rate-only Lean gate with unique-slot telemetry.
# ---------------------------------------------------------------------------


def _loaded(tmp_path: Path, **document: object) -> Any:
    from leanfaith.sft2a.models import ExecutionCeilings
    from leanfaith.sft2a.provider_rehearsal_v52 import LoadedProviderRehearsalV52

    sample = tmp_path / "sample.jsonl"
    sample.write_bytes(
        b"".join(
            canonical_json_bytes({"root": {"root_id": f"mathlib:t:{i}"}}) + b"\n" for i in range(20)
        )
    )
    return LoadedProviderRehearsalV52(
        path=tmp_path / "c.json",
        document=dict(document),
        sha256="d" * 64,
        base=cast(
            Any,
            SimpleNamespace(
                config=SimpleNamespace(staging_root=str(tmp_path)),
                repo_root=tmp_path,
                config_hash="h" * 64,
            ),
        ),
        sample_path=sample,
        output_root=tmp_path / "run",
        ceilings=ExecutionCeilings.model_validate(
            {
                "maximum_roots": 20,
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


def _evaluate(
    tmp_path: Path, manifests: list[dict[str, object]], **overrides: Any
) -> dict[str, object]:
    values: dict[str, Any] = {
        "compaction": {"accepted_rows": 66, "self_pairs": 0, "candidate_duplicates": 0},
        "replay": {"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        "generation_wall_seconds": 985.0,
        "malformed_injection": {"passed": True},
        "resume_check": {
            "manifests_unchanged": True,
            "provider_calls_for_completed_roots_after_resume": 0,
            "lean_requests_for_completed_roots_after_resume": 0,
        },
        "root_manifests": manifests,
        "infrastructure": {"infrastructure_failure_rate": 0.0},
    }
    values.update(overrides)
    return evaluate_sprint_pilot_thresholds(_loaded(tmp_path), **values)


def _manifest(invalid: int, requests: int) -> dict[str, object]:
    return {
        "counts": {"lean_invalid_attempts": invalid, "candidate_attempts": requests},
        "lean": {"candidate_requests": requests},
    }


def test_lean_gate_is_rate_only_and_per_slot_counts_are_telemetry(tmp_path: Path) -> None:
    # 24 invalid of 118 elaborations = 20.3% passes even though 24 exceeds a 20-of-80 slot count.
    manifests = [_manifest(1, 6)] * 18 + [_manifest(3, 5)] * 2
    result = _evaluate(tmp_path, manifests)
    assert result["lean_invalid_attempts"] == 24 and result["candidate_lean_requests"] == 118
    assert result["passed"] is True
    assert result["lean_invalid_gate"] == "lean_invalid_attempts / candidate_lean_requests < 0.25"
    assert result["lean_invalid_unique_slots"] == 0  # telemetry only; no journals in this fixture
    # v1's 36 of 118 fails on the rate alone.
    failed = _evaluate(tmp_path, [_manifest(2, 6)] * 18 + [_manifest(0, 5)] * 2)
    assert failed["passed"] is False and failed["failed_checks"] == ["lean_invalid_below_25pct"]


# ---------------------------------------------------------------------------
# 5. Receipt binding, projection, and combined compaction.
# ---------------------------------------------------------------------------


def test_prerequisite_receipts_verify_config_method_and_elaborator_identity(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path, oracle_v2_gate_receipt_path=str(tmp_path / "gate.json"))
    canary = sprint_pilot_v52.run_paths(loaded.base).one_root if False else None  # noqa: F841
    from leanfaith.sft2a.layout import run_paths as real_run_paths

    class _Config(SimpleNamespace):
        pass

    # Point run_paths at a temp layout through a minimal Opus-free config: patch run_paths.
    canary_dir = tmp_path / "one_root/closure_canaries_v5"
    canary_dir.mkdir(parents=True)
    canary_path = canary_dir / "manifest.json"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        sprint_pilot_v52, "run_paths", lambda base: SimpleNamespace(one_root=tmp_path / "one_root")
    )
    try:
        canary_path.write_text(json.dumps({"all_passed": True, "config_hash": "other"}))
        with pytest.raises(SprintPilotError, match="different base config"):
            sprint_pilot_v52.require_sprint_prerequisite_receipts(loaded)
        canary_path.write_text(json.dumps({"all_passed": True, "config_hash": "h" * 64}))
        gate = tmp_path / "gate.json"
        gate.write_text(
            json.dumps(
                {
                    "all_passed": True,
                    "method_version": ORACLE_METHOD_VERSION_V2,
                    "cache_version": "v2",
                    "elaborator_sha256": "stale",
                    "command_template_version": COMMAND_TEMPLATE_VERSION_V2,
                    "base_config_hash": "h" * 64,
                }
            )
        )
        with pytest.raises(SprintPilotError, match="identity differs"):
            sprint_pilot_v52.require_sprint_prerequisite_receipts(loaded)
        gate.write_text(
            json.dumps(
                {
                    "all_passed": True,
                    "method_version": ORACLE_METHOD_VERSION_V2,
                    "cache_version": "v2",
                    "elaborator_sha256": elaborator_sha256("v2"),
                    "command_template_version": COMMAND_TEMPLATE_VERSION_V2,
                    "base_config_hash": "h" * 64,
                }
            )
        )
        receipts = sprint_pilot_v52.require_sprint_prerequisite_receipts(loaded)
        assert receipts["oracle_v2_live_gate_identity"]["elaborator_sha256"] == elaborator_sha256(
            "v2"
        )
        assert receipts["closure_canaries_config_hash"] == "h" * 64
    finally:
        monkey.undo()
    del real_run_paths, _Config


def test_shard_projection_stops_chain_when_window_is_exceeded(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    soon = (datetime.now(UTC) + timedelta(hours=10)).isoformat()
    shard = _loaded(
        tmp_path,
        sprint_role="shard",
        shard_index=1,
        shard_count=10,
        next_shard_config_path=str(tmp_path / "next.json"),
        sprint_deadline_utc=soon,
        fallback_provider_concurrency=8,
    )
    evaluation = {
        "failed_checks": [],
        "generation_wall_seconds": 6 * 3600.0,
        "accepted_rows_per_minute": 8.5,
    }
    projection = sprint_window_projection(shard, evaluation)
    assert projection["remaining_shards"] == 9 and projection["fits_sprint_window"] is False
    decision = chain_decision(shard, terminal={"status": "complete"}, evaluation=evaluation)
    assert decision["action"] == "stop" and decision["reason"] == "projection_exceeds_sprint_window"
    fast = {
        "failed_checks": [],
        "generation_wall_seconds": 30 * 60.0,
        "accepted_rows_per_minute": 40.0,
    }
    assert (
        chain_decision(shard, terminal={"status": "complete"}, evaluation=fast)["action"]
        == "launch_next_shard"
    )
    no_deadline = _loaded(
        tmp_path,
        sprint_role="shard",
        shard_index=1,
        shard_count=10,
        next_shard_config_path=str(tmp_path / "next.json"),
        fallback_provider_concurrency=8,
    )
    assert sprint_window_projection(no_deadline, evaluation)["fits_sprint_window"] is None


def test_shard_config_contract_allows_one_cooperative_worker(tmp_path: Path) -> None:
    document = json.loads(Path("configs/sft2a/sprint_pilot_20roots_v2.json").read_text())
    shard = {
        **document,
        "sprint_role": "shard",
        "kimi_audit_fraction": 0.1,
        "kimi_audit_rows_maximum": 8,
        "fallback_provider_concurrency": 4,
        "minimum_accepted_rows_per_minute": 8.0,
        "maximum_root_workers": 1,
        "maximum_total_lean_workers": 1,
        "maximum_measured_rss_gib": 20.0,
        "shared_candidate_registry_path": str(tmp_path / "registry.jsonl"),
        "sprint_deadline_utc": "2026-09-04T00:00:00+00:00",
    }
    path = tmp_path / "shard.json"
    path.write_text(json.dumps(shard))
    loaded = load_provider_rehearsal_v52(path)
    assert loaded.document["maximum_total_lean_workers"] == 1
    path.write_text(json.dumps({**shard, "maximum_measured_rss_gib": 40.0}))
    with pytest.raises(ProviderRehearsalV52Error, match="20 GiB each"):
        load_provider_rehearsal_v52(path)
    path.write_text(
        json.dumps(
            {
                **document,
                "maximum_total_lean_workers": 1,
                "maximum_root_workers": 1,
                "maximum_measured_rss_gib": 20.0,
            }
        )
    )
    with pytest.raises(ProviderRehearsalV52Error, match="exactly two persistent"):
        load_provider_rehearsal_v52(path)


def test_combined_compaction_dedups_across_shards_and_applies_exclusions(tmp_path: Path) -> None:
    shard_root = tmp_path / "shards"
    receipts = []
    for shard in (1, 2):
        run = shard_root / f"shard_{shard:02d}/run"
        (run / "compacted/new_core").mkdir(parents=True)
        (run / "audit_kimi").mkdir(parents=True)
        core = []
        sidecars = []
        for index in range(3):
            signature = (
                f"∀ (n : ℕ), n + {index} = {index} + n"
                if not (shard == 2 and index == 0)
                else "∀ (n : ℕ), n + 0 = 0 + n"
            )
            row_id = f"sft2a-new:s{shard}:{index}"
            core.append(
                {"reference": "⊢ R", "candidate": f"⊢ {signature}", "label": index % 2 == 0}
            )
            sidecars.append(
                {
                    "row_id": row_id,
                    "raw_candidate_signature": signature,
                    "candidate_closed_expr_hash": hash_canonical({"e": signature}),
                    "candidate_repr": {"record": {"goal_v1": f"⊢ {signature}"}},
                }
            )
        (run / "compacted/new_core/core.jsonl").write_bytes(
            b"".join(canonical_json_bytes(r) + b"\n" for r in core)
        )
        (run / "compacted/new_core/sidecar.jsonl").write_bytes(
            b"".join(canonical_json_bytes(r) + b"\n" for r in sidecars)
        )
        (run / "compacted/manifest.json").write_text("{}")
        audit = [
            {
                "row_id": f"sft2a-new:s{shard}:1",
                "action": "unknown_review_exclude_core" if shard == 1 else "retain",
            }
        ]
        (run / "audit_kimi/audit_rows.jsonl").write_bytes(
            b"".join(canonical_json_bytes(r) + b"\n" for r in audit)
        )
        config_path = shard_root / f"shard_{shard:02d}/provider_config.json"
        config_path.write_text("{}")
        receipts.append({"shard": shard, "provider_config_path": str(config_path)})
    (shard_root / "shards_manifest.json").write_text(json.dumps({"shards": receipts}))
    loaded = SimpleNamespace(shard_root=shard_root, sha256="p" * 64)
    manifest = sprint_scale_v52.compact_sprint_shards(
        cast(Any, loaded), quarantine_row_ids=frozenset({"sft2a-new:s2:2"})
    )
    assert manifest["rows_before_dedup"] == 6
    assert manifest["cross_shard_duplicates"] == 1  # shard 2 row 0 duplicates shard 1 row 0
    assert manifest["excluded_by_kimi_telemetry"] == 1
    assert manifest["excluded_quarantined"] == 1
    assert manifest["rows"] == 3
    rows = [
        json.loads(line)
        for line in (shard_root / "combined/sidecar.jsonl").read_text().splitlines()
    ]
    assert [row["row_id"] for row in rows] == sorted(row["row_id"] for row in rows)
    assert manifest == sprint_scale_v52.compact_sprint_shards(
        cast(Any, loaded), quarantine_row_ids=frozenset({"sft2a-new:s2:2"})
    )
    duplicates = [
        json.loads(line)
        for line in (shard_root / "combined/cross_shard_duplicates.jsonl").read_text().splitlines()
    ]
    assert duplicates == [
        {"row_id": "sft2a-new:s2:0", "duplicate_of": "sft2a-new:s1:0", "shard": 2}
    ]
    assert hash_file(shard_root / "combined/core.jsonl") == manifest["core_sha256"]

"""Focused, offline tests for the recovered Qwen/Kimi single-pass judge."""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import leanfaith.corpus2.judge_recovered as recovered
import leanfaith.generation.lf022_codex_audit as audit
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.generation.lf022_codex_audit import LF022CodexAuditInput
from leanfaith.generation.weak_supervision import (
    JudgeResponse,
    PublicLeanJudgePair,
    make_swapped_presentations,
)
from leanfaith.schemas.ids import make_id


def _audit_input(proposer: str, index: int) -> LF022CodexAuditInput:
    pair = PublicLeanJudgePair(
        pair_id=make_id("pair", {"proposer": proposer, "index": index}),
        canonical_lean_a=f"theorem source_{proposer}_{index} (n : Nat) : n = n",
        canonical_lean_b=f"theorem candidate_{proposer}_{index} (n : Nat) : n + 0 = n",
        source_record_ids=(
            "thm:" + f"{index + 1:064x}",
            "var:" + f"{10_000 + index:064x}",
        ),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    presentation = next(
        item
        for item in make_swapped_presentations(
            source=pair,
            judge_slot="judge_A",
            randomization_key=(proposer.encode("utf-8") + b"x" * 32)[:32],
        )
        if item.orientation == "AB"
    )
    values: dict[str, object] = {
        "lean_check_id": "lf022_lean_check:" + f"{20_000 + index:064x}",
        "variant_id": "var:" + f"{10_000 + index:064x}",
        "pair": pair,
        "presentation": presentation,
        "source_task_sha256": f"{30_000 + index:064x}",
        "source_variant_artifact_sha256": f"{40_000 + index:064x}",
        "source_variant_line_sha256": f"{50_000 + index:064x}",
    }
    item_id = audit._audit_item_id_values(
        lean_check_id=str(values["lean_check_id"]),
        variant_id=str(values["variant_id"]),
        pair=pair,
        presentation=presentation,
        source_task_sha256=str(values["source_task_sha256"]),
        source_variant_artifact_sha256=str(values["source_variant_artifact_sha256"]),
        source_variant_line_sha256=str(values["source_variant_line_sha256"]),
    )
    return LF022CodexAuditInput.model_validate(
        {
            **values,
            "audit_item_id": item_id,
            "audit_only": True,
            "semantic_labels_created": False,
            "silver_records_created": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
    )


def _response(
    answer: str = "same_claim",
    *,
    confidence: float = 0.9,
    relation: str | None = None,
) -> JudgeResponse:
    if relation is None and answer != "uncertain":
        relation = {
            "same_claim": "equivalent",
            "not_same_claim": "A_stronger",
            "ambiguous": "ambiguous",
        }[answer]
    if answer == "not_same_claim":
        a_implies_b, b_implies_a = "yes", "no"
    elif answer == "same_claim":
        a_implies_b = b_implies_a = "yes"
    else:
        a_implies_b = b_implies_a = "unknown"
    return JudgeResponse.model_validate(
        {
            "same_claim_answer": answer,
            "relation": relation,
            "A_implies_B": a_implies_b,
            "B_implies_A": b_implies_a,
            "error_types": [],
            "confidence": confidence,
            "rationale": "Synthetic offline judgment.",
            "needs_expert_review": answer in {"ambiguous", "uncertain"},
        }
    )


def _response_raw(response: JudgeResponse | None = None) -> str:
    value = response or _response()
    return canonical_json_bytes(value.model_dump(mode="json", by_alias=True)).decode("utf-8")


def _small_plan() -> Any:
    return recovered.build_pilot_plan(
        qwen_inputs=(_audit_input("qwen", 0),),
        kimi_inputs=(_audit_input("kimi", 1),),
        per_proposer=1,
    )


def test_pilot_is_deterministic_balanced_and_binds_the_full_plan() -> None:
    qwen = tuple(_audit_input("qwen", index) for index in range(53))
    kimi = tuple(_audit_input("kimi", 1_000 + index) for index in range(52))

    first = recovered.build_pilot_plan(
        qwen_inputs=tuple(reversed(qwen)),
        kimi_inputs=kimi,
        per_proposer=50,
    )
    replay = recovered.build_pilot_plan(
        qwen_inputs=qwen,
        kimi_inputs=tuple(reversed(kimi)),
        per_proposer=50,
    )

    assert len(first.rows) == 100
    assert all(isinstance(row, recovered.RecoveredPlanRow) for row in first.rows)
    assert Counter(row.proposer for row in first.rows) == {"qwen": 50, "kimi": 50}
    assert [row.plan_row_id for row in first.rows] == [row.plan_row_id for row in replay.rows]
    assert first.total_pair_count == 105
    assert set(first.ordered_all_audit_item_ids) == {item.audit_item_id for item in (*qwen, *kimi)}
    assert first.ordered_all_audit_item_ids_sha256 == hash_canonical(
        list(first.ordered_all_audit_item_ids)
    )
    assert first.ordered_pilot_plan_row_ids_sha256 == hash_canonical(
        [row.plan_row_id for row in first.rows]
    )


def test_codex_executor_uses_medium_positional_prompt_and_closed_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[list[str], dict[str, Any]]] = []
    communicate_timeouts: list[int | None] = []

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            communicate_timeouts.append(timeout)
            return _response_raw(), ""

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        invocations.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(recovered.subprocess, "Popen", fake_popen)
    recovered.CodexJudgeExecutor().execute(
        prompt="BLINDED JUDGE PROMPT",
        cwd=tmp_path,
        timeout_seconds=17,
    )

    assert len(invocations) == 1
    command, kwargs = invocations[0]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="medium"' in command
    assert command[command.index("--disable") : command.index("--disable") + 2] == [
        "--disable",
        "shell_tool",
    ]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'web_search="disabled"' in command
    assert command[-1] == "BLINDED JUDGE PROMPT"
    assert command[-1] != "-"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["cwd"] == tmp_path
    assert communicate_timeouts == [17]


@pytest.mark.parametrize(
    ("primary", "expected_escalation"),
    [
        (None, True),
        (_response("ambiguous"), True),
        (_response("uncertain"), True),
        (_response(confidence=0.749), True),
        (_response(confidence=0.75), False),
        (_response("not_same_claim", confidence=0.99), False),
    ],
)
def test_reverse_orientation_is_requested_only_for_the_frozen_conditions(
    primary: JudgeResponse | None,
    expected_escalation: bool,
) -> None:
    resolution = recovered.resolve_primary_and_optional_ba(primary, None)

    assert resolution.escalated is expected_escalation
    if expected_escalation:
        assert resolution.final_label is None
        assert resolution.status == "needs_reverse"
    else:
        assert resolution.status == "resolved_primary"
        assert resolution.final_label is (
            primary is not None and primary.same_claim_answer == "same_claim"
        )


def test_reverse_judgment_is_remapped_and_conflicts_fail_closed() -> None:
    reverse_only = recovered.resolve_primary_and_optional_ba(
        None,
        _response("not_same_claim", relation="A_stronger"),
    )

    assert reverse_only.escalated is True
    assert reverse_only.final_label is False
    assert reverse_only.status == "resolved_reverse"
    assert reverse_only.reverse is not None
    assert reverse_only.reverse.relation == "B_stronger"
    assert reverse_only.reverse.a_implies_b == "no"
    assert reverse_only.reverse.b_implies_a == "yes"

    agreement = recovered.resolve_primary_and_optional_ba(
        _response(confidence=0.7),
        _response(),
    )
    assert agreement.final_label is True
    assert agreement.status == "resolved_agreement"

    conflict = recovered.resolve_primary_and_optional_ba(
        _response(confidence=0.7),
        _response("not_same_claim"),
    )
    assert conflict.escalated is True
    assert conflict.final_label is None
    assert conflict.status == "conflict"


class _FakeExecutor:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _SimulatedCrash(BaseException):
    pass


class _CrashAfterJournalExecutor:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.calls = 0

    def execute(self, **_kwargs: Any) -> str:
        self.calls += 1
        assert len(list(self.output_root.rglob("request.json"))) == 1
        assert not list(self.output_root.rglob("terminal.json"))
        raise _SimulatedCrash


def test_incomplete_precall_journal_refuses_implicit_duplicate(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "crashed-run"
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=output_root,
        enforce_storage_root=False,
    )
    plan = _small_plan()
    crashing = _CrashAfterJournalExecutor(output_root)

    with pytest.raises(_SimulatedCrash):
        recovered.run_recovered_judge(config=config, plan=plan, executor=crashing)

    duplicate = _FakeExecutor([_response_raw(), _response_raw()])
    with pytest.raises(RuntimeError, match=r"incomplete.*explicit|explicit.*retry"):
        recovered.run_recovered_judge(config=config, plan=plan, executor=duplicate)

    assert crashing.calls == 1
    assert duplicate.calls == []


def test_keyboard_interrupt_stops_before_the_next_paid_dispatch(tmp_path: Path) -> None:
    class InterruptingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **_kwargs: Any) -> str:
            self.calls += 1
            raise KeyboardInterrupt

    executor = InterruptingExecutor()
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=tmp_path / "interrupted-run",
        enforce_storage_root=False,
    )

    with pytest.raises(KeyboardInterrupt):
        recovered.run_recovered_judge(
            config=config,
            plan=_small_plan(),
            executor=executor,
        )

    assert executor.calls == 1
    assert len(list(config.output_root.rglob("request.json"))) == 1


def test_resume_reuses_completed_terminals_without_provider_calls(tmp_path: Path) -> None:
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=tmp_path / "completed-run",
        enforce_storage_root=False,
    )
    plan = _small_plan()
    first_executor = _FakeExecutor([_response_raw(), _response_raw()])

    first = recovered.run_recovered_judge(
        config=config,
        plan=plan,
        executor=first_executor,
    )
    replay_executor = _FakeExecutor([])
    replay = recovered.run_recovered_judge(
        config=config,
        plan=plan,
        executor=replay_executor,
    )

    assert len(first_executor.calls) == 2
    assert first.manifest.invoked_count == 2
    assert first.manifest.completed_count == 2
    assert replay.manifest.invoked_count == 0
    assert replay.manifest.reused_count == 2
    assert replay_executor.calls == []


def test_second_parse_failure_finishes_as_unresolved(tmp_path: Path) -> None:
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=tmp_path / "double-parse-failure",
        count=1,
        enforce_storage_root=False,
    )
    executor = _FakeExecutor(["not-json", "still-not-json"])

    result = recovered.run_recovered_judge(
        config=config,
        plan=_small_plan(),
        executor=executor,
    )

    assert len(executor.calls) == 2
    assert result.manifest.completed_count == 1
    assert result.judgments[0].status == "unresolved_reverse"
    assert result.judgments[0].final_label is None


def test_resume_replays_and_rejects_tampered_provider_bytes(tmp_path: Path) -> None:
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=tmp_path / "tamper-replay",
        count=1,
        enforce_storage_root=False,
    )
    recovered.run_recovered_judge(
        config=config,
        plan=_small_plan(),
        executor=_FakeExecutor([_response_raw()]),
    )
    final_path = next(config.output_root.rglob("final_message.json"))
    final_path.write_text(_response_raw(_response("not_same_claim")), encoding="utf-8")

    with pytest.raises(RuntimeError, match="final message hash mismatch"):
        recovered.run_recovered_judge(
            config=config,
            plan=_small_plan(),
            executor=_FakeExecutor([]),
        )


def test_production_slice_cannot_cross_an_unfinished_pilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = recovered.build_pilot_plan(
        qwen_inputs=(_audit_input("qwen", 1), _audit_input("qwen", 2)),
        kimi_inputs=(_audit_input("kimi", 3),),
        per_proposer=1,
    )
    monkeypatch.setattr(recovered, "PRODUCTION_PAIR_COUNT", 3)
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=tmp_path / "pilot-barrier",
        expected_total=3,
        count=3,
        enforce_storage_root=False,
    )
    executor = _FakeExecutor([_response_raw(), _response_raw(), _response_raw()])

    with pytest.raises(RuntimeError, match="pilot must complete"):
        recovered.run_recovered_judge(config=config, plan=plan, executor=executor)

    assert executor.calls == []


def test_final_attempt_ledger_binds_failed_retry_history(tmp_path: Path) -> None:
    output_root = tmp_path / "retry-ledger"
    base_values = {
        "repo_root": tmp_path,
        "output_root": output_root,
        "count": 1,
        "enforce_storage_root": False,
    }
    plan = _small_plan()
    first = recovered.RecoveredJudgeConfig.model_validate(base_values)
    failed = _FakeExecutor(
        [recovered.JudgeProcessCapture("completed", 1, b"event\n", b"failed\n", None)]
    )
    recovered.run_recovered_judge(config=first, plan=plan, executor=failed)

    retry = recovered.RecoveredJudgeConfig.model_validate(
        {**base_values, "retry_incomplete_attempts": True}
    )
    recovered.run_recovered_judge(
        config=retry,
        plan=plan,
        executor=_FakeExecutor([_response_raw()]),
    )
    final = recovered.finalize_recovered_judge(
        config=first,
        plan=plan,
        require_complete=False,
    )

    assert final["counts"]["provider_request_journals"] == 2
    assert final["attempt_status_counts"] == {"completed": 1, "process_failed": 1}
    ledger_path = Path(final["outputs"]["attempt_ledger"]["path"])
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == 2


def test_requestless_crash_directory_can_retry_and_finalize(tmp_path: Path) -> None:
    output_root = tmp_path / "requestless-retry"
    plan = _small_plan()
    row = plan.execution_rows[0]
    empty_attempt = recovered._item_dir(output_root, row) / "primary_ab" / "attempts" / "0000"
    empty_attempt.mkdir(parents=True)
    config = recovered.RecoveredJudgeConfig(
        repo_root=tmp_path,
        output_root=output_root,
        count=1,
        retry_incomplete_attempts=True,
        enforce_storage_root=False,
    )

    recovered.run_recovered_judge(
        config=config,
        plan=plan,
        executor=_FakeExecutor([_response_raw()]),
    )
    final = recovered.finalize_recovered_judge(
        config=config,
        plan=plan,
        require_complete=False,
    )

    assert final["attempt_status_counts"] == {
        "aborted_before_journal": 1,
        "completed": 1,
    }
    assert final["counts"]["ambiguous_paid_calls"] == 0


@dataclass(frozen=True, slots=True)
class _AuditFixture:
    record_id: str
    proposer: str
    final_label: bool
    escalated: bool


def test_audit_sample_is_deterministic_and_stratified() -> None:
    records = [
        _AuditFixture(
            record_id=f"{proposer}-{int(label)}-{int(escalated)}-{index}",
            proposer=proposer,
            final_label=label,
            escalated=escalated,
        )
        for proposer in ("qwen", "kimi")
        for label in (False, True)
        for escalated in (False, True)
        for index in range(4)
    ]

    first = recovered.deterministic_audit_sample(records, sample_size=16, seed=20260828)
    replay = recovered.deterministic_audit_sample(
        list(reversed(records)),
        sample_size=16,
        seed=20260828,
    )

    assert [item.record_id for item in first] == [item.record_id for item in replay]
    assert len(first) == 16
    assert Counter((item.proposer, item.final_label, item.escalated) for item in first) == {
        (proposer, label, escalated): 2
        for proposer in ("qwen", "kimi")
        for label in (False, True)
        for escalated in (False, True)
    }

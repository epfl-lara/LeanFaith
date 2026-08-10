from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import leanfaith.generation.lf022_codex_audit as audit
import leanfaith.generation.weak_supervision as weak_supervision
from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    ProcessCapture,
    audit_lean_valid_lf022_pairs,
)
from leanfaith.generation.weak_supervision import (
    JudgeResponse,
    PublicLeanJudgePair,
    make_swapped_presentations,
)
from leanfaith.schemas.ids import make_id


def _input() -> LF022CodexAuditInput:
    lean_check_id = "lf022_lean_check:" + "3" * 64
    variant_id = "var:" + "2" * 64
    source_task_sha256 = "4" * 64
    source_variant_artifact_sha256 = "5" * 64
    source_variant_line_sha256 = "6" * 64
    pair = PublicLeanJudgePair(
        pair_id=make_id("pair", {"test": "lf022-codex-audit"}),
        canonical_lean_a="theorem source (n : Nat) : n = n",
        canonical_lean_b="theorem candidate (n : Nat) : n + 0 = n",
        source_record_ids=("thm:" + "1" * 64, "var:" + "2" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    presentations = make_swapped_presentations(
        source=pair,
        judge_slot="judge_A",
        randomization_key=b"a" * 32,
    )
    presentation = next(item for item in presentations if item.orientation == "AB")
    values = {
        "schema_version": 1,
        "audit_only": True,
        "lean_check_id": lean_check_id,
        "variant_id": variant_id,
        "pair": pair,
        "presentation": presentation,
        "source_task_sha256": source_task_sha256,
        "source_variant_artifact_sha256": source_variant_artifact_sha256,
        "source_variant_line_sha256": source_variant_line_sha256,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    item_id = audit._audit_item_id_values(
        lean_check_id=lean_check_id,
        variant_id=variant_id,
        pair=pair,
        presentation=presentation,
        source_task_sha256=source_task_sha256,
        source_variant_artifact_sha256=source_variant_artifact_sha256,
        source_variant_line_sha256=source_variant_line_sha256,
    )
    return LF022CodexAuditInput.model_validate({**values, "audit_item_id": item_id})


def _response() -> bytes:
    return canonical_json_bytes(
        {
            "same_claim_answer": "same_claim",
            "relation": "equivalent",
            "A_implies_B": "yes",
            "B_implies_A": "yes",
            "error_types": [],
            "confidence": 0.9,
            "rationale": "The candidate changes only a normalized arithmetic expression.",
            "needs_expert_review": False,
        }
    )


class FakeExecutor:
    def __init__(self, captures: Sequence[ProcessCapture]) -> None:
        self.captures = list(captures)
        self.calls: list[tuple[tuple[str, ...], bytes, Path]] = []

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ProcessCapture:
        del final_message_path, timeout_seconds, termination_grace_seconds
        self.calls.append((tuple(argv), prompt, cwd))
        return self.captures.pop(0)


def test_audit_is_raw_first_exact_codex_command_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _input()
    checks_path = tmp_path / "checks" / "checks.jsonl"
    checks_path.parent.mkdir()
    checks_path.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "audit"
    monkeypatch.setattr(audit, "load_lean_valid_audit_inputs", lambda **_kwargs: (item,))
    original_parse = weak_supervision.parse_blinded_judge_output

    def assert_raw_first(raw_output: str) -> JudgeResponse:
        item_dir = audit._item_dir(output_root, item.audit_item_id)
        attempt = item_dir / "attempts" / "0000"
        assert (attempt / "stdout.jsonl").read_bytes() == b'{"type":"turn.completed"}\n'
        assert (attempt / "stderr.txt").read_bytes() == b""
        assert (attempt / "final_message.json").read_bytes() == _response()
        assert not (attempt / "parsed_response.json").exists()
        return original_parse(raw_output)

    monkeypatch.setattr(audit, "parse_blinded_judge_output", assert_raw_first)
    executor = FakeExecutor(
        [
            ProcessCapture(
                status="completed",
                exit_code=0,
                stdout=b'{"type":"turn.completed"}\n',
                stderr=b"",
                final_message=_response(),
            )
        ]
    )
    result = audit_lean_valid_lf022_pairs(
        repo_root=tmp_path,
        checks_path=checks_path,
        output_root=output_root,
        executor=executor,
    )
    assert result.manifest.completed_count == 1
    assert result.manifest.method_version == "lf022_codex_audit_v2"
    assert result.manifest.invoked_count == 1
    assert result.manifest.audit_only is True
    assert result.manifest.semantic_labels_created is False
    assert result.manifest.training_eligible is False
    schema_path = next((output_root / "schemas").glob("judge_response.*.schema.json"))
    schema = json.loads(schema_path.read_text())
    assert set(schema["required"]) == set(schema["properties"])
    argv = executor.calls[0][0]
    assert argv[:4] == ("codex", "exec", "--ephemeral", "--ignore-user-config")
    assert "--ignore-rules" in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in argv
    assert "web_search=disabled" in argv
    assert "shell_environment_policy.inherit=none" in argv
    assert argv[-1] == "-"
    assert b"source (n : Nat)" in executor.calls[0][1]
    assert b"F1 \xe2\x80\x94 claim faithfulness" in executor.calls[0][1]
    assert b"F2 \xe2\x80\x94 truth-level implication" in executor.calls[0][1]
    assert b"lf022_codex_audit_item" not in executor.calls[0][1]

    replay_executor = FakeExecutor([])
    replay = audit_lean_valid_lf022_pairs(
        repo_root=tmp_path,
        checks_path=checks_path,
        output_root=output_root,
        executor=replay_executor,
    )
    assert replay.manifest.invoked_count == 0
    assert replay.manifest.reused_count == 1
    assert not replay_executor.calls


def test_v2_audit_identity_does_not_reuse_v1_prompt_results() -> None:
    item = _input()
    v1_id = make_id(
        "lf022_codex_audit_item",
        {
            "schema": "lf022_codex_audit_v1",
            "lean_check_id": item.lean_check_id,
            "variant_id": item.variant_id,
            "pair_id": item.pair.pair_id,
            "pair_admission_sha256": item.pair.admission_sha256,
            "presentation_task_id": item.presentation.task_id,
            "source_task_sha256": item.source_task_sha256,
            "source_variant_artifact_sha256": item.source_variant_artifact_sha256,
            "source_variant_line_sha256": item.source_variant_line_sha256,
        },
    )

    assert item.audit_item_id != v1_id
    with pytest.raises(ValueError, match="audit_item_id"):
        LF022CodexAuditInput.model_validate(
            {**item.model_dump(mode="json"), "audit_item_id": v1_id}
        )


def test_failed_parse_is_not_completed_and_next_run_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _input()
    checks_path = tmp_path / "checks" / "checks.jsonl"
    checks_path.parent.mkdir()
    checks_path.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "audit"
    monkeypatch.setattr(audit, "load_lean_valid_audit_inputs", lambda **_kwargs: (item,))
    first = FakeExecutor([ProcessCapture("completed", 0, b"event\n", b"", b"not json")])
    result = audit_lean_valid_lf022_pairs(
        repo_root=tmp_path,
        checks_path=checks_path,
        output_root=output_root,
        executor=first,
    )
    assert result.terminals[0].status == "parse_failed"
    item_dir = audit._item_dir(output_root, item.audit_item_id)
    assert not (item_dir / "completed.json").exists()

    second = FakeExecutor([ProcessCapture("completed", 0, b"event\n", b"", _response())])
    retry = audit_lean_valid_lf022_pairs(
        repo_root=tmp_path,
        checks_path=checks_path,
        output_root=output_root,
        executor=second,
    )
    assert retry.terminals[0].status == "completed"
    assert (item_dir / "attempts" / "0001" / "terminal.json").is_file()


def test_audit_models_reject_private_flags_and_cli_is_exposed() -> None:
    payload = _input().model_dump(mode="json")
    payload["pair"]["private_source_content"] = True
    try:
        LF022CodexAuditInput.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("private audit input was accepted")
    result = CliRunner().invoke(app, ["audit-lf022-codex", "--help"])
    assert result.exit_code == 0
    assert "gpt-5.6-sol" in result.stdout
    assert "audit only" in result.stdout
    summary_help = CliRunner().invoke(app, ["summarize-lf022-codex-audit", "--help"])
    assert summary_help.exit_code == 0
    assert "without creating labels" in summary_help.stdout


def test_summary_bucket_reconciles_verdicts_and_multilabel_errors() -> None:
    first = JudgeResponse.model_validate_json(_response())
    second = JudgeResponse.model_validate(
        {
            "same_claim_answer": "not_same_claim",
            "relation": "A_stronger",
            "A_implies_B": "yes",
            "B_implies_A": "no",
            "error_types": ["E01", "E03"],
            "confidence": 0.8,
            "rationale": "The candidate drops a necessary premise.",
            "needs_expert_review": False,
        }
    )
    judgments = [
        audit._VerifiedAuditJudgment("item-a", "kimi", first, "1" * 64, "2" * 64),
        audit._VerifiedAuditJudgment("item-b", "qwen", second, "3" * 64, "4" * 64),
    ]

    bucket = audit._make_summary_bucket(judgments)

    assert bucket.total_count == 2
    assert bucket.same_claim_counts == {"not_same_claim": 1, "same_claim": 1}
    assert bucket.relation_counts == {"A_stronger": 1, "equivalent": 1}
    assert bucket.error_type_counts == {"E01": 1, "E03": 1}
    assert bucket.confidence_mean == pytest.approx(0.85)


def test_completed_summary_replays_hashes_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _input()
    checks_path = tmp_path / "checks" / "checks.jsonl"
    checks_path.parent.mkdir()
    checks_path.write_text("{}\n", encoding="utf-8")
    audit_root = tmp_path / "audit"
    monkeypatch.setattr(audit, "load_lean_valid_audit_inputs", lambda **_kwargs: (item,))
    executor = FakeExecutor(
        [ProcessCapture("completed", 0, b'{"type":"turn.completed"}\n', b"", _response())]
    )
    audit.audit_lean_valid_lf022_pairs(
        repo_root=tmp_path,
        checks_path=checks_path,
        output_root=audit_root,
        executor=executor,
    )
    check = SimpleNamespace(
        check_id=item.lean_check_id,
        outcome="elaborates_with_placeholder",
    )
    monkeypatch.setattr(audit, "_load_check_inventory", lambda _path: (check,))
    monkeypatch.setattr(audit, "_proposer_family_for_check", lambda *_args, **_kwargs: "qwen3")
    json_path = tmp_path / "reports" / "summary.json"
    markdown_path = tmp_path / "reports" / "summary.md"

    result = audit.summarize_completed_lf022_codex_audit(
        repo_root=tmp_path,
        checks_path=checks_path,
        audit_root=audit_root,
        output_json_path=json_path,
        output_markdown_path=markdown_path,
    )

    assert result.summary.completed_judgment_count == 1
    assert result.summary.by_proposer_family["qwen3"].total_count == 1
    assert result.summary.human_labels_created is False
    assert result.summary.training_eligible is False
    assert "not human gold" in markdown_path.read_text(encoding="utf-8")
    assert audit.LF022CodexAuditSummary.model_validate_json(json_path.read_bytes()) == (
        result.summary
    )

    item_dir = audit._item_dir(audit_root, item.audit_item_id)
    parsed_path = item_dir / "attempts" / "0000" / "parsed_response.json"
    parsed_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(audit.LF022CodexAuditError, match="parsed response hash mismatch"):
        audit.summarize_completed_lf022_codex_audit(
            repo_root=tmp_path,
            checks_path=checks_path,
            audit_root=audit_root,
            output_json_path=json_path,
            output_markdown_path=markdown_path,
        )

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.lean_oracle import SignatureOracleResult
from leanfaith.sft2a.pipeline import run_lemex_audit, run_one_root, verify_one_root_replay
from leanfaith.sft2a.providers import ProviderCallResult


def _proposer_output(polarity: str, signature: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "requested_polarity": polarity,
        "mechanism": "other",
        "candidate_signature": signature,
        "change_summary": "A bounded synthetic unit-test transformation.",
        "judge_trap": "Check only the two propositions.",
        "informative": True,
        "proof_free": True,
    }


def _judge_output(verdict: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": "Independent blinded unit-test judgment.",
    }


class ScriptedProvider:
    def __init__(
        self,
        tmp_path: Path,
        provider_id: str,
        responses: Sequence[dict[str, object]],
    ) -> None:
        self.tmp_path = tmp_path
        self.provider_id = provider_id
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt
        index = len(self.calls)
        self.calls.append(tuple(input_ids))
        structured = self.responses[index]
        return ProviderCallResult(
            call_key=f"{self.provider_id}:{index}",
            provider_id=self.provider_id,
            structured=structured,
            usage={"input_tokens": 1, "output_tokens": 1},
            cost_usd=0.01,
            elapsed_seconds=0.1,
            cache_hit=False,
            terminal_path=self.tmp_path / f"{self.provider_id}-{index}.json",
        )


class FakeOracle:
    def __init__(self, loaded: LoadedSFT2AConfig) -> None:
        self.loaded = loaded
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: Literal["reference", "candidate"],
    ) -> SignatureOracleResult:
        self.calls.append((signature, endpoint_role))
        goal = (
            self.loaded.config.root.expected_reference_goal_v1
            if endpoint_role == "reference"
            else f"⊢ {signature}"
        )
        digest = sha256_hex(signature.encode("utf-8"))
        return SignatureOracleResult(
            status="valid",
            cache_key=f"lean:{digest}",
            cache_hit=False,
            signature_sha256=digest,
            goal_v1=goal,
            sidecar={"record": {"goal_v1": goal}},
            lean_status="valid",
            request_hash=f"request:{digest}",
            elapsed_ms=100,
            raw_response_path=None,
            detail="fake proof-free elaboration",
        )

    def close(self) -> None:
        self.closed = True


def _temporary_loaded(tmp_path: Path) -> LoadedSFT2AConfig:
    loaded = load_sft2a_config()
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path)})
    return replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


def test_one_root_retries_only_rejected_slot_and_preserves_siblings(tmp_path: Path) -> None:
    loaded = _temporary_loaded(tmp_path)
    proposer = ScriptedProvider(
        tmp_path,
        loaded.config.proposer.provider_id,
        [
            _proposer_output("preserving", "1 = 1"),
            _proposer_output("preserving", "2 = 2"),
            _proposer_output("preserving", "3 = 3"),
            _proposer_output("breaking", "4 = 5"),
            _proposer_output("breaking", "5 = 6"),
        ],
    )
    judge = ScriptedProvider(
        tmp_path,
        loaded.config.claude_judge.provider_id,
        [
            _judge_output("non_equivalent"),
            _judge_output("equivalent"),
            _judge_output("equivalent"),
            _judge_output("non_equivalent"),
            _judge_output("non_equivalent"),
        ],
    )
    oracle = FakeOracle(loaded)

    result = run_one_root(loaded, proposer=proposer, claude_judge=judge, oracle=oracle)

    assert result.manifest["counts"] == {
        "accepted": 4,
        "accepted_positive": 2,
        "accepted_negative": 2,
        "invalid_attempts": 0,
        "unknown_rows": 0,
        "judge_disagreements": 1,
        "gold_contamination": 0,
        "cross_root_duplicates": 0,
        "retry_slots": 1,
        "attempts": 5,
    }
    assert [call[1:3] for call in proposer.calls] == [
        ("preserve_0", "attempt:1"),
        ("preserve_0", "attempt:2"),
        ("preserve_1", "attempt:1"),
        ("break_0", "attempt:1"),
        ("break_1", "attempt:1"),
    ]
    assert len(oracle.calls) == 6
    assert len({signature for signature, role in oracle.calls if role == "candidate"}) == 5
    assert len(judge.calls) == 5
    assert not oracle.closed

    receipt = verify_one_root_replay(loaded)
    assert receipt["reproducible"] is True
    assert receipt["provider_calls_executed"] == 0
    assert receipt["lean_requests_executed"] == 0
    assert verify_one_root_replay(loaded) == receipt
    replay = run_one_root(loaded, proposer=proposer, claude_judge=judge, oracle=oracle)
    assert replay.replayed is True
    assert len(proposer.calls) == 5
    assert len(judge.calls) == 5
    assert len(oracle.calls) == 6

    auditor = ScriptedProvider(
        tmp_path,
        loaded.config.lemex_auditor.provider_id,
        [_judge_output("equivalent"), _judge_output("non_equivalent")],
    )
    audit = run_lemex_audit(loaded, auditor=auditor)
    assert audit.manifest["target_fraction"] == 0.1
    assert audit.manifest["population_rows"] == 4
    assert audit.manifest["selected_rows"] == 2
    assert audit.manifest["disagreements"] == 0
    assert (
        audit.manifest["source_run_manifest_sha256"] == audit.manifest["one_root_manifest_sha256"]
    )
    assert audit.manifest["providers"]["opus_source_judge"] == (  # type: ignore[index]
        loaded.config.claude_judge.model_dump(mode="json")
    )
    assert audit.manifest["providers"]["lemex_auditor"] == (  # type: ignore[index]
        loaded.config.lemex_auditor.model_dump(mode="json")
    )
    assert audit.manifest["prompt"]["artifact"] == (  # type: ignore[index]
        loaded.config.prompts.blinded_claude_judge.model_dump(mode="json")
    )
    assert audit.manifest["llm"]["calls"] == 2  # type: ignore[index]
    assert (audit.output_root / "unknown_review/rows.jsonl").read_bytes() == b""

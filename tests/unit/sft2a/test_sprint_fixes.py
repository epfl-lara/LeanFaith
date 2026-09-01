from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.generation.claude_fable_judge_v1 import ClaudeCliCapture
from leanfaith.sft2a import providers
from leanfaith.sft2a.config import load_sft2a_config
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.models import JudgeOutput, JudgeOutputV5
from leanfaith.sft2a.providers import ProviderCallResult, claude_judge_provider


def _temporary_loaded(tmp_path: Path):
    loaded = load_sft2a_config()
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path)})
    return replace(
        loaded, config=config, config_hash=hash_canonical(config.model_dump(mode="json"))
    )


class _SchemaInvalidClaude:
    """A successful Claude wrapper whose structured_output fails the frozen schema."""

    calls = 0

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ClaudeCliCapture:
        del argv, prompt, cwd, env, timeout_seconds, termination_grace_seconds
        type(self).calls += 1
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {"schema_version": 1},  # missing verdict/confidence/...
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.01,
            "duration_ms": 100,
        }
        return ClaudeCliCapture(
            status="completed",
            exit_code=0,
            stdout=canonical_json_bytes(wrapper),
            stderr=b"",
        )


def test_schema_invalid_provider_output_is_persisted_as_terminal_and_replay_cache_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _temporary_loaded(tmp_path)
    _SchemaInvalidClaude.calls = 0
    monkeypatch.setattr(providers, "verify_provider_binary", lambda _pin: None)
    monkeypatch.setattr(providers, "SubprocessClaudeCliExecutor", _SchemaInvalidClaude)
    provider = claude_judge_provider(loaded)
    input_ids = ("root", "candidate", "blinded_claude_judge")

    result = provider.call(prompt="judge prompt", input_ids=input_ids)
    assert result.cache_hit is False
    assert result.structured == {"schema_version": 1}
    terminal = json.loads(result.terminal_path.read_text())
    assert terminal["schema_invalid"] is True
    assert "schema_invalid_detail" in terminal
    assert not (result.terminal_path.parent / "attempts/001/failure.json").is_file()

    replay = provider.call(prompt="judge prompt", input_ids=input_ids)
    assert replay.cache_hit is True
    assert replay.structured == {"schema_version": 1}
    assert _SchemaInvalidClaude.calls == 1


class _CachedJudgeProvider:
    """Wraps a pre-built ProviderCallResult to exercise the judge retry layer."""

    def __init__(self, result: ProviderCallResult) -> None:
        self._result = result
        self.calls = 0

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt, input_ids
        self.calls += 1
        return self._result


def test_call_consistent_judge_routes_schema_invalid_to_none_without_raising(
    tmp_path: Path,
) -> None:
    raw = ProviderCallResult(
        call_key="schema-invalid-judge",
        provider_id="claude",
        structured={"schema_version": 1},
        usage={"input_tokens": 1, "output_tokens": 1},
        cost_usd=0.01,
        elapsed_seconds=0.1,
        cache_hit=False,
        terminal_path=tmp_path / "terminal.json",
    )
    provider = _CachedJudgeProvider(raw)
    outcome = call_consistent_judge(
        provider,
        prompt="judge prompt",
        input_ids=("root", "cand", "blinded_claude_judge_v5"),
        closure_aware=True,
        malformed_retries=1,
    )
    assert outcome.judgment is None
    assert len(outcome.calls) == 2
    assert outcome.malformed_attempts[0]["reason"].startswith("schema:")


def _judge_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "equivalent",
        "confidence": "high",
        "relation_class": "logical_restatement",
        "error_type": "none",
        "rationale": "They match.",
    }
    payload.update(overrides)
    return payload


def test_binary_verdict_with_low_confidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="low confidence"):
        JudgeOutput.model_validate(_judge_payload(confidence="low"))
    with pytest.raises(ValidationError, match="binary verdicts require error_type=none"):
        JudgeOutput.model_validate(_judge_payload(error_type="ambiguous"))


def test_unknown_verdict_requires_error_type() -> None:
    with pytest.raises(ValidationError, match="unknown requires an error_type"):
        JudgeOutput.model_validate(_judge_payload(verdict="unknown"))
    valid = JudgeOutput.model_validate(
        _judge_payload(verdict="unknown", confidence="low", error_type="insufficient_confidence")
    )
    assert valid.verdict == "unknown"


def test_v5_judge_inherits_strict_unknown_contract() -> None:
    payload = _judge_payload(
        schema_version=5,
        confidence="low",
        closure_checks={
            "entire_universally_closed_proposition": True,
            "argument_swapping": "supports_equivalence",
            "symmetry": "supports_equivalence",
            "antisymmetry": "not_applicable",
            "extensionality": "not_applicable",
            "recoverable_boundary_cases": "checked_no_effect",
        },
    )
    with pytest.raises(ValidationError, match="low confidence"):
        JudgeOutputV5.model_validate(payload)

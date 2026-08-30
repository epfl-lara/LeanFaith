from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.generation.claude_fable_judge_v1 import ClaudeCliCapture
from leanfaith.sft2a import providers
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.providers import StructuredProviderError, claude_judge_provider


def _temporary_loaded(tmp_path: Path) -> LoadedSFT2AConfig:
    loaded = load_sft2a_config()
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path)})
    return replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


class FailingThenSuccessfulClaude:
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
        if type(self).calls == 1:
            return ClaudeCliCapture(
                status="completed",
                exit_code=1,
                stdout=b"",
                stderr=b"transport schema rejected",
            )
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {
                "schema_version": 1,
                "verdict": "equivalent",
                "confidence": "high",
                "relation_class": "logical_restatement",
                "error_type": "none",
                "rationale": "The propositions are equivalent.",
            },
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


def test_provider_infrastructure_retry_is_durable_and_not_a_semantic_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _temporary_loaded(tmp_path)
    FailingThenSuccessfulClaude.calls = 0
    monkeypatch.setattr(providers, "verify_provider_binary", lambda _pin: None)
    monkeypatch.setattr(
        providers,
        "SubprocessClaudeCliExecutor",
        FailingThenSuccessfulClaude,
    )
    provider = claude_judge_provider(loaded)
    input_ids = ("root", "candidate", "blinded_claude_judge")

    with pytest.raises(StructuredProviderError, match="exit=1"):
        provider.call(prompt="judge prompt", input_ids=input_ids)
    result = provider.call(prompt="judge prompt", input_ids=input_ids)
    replay = provider.call(prompt="judge prompt", input_ids=input_ids)

    call_dir = result.terminal_path.parent
    assert json.loads((call_dir / "attempts/001/failure.json").read_text())["attempt_number"] == 1
    assert json.loads(result.terminal_path.read_text())["attempt_number"] == 2
    assert result.cache_hit is False
    assert replay.cache_hit is True
    assert result.call_key == replay.call_key
    assert FailingThenSuccessfulClaude.calls == 2

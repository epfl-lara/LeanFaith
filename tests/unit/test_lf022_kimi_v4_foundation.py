"""Focused tests for the versioned Kimi proposer-v4 foundation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.generation.lf022_execution import (
    LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256,
    LF022RCPDecodingContract,
    lf022_reviewed_proposer_prompt,
)
from leanfaith.generation.llm_variants import (
    PROPOSER_TEMPLATE_V2,
    PublicLeanVariantSource,
    VariantOutputErrorCode,
    VariantOutputParseError,
    VariantPromptRequest,
    parse_variant_proposer_output,
    render_variant_proposer_prompt,
)
from leanfaith.generation.rcp_provider import RCPResponseError, parse_chat_completion
from leanfaith.schemas.enums import IntendedRelation

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml"
MODEL = "moonshotai/Kimi-K2.7-Code"


def _request() -> VariantPromptRequest:
    source = PublicLeanVariantSource(
        source_theorem_id="thm:" + "1" * 64,
        source_representation_id="repr:" + "2" * 64,
        context_id="ctx:" + "3" * 64,
        imports=("Mathlib",),
        source_statement="theorem source_identity (n : Nat) : n = n",
        optional_natural_language=None,
        source_id="mathlib",
        source_revision="fixture-revision",
        source_license="Apache-2.0",
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    return VariantPromptRequest(
        request_id="lf022-kimi-v4-fixture",
        source=source,
        proposal_count=1,
        requested_relations=(IntendedRelation.NEAR_MISS,),
        requested_error_types=(),
        requested_sci_categories=(),
        generation_distribution="G_open",
    )


def _completion_body(*, finish_reason: str, content: str) -> bytes:
    return canonical_json_bytes(
        {
            "id": "chatcmpl-kimi-v4-fixture",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "long internal reasoning",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 32768,
                "total_tokens": 32868,
            },
        }
    )


def test_v4_config_binds_reviewed_prompt_and_exact_high_reasoning_decoding() -> None:
    config = load_yaml_mapping(CONFIG)
    prompt = config["prompt"]
    assert prompt == {
        "artifact": "prompts/proposers/lean_variant_v2.txt",
        "sha256": LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256,
        "template_id": "lean_variant",
        "template_version": "v2",
    }
    assert hash_file(ROOT / str(prompt["artifact"])) == prompt["sha256"]

    decoding = LF022RCPDecodingContract.model_validate(
        {
            "schema_version": 1,
            "contract_id": config["contract_id"],
            **config["decoding"],
        }
    )
    assert decoding.max_tokens == 32_768
    assert decoding.thinking_mode == "forced_thinking"
    assert decoding.reasoning_effort == "high"
    assert decoding.chat_template_enable_thinking is True
    assert decoding.temperature == 1.0
    assert decoding.top_p == 0.95
    assert config["prior_lineage"] == {
        "batch_id": "lf022_public_batch:ea34d07c4162eb3e5e2b35f1465b26afd997095b3dcd2d87bff9382564093a9d",
        "execution_admission_id": "lf022_execution_admission:c97dcb54c8dd425aaa6cfe2ed0f20bfd5b4b67c929dcf55bc4362729b5c90f31",
        "batch_manifest": {
            "path": "data/lf022_kimi_scientific_cfdbb46/prefix_256/batch/batch_manifest.json",
            "sha256": "6f42cf6dfd1d00217894f29e353fa11bb873699f0fba208f0f36fe688eefa1f4",
        },
        "execution_admission": {
            "path": "data/lf022_kimi_scientific_cfdbb46/prefix_256/batch/admissions/moonshot_kimi_k2.json",
            "sha256": "90cd5c8923c0c097d2bd77b09f848f573ab1e0075cd1c4f2885244dad7b10028",
        },
        "exact_offline_replay_report_id": "lf022_batch_run:9fc94ffe7c230634f961c6519bb5f70834de769afcbf5856affb4959117bf016",
        "exact_offline_replay_report": {
            "path": "data/lf022_kimi_scientific_cfdbb46/prefix_256/batch/runs/9fc94ffe7c230634f961c6519bb5f70834de769afcbf5856affb4959117bf016.json",
            "sha256": "bea16e2926ed5bf49361ef0384b7fe6a4fe8a6fb499c5ac364f333cf92f9fd9e",
        },
    }


def test_v2_prompt_is_hash_bound_and_resolves_the_proof_stripped_signature_contract() -> None:
    rendered = render_variant_proposer_prompt(_request(), template_path=PROPOSER_TEMPLATE_V2)
    assert rendered.template_version == "v2"
    assert rendered.template_sha256 == LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256
    assert "proof-stripped theorem or lemma\nsignature" in rendered.text
    assert "deliberately omits the Lean value/body" in rendered.text
    assert "top-level declaration value/body introduced by `:=`" in rendered.text
    assert "proposition itself may use valid\nLean syntax containing `:=`" in rendered.text
    assert lf022_reviewed_proposer_prompt("v2") == (
        "prompts/proposers/lean_variant_v2.txt",
        LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256,
    )


@pytest.mark.parametrize("content", ("", '{"variants":['))
def test_finish_reason_length_is_output_budget_exhausted_before_content_parse(
    content: str,
) -> None:
    with pytest.raises(RCPResponseError) as caught:
        parse_chat_completion(
            _completion_body(finish_reason="length", content=content),
            expected_model=MODEL,
        )
    assert caught.value.code == "output_budget_exhausted"
    assert caught.value.retryable is False


def test_true_empty_stop_response_remains_distinct_from_output_budget_exhaustion() -> None:
    with pytest.raises(RCPResponseError) as caught:
        parse_chat_completion(
            _completion_body(finish_reason="stop", content=""),
            expected_model=MODEL,
        )
    assert caught.value.code == "empty_response"
    assert caught.value.retryable is False


def test_stopped_v4_json_content_remains_parseable() -> None:
    content = json.dumps({"variants": []})
    completion = parse_chat_completion(
        _completion_body(finish_reason="stop", content=content),
        expected_model=MODEL,
    )
    assert completion.content == content
    assert completion.finish_reason == "stop"


def _variant_output(candidate: str) -> str:
    return json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": candidate,
                    "intended_relation": "near_miss",
                    "intended_error_types": [],
                    "edit_summary": "fixture",
                    "confidence": 0.5,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        }
    )


def test_strict_parser_allows_assignment_syntax_inside_the_proposition() -> None:
    batch = parse_variant_proposer_output(_variant_output("theorem local_let : let n := 1; n = 1"))
    assert batch.variants[0].candidate_lean.endswith("let n := 1; n = 1")


def test_strict_parser_still_rejects_a_top_level_declaration_value() -> None:
    with pytest.raises(VariantOutputParseError) as caught:
        parse_variant_proposer_output(_variant_output("theorem proof_bearing : True := True.intro"))
    assert caught.value.code is VariantOutputErrorCode.PROOF_BEARING_CANDIDATE

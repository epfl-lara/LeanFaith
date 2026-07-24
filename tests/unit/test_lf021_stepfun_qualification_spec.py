"""Frozen StepFun-Formalizer fixture-qualification inputs.

These tests do not load checkpoint weights. They bind the public-fixture
prompt, exact pinned model artifacts, sampled decoding, observed special-token
IDs, and the post-template ``<think>`` suffix without granting Gate-5G credit
or creating semantic labels.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_hf import (
    ChatTemplatePromptFormatter,
    LocalHFDecodingConfig,
    LocalHFGenerationRequest,
    LocalHFModelPin,
    PostTemplateSuffix,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG_PATH = ROOT / "configs/generation/local_qualification_stepfun_v1.yaml"
PROMPT_PATH = ROOT / "prompts/autoformalizers/stepfun_card_chat_v1.txt"
SUFFIX_PATH = ROOT / "prompts/autoformalizers/common_final_fence_v1.txt"
PARSER_PATH = ROOT / "src/leanfaith/generation/local_output_adapter_stepfun.py"
FIXTURE_PATH = ROOT / "examples/lf021_stepfun_mathlib_nat_comm_20260723_v1.json"
HEADER_PATH = ROOT / "examples/lf021_stepfun_mathlib_nat_header_v1.lean"

REVISION = "fb0dc612761fecd64ebbc489c2a3417e9ea01968"
PROMPT_SHA256 = "f33fe08c5bc09d6ad97deeac31121d687b2be4b27f0a77d0870ada3b2249e8c1"
CHAT_TEMPLATE_SHA256 = "b6835114b7303ddd78919a82e4d9f7d8c26ed0d7dfc36beeb12d524f6144eab1"
FORMATTER_SHA256 = "63f734b421b5a553a894409925e874eb33e86c55796c98ad0b936b0d251eb7c0"
THINK_SHA256 = "7d329bb7d9d43bf17bcafd4cb8203e1b94423923e87980bd1d2d9fc525d50b99"
DECODING_SHA256 = "f23566cbeffd74373b77dec71c3eb7d80dec4e340e4f7e8da056bf991628cebf"


def _config() -> dict[str, object]:
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


class _CapturingTokenizer:
    def __init__(self) -> None:
        self.messages: Sequence[Mapping[str, str]] | None = None
        self.tokenize: bool | None = None
        self.add_generation_prompt: bool | None = None

    def apply_chat_template(
        self,
        conversation: Sequence[Mapping[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        self.messages = conversation
        self.tokenize = tokenize
        self.add_generation_prompt = add_generation_prompt
        return (
            "<｜begin▁of▁sentence｜>"
            + conversation[0]["content"]
            + "<｜User｜>"
            + conversation[1]["content"]
            + "<｜Assistant｜>"
        )


def _stepfun_formatter(prompt: dict[str, object]) -> ChatTemplatePromptFormatter:
    suffix = _mapping(prompt["post_template_suffix"])
    return ChatTemplatePromptFormatter(
        formatter_id=str(prompt["formatter_id"]),
        system_prompt=str(prompt["system_prompt"]),
        add_generation_prompt=bool(prompt["add_generation_prompt"]),
        post_template_suffix=PostTemplateSuffix(
            suffix_id=str(suffix["suffix_id"]),
            text=str(suffix["text"]),
        ),
    )


def test_stepfun_prompt_is_card_exact_system_user_chat_plus_external_think() -> None:
    template = PROMPT_PATH.read_bytes()
    suffix = SUFFIX_PATH.read_text(encoding="utf-8").strip()
    assert sha256_hex(template) == PROMPT_SHA256
    decoded = template.decode("utf-8")
    assert decoded == (
        "Please autoformalize the following problem in Lean 4 with a header. "
        "Use the following theorem names: {{THEOREM_NAME}}.\n\n"
        "{{NL_STATEMENT}}\n\n"
        "Your code should start with:\n"
        "```Lean4\n"
        "{{REGISTERED_HEADER}}\n"
        "```\n\n"
        "{{COMMON_SUFFIX}}\n"
    )
    for placeholder in (
        "{{THEOREM_NAME}}",
        "{{NL_STATEMENT}}",
        "{{REGISTERED_HEADER}}",
        "{{COMMON_SUFFIX}}",
    ):
        assert decoded.count(placeholder) == 1

    rendered = (
        decoded.replace("{{THEOREM_NAME}}", "lf021_stepfun_fixture")
        .replace(
            "{{NL_STATEMENT}}",
            "For every natural n, n + 20260723 equals 20260723 + n.",
        )
        .replace("{{REGISTERED_HEADER}}", "import Mathlib")
        .replace("{{COMMON_SUFFIX}}", suffix)
    )
    prompt = _mapping(_config()["prompt"])
    tokenizer = _CapturingTokenizer()
    formatter = _stepfun_formatter(prompt)
    formatted = formatter.format_prompt(
        rendered,
        tokenizer=tokenizer,
        pin=LocalHFModelPin(
            repo_id="stepfun-ai/StepFun-Formalizer-7B",
            revision=REVISION,
            dtype="bfloat16",
        ),
    )

    assert list(tokenizer.messages or ()) == [
        {
            "role": "system",
            "content": "You are an expert in mathematics and Lean 4.",
        },
        {"role": "user", "content": rendered},
    ]
    assert tokenizer.tokenize is False
    assert tokenizer.add_generation_prompt is True
    assert all("<think>" not in message["content"] for message in tokenizer.messages or ())
    assert formatted.endswith("<｜Assistant｜><think>")
    assert formatter.formatter_hash == FORMATTER_SHA256
    assert formatter.requires_hash_binding is True


def test_stepfun_config_binds_model_prompt_parser_decoding_and_observed_tokens() -> None:
    config = _config()
    model = _mapping(config["active_model"])
    prompt = _mapping(config["prompt"])
    observed = _mapping(config["observed_special_tokens"])
    fixture = _mapping(config["qualification_fixture"])
    decoding = LocalHFDecodingConfig.model_validate(config["decoding"])

    assert config["config_id"] == "lf021_local_qualification_stepfun_v1"
    assert config["implementation_status"] == "pending_generic_qualification_schema"
    assert config["artifact_class"] == "smoke"
    assert config["qualifies_for_gate5g"] is False
    assert config["semantic_labels_created"] is False
    assert config["external_endpoints_allowed"] is False
    assert config["private_source_content_allowed"] is False

    assert model["family_id"] == "stepfun_formalizer_7b"
    assert model["provider_slot"] == "local_stepfun_qualification"
    assert model["repo_id"] == "stepfun-ai/StepFun-Formalizer-7B"
    assert model["revision"] == REVISION
    assert model["tokenizer_revision"] == REVISION
    assert model["architecture"] == "Qwen2ForCausalLM"
    assert model["model_positions"] == 131_072
    assert model["tokenizer_positions"] == 16_384
    assert model["checkpoint_bytes"] == 15_231_271_960
    assert model["supervision_eligible"] is False
    assert model["heldout_generator"] is False

    assert prompt["formatter_id"] == "stepfun_card_think_v1"
    assert prompt["formatter_hash"] == FORMATTER_SHA256
    assert prompt["system_prompt"] == "You are an expert in mathematics and Lean 4."
    assert prompt["add_generation_prompt"] is True
    assert prompt["chat_template_sha256"] == CHAT_TEMPLATE_SHA256
    assert prompt["template_sha256"] == sha256_hex(PROMPT_PATH.read_bytes())
    assert prompt["common_suffix_sha256"] == sha256_hex(SUFFIX_PATH.read_bytes())
    assert prompt["parser_source_sha256"] == sha256_hex(PARSER_PATH.read_bytes())
    assert prompt["parser_id"] == "lean_stepfun_think_terminal_fence_v1"
    post_suffix = _mapping(prompt["post_template_suffix"])
    assert post_suffix == {
        "suffix_id": "stepfun_think_v1",
        "text": "<think>",
        "content_sha256": THINK_SHA256,
    }

    assert observed == {
        "observation_mode": "exact_local_tokenizer_and_generation_config",
        "transformers_version": "4.57.6",
        "tokenizer_class": "LlamaTokenizerFast",
        "tokenizer_bos_token_id": 151_646,
        "tokenizer_eos_token_id": 151_643,
        "tokenizer_pad_token_id": 151_643,
        "model_config_bos_token_id": 151_643,
        "model_config_eos_token_id": 151_643,
        "generation_config_bos_token_id": 151_646,
        "generation_config_eos_token_id": 151_643,
        "generation_config_pad_token_id": None,
        "rendered_prompt_first_token_id": 151_646,
        "request_eos_token_id": 151_643,
        "request_pad_token_id": 151_643,
    }
    assert decoding.model_dump(mode="json") == {
        "max_new_tokens": 16_384,
        "do_sample": True,
        "num_beams": 1,
        "num_return_sequences": 1,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": None,
        "seed": 0,
        "repetition_penalty": 1.0,
        "eos_token_id": 151_643,
        "pad_token_id": 151_643,
    }
    assert decoding.decoding_hash == DECODING_SHA256

    assert fixture["public_only"] is True
    assert fixture["private_source_content"] is False
    assert fixture["project_registry_key"] == "mathlib"
    assert fixture["fixture_sha256"] == sha256_hex(FIXTURE_PATH.read_bytes())
    assert fixture["import_header_sha256"] == sha256_hex(HEADER_PATH.read_bytes())


def test_stepfun_checkpoint_manifest_is_complete_and_size_reconciled() -> None:
    model = _mapping(_config()["active_model"])
    checkpoint = _mapping(model["checkpoint_artifacts"])
    index = _mapping(checkpoint["index"])
    shards = checkpoint["shards"]
    assert isinstance(shards, list)
    assert index == {
        "artifact": "model.safetensors.index.json",
        "bytes": 26_377,
        "sha256": "fe23314bcd200dbcb9e9e00de184fd9bb6de85eb6feca0864da03c8e11fba189",
    }
    expected = (
        (
            "model-00001-of-00002.safetensors",
            13_172_283_168,
            "22643a339123d56ef97b849e44e6e2a5741eee48e2de4f695a23f953186a65af",
        ),
        (
            "model-00002-of-00002.safetensors",
            2_058_988_792,
            "7c705c27c3da6b8f342f7aa3f6f03008fa8ff644db9eed7e23bcf93572cc60c6",
        ),
    )
    observed = tuple(
        (
            _mapping(shard)["artifact"],
            _mapping(shard)["bytes"],
            _mapping(shard)["sha256"],
        )
        for shard in shards
    )
    assert observed == expected
    assert sum(_integer(_mapping(shard)["bytes"]) for shard in shards) == model["checkpoint_bytes"]


def test_stepfun_local_request_hash_binds_formatter_suffix_and_effective_eos() -> None:
    config = _config()
    prompt = _mapping(config["prompt"])
    formatter = _stepfun_formatter(prompt)
    decoding = LocalHFDecodingConfig.model_validate(config["decoding"])
    request = LocalHFGenerationRequest(
        pin=LocalHFModelPin(
            repo_id="stepfun-ai/StepFun-Formalizer-7B",
            revision=REVISION,
            dtype="bfloat16",
        ),
        prompt="fixture prompt",
        prompt_formatter_id=formatter.formatter_id,
        prompt_formatter_hash=formatter.formatter_hash,
        decoding=decoding,
        input_ids=("problem:public_fixture",),
        private_source_content=False,
        execution_purpose="qualification_fixture",
    )
    changed_suffix = ChatTemplatePromptFormatter(
        formatter_id=formatter.formatter_id,
        system_prompt="You are an expert in mathematics and Lean 4.",
        post_template_suffix=PostTemplateSuffix(
            suffix_id="stepfun_think_v1",
            text="<think>\n",
        ),
    )
    changed_eos = request.model_copy(
        update={"decoding": decoding.model_copy(update={"eos_token_id": 151_645})}
    )

    assert request.prompt_formatter_hash == FORMATTER_SHA256
    assert changed_suffix.formatter_hash != request.prompt_formatter_hash
    assert request.request_hash != changed_eos.request_hash

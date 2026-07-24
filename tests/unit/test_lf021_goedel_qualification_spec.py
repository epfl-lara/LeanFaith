"""Frozen Goedel-Formalizer-V2 fixture-qualification inputs.

These tests deliberately stop before loading checkpoint weights.  They make
the new prompt/config artifacts executable specifications for the shared
qualification runner without granting Gate-5G credit or producing labels.
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
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG_PATH = ROOT / "configs/generation/local_qualification_goedel_v1.yaml"
PROMPT_PATH = ROOT / "prompts/autoformalizers/goedel_card_chat_v1.txt"
SUFFIX_PATH = ROOT / "prompts/autoformalizers/common_final_fence_v1.txt"
PARSER_PATH = ROOT / "src/leanfaith/generation/local_output_adapter.py"
FIXTURE_PATH = ROOT / "examples/lf021_goedel_mathlib_nat_comm_20260723_v1.json"
HEADER_PATH = ROOT / "examples/lf021_goedel_mathlib_standard_header_v1.lean"

REVISION = "fe2d362d899601abe79d7d5e95eaa7fe9883a0cb"
PROMPT_SHA256 = "1fb4e6972c27c0a937a35913a75b1c705412416009f38a204b8824cd7ccb04c3"
CHAT_TEMPLATE_SHA256 = "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8"


def _config() -> dict[str, object]:
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _integer(value: object) -> int:
    assert isinstance(value, int)
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
            "<|im_start|>user\n"
            + conversation[0]["content"]
            + ("<|im_end|>\n<|im_start|>assistant\n")
        )


def test_goedel_prompt_is_card_exact_user_only_plus_frozen_output_contract() -> None:
    template = PROMPT_PATH.read_bytes()
    suffix = SUFFIX_PATH.read_text(encoding="utf-8").strip()
    assert sha256_hex(template) == PROMPT_SHA256
    decoded = template.decode("utf-8")
    assert decoded == (
        "Please autoformalize the following natural language problem statement in Lean 4. "
        "Use the following theorem name: {{THEOREM_NAME}}\n"
        "The natural language statement is: \n"
        "{{NL_STATEMENT}}Think before you provide the lean statement.\n\n"
        "{{COMMON_SUFFIX}}\n"
    )
    assert decoded.count("{{THEOREM_NAME}}") == 1
    assert decoded.count("{{NL_STATEMENT}}") == 1
    assert decoded.count("{{COMMON_SUFFIX}}") == 1
    assert "{{REGISTERED_HEADER}}" not in decoded

    rendered = (
        decoded.replace("{{THEOREM_NAME}}", "lf021_goedel_fixture")
        .replace("{{NL_STATEMENT}}", "For every n, n + 1 is positive.")
        .replace("{{COMMON_SUFFIX}}", suffix)
    )
    tokenizer = _CapturingTokenizer()
    formatter = ChatTemplatePromptFormatter(
        formatter_id="goedel_card_chat_v1",
        system_prompt=None,
        add_generation_prompt=True,
    )
    formatted = formatter.format_prompt(
        rendered,
        tokenizer=tokenizer,
        pin=LocalHFModelPin(
            repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
            revision=REVISION,
            dtype="bfloat16",
        ),
    )
    assert list(tokenizer.messages or ()) == [
        {
            "role": "user",
            "content": rendered,
        }
    ]
    assert tokenizer.tokenize is False
    assert tokenizer.add_generation_prompt is True
    assert formatted.startswith("<|im_start|>user\n")
    assert formatted.endswith("<|im_start|>assistant\n")


def test_goedel_config_binds_exact_model_prompt_parser_and_card_decoding() -> None:
    config = _config()
    model = _mapping(config["active_model"])
    prompt = _mapping(config["prompt"])
    fixture = _mapping(config["qualification_fixture"])
    decoding = LocalHFDecodingConfig.model_validate(config["decoding"])

    assert config["config_id"] == "lf021_local_qualification_goedel_v1"
    assert config["artifact_class"] == "smoke"
    assert config["qualifies_for_gate5g"] is False
    assert config["semantic_labels_created"] is False
    assert config["external_endpoints_allowed"] is False
    assert config["private_source_content_allowed"] is False

    assert model["family_id"] == "goedel_formalizer_v2_8b"
    assert model["provider_slot"] == "local_goedel_qualification"
    assert model["repo_id"] == "Goedel-LM/Goedel-Formalizer-V2-8B"
    assert model["revision"] == REVISION
    assert model["tokenizer_revision"] == REVISION
    assert model["architecture"] == "Qwen3ForCausalLM"
    assert model["model_positions"] == 40_960
    assert model["checkpoint_bytes"] == 16_381_516_824
    assert model["supervision_eligible"] is False
    assert model["heldout_generator"] is False

    assert prompt["formatter_id"] == "goedel_card_chat_v1"
    assert prompt["system_prompt"] is None
    assert prompt["add_generation_prompt"] is True
    assert prompt["post_template_suffix"] is None
    assert prompt["chat_template_sha256"] == CHAT_TEMPLATE_SHA256
    assert prompt["template_sha256"] == sha256_hex(PROMPT_PATH.read_bytes())
    assert prompt["common_suffix_sha256"] == sha256_hex(SUFFIX_PATH.read_bytes())
    assert prompt["parser_source_sha256"] == sha256_hex(PARSER_PATH.read_bytes())
    assert prompt["parser_id"] == "lean_terminal_fence_or_raw_signature_v3"

    assert decoding.model_dump(mode="json") == {
        "max_new_tokens": 16_384,
        "do_sample": True,
        "num_beams": 1,
        "num_return_sequences": 1,
        "temperature": 0.9,
        "top_p": 0.95,
        "top_k": 20,
        "seed": 30,
        "repetition_penalty": 1.0,
        "eos_token_id": [151_645, 151_643],
        "pad_token_id": 151_643,
    }
    assert decoding.generation_kwargs()["eos_token_id"] == (151_645, 151_643)

    assert fixture == {
        "fixture_artifact": "examples/lf021_goedel_mathlib_nat_comm_20260723_v1.json",
        "fixture_sha256": sha256_hex(FIXTURE_PATH.read_bytes()),
        "import_header_artifact": "examples/lf021_goedel_mathlib_standard_header_v1.lean",
        "import_header_sha256": sha256_hex(HEADER_PATH.read_bytes()),
        "project_registry_key": "mathlib",
        "public_only": True,
        "private_source_content": False,
    }
    assert HEADER_PATH.read_text(encoding="utf-8") == (
        "import Mathlib\n"
        "import Aesop\n\n"
        "set_option maxHeartbeats 0\n\n"
        "open BigOperators Real Nat Topology Rat\n"
    )


def test_goedel_checkpoint_manifest_is_complete_and_size_reconciled() -> None:
    model = _mapping(_config()["active_model"])
    checkpoint = _mapping(model["checkpoint_artifacts"])
    index = _mapping(checkpoint["index"])
    shards = checkpoint["shards"]
    assert isinstance(shards, list)
    assert index == {
        "artifact": "model.safetensors.index.json",
        "bytes": 32_878,
        "sha256": "3649bb967710edc0f995e3839a363a069482a1688216f87745248966751d0221",
    }
    expected = (
        (
            "model-00001-of-00004.safetensors",
            4_902_257_696,
            "e70040a504dbd55b64d83cbcb3754757511102de1c965a0b69f0ab72af8c1031",
        ),
        (
            "model-00002-of-00004.safetensors",
            4_915_960_368,
            "c47a4d4d82f82e2dfb2364f6faabb8bd96220874a779a6152cd5f184695e1361",
        ),
        (
            "model-00003-of-00004.safetensors",
            4_983_068_496,
            "b57b17f0bafd3273d33a0e8d40139def218b27416e7bfc2e2fcd80d03d06405a",
        ),
        (
            "model-00004-of-00004.safetensors",
            1_580_230_264,
            "f1b2d9540cc59678f521dc29494fa98c0dfd0c80427915747511c4fafbfeb8c5",
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


def test_goedel_local_request_hash_binds_formatter_and_ordered_eos_ids() -> None:
    config = _config()
    decoding = LocalHFDecodingConfig.model_validate(config["decoding"])
    formatter = ChatTemplatePromptFormatter(
        formatter_id="goedel_card_chat_v1",
        system_prompt=None,
        add_generation_prompt=True,
    )
    request = LocalHFGenerationRequest(
        pin=LocalHFModelPin(
            repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
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
    reversed_eos = request.model_copy(
        update={"decoding": decoding.model_copy(update={"eos_token_id": (151_643, 151_645)})}
    )
    assert request.prompt_formatter_hash == formatter.formatter_hash
    assert request.request_hash != reversed_eos.request_hash

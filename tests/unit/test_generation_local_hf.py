"""Local-only Hugging Face generation boundary without model downloads."""

from __future__ import annotations

import sys
import threading
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.local_hf import (
    ChatTemplatePromptFormatter,
    IdentityPromptFormatter,
    LoadedLocalHFModel,
    LocalHFArtifactUnavailableError,
    LocalHFDecodingConfig,
    LocalHFDependencyUnavailableError,
    LocalHFGeneratedText,
    LocalHFGenerationError,
    LocalHFGenerationRequest,
    LocalHFModelPin,
    LocalHFPromptFormattingError,
    LocalHFSequentialRuntime,
    PostTemplateSuffix,
    TransformersCausalGenerator,
    TransformersLocalLoader,
)

_REPO = "AI-MO/Kimina-Autoformalizer-7B"
_REVISION = "ddd47cb477d93b3ca990468e1c0d5ad6b60973dd"


def _pin(**updates: object) -> LocalHFModelPin:
    payload: dict[str, object] = {"repo_id": _REPO, "revision": _REVISION}
    payload.update(updates)
    return LocalHFModelPin.model_validate(payload)


def _request(
    *,
    formatter_id: str = "identity_v1",
    private: bool = False,
) -> LocalHFGenerationRequest:
    return LocalHFGenerationRequest(
        pin=_pin(),
        prompt="Formalize the identity n = n.",
        prompt_formatter_id=formatter_id,
        decoding=LocalHFDecodingConfig(max_new_tokens=128, seed=7),
        input_ids=("problem:fixture",),
        private_source_content=private,
    )


def test_model_pin_requires_exact_repo_and_40_hex_revision() -> None:
    assert _pin().revision == _REVISION

    for repo_id in ("no-namespace", "https://huggingface.co/model", "org/"):
        with pytest.raises(ValidationError):
            _pin(repo_id=repo_id)
    for revision in ("main", "a" * 39, "A" * 40, "a" * 41):
        with pytest.raises(ValidationError):
            _pin(revision=revision)


def test_model_pin_refuses_endpoints_and_remote_code_without_authorization() -> None:
    with pytest.raises(ValidationError, match="external endpoints are forbidden"):
        _pin(endpoint="https://api.example.test/inference")
    with pytest.raises(ValidationError, match="requires an explicit"):
        _pin(allow_remote_code=True)
    with pytest.raises(ValidationError, match="must be absent"):
        _pin(remote_code_authorization="ADR-unsafe")

    authorized = _pin(
        allow_remote_code=True,
        remote_code_authorization="docs/adr/ADR-local-model-remote-code.md",
    )
    assert authorized.allow_remote_code is True


def test_decoding_config_is_deterministic_complete_and_hash_stable() -> None:
    first = LocalHFDecodingConfig(max_new_tokens=64, seed=11)
    second = LocalHFDecodingConfig(max_new_tokens=64, seed=11)

    assert first == second
    assert first.decoding_hash == second.decoding_hash
    assert first.generation_kwargs() == {
        "max_new_tokens": 64,
        "do_sample": False,
        "num_beams": 1,
        "num_return_sequences": 1,
        "repetition_penalty": 1.0,
    }
    with pytest.raises(ValidationError):
        LocalHFDecodingConfig.model_validate({"max_new_tokens": 64, "do_sample": True})
    with pytest.raises(ValidationError):
        LocalHFDecodingConfig.model_validate({"max_new_tokens": 64, "temperature": 0.7})
    with pytest.raises(ValidationError):
        LocalHFDecodingConfig.model_validate({"max_new_tokens": 64, "num_beams": 4})


def test_decoding_config_preserves_one_or_multiple_eos_token_ids_exactly() -> None:
    single = LocalHFDecodingConfig(max_new_tokens=64, eos_token_id=151_645)
    multiple = LocalHFDecodingConfig.model_validate(
        {
            "max_new_tokens": 64,
            "eos_token_id": [151_645, 151_643],
            "pad_token_id": 151_643,
        }
    )

    assert single.eos_token_id == 151_645
    assert single.generation_kwargs()["eos_token_id"] == 151_645
    assert multiple.eos_token_id == (151_645, 151_643)
    assert multiple.generation_kwargs()["eos_token_id"] == (151_645, 151_643)
    assert multiple.model_dump(mode="json")["eos_token_id"] == [151_645, 151_643]
    assert (
        LocalHFDecodingConfig.model_validate(multiple.model_dump(mode="json")).decoding_hash
        == multiple.decoding_hash
    )
    reversed_ids = LocalHFDecodingConfig(
        max_new_tokens=64,
        eos_token_id=(151_643, 151_645),
        pad_token_id=151_643,
    )
    assert reversed_ids.decoding_hash != multiple.decoding_hash

    for invalid in ([], [-1], [151_645, -1], ["151645"], [True]):
        with pytest.raises(ValidationError):
            LocalHFDecodingConfig.model_validate({"max_new_tokens": 64, "eos_token_id": invalid})


def test_seeded_sampling_preserves_card_parameters_and_reform_token_budget() -> None:
    sampled = LocalHFDecodingConfig(
        max_new_tokens=32_768,
        do_sample=True,
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        repetition_penalty=1.1,
        seed=19,
    )

    assert sampled.generation_kwargs() == {
        "max_new_tokens": 32_768,
        "do_sample": True,
        "num_beams": 1,
        "num_return_sequences": 1,
        "repetition_penalty": 1.1,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
    }
    assert (
        sampled.decoding_hash
        == LocalHFDecodingConfig.model_validate(sampled.model_dump(mode="json")).decoding_hash
    )
    with pytest.raises(ValidationError, match="requires explicit"):
        LocalHFDecodingConfig(max_new_tokens=32_768, do_sample=True)
    with pytest.raises(ValidationError, match="cannot specify"):
        LocalHFDecodingConfig(max_new_tokens=128, top_p=0.95)


def test_execution_purpose_separates_smoke_from_sampled_qualification() -> None:
    sampled = LocalHFDecodingConfig(
        max_new_tokens=2048,
        do_sample=True,
        temperature=0.6,
        top_p=0.95,
        top_k=20,
    )
    base = _request().model_dump(mode="python")
    base["decoding"] = sampled

    with pytest.raises(ValidationError, match="implementation_smoke"):
        LocalHFGenerationRequest.model_validate(base)

    base["execution_purpose"] = "qualification_fixture"
    qualification = LocalHFGenerationRequest.model_validate(base)
    assert qualification.execution_purpose == "qualification_fixture"

    deterministic = _request().model_dump(mode="python")
    deterministic["execution_purpose"] = "qualification_fixture"
    with pytest.raises(ValidationError, match="frozen sampled"):
        LocalHFGenerationRequest.model_validate(deterministic)


def test_module_import_does_not_eagerly_import_optional_runtime_dependencies() -> None:
    # The test suite has no optional inference dependencies installed.  Their
    # absence after importing local_hf proves the module boundary is lazy.
    assert "transformers" not in sys.modules
    assert "torch" not in sys.modules


@dataclass
class _FakeLoader:
    events: list[str] = field(default_factory=list)

    def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
        self.events.append(f"load:{pin.repo_id}@{pin.revision}")
        return LoadedLocalHFModel(tokenizer=object(), model=object())

    def unload(self, loaded: LoadedLocalHFModel) -> None:
        del loaded
        self.events.append("unload")


@dataclass
class _FakeFormatter:
    formatter_id: str = "kimina_prompt_v1"
    seen: list[str] = field(default_factory=list)

    @property
    def formatter_hash(self) -> str:
        return sha256_hex(b"fake-kimina-prompt-v1")

    @property
    def requires_hash_binding(self) -> bool:
        return False

    def format_prompt(
        self,
        prompt: str,
        *,
        tokenizer: object,
        pin: LocalHFModelPin,
    ) -> str:
        del tokenizer
        self.seen.append(pin.repo_id)
        return f"<kimina>{prompt}</kimina>"


@dataclass
class _FakeGenerator:
    prompts: list[str] = field(default_factory=list)
    decodings: list[LocalHFDecodingConfig] = field(default_factory=list)

    def generate(
        self,
        *,
        loaded: LoadedLocalHFModel,
        formatted_prompt: str,
        decoding: LocalHFDecodingConfig,
        device: str,
    ) -> LocalHFGeneratedText:
        del loaded
        assert device == "cuda:0"
        self.prompts.append(formatted_prompt)
        self.decodings.append(decoding)
        return LocalHFGeneratedText(
            raw_text="```lean\ntheorem generated (n : Nat) : n = n\n```",
            prompt_tokens=13,
            output_tokens=17,
        )


@dataclass
class _Clock:
    readings: Sequence[float]
    index: int = 0

    def __call__(self) -> float:
        value = self.readings[self.index]
        self.index += 1
        return value


def test_injected_runtime_returns_raw_text_measurements_and_bridge_metadata() -> None:
    loader = _FakeLoader()
    generator = _FakeGenerator()
    formatter = _FakeFormatter()
    request = _request(formatter_id=formatter.formatter_id, private=True)
    runtime = LocalHFSequentialRuntime(
        loader=loader,
        generator=generator,
        formatter=formatter,
        clock=_Clock((10.0, 10.125, 10.625, 10.625)),
    )

    result = runtime.generate(request)

    assert loader.events == [f"load:{_REPO}@{_REVISION}", "unload"]
    assert generator.prompts == [
        "<kimina>Formalize the identity n = n.</kimina>",
    ]
    assert generator.decodings == [request.decoding]
    assert result.raw_text.startswith("```lean")
    assert result.output_hash == sha256_hex(result.raw_text.encode("utf-8"))
    assert (result.prompt_tokens, result.output_tokens, result.total_tokens) == (13, 17, 30)
    assert result.load_latency_ms == 125
    assert result.generation_latency_ms == 500
    assert result.unload_latency_ms == 0
    assert result.total_latency_ms == 625
    assert result.request_hash == request.request_hash
    assert result.decoding == request.decoding
    assert result.compatibility.model == _REPO
    assert result.compatibility.revision == _REVISION
    assert result.compatibility.execution_mode == "local"
    assert result.compatibility.transport == "in_process"
    assert result.compatibility.local_files_only is True
    assert result.compatibility.endpoint is None
    assert result.compatibility.private_source_content is True
    assert result.compatibility.private_content_transmitted is False
    assert result.compatibility.execution_purpose == "implementation_smoke"
    assert result.compatibility.qualifies_for_gate5g is False
    assert not hasattr(result, "parse_status")
    assert not hasattr(result, "label")


def test_runtime_rejects_wrong_prompt_formatter_and_unloads_on_generation_error() -> None:
    loader = _FakeLoader()
    runtime = LocalHFSequentialRuntime(
        loader=loader,
        generator=_FakeGenerator(),
        formatter=IdentityPromptFormatter(),
    )
    with pytest.raises(LocalHFPromptFormattingError, match="does not match"):
        runtime.generate(_request(formatter_id="other_formatter"))
    assert loader.events == []

    @dataclass
    class FailingGenerator:
        def generate(
            self,
            *,
            loaded: LoadedLocalHFModel,
            formatted_prompt: str,
            decoding: LocalHFDecodingConfig,
            device: str,
        ) -> LocalHFGeneratedText:
            del loaded, formatted_prompt, decoding, device
            raise RuntimeError("fixture failure")

    with pytest.raises(LocalHFGenerationError, match="hook failed"):
        LocalHFSequentialRuntime(
            loader=loader,
            generator=FailingGenerator(),
            formatter=IdentityPromptFormatter(),
        ).generate(_request())
    assert loader.events[-2:] == [f"load:{_REPO}@{_REVISION}", "unload"]


def test_process_global_gpu_lifecycle_is_serialized_across_runtime_instances() -> None:
    state_lock = threading.Lock()
    first_entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    load_count = 0

    @dataclass
    class BlockingLoader:
        def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
            nonlocal active, load_count, maximum_active
            del pin
            with state_lock:
                active += 1
                load_count += 1
                maximum_active = max(maximum_active, active)
                this_load = load_count
            if this_load == 1:
                first_entered.set()
                release.wait(timeout=2)
            return LoadedLocalHFModel(tokenizer=object(), model=object())

        def unload(self, loaded: LoadedLocalHFModel) -> None:
            nonlocal active
            del loaded
            with state_lock:
                active -= 1

    # Only the first thread can enter load; the second remains behind the global
    # lock.  The main thread provides the second barrier participant.
    loader = BlockingLoader()
    runtimes = [
        LocalHFSequentialRuntime(
            loader=loader,
            generator=_FakeGenerator(),
            formatter=IdentityPromptFormatter(),
        )
        for _ in range(2)
    ]
    errors: list[BaseException] = []

    def run(runtime: LocalHFSequentialRuntime) -> None:
        try:
            runtime.generate(_request())
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(runtime,)) for runtime in runtimes]
    for thread in threads:
        thread.start()
    assert first_entered.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=4)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1


def test_default_loader_fails_closed_when_optional_dependencies_are_missing() -> None:
    def missing(name: str) -> object:
        raise ModuleNotFoundError(name)

    with pytest.raises(LocalHFDependencyUnavailableError, match="optional torch"):
        TransformersLocalLoader(module_importer=missing).load(_pin())


def test_default_loader_passes_exact_local_only_pin_and_wraps_missing_revision() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Factory:
        def __init__(self, kind: str, *, fail: bool = False) -> None:
            self.kind = kind
            self.fail = fail

        def from_pretrained(self, repo_id: str, **kwargs: object) -> object:
            calls.append((self.kind, repo_id, kwargs))
            if self.fail:
                raise OSError("not in local cache")
            return Movable()

    class Movable:
        def to(self, device: str) -> Movable:
            calls.append(("move", device, {}))
            return self

        def eval(self) -> Movable:
            calls.append(("eval", "", {}))
            return self

    class Cuda:
        def is_available(self) -> bool:
            return True

        def device_count(self) -> int:
            return 1

        def empty_cache(self) -> None:
            return None

        def manual_seed_all(self, seed: int) -> None:
            del seed

    class Torch:
        cuda = Cuda()
        float16 = object()
        bfloat16 = object()

        def manual_seed(self, seed: int) -> None:
            del seed

        def inference_mode(self) -> object:
            return nullcontext()

    class Transformers:
        AutoTokenizer = Factory("tokenizer")
        AutoModelForCausalLM = Factory("model")

    modules = {"torch": Torch(), "transformers": Transformers()}
    loader = TransformersLocalLoader(module_importer=lambda name: modules[name])
    loaded = loader.load(_pin())
    assert isinstance(loaded, LoadedLocalHFModel)
    assert calls[:2] == [
        (
            "tokenizer",
            _REPO,
            {
                "revision": _REVISION,
                "local_files_only": True,
                "trust_remote_code": False,
            },
        ),
        (
            "model",
            _REPO,
            {
                "revision": _REVISION,
                "local_files_only": True,
                "trust_remote_code": False,
                "dtype": "auto",
            },
        ),
    ]
    assert calls[2:4] == [("move", "cuda:0", {}), ("eval", "", {})]

    Transformers.AutoModelForCausalLM = Factory("model", fail=True)
    with pytest.raises(LocalHFArtifactUnavailableError, match=f"{_REPO}@{_REVISION}"):
        loader.load(_pin())


def test_default_generator_returns_completion_only_and_preserves_sampled_kwargs() -> None:
    class Tensor:
        def __init__(self, values: list[int] | list[list[int]]) -> None:
            self.values = values

        @property
        def shape(self) -> tuple[int, ...]:
            if self.values and isinstance(self.values[0], list):
                rows = self.values
                return (len(rows), len(rows[0]))
            return (len(cast(list[int], self.values)),)

        def to(self, device: str) -> Tensor:
            assert device == "cuda:0"
            return self

        def __getitem__(self, key: object) -> Tensor:
            if isinstance(key, int):
                rows = cast(list[list[int]], self.values)
                return Tensor(rows[key])
            if isinstance(key, slice):
                values = cast(list[int], self.values)
                return Tensor(values[key])
            raise TypeError(key)

    decoded: list[list[int]] = []

    class Tokenizer:
        def __call__(self, text: str, **kwargs: object) -> object:
            assert text == "<formatted>"
            assert kwargs == {"return_tensors": "pt"}
            return {
                "input_ids": Tensor([[10, 11, 12]]),
                "attention_mask": Tensor([[1, 1, 1]]),
            }

        def decode(self, token_ids: object, **kwargs: object) -> str:
            assert kwargs == {"skip_special_tokens": True}
            values = cast(Tensor, token_ids).values
            decoded.append(cast(list[int], values))
            return "completion only"

    generated_kwargs: list[dict[str, object]] = []

    class Model:
        def generate(self, **kwargs: object) -> object:
            generated_kwargs.append(kwargs)
            return Tensor([[10, 11, 12, 20, 21]])

    seeded: list[int] = []

    class Cuda:
        def manual_seed_all(self, seed: int) -> None:
            seeded.append(seed)

    class Torch:
        cuda = Cuda()

        def manual_seed(self, seed: int) -> None:
            seeded.append(seed)

        def inference_mode(self) -> object:
            return nullcontext()

    decoding = LocalHFDecodingConfig(
        max_new_tokens=2048,
        do_sample=True,
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        repetition_penalty=1.1,
        seed=37,
        eos_token_id=(151_645, 151_643),
        pad_token_id=151_643,
    )
    generator = TransformersCausalGenerator(
        module_importer=lambda name: Torch() if name == "torch" else object()
    )
    result = generator.generate(
        loaded=LoadedLocalHFModel(tokenizer=Tokenizer(), model=Model()),
        formatted_prompt="<formatted>",
        decoding=decoding,
        device="cuda:0",
    )

    assert result.raw_text == "completion only"
    assert (result.prompt_tokens, result.output_tokens) == (3, 2)
    assert decoded == [[20, 21]]
    assert seeded == [37, 37]
    assert generated_kwargs == [
        {
            "input_ids": generated_kwargs[0]["input_ids"],
            "attention_mask": generated_kwargs[0]["attention_mask"],
            **decoding.generation_kwargs(),
        }
    ]


def test_chat_template_formatter_is_model_specific_and_fail_closed() -> None:
    seen: list[object] = []

    class Tokenizer:
        def apply_chat_template(
            self,
            conversation: object,
            *,
            tokenize: bool,
            add_generation_prompt: bool,
        ) -> str:
            seen.extend([conversation, tokenize, add_generation_prompt])
            return "<chat>formatted</chat>"

    formatter = ChatTemplatePromptFormatter(
        formatter_id="reform_chat_v1",
        system_prompt="Return Lean only.",
    )
    assert (
        formatter.format_prompt("Prove n = n.", tokenizer=Tokenizer(), pin=_pin())
        == "<chat>formatted</chat>"
    )
    assert cast(list[dict[str, str]], seen[0]) == [
        {"role": "system", "content": "Return Lean only."},
        {"role": "user", "content": "Prove n = n."},
    ]

    class BrokenTokenizer:
        def apply_chat_template(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            raise ValueError("no template")

    with pytest.raises(LocalHFPromptFormattingError, match="reform_chat_v1"):
        formatter.format_prompt("x", tokenizer=BrokenTokenizer(), pin=_pin())


def test_post_template_suffix_is_outside_messages_and_hash_bound() -> None:
    seen_messages: list[object] = []

    class Tokenizer:
        def apply_chat_template(
            self,
            conversation: object,
            *,
            tokenize: bool,
            add_generation_prompt: bool,
        ) -> str:
            seen_messages.append(conversation)
            assert tokenize is False
            assert add_generation_prompt is True
            return "<|system|>system<|user|>user<|assistant|>"

    suffix = PostTemplateSuffix(
        suffix_id="stepfun_think_v1",
        text="<think>",
    )
    formatter = ChatTemplatePromptFormatter(
        formatter_id="stepfun_card_think_v1",
        system_prompt="You are an expert in mathematics and Lean 4.",
        post_template_suffix=suffix,
    )
    formatted = formatter.format_prompt(
        "Formalize n = n.",
        tokenizer=Tokenizer(),
        pin=_pin(),
    )

    assert formatted == "<|system|>system<|user|>user<|assistant|><think>"
    messages = cast(list[dict[str, str]], seen_messages[0])
    assert messages == [
        {
            "role": "system",
            "content": "You are an expert in mathematics and Lean 4.",
        },
        {"role": "user", "content": "Formalize n = n."},
    ]
    assert all("<think>" not in message["content"] for message in messages)
    assert suffix.content_hash == "7d329bb7d9d43bf17bcafd4cb8203e1b94423923e87980bd1d2d9fc525d50b99"
    assert formatter.requires_hash_binding is True
    assert (
        ChatTemplatePromptFormatter(
            formatter_id="stepfun_card_think_v1",
            system_prompt="You are an expert in mathematics and Lean 4.",
            post_template_suffix=PostTemplateSuffix(
                suffix_id="stepfun_think_v1",
                text="<think>",
            ),
        ).formatter_hash
        == formatter.formatter_hash
    )
    changed = ChatTemplatePromptFormatter(
        formatter_id="stepfun_card_think_v1",
        system_prompt="You are an expert in mathematics and Lean 4.",
        post_template_suffix=PostTemplateSuffix(
            suffix_id="stepfun_think_v1",
            text="<think>\n",
        ),
    )
    assert changed.formatter_hash != formatter.formatter_hash
    with pytest.raises(ValidationError):
        PostTemplateSuffix.model_validate(
            {
                "suffix_id": "stepfun_think_v1",
                "text": "<think>",
                "unbound_extra": True,
            }
        )
    with pytest.raises(TypeError, match="PostTemplateSuffix"):
        ChatTemplatePromptFormatter(
            formatter_id="stepfun_card_think_v1",
            post_template_suffix=cast(PostTemplateSuffix, "<think>"),
        )


def test_post_template_suffix_requires_exact_request_hash_binding() -> None:
    class Tokenizer:
        def apply_chat_template(
            self,
            conversation: object,
            *,
            tokenize: bool,
            add_generation_prompt: bool,
        ) -> str:
            del conversation, tokenize, add_generation_prompt
            return "<assistant>"

    @dataclass
    class Loader:
        load_count: int = 0

        def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
            del pin
            self.load_count += 1
            return LoadedLocalHFModel(tokenizer=Tokenizer(), model=object())

        def unload(self, loaded: LoadedLocalHFModel) -> None:
            del loaded

    formatter = ChatTemplatePromptFormatter(
        formatter_id="stepfun_card_think_v1",
        post_template_suffix=PostTemplateSuffix(
            suffix_id="stepfun_think_v1",
            text="<think>",
        ),
    )
    loader = Loader()
    generator = _FakeGenerator()
    runtime = LocalHFSequentialRuntime(
        loader=loader,
        generator=generator,
        formatter=formatter,
    )
    unbound = _request(formatter_id=formatter.formatter_id)
    with pytest.raises(LocalHFPromptFormattingError, match="requires exact hash binding"):
        runtime.generate(unbound)
    assert loader.load_count == 0

    mismatched = unbound.model_copy(update={"prompt_formatter_hash": "0" * 64})
    with pytest.raises(LocalHFPromptFormattingError, match="does not match"):
        runtime.generate(mismatched)
    assert loader.load_count == 0

    bound = unbound.model_copy(update={"prompt_formatter_hash": formatter.formatter_hash})
    assert bound.request_hash != unbound.request_hash
    result = runtime.generate(bound)
    assert loader.load_count == 1
    assert generator.prompts == ["<assistant><think>"]
    assert result.prompt_formatter_hash == formatter.formatter_hash
    assert result.compatibility.prompt_formatter_hash == formatter.formatter_hash
    assert result.formatted_prompt_hash == sha256_hex(b"<assistant><think>")


def test_probe_pins_are_source_constants_not_an_enabled_configuration() -> None:
    from leanfaith.generation.local_hf import LOCAL_MODEL_PROBE_V1_PINS

    assert (_REPO, _REVISION) in LOCAL_MODEL_PROBE_V1_PINS
    assert all(len(revision) == 40 for _, revision in LOCAL_MODEL_PROBE_V1_PINS)
    assert not any(isinstance(item, Path) for item in LOCAL_MODEL_PROBE_V1_PINS)

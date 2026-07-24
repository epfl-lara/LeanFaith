"""Optional, local-only Hugging Face generation runtime for LF-021.

This module is deliberately isolated from the provider registry and CLI:

* importing it does not import ``torch`` or ``transformers``;
* model and tokenizer artifacts must already exist in the local Hub cache;
* an exact repository ID and 40-hex commit revision are mandatory;
* no endpoint or remote inference transport is accepted;
* one process-global lock serializes the load/generate/unload lifecycle on
  ``cuda:0``;
* results contain raw text and operational measurements only.  They contain no
  parsing result, semantic label, or promotion decision.

The pins copied from ``reports/generation/lf021_local_model_probe_v1.json`` are
availability observations, not enabled configuration or execution authority.
"""

from __future__ import annotations

import gc
import importlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel

_HEX40_PATTERN = r"^[0-9a-f]{40}$"
_HEX64_PATTERN = r"^[0-9a-f]{64}$"
_REPO_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]*(?:_v[0-9]+)?$"
_StrictTokenId = Annotated[int, Field(ge=0, strict=True)]

# Availability observations from lf021_local_model_probe_v1.json.  Nothing in
# this module selects or enables one of these models.
LOCAL_MODEL_PROBE_V1_PINS: tuple[tuple[str, str], ...] = (
    (
        "AI-MO/Kimina-Autoformalizer-7B",
        "ddd47cb477d93b3ca990468e1c0d5ad6b60973dd",
    ),
    (
        "Goedel-LM/Goedel-Formalizer-V2-8B",
        "fe2d362d899601abe79d7d5e95eaa7fe9883a0cb",
    ),
    (
        "stepfun-ai/StepFun-Formalizer-7B",
        "fb0dc612761fecd64ebbc489c2a3417e9ea01968",
    ),
    (
        "GuoxinChen/ReForm-8B",
        "1589c832cfad679a280b222e694b987a33befd26",
    ),
)


class LocalHFError(RuntimeError):
    """Base class for local Hugging Face runtime failures."""


class LocalHFExternalEndpointError(LocalHFError):
    """An external inference endpoint was supplied to a local-only runtime."""


class LocalHFDependencyUnavailableError(LocalHFError):
    """An optional local-inference dependency is unavailable."""


class LocalHFArtifactUnavailableError(LocalHFError):
    """The exact pinned tokenizer or model revision is absent locally."""


class LocalHFDeviceUnavailableError(LocalHFError):
    """The required single local CUDA device is unavailable."""


class LocalHFPromptFormattingError(LocalHFError):
    """A model-specific prompt formatter could not render the prompt."""


class LocalHFGenerationError(LocalHFError):
    """The local model failed while generating raw text."""


class LocalHFModelPin(StrictModel):
    """Exact, local-only model identity and loading policy."""

    repo_id: str = Field(pattern=_REPO_ID_PATTERN)
    revision: str = Field(pattern=_HEX40_PATTERN)
    device: Literal["cuda:0"] = "cuda:0"
    dtype: Literal["auto", "float16", "bfloat16"] = "auto"
    endpoint: str | None = None
    allow_remote_code: bool = False
    remote_code_authorization: str | None = None

    @model_validator(mode="after")
    def _fail_closed_transport_and_code_policy(self) -> Self:
        if self.endpoint is not None:
            raise ValueError("external endpoints are forbidden for the local Hugging Face runtime")
        if self.allow_remote_code and not self.remote_code_authorization:
            raise ValueError(
                "allow_remote_code requires an explicit remote_code_authorization record"
            )
        if not self.allow_remote_code and self.remote_code_authorization is not None:
            raise ValueError(
                "remote_code_authorization must be absent when allow_remote_code is false"
            )
        return self


class LocalHFDecodingConfig(StrictModel):
    """Frozen, seeded decoding parameters.

    Greedy implementation smoke and the card-derived sampled qualification
    configurations use one strict schema.  Sampling fields must be internally
    coherent, multi-sequence generation remains unsupported, and the complete
    validated object is hashed and returned with every result.
    """

    max_new_tokens: int = Field(ge=1, le=32_768, strict=True)
    do_sample: bool = False
    num_beams: Literal[1] = 1
    num_return_sequences: Literal[1] = 1
    temperature: float | None = Field(default=None, gt=0.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    seed: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    repetition_penalty: float = Field(default=1.0, gt=0.0)
    eos_token_id: _StrictTokenId | tuple[_StrictTokenId, ...] | None = None
    pad_token_id: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def _sampling_fields_are_coherent(self) -> Self:
        sampling_fields = (self.temperature, self.top_p, self.top_k)
        if self.do_sample:
            if self.temperature is None or self.top_p is None:
                raise ValueError("sampled decoding requires explicit temperature and top_p")
        elif any(value is not None for value in sampling_fields):
            raise ValueError("greedy decoding cannot specify temperature, top_p, or top_k")
        if isinstance(self.eos_token_id, tuple) and not self.eos_token_id:
            raise ValueError("multiple EOS token IDs must be a nonempty ordered tuple")
        return self

    @property
    def decoding_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))

    def generation_kwargs(self) -> dict[str, object]:
        """Return only arguments intended for ``model.generate``."""

        result: dict[str, object] = {
            "max_new_tokens": self.max_new_tokens,
            "do_sample": self.do_sample,
            "num_beams": 1,
            "num_return_sequences": 1,
            "repetition_penalty": self.repetition_penalty,
        }
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.top_p is not None:
            result["top_p"] = self.top_p
        if self.top_k is not None:
            result["top_k"] = self.top_k
        if self.eos_token_id is not None:
            result["eos_token_id"] = self.eos_token_id
        if self.pad_token_id is not None:
            result["pad_token_id"] = self.pad_token_id
        return result


class LocalHFGenerationRequest(StrictModel):
    """One raw local generation request."""

    schema_version: Literal[1] = 1
    pin: LocalHFModelPin
    prompt: str = Field(min_length=1)
    prompt_formatter_id: str = Field(min_length=1)
    prompt_formatter_hash: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    decoding: LocalHFDecodingConfig
    input_ids: tuple[str, ...] = ()
    private_source_content: bool = False
    execution_purpose: Literal[
        "implementation_smoke",
        "qualification_fixture",
        "research_collection",
    ] = "implementation_smoke"

    @model_validator(mode="after")
    def _purpose_matches_decoding(self) -> Self:
        if self.execution_purpose == "implementation_smoke" and self.decoding.do_sample:
            raise ValueError("implementation_smoke requires deterministic greedy decoding")
        if self.execution_purpose == "qualification_fixture" and not self.decoding.do_sample:
            raise ValueError(
                "qualification_fixture requires the frozen sampled decoding configuration"
            )
        return self

    @property
    def request_hash(self) -> str:
        payload: dict[str, object] = {
            "schema": "local_hf_generation_request_v1",
            "repo_id": self.pin.repo_id,
            "revision": self.pin.revision,
            "device": self.pin.device,
            "dtype": self.pin.dtype,
            "allow_remote_code": self.pin.allow_remote_code,
            "remote_code_authorization": self.pin.remote_code_authorization,
            "prompt": self.prompt,
            "prompt_formatter_id": self.prompt_formatter_id,
            "decoding": self.decoding.model_dump(mode="json"),
            "input_ids": self.input_ids,
            "private_source_content": self.private_source_content,
            "execution_purpose": self.execution_purpose,
        }
        # Preserve the pre-hash-binding request identity for existing Kimina
        # artifacts. New formatters that alter the rendered template after
        # tokenization require this field and therefore receive a new,
        # configuration-bound request identity.
        if self.prompt_formatter_hash is not None:
            payload["prompt_formatter_hash"] = self.prompt_formatter_hash
        return hash_canonical(payload)


class LocalHFProviderCompatibility(StrictModel):
    """Metadata required to bridge a result into ``ProviderRawResponse`` later.

    Request-specific provider hashes and attempt IDs remain the responsibility
    of the provider boundary.  This record supplies the stable transport and
    output facts without importing that boundary here.
    """

    provider: Literal["local_hf"] = "local_hf"
    model: str = Field(pattern=_REPO_ID_PATTERN)
    revision: str = Field(pattern=_HEX40_PATTERN)
    execution_mode: Literal["local"] = "local"
    transport: Literal["in_process"] = "in_process"
    local_files_only: Literal[True] = True
    endpoint: None = None
    remote_code_authorized: bool
    private_source_content: bool
    private_content_transmitted: Literal[False] = False
    execution_purpose: Literal[
        "implementation_smoke",
        "qualification_fixture",
        "research_collection",
    ]
    qualifies_for_gate5g: Literal[False] = False
    raw_response_status: Literal["success"] = "success"
    output_hash: str = Field(pattern=_HEX64_PATTERN)
    formatted_prompt_hash: str = Field(pattern=_HEX64_PATTERN)
    prompt_formatter_id: str = Field(min_length=1)
    prompt_formatter_hash: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    decoding_hash: str = Field(pattern=_HEX64_PATTERN)


class LocalHFGenerationResult(StrictModel):
    """Raw generation plus reproducible measurements; never a semantic label."""

    schema_version: Literal[1] = 1
    request_hash: str = Field(pattern=_HEX64_PATTERN)
    formatted_prompt_hash: str = Field(pattern=_HEX64_PATTERN)
    raw_text: str
    output_hash: str = Field(pattern=_HEX64_PATTERN)
    prompt_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)
    load_latency_ms: int = Field(ge=0, strict=True)
    generation_latency_ms: int = Field(ge=0, strict=True)
    unload_latency_ms: int = Field(ge=0, strict=True)
    total_latency_ms: int = Field(ge=0, strict=True)
    decoding: LocalHFDecodingConfig
    decoding_hash: str = Field(pattern=_HEX64_PATTERN)
    prompt_formatter_hash: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    compatibility: LocalHFProviderCompatibility

    @model_validator(mode="after")
    def _measurements_match(self) -> Self:
        if self.output_hash != sha256_hex(self.raw_text.encode("utf-8")):
            raise ValueError("output_hash does not match raw_text")
        if self.total_tokens != self.prompt_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + output_tokens")
        if self.decoding_hash != self.decoding.decoding_hash:
            raise ValueError("decoding_hash does not match decoding")
        if self.compatibility.output_hash != self.output_hash:
            raise ValueError("compatibility output_hash does not match result")
        if self.compatibility.formatted_prompt_hash != self.formatted_prompt_hash:
            raise ValueError("compatibility formatted_prompt_hash does not match result")
        if self.compatibility.prompt_formatter_hash != self.prompt_formatter_hash:
            raise ValueError("compatibility prompt_formatter_hash does not match result")
        if self.compatibility.decoding_hash != self.decoding_hash:
            raise ValueError("compatibility decoding_hash does not match result")
        minimum_total = self.load_latency_ms + self.generation_latency_ms + self.unload_latency_ms
        if self.total_latency_ms < minimum_total:
            raise ValueError("total_latency_ms cannot be smaller than measured lifecycle stages")
        return self


@dataclass(frozen=True, slots=True)
class LoadedLocalHFModel:
    """Opaque tokenizer/model objects returned by an injectable loader."""

    tokenizer: object
    model: object


@dataclass(frozen=True, slots=True)
class LocalHFGeneratedText:
    """Low-level generator output before runtime measurements are attached."""

    raw_text: str
    prompt_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token counts must be nonnegative")


class LocalHFLoader(Protocol):
    """Injectable exact-revision model/tokenizer lifecycle."""

    def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel: ...

    def unload(self, loaded: LoadedLocalHFModel) -> None: ...


class LocalHFGenerator(Protocol):
    """Injectable raw-text generator."""

    def generate(
        self,
        *,
        loaded: LoadedLocalHFModel,
        formatted_prompt: str,
        decoding: LocalHFDecodingConfig,
        device: Literal["cuda:0"],
    ) -> LocalHFGeneratedText: ...


class LocalHFPromptFormatter(Protocol):
    """Model-specific prompt formatting hook."""

    @property
    def formatter_id(self) -> str: ...

    @property
    def formatter_hash(self) -> str: ...

    @property
    def requires_hash_binding(self) -> bool: ...

    def format_prompt(
        self,
        prompt: str,
        *,
        tokenizer: object,
        pin: LocalHFModelPin,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class IdentityPromptFormatter:
    """Use an already rendered model prompt without modification."""

    formatter_id: str = "identity_v1"

    @property
    def formatter_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "local_hf_prompt_formatter_v1",
                "kind": "identity",
                "formatter_id": self.formatter_id,
            }
        )

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
        del tokenizer, pin
        return prompt


class _ChatTemplateTokenizer(Protocol):
    def apply_chat_template(
        self,
        conversation: Sequence[Mapping[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str: ...


class PostTemplateSuffix(StrictModel):
    """Exact text appended after a tokenizer-rendered chat template.

    The suffix is a typed, hashable input rather than an unrecorded string
    concatenation. StepFun's card-exact assistant prefix is represented as
    ``PostTemplateSuffix(suffix_id="stepfun_think_v1", text="<think>")``.
    """

    suffix_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    text: str = Field(min_length=1)

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ChatTemplatePromptFormatter:
    """Format a user prompt with the pinned tokenizer's chat template."""

    formatter_id: str
    system_prompt: str | None = None
    add_generation_prompt: bool = True
    post_template_suffix: PostTemplateSuffix | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.formatter_id, str) or not self.formatter_id:
            raise ValueError("formatter_id must be a nonempty string")
        if self.system_prompt is not None and not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string or None")
        if not isinstance(self.add_generation_prompt, bool):
            raise TypeError("add_generation_prompt must be a boolean")
        if self.post_template_suffix is not None and not isinstance(
            self.post_template_suffix, PostTemplateSuffix
        ):
            raise TypeError("post_template_suffix must be a PostTemplateSuffix or None")

    @property
    def formatter_hash(self) -> str:
        suffix = self.post_template_suffix
        return hash_canonical(
            {
                "schema": "local_hf_prompt_formatter_v1",
                "kind": "chat_template",
                "formatter_id": self.formatter_id,
                "system_prompt": self.system_prompt,
                "add_generation_prompt": self.add_generation_prompt,
                "post_template_suffix": (
                    None
                    if suffix is None
                    else {
                        **suffix.model_dump(mode="json"),
                        "content_hash": suffix.content_hash,
                    }
                ),
            }
        )

    @property
    def requires_hash_binding(self) -> bool:
        # A suffix changes the assistant-side prompt outside the chat message
        # list. Require its complete formatter identity to be included in the
        # request hash so it can never vary behind a stable formatter ID.
        return self.post_template_suffix is not None

    def format_prompt(
        self,
        prompt: str,
        *,
        tokenizer: object,
        pin: LocalHFModelPin,
    ) -> str:
        messages: list[Mapping[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        template_tokenizer = cast(_ChatTemplateTokenizer, tokenizer)
        try:
            formatted = template_tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=self.add_generation_prompt,
            )
        except Exception as error:
            raise LocalHFPromptFormattingError(
                f"prompt formatter {self.formatter_id!r} failed for {pin.repo_id}@{pin.revision}"
            ) from error
        if not isinstance(formatted, str) or not formatted:
            raise LocalHFPromptFormattingError(
                f"prompt formatter {self.formatter_id!r} returned no text"
            )
        if self.post_template_suffix is not None:
            formatted += self.post_template_suffix.text
        return formatted


class _Factory(Protocol):
    def from_pretrained(self, pretrained_model_name_or_path: str, **kwargs: object) -> object: ...


class _TransformersModule(Protocol):
    AutoTokenizer: _Factory
    AutoModelForCausalLM: _Factory


class _TorchCuda(Protocol):
    def is_available(self) -> bool: ...

    def device_count(self) -> int: ...

    def empty_cache(self) -> None: ...

    def manual_seed_all(self, seed: int) -> None: ...


class _TorchModule(Protocol):
    cuda: _TorchCuda
    float16: object
    bfloat16: object

    def manual_seed(self, seed: int) -> object: ...

    def inference_mode(self) -> AbstractContextManager[object]: ...


class _MovableModel(Protocol):
    def to(self, device: str) -> object: ...

    def eval(self) -> object: ...


ModuleImporter = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class TransformersLocalLoader:
    """Lazy ``transformers`` loader restricted to the existing local cache."""

    module_importer: ModuleImporter = importlib.import_module

    def _dependencies(self) -> tuple[_TransformersModule, _TorchModule]:
        try:
            transformers = cast(_TransformersModule, self.module_importer("transformers"))
            torch = cast(_TorchModule, self.module_importer("torch"))
        except (ImportError, ModuleNotFoundError) as error:
            raise LocalHFDependencyUnavailableError(
                "local Hugging Face generation requires optional torch and transformers "
                "dependencies"
            ) from error
        return transformers, torch

    def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
        transformers, torch = self._dependencies()
        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            raise LocalHFDeviceUnavailableError("cuda:0 is unavailable")

        try:
            tokenizer_factory = transformers.AutoTokenizer
            model_factory = transformers.AutoModelForCausalLM
        except AttributeError as error:
            raise LocalHFDependencyUnavailableError(
                "transformers lacks AutoTokenizer or AutoModelForCausalLM"
            ) from error

        common: dict[str, object] = {
            "revision": pin.revision,
            "local_files_only": True,
            "trust_remote_code": pin.allow_remote_code,
        }
        model_options = dict(common)
        if pin.dtype == "float16":
            model_options["dtype"] = torch.float16
        elif pin.dtype == "bfloat16":
            model_options["dtype"] = torch.bfloat16
        else:
            model_options["dtype"] = "auto"

        try:
            tokenizer = tokenizer_factory.from_pretrained(pin.repo_id, **common)
            model = model_factory.from_pretrained(pin.repo_id, **model_options)
            cast(_MovableModel, model).to(pin.device)
            cast(_MovableModel, model).eval()
        except Exception as error:
            raise LocalHFArtifactUnavailableError(
                "exact local model/tokenizer artifacts are unavailable or failed to load: "
                f"{pin.repo_id}@{pin.revision}"
            ) from error
        return LoadedLocalHFModel(tokenizer=tokenizer, model=model)

    def unload(self, loaded: LoadedLocalHFModel) -> None:
        try:
            cast(_MovableModel, loaded.model).to("cpu")
        finally:
            gc.collect()
            try:
                _, torch = self._dependencies()
            except LocalHFDependencyUnavailableError:
                return
            if torch.cuda.is_available():
                torch.cuda.empty_cache()


class _Tokenizer(Protocol):
    def __call__(self, text: str, **kwargs: object) -> object: ...

    def decode(self, token_ids: object, **kwargs: object) -> str: ...


class _GeneratingModel(Protocol):
    def generate(self, **kwargs: object) -> object: ...


class _Indexable(Protocol):
    def __getitem__(self, key: object) -> object: ...


class _Shaped(Protocol):
    @property
    def shape(self) -> Sequence[int]: ...


class _DeviceMovable(Protocol):
    def to(self, device: str) -> object: ...


def _token_count(value: object) -> int:
    shape = cast(_Shaped, value).shape
    if len(shape) < 1:
        raise LocalHFGenerationError("token tensor has no token dimension")
    return int(shape[-1])


def _move_to_device(value: object, device: str) -> object:
    mover = cast(_DeviceMovable, value)
    return mover.to(device)


def _slice_first_sequence(value: object, start: int) -> object:
    first = cast(_Indexable, value)[0]
    return cast(_Indexable, first)[slice(start, None)]


@dataclass(frozen=True, slots=True)
class TransformersCausalGenerator:
    """Lazy seeded causal generation for a loaded model.

    ``LocalHFDecodingConfig`` determines whether decoding is greedy or
    sampled.  The older ``TransformersGreedyGenerator`` name became
    inaccurate once qualification decoding gained an explicit sampled mode.
    """

    module_importer: ModuleImporter = importlib.import_module

    def generate(
        self,
        *,
        loaded: LoadedLocalHFModel,
        formatted_prompt: str,
        decoding: LocalHFDecodingConfig,
        device: Literal["cuda:0"],
    ) -> LocalHFGeneratedText:
        try:
            torch = cast(_TorchModule, self.module_importer("torch"))
        except (ImportError, ModuleNotFoundError) as error:
            raise LocalHFDependencyUnavailableError(
                "local Hugging Face generation requires optional torch"
            ) from error

        tokenizer = cast(_Tokenizer, loaded.tokenizer)
        model = cast(_GeneratingModel, loaded.model)
        try:
            encoded = tokenizer(formatted_prompt, return_tensors="pt")
            if not isinstance(encoded, Mapping):
                raise TypeError("tokenizer output is not a mapping")
            device_inputs = {
                str(name): _move_to_device(value, device) for name, value in encoded.items()
            }
            if "input_ids" not in device_inputs:
                raise KeyError("tokenizer output has no input_ids")
            prompt_tokens = _token_count(device_inputs["input_ids"])
            torch.manual_seed(decoding.seed)
            torch.cuda.manual_seed_all(decoding.seed)
            with torch.inference_mode():
                generated = model.generate(
                    **device_inputs,
                    **decoding.generation_kwargs(),
                )
            generated_tokens = _slice_first_sequence(generated, prompt_tokens)
            output_tokens = _token_count(generated_tokens)
            raw_text = tokenizer.decode(generated_tokens, skip_special_tokens=True)
        except LocalHFError:
            raise
        except Exception as error:
            raise LocalHFGenerationError("local model generation failed") from error

        if not isinstance(raw_text, str):
            raise LocalHFGenerationError("tokenizer decode did not return text")
        return LocalHFGeneratedText(
            raw_text=raw_text,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )


_SINGLE_GPU_LIFECYCLE_LOCK = threading.Lock()
Clock = Callable[[], float]


@dataclass(slots=True)
class LocalHFSequentialRuntime:
    """Load, run, and unload one pinned model under a process-global GPU lock."""

    loader: LocalHFLoader
    generator: LocalHFGenerator
    formatter: LocalHFPromptFormatter
    clock: Clock = time.perf_counter

    def generate(self, request: LocalHFGenerationRequest) -> LocalHFGenerationResult:
        if request.pin.endpoint is not None:
            # Pydantic rejects this earlier, but retain an explicit execution
            # boundary in case a non-standard object bypasses validation.
            raise LocalHFExternalEndpointError("external endpoints are forbidden")
        if request.prompt_formatter_id != self.formatter.formatter_id:
            raise LocalHFPromptFormattingError(
                "request prompt_formatter_id does not match runtime formatter: "
                f"{request.prompt_formatter_id!r} != {self.formatter.formatter_id!r}"
            )
        formatter_hash = self.formatter.formatter_hash
        if self.formatter.requires_hash_binding and request.prompt_formatter_hash is None:
            raise LocalHFPromptFormattingError(
                "prompt formatter requires exact hash binding in the request: "
                f"{self.formatter.formatter_id!r}"
            )
        if (
            request.prompt_formatter_hash is not None
            and request.prompt_formatter_hash != formatter_hash
        ):
            raise LocalHFPromptFormattingError(
                "request prompt_formatter_hash does not match runtime formatter: "
                f"{request.prompt_formatter_hash!r} != {formatter_hash!r}"
            )

        with _SINGLE_GPU_LIFECYCLE_LOCK:
            lifecycle_started = self.clock()
            load_started = lifecycle_started
            loaded = self.loader.load(request.pin)
            load_completed = self.clock()
            generation_started = load_completed
            generated: LocalHFGeneratedText | None = None
            generation_completed = generation_started
            unload_started = generation_started
            try:
                formatted_prompt = self.formatter.format_prompt(
                    request.prompt,
                    tokenizer=loaded.tokenizer,
                    pin=request.pin,
                )
                generated = self.generator.generate(
                    loaded=loaded,
                    formatted_prompt=formatted_prompt,
                    decoding=request.decoding,
                    device=request.pin.device,
                )
                generation_completed = self.clock()
                unload_started = generation_completed
            except LocalHFError:
                raise
            except Exception as error:
                raise LocalHFGenerationError("local generation hook failed") from error
            finally:
                self.loader.unload(loaded)
            lifecycle_completed = self.clock()

        if generated is None:
            raise LocalHFGenerationError("generator returned no result")

        output_hash = sha256_hex(generated.raw_text.encode("utf-8"))
        formatted_prompt_hash = sha256_hex(formatted_prompt.encode("utf-8"))
        decoding_hash = request.decoding.decoding_hash
        compatibility = LocalHFProviderCompatibility(
            model=request.pin.repo_id,
            revision=request.pin.revision,
            remote_code_authorized=request.pin.allow_remote_code,
            private_source_content=request.private_source_content,
            execution_purpose=request.execution_purpose,
            output_hash=output_hash,
            formatted_prompt_hash=formatted_prompt_hash,
            prompt_formatter_id=request.prompt_formatter_id,
            prompt_formatter_hash=formatter_hash,
            decoding_hash=decoding_hash,
        )
        load_latency_ms = _elapsed_ms(load_started, load_completed)
        generation_latency_ms = _elapsed_ms(generation_started, generation_completed)
        unload_latency_ms = _elapsed_ms(unload_started, lifecycle_completed)
        measured_stage_latency_ms = load_latency_ms + generation_latency_ms + unload_latency_ms
        total_latency_ms = max(
            measured_stage_latency_ms,
            _elapsed_ms(lifecycle_started, lifecycle_completed),
        )
        return LocalHFGenerationResult(
            request_hash=request.request_hash,
            formatted_prompt_hash=formatted_prompt_hash,
            raw_text=generated.raw_text,
            output_hash=output_hash,
            prompt_tokens=generated.prompt_tokens,
            output_tokens=generated.output_tokens,
            total_tokens=generated.prompt_tokens + generated.output_tokens,
            load_latency_ms=load_latency_ms,
            generation_latency_ms=generation_latency_ms,
            unload_latency_ms=unload_latency_ms,
            total_latency_ms=total_latency_ms,
            decoding=request.decoding,
            decoding_hash=decoding_hash,
            prompt_formatter_hash=formatter_hash,
            compatibility=compatibility,
        )


def _elapsed_ms(start: float, end: float) -> int:
    return max(0, round((end - start) * 1000))


__all__ = [
    "LOCAL_MODEL_PROBE_V1_PINS",
    "ChatTemplatePromptFormatter",
    "IdentityPromptFormatter",
    "LoadedLocalHFModel",
    "LocalHFArtifactUnavailableError",
    "LocalHFDecodingConfig",
    "LocalHFDependencyUnavailableError",
    "LocalHFDeviceUnavailableError",
    "LocalHFError",
    "LocalHFExternalEndpointError",
    "LocalHFGeneratedText",
    "LocalHFGenerationError",
    "LocalHFGenerationRequest",
    "LocalHFGenerationResult",
    "LocalHFGenerator",
    "LocalHFLoader",
    "LocalHFModelPin",
    "LocalHFPromptFormatter",
    "LocalHFPromptFormattingError",
    "LocalHFProviderCompatibility",
    "LocalHFSequentialRuntime",
    "PostTemplateSuffix",
    "TransformersCausalGenerator",
    "TransformersLocalLoader",
]

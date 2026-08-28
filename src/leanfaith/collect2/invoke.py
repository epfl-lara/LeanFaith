"""Small D-2 provider boundary for autoformalization collection.

The three local prompt profiles are byte-for-byte extractions of LF-021's
reviewed model-card prompts and the corresponding pinned tokenizer rendering
for a single user request.  Heavy inference dependencies are imported only
when a ``local_hf`` invocation actually runs.
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

ProviderKind = Literal["local_hf", "cli"]
CliName = Literal["codex", "lemex"]
ModelFamily = Literal["goedel", "kimina", "stepfun"]

COMMON_FINAL_FENCE = (
    "Return the final answer as one Lean 4 theorem or lemma declaration in one final Markdown "
    "fence labelled `lean4`. Use the registered theorem name. Do not invent a different import "
    "context. Explanatory reasoning may precede the final fence, but return no second Lean fence "
    "or alternative declaration."
)

GOEDEL_USER_TEMPLATE = (
    "Please autoformalize the following natural language problem statement in Lean 4. Use the "
    "following theorem name: {{THEOREM_NAME}}\n"
    "The natural language statement is: \n"
    "{{NL_STATEMENT}}Think before you provide the lean statement.\n\n"
    "{{COMMON_SUFFIX}}\n"
)

KIMINA_USER_TEMPLATE = (
    "Please autoformalize the following problem in Lean 4 with a header. Use the following theorem "
    "names: {{THEOREM_NAME}}.\n\n"
    "{{NL_STATEMENT}}\n\n"
    "The registered Lean header is:\n"
    "{{REGISTERED_HEADER}}\n\n"
    "{{COMMON_SUFFIX}}\n"
)

STEPFUN_USER_TEMPLATE = (
    "Please autoformalize the following problem in Lean 4 with a header. Use the following theorem "
    "names: {{THEOREM_NAME}}.\n\n"
    "{{NL_STATEMENT}}\n\n"
    "Your code should start with:\n"
    "```Lean4\n"
    "{{REGISTERED_HEADER}}\n"
    "```\n\n"
    "{{COMMON_SUFFIX}}\n"
)

EXPERT_SYSTEM_PROMPT = "You are an expert in mathematics and Lean 4."


class InvocationError(RuntimeError):
    """A provider could not return a usable raw candidate."""


@dataclass(frozen=True, slots=True)
class DecodingConfig:
    """The sampled decoding fields retained from the LF-021 model profiles."""

    max_new_tokens: int
    temperature: float
    top_p: float
    top_k: int | None
    seed: int
    repetition_penalty: float
    eos_token_id: int | tuple[int, ...] | None
    pad_token_id: int | None

    def __post_init__(self) -> None:
        if self.max_new_tokens < 1:
            raise ValueError("max_new_tokens must be positive")
        if self.temperature <= 0 or not 0 < self.top_p <= 1:
            raise ValueError("temperature and top_p must be in their sampled-decoding ranges")
        if self.top_k is not None and self.top_k < 0:
            raise ValueError("top_k cannot be negative")
        if self.seed < 0 or self.repetition_penalty <= 0:
            raise ValueError("seed and repetition_penalty are invalid")

    def generation_kwargs(self) -> dict[str, object]:
        values: dict[str, object] = {
            "max_new_tokens": self.max_new_tokens,
            "do_sample": True,
            "num_beams": 1,
            "num_return_sequences": 1,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
        }
        if self.top_k is not None:
            values["top_k"] = self.top_k
        if self.eos_token_id is not None:
            values["eos_token_id"] = self.eos_token_id
        if self.pad_token_id is not None:
            values["pad_token_id"] = self.pad_token_id
        return values


@dataclass(frozen=True, slots=True)
class LocalModelProfile:
    family: ModelFamily
    repo_id: str
    revision: str
    user_template: str
    decoding: DecodingConfig


LOCAL_MODEL_PROFILES: dict[ModelFamily, LocalModelProfile] = {
    "goedel": LocalModelProfile(
        family="goedel",
        repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
        revision="fe2d362d899601abe79d7d5e95eaa7fe9883a0cb",
        user_template=GOEDEL_USER_TEMPLATE,
        decoding=DecodingConfig(
            max_new_tokens=16_384,
            temperature=0.9,
            top_p=0.95,
            top_k=20,
            seed=30,
            repetition_penalty=1.0,
            eos_token_id=(151645, 151643),
            pad_token_id=151643,
        ),
    ),
    "kimina": LocalModelProfile(
        family="kimina",
        repo_id="AI-MO/Kimina-Autoformalizer-7B",
        revision="ddd47cb477d93b3ca990468e1c0d5ad6b60973dd",
        user_template=KIMINA_USER_TEMPLATE,
        decoding=DecodingConfig(
            max_new_tokens=2048,
            temperature=0.6,
            top_p=0.95,
            top_k=20,
            seed=0,
            repetition_penalty=1.1,
            eos_token_id=None,
            pad_token_id=None,
        ),
    ),
    "stepfun": LocalModelProfile(
        family="stepfun",
        repo_id="stepfun-ai/StepFun-Formalizer-7B",
        revision="fb0dc612761fecd64ebbc489c2a3417e9ea01968",
        user_template=STEPFUN_USER_TEMPLATE,
        decoding=DecodingConfig(
            max_new_tokens=16_384,
            temperature=0.6,
            top_p=0.95,
            top_k=None,
            seed=0,
            repetition_penalty=1.0,
            eos_token_id=151643,
            pad_token_id=151643,
        ),
    ),
}

_MODEL_ALIASES: dict[str, ModelFamily] = {
    "goedel": "goedel",
    "goedel_formalizer_v2_8b": "goedel",
    "goedel-lm/goedel-formalizer-v2-8b": "goedel",
    "kimina": "kimina",
    "kimina_autoformalizer_7b": "kimina",
    "ai-mo/kimina-autoformalizer-7b": "kimina",
    "stepfun": "stepfun",
    "stepfun_formalizer_7b": "stepfun",
    "stepfun-ai/stepfun-formalizer-7b": "stepfun",
}


def resolve_local_profile(model: str) -> LocalModelProfile:
    """Resolve a stable family alias or exact model repository ID."""

    family = _MODEL_ALIASES.get(model.strip().casefold())
    if family is None:
        supported = ", ".join(profile.repo_id for profile in LOCAL_MODEL_PROFILES.values())
        raise ValueError(f"unsupported local autoformalizer {model!r}; expected one of {supported}")
    return LOCAL_MODEL_PROFILES[family]


def theorem_name_for(problem_id: str) -> str:
    """Create a stable Lean identifier without assuming a source-specific ID grammar."""

    bare = problem_id.rsplit("::", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9_']+", "_", bare).strip("_").lower() or "problem"
    digest = hashlib.sha256(problem_id.encode("utf-8")).hexdigest()[:10]
    return f"collect2_{slug}_{digest}"


@dataclass(frozen=True, slots=True)
class AutoformalizationTask:
    """One task after its declaration name has been fixed."""

    problem_id: str
    nl_statement: str
    header: str
    theorem_name: str

    def __post_init__(self) -> None:
        for name, value in (
            ("problem_id", self.problem_id),
            ("nl_statement", self.nl_statement),
            ("theorem_name", self.theorem_name),
        ):
            if not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be nonempty and contain no NUL bytes")
        if "\x00" in self.header:
            raise ValueError("header contains a NUL byte")

    @classmethod
    def named_from_problem(
        cls,
        *,
        problem_id: str,
        nl_statement: str,
        header: str,
    ) -> AutoformalizationTask:
        return cls(
            problem_id=problem_id,
            nl_statement=nl_statement,
            header=header,
            theorem_name=theorem_name_for(problem_id),
        )


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """A local pinned checkpoint or an external Codex/Lemex CLI."""

    kind: ProviderKind
    model: str
    cli: CliName | None = None
    revision: str | None = None
    device: str = "cuda:0"
    timeout_seconds: int = 240
    cwd: Path = Path("/tmp")
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "high"
    decoding: DecodingConfig | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("provider model must be nonempty")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if self.kind == "local_hf":
            resolve_local_profile(self.model)
            if self.cli is not None:
                raise ValueError("local_hf providers cannot set cli")
        elif self.cli not in ("codex", "lemex"):
            raise ValueError("cli providers require cli='codex' or cli='lemex'")

    @property
    def provider_label(self) -> str:
        return "local_hf" if self.kind == "local_hf" else cast(CliName, self.cli)

    @property
    def resolved_revision(self) -> str | None:
        if self.kind == "cli":
            return self.revision
        return self.revision or resolve_local_profile(self.model).revision


@dataclass(frozen=True, slots=True)
class RenderedAutoformalizationTask:
    task: AutoformalizationTask
    prompt: str
    user_prompt: str
    family: ModelFamily | None

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InvocationResult:
    provider: str
    model: str
    prompt: str
    raw_output: str
    candidate_output: str

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def _render_user_prompt(task: AutoformalizationTask, profile: LocalModelProfile) -> str:
    rendered = (
        profile.user_template.replace("{{THEOREM_NAME}}", task.theorem_name)
        .replace("{{NL_STATEMENT}}", task.nl_statement)
        .replace("{{REGISTERED_HEADER}}", task.header.rstrip())
        .replace("{{COMMON_SUFFIX}}", COMMON_FINAL_FENCE)
    )
    unresolved = ("THEOREM_NAME", "NL_STATEMENT", "REGISTERED_HEADER", "COMMON_SUFFIX")
    if any("{{" + name + "}}" in rendered for name in unresolved):
        raise ValueError("rendered local prompt contains an unresolved placeholder")
    return rendered


def _wrap_local_prompt(user_prompt: str, family: ModelFamily) -> str:
    """Apply the exact pinned tokenizer rendering observed by LF-021."""

    if family == "goedel":
        return f"<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"
    if family == "kimina":
        return (
            f"<|im_start|>system\n{EXPERT_SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"
        )
    return (
        f"<｜begin▁of▁sentence｜>{EXPERT_SYSTEM_PROMPT}<｜User｜>{user_prompt}"  # noqa: RUF001
        "<｜Assistant｜><think>"  # noqa: RUF001
    )


def _render_cli_prompt(task: AutoformalizationTask) -> str:
    return f"""You are an expert in mathematics and Lean 4.

Autoformalize the natural-language problem below as one Lean 4 theorem or lemma declaration.
Use exactly the declaration name `{task.theorem_name}` and the registered header. You may include
`:= by sorry`; do not return a proof attempt or an alternative declaration.

REGISTERED HEADER:
```lean4
{task.header.rstrip()}
```

NATURAL-LANGUAGE STATEMENT:
{task.nl_statement}

After any reasoning, the LAST line must be one strict JSON object with no trailing text:
{{"candidate_lean": "one theorem or lemma declaration as a JSON string"}}"""


def render_task(
    task: AutoformalizationTask, provider: ProviderSpec
) -> RenderedAutoformalizationTask:
    """Render the complete provider input, including local special-token wrappers."""

    if provider.kind == "local_hf":
        profile = resolve_local_profile(provider.model)
        user_prompt = _render_user_prompt(task, profile)
        return RenderedAutoformalizationTask(
            task=task,
            prompt=_wrap_local_prompt(user_prompt, profile.family),
            user_prompt=user_prompt,
            family=profile.family,
        )
    prompt = _render_cli_prompt(task)
    return RenderedAutoformalizationTask(
        task=task,
        prompt=prompt,
        user_prompt=prompt,
        family=None,
    )


def _iter_json_objects(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    cleaned = text.replace("```json", "\n").replace("```JSON", "\n").replace("```", "\n")
    found: list[dict[str, object]] = []
    for source in (text, cleaned):
        for start in reversed([index for index, char in enumerate(source) if char == "{"]):
            try:
                value, _ = decoder.raw_decode(source[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                found.append(cast(dict[str, object], value))
        if found:
            break
    return found


def _candidate_from_json(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    mapping = cast(dict[object, object], value)
    for key in ("candidate_lean", "lean_statement", "statement"):
        candidate = mapping.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    for key in ("result", "item", "message", "output", "response", "text"):
        nested = mapping.get(key)
        candidate = _candidate_from_json(nested)
        if candidate is not None:
            return candidate
        if isinstance(nested, str):
            for document in _iter_json_objects(nested):
                candidate = _candidate_from_json(document)
                if candidate is not None:
                    return candidate
    return None


def parse_cli_json_tail(stdout: str) -> str:
    """Return the last schema-bearing JSON candidate from noisy CLI stdout."""

    for document in _iter_json_objects(stdout):
        candidate = _candidate_from_json(document)
        if candidate is not None:
            return candidate
    raise InvocationError("CLI stdout contains no JSON object with candidate_lean")


class _Factory(Protocol):
    def from_pretrained(self, pretrained_model_name_or_path: str, **kwargs: object) -> object: ...


class _TransformersModule(Protocol):
    AutoTokenizer: _Factory
    AutoModelForCausalLM: _Factory


class _CudaModule(Protocol):
    def is_available(self) -> bool: ...

    def device_count(self) -> int: ...

    def manual_seed_all(self, seed: int) -> None: ...

    def empty_cache(self) -> None: ...


class _TorchModule(Protocol):
    cuda: _CudaModule

    def manual_seed(self, seed: int) -> object: ...

    def inference_mode(self) -> AbstractContextManager[object]: ...


class _Movable(Protocol):
    def to(self, device: str) -> object: ...


class _Model(_Movable, Protocol):
    def eval(self) -> object: ...

    def generate(self, **kwargs: object) -> object: ...


class _Tokenizer(Protocol):
    def __call__(self, text: str, **kwargs: object) -> object: ...

    def decode(self, token_ids: object, **kwargs: object) -> str: ...


class _Shaped(Protocol):
    @property
    def shape(self) -> Sequence[int]: ...


class _Indexable(Protocol):
    def __getitem__(self, key: object) -> object: ...


def _move(value: object, device: str) -> object:
    return cast(_Movable, value).to(device)


def _token_count(value: object) -> int:
    shape = cast(_Shaped, value).shape
    if not shape:
        raise InvocationError("token tensor has no token dimension")
    return int(shape[-1])


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]
ModuleImporter = Callable[[str], object]


class InvocationSession:
    """Reuse a loaded local checkpoint across a batch; CLI calls remain one-shot."""

    def __init__(
        self,
        provider: ProviderSpec,
        *,
        subprocess_runner: SubprocessRunner = subprocess.run,
        module_importer: ModuleImporter = importlib.import_module,
    ) -> None:
        self.provider = provider
        self._subprocess_runner = subprocess_runner
        self._module_importer = module_importer
        self._tokenizer: object | None = None
        self._model: object | None = None
        self._torch: _TorchModule | None = None

    def __enter__(self) -> InvocationSession:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._model is not None:
            try:
                cast(_Movable, self._model).to("cpu")
            finally:
                self._model = None
                self._tokenizer = None
                gc.collect()
                if self._torch is not None and self._torch.cuda.is_available():
                    self._torch.cuda.empty_cache()

    def run(self, task: RenderedAutoformalizationTask) -> InvocationResult:
        if self.provider.kind == "cli":
            return self._run_cli(task)
        return self._run_local(task)

    def _load_local(self) -> tuple[_Tokenizer, _Model, _TorchModule]:
        if self._tokenizer is not None and self._model is not None and self._torch is not None:
            return (
                cast(_Tokenizer, self._tokenizer),
                cast(_Model, self._model),
                self._torch,
            )
        try:
            transformers = cast(_TransformersModule, self._module_importer("transformers"))
            torch = cast(_TorchModule, self._module_importer("torch"))
        except (ImportError, ModuleNotFoundError) as exc:
            raise InvocationError(
                "local_hf requires the local-inference dependency group (torch and transformers)"
            ) from exc
        if self.provider.device.startswith("cuda") and (
            not torch.cuda.is_available() or torch.cuda.device_count() < 1
        ):
            raise InvocationError(f"requested device {self.provider.device!r} is unavailable")
        profile = resolve_local_profile(self.provider.model)
        common: dict[str, object] = {
            "revision": cast(str, self.provider.resolved_revision),
            "local_files_only": True,
            "trust_remote_code": False,
        }
        try:
            tokenizer = transformers.AutoTokenizer.from_pretrained(profile.repo_id, **common)
            model = transformers.AutoModelForCausalLM.from_pretrained(
                profile.repo_id,
                **common,
                dtype="auto",
            )
            cast(_Model, model).to(self.provider.device)
            cast(_Model, model).eval()
        except Exception as exc:
            raise InvocationError(
                f"could not load exact local artifacts {profile.repo_id}@"
                f"{self.provider.resolved_revision}"
            ) from exc
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch
        return cast(_Tokenizer, tokenizer), cast(_Model, model), torch

    def _run_local(self, task: RenderedAutoformalizationTask) -> InvocationResult:
        tokenizer, model, torch = self._load_local()
        profile = resolve_local_profile(self.provider.model)
        decoding = self.provider.decoding or profile.decoding
        try:
            encoded = tokenizer(task.prompt, return_tensors="pt")
            if not isinstance(encoded, Mapping):
                raise TypeError("tokenizer output is not a mapping")
            device_inputs = {
                str(name): _move(value, self.provider.device) for name, value in encoded.items()
            }
            if "input_ids" not in device_inputs:
                raise KeyError("tokenizer output has no input_ids")
            prompt_tokens = _token_count(device_inputs["input_ids"])
            torch.manual_seed(decoding.seed)
            torch.cuda.manual_seed_all(decoding.seed)
            with torch.inference_mode():
                generated = model.generate(**device_inputs, **decoding.generation_kwargs())
            first = cast(_Indexable, generated)[0]
            completion = cast(_Indexable, first)[slice(prompt_tokens, None)]
            raw_text = tokenizer.decode(completion, skip_special_tokens=True)
        except InvocationError:
            raise
        except Exception as exc:
            raise InvocationError("local_hf generation failed") from exc
        if not isinstance(raw_text, str):
            raise InvocationError("local_hf tokenizer decode returned no text")
        return InvocationResult(
            provider=self.provider.provider_label,
            model=profile.repo_id,
            prompt=task.prompt,
            raw_output=raw_text,
            candidate_output=raw_text,
        )

    def _run_cli(self, task: RenderedAutoformalizationTask) -> InvocationResult:
        cli = cast(CliName, self.provider.cli)
        command = [
            cli,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            f'model_reasoning_effort="{self.provider.reasoning_effort}"',
            "--skip-git-repo-check",
            "-m",
            self.provider.model,
            task.prompt,
        ]
        try:
            completed = self._subprocess_runner(
                command,
                cwd=self.provider.cwd,
                capture_output=True,
                text=True,
                timeout=self.provider.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise InvocationError(
                f"{cli} timed out after {self.provider.timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise InvocationError(f"could not execute {cli}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-1000:] or "no stderr"
            raise InvocationError(f"{cli} exited {completed.returncode}: {detail}")
        candidate = parse_cli_json_tail(completed.stdout)
        return InvocationResult(
            provider=cli,
            model=self.provider.model,
            prompt=task.prompt,
            raw_output=completed.stdout,
            candidate_output=candidate,
        )


def invoke(
    task: RenderedAutoformalizationTask,
    provider: ProviderSpec,
) -> InvocationResult:
    """Run one task, releasing a local checkpoint immediately afterward."""

    with InvocationSession(provider) as session:
        return session.run(task)


__all__ = [
    "COMMON_FINAL_FENCE",
    "EXPERT_SYSTEM_PROMPT",
    "GOEDEL_USER_TEMPLATE",
    "KIMINA_USER_TEMPLATE",
    "LOCAL_MODEL_PROFILES",
    "STEPFUN_USER_TEMPLATE",
    "AutoformalizationTask",
    "DecodingConfig",
    "InvocationError",
    "InvocationResult",
    "InvocationSession",
    "LocalModelProfile",
    "ModelFamily",
    "ProviderSpec",
    "RenderedAutoformalizationTask",
    "invoke",
    "parse_cli_json_tail",
    "render_task",
    "resolve_local_profile",
    "theorem_name_for",
]

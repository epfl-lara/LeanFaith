"""Pinned, restart-safe one-source ReForm-8B generation."""

from __future__ import annotations

import gc
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.sft2b.durable import immutable_write, read_model, write_json, write_model
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateRecord,
    CandidateSlot,
    FormalizerAttempt,
    FormalizerLineage,
    SourceRecord,
    stable_id,
)

_START = "<<<LEAN_PROPOSITION>>>"
_END = "<<<END_LEAN_PROPOSITION>>>"
_DECLARATION = re.compile(
    r"(?m)^\s*(?:import|universe|namespace|section|open|attribute|theorem|lemma|"
    r"example|axiom|def|instance|class|structure|inductive)\b"
)
_PROOF_TOKEN = re.compile(r"(?:\bsorry\b|\bby\b|:=)", flags=re.IGNORECASE)
_FINAL_LEAN_FENCE = re.compile(r"```(?:lean4|lean)\s*\n(?P<code>.*?)```\s*$", flags=re.DOTALL)
_THEOREM_HEAD = re.compile(r"^theorem\s+(?:«[^»\n]+»|[^\s:(){}\[\]]+)")
_PLACEHOLDER_PROOF = re.compile(r"^(?:by\s+sorry|sorry)$", flags=re.DOTALL)
_TYPE_STAR = re.compile(r"\b(Type|Sort)\s*\*")


class FormalizerError(RuntimeError):
    """Raised when model/config integrity or generation execution fails."""


@dataclass(frozen=True, slots=True)
class SlotSpec:
    slot: CandidateSlot
    seed: int


@dataclass(frozen=True, slots=True)
class FormalizerConfig:
    model_id: str
    model_revision: str
    origin: CandidateOrigin
    staging_subdir: str
    snapshot_path: Path
    snapshot_files: dict[str, str]
    prompt_path: Path
    prompt_sha256: str
    extraction_contract: str
    slots: tuple[SlotSpec, SlotSpec, SlotSpec, SlotSpec]
    decoding: dict[str, object]
    decoding_sha256: str
    dtype: str
    device: str
    trust_remote_code: bool
    local_files_only: bool
    staging_root: Path
    owner_session: str
    config_sha256: str
    snapshot_binding_sha256: str


@dataclass(frozen=True, slots=True)
class FormalizerRunResult:
    run_id: str
    root: Path
    attempts: tuple[FormalizerAttempt, FormalizerAttempt, FormalizerAttempt, FormalizerAttempt]
    candidates: tuple[CandidateRecord, ...]
    model_calls: int
    model_loaded: bool


def load_formalizer_config(repo_root: Path, path: Path) -> FormalizerConfig:
    """Verify the complete local snapshot and generation contract before staging."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {
        "sft2b_reform_8b_smoke_v1",
        "sft2b_reform_8b_smoke_v2",
    }:
        raise FormalizerError("unsupported ReForm smoke config")
    extraction_contract = str(value.get("extraction_contract", "marked_proposition_v1"))
    if extraction_contract not in {"marked_proposition_v1", "final_theorem_signature_v1"}:
        raise FormalizerError("unsupported ReForm extraction contract")
    if (
        value.get("schema_version") == "sft2b_reform_8b_smoke_v1"
        and extraction_contract != "marked_proposition_v1"
    ):
        raise FormalizerError("v1 ReForm config must use the marked-proposition contract")
    snapshot = Path(str(value["snapshot_path"]))
    revision = str(value["model_revision"])
    if snapshot.name != revision or not snapshot.is_dir():
        raise FormalizerError("ReForm snapshot path/revision mismatch")
    expected_files = cast(dict[str, str], value["snapshot_files"])
    actual_names = {item.name for item in snapshot.iterdir() if item.is_file()}
    if actual_names != set(expected_files):
        raise FormalizerError("ReForm snapshot file set drifted")
    for name, expected in expected_files.items():
        if hash_file(snapshot / name) != expected:
            raise FormalizerError(f"ReForm snapshot hash mismatch: {name}")
    model_config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    if not isinstance(model_config, dict) or model_config.get("architectures") != [
        "Qwen3ForCausalLM"
    ]:
        raise FormalizerError("unexpected ReForm architecture")
    if model_config.get("torch_dtype") != "bfloat16":
        raise FormalizerError("unexpected ReForm checkpoint dtype")
    prompt_path = repo_root / str(value["prompt_path"])
    prompt_hash = hash_file(prompt_path)
    if prompt_hash != value["prompt_sha256"]:
        raise FormalizerError("ReForm prompt hash mismatch")
    prompt = prompt_path.read_text(encoding="utf-8")
    if prompt.count("{{NL}}") != 1:
        raise FormalizerError("ReForm prompt source placeholder drifted")
    if extraction_contract == "marked_proposition_v1":
        if prompt.count(_START) != 1 or prompt.count(_END) != 1:
            raise FormalizerError("ReForm prompt markers drifted")
    elif _START in prompt or _END in prompt or "theorem sft2b_candidate" not in prompt:
        raise FormalizerError("ReForm theorem-signature prompt contract drifted")
    raw_slots = cast(list[dict[str, object]], value["candidate_slots"])
    if any(
        not isinstance(item.get("slot"), str)
        or not isinstance(item.get("seed"), int)
        or isinstance(item.get("seed"), bool)
        for item in raw_slots
    ):
        raise FormalizerError("ReForm slots have invalid names or seeds")
    slots = tuple(
        SlotSpec(slot=CandidateSlot(cast(str, item["slot"])), seed=cast(int, item["seed"]))
        for item in raw_slots
    )
    if len(slots) != 4 or {item.slot for item in slots} != set(CandidateSlot):
        raise FormalizerError("ReForm config must define exactly four distinct slots")
    if len({item.seed for item in slots}) != 4 or any(item.seed < 0 for item in slots):
        raise FormalizerError("ReForm slot seeds must be distinct nonnegative integers")
    decoding = cast(dict[str, object], value["decoding"])
    expected_decoding = {
        "do_sample": True,
        "max_new_tokens": 4096,
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
        "repetition_penalty": 1.0,
        "use_cache": True,
    }
    if decoding != expected_decoding:
        raise FormalizerError("ReForm smoke decoding drifted")
    if value.get("dtype") != "bfloat16" or value.get("device") != "cuda:0":
        raise FormalizerError("ReForm smoke must use local CUDA bfloat16")
    if value.get("trust_remote_code") is not False or value.get("local_files_only") is not True:
        raise FormalizerError("ReForm smoke must be offline with remote code disabled")
    return FormalizerConfig(
        model_id=str(value["model_id"]),
        model_revision=revision,
        origin=CandidateOrigin.REFORM_8B,
        staging_subdir="reform_8b",
        snapshot_path=snapshot,
        snapshot_files=expected_files,
        prompt_path=prompt_path,
        prompt_sha256=prompt_hash,
        extraction_contract=extraction_contract,
        slots=slots,
        decoding=decoding,
        decoding_sha256=hash_canonical(decoding),
        dtype=str(value["dtype"]),
        device=str(value["device"]),
        trust_remote_code=bool(value["trust_remote_code"]),
        local_files_only=bool(value["local_files_only"]),
        staging_root=Path(str(value["staging_root"])),
        owner_session=str(value["owner_session"]),
        config_sha256=hash_file(path),
        snapshot_binding_sha256=hash_canonical(expected_files),
    )


def render_generation_prompt(config: FormalizerConfig, source: SourceRecord) -> str:
    template = config.prompt_path.read_text(encoding="utf-8")
    prompt = template.replace("{{NL}}", source.nl_statement)
    if "{{NL}}" in prompt or source.reference_proposition in prompt:
        raise FormalizerError("formalizer prompt is incomplete or leaks the trusted reference")
    return prompt


def extract_proposition(raw_output: str) -> tuple[str | None, str | None]:
    """Extract one tagged, proof-free proposition or return a stable failure detail."""

    if raw_output.count(_START) != 1 or raw_output.count(_END) != 1:
        return None, "expected exactly one proposition marker pair"
    before, tail = raw_output.split(_START, 1)
    proposition, after = tail.split(_END, 1)
    del before
    proposition = proposition.strip()
    if after.strip():
        return None, "non-whitespace content follows the final proposition marker"
    if not proposition or len(proposition) > 20_000:
        return None, "marked proposition is empty or exceeds 20000 characters"
    if proposition == "<one closed Lean proposition term>":
        return None, "model copied the prompt placeholder"
    if "```" in proposition or _DECLARATION.search(proposition):
        return None, "marked region contains a command, declaration, or Markdown fence"
    if _PROOF_TOKEN.search(proposition):
        return None, "marked region contains a proof/tactic token"
    if "[anonymous]" in proposition or "⋯" in proposition:
        return None, "marked region contains a forbidden placeholder"
    return proposition, None


def _top_level_signature_separators(source: str) -> tuple[int, int] | None:
    """Find the result colon and proof assignment in one theorem declaration."""

    depths = {"(": 0, "[": 0, "{": 0}
    closers = {")": "(", "]": "[", "}": "{"}
    in_string = False
    escaped = False
    result_colon: int | None = None
    index = 0
    while index < len(source):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char in depths:
            depths[char] += 1
        elif char in closers:
            opener = closers[char]
            depths[opener] -= 1
            if depths[opener] < 0:
                return None
        elif not any(depths.values()) and source.startswith(":=", index):
            if result_colon is None:
                return None
            return result_colon, index
        elif not any(depths.values()) and char == ":" and result_colon is None:
            result_colon = index
        index += 1
    return None


def _generalize_type_stars(source: str) -> str:
    """Make theorem-style ``Type*`` universe generalization explicit in a term."""

    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        replacement = f"{match.group(1)} sft2b_u_{counter}"
        counter += 1
        return replacement

    return _TYPE_STAR.sub(replace, source)


def extract_final_theorem_signature(raw_output: str) -> tuple[str | None, str | None]:
    """Extract a proof-free proposition from ReForm's native final declaration.

    Reflection is ignored. The final envelope must contain one theorem and only
    a placeholder proof. The declaration name and placeholder proof are
    discarded; neither is ever sent to Lean. ``Type*`` binders are generalized
    to deterministic explicit universe names because the destination is a term,
    not a declaration command.
    """

    suffix = raw_output.rsplit("</think>", 1)[-1]
    fences = list(_FINAL_LEAN_FENCE.finditer(suffix))
    if len(fences) != 1 or suffix.count("```") != 2:
        return None, "expected exactly one final Lean fence after reflection"
    code = fences[0].group("code").strip()
    lines = code.splitlines()
    if lines and lines[0].strip() == "import Mathlib":
        code = "\n".join(lines[1:]).strip()
    if "import " in code:
        return None, "final fence contains an unapproved import"
    head = _THEOREM_HEAD.match(code)
    if head is None:
        return None, "final fence does not start with one theorem declaration"
    remainder = code[head.end() :].strip()
    separators = _top_level_signature_separators(remainder)
    if separators is None:
        return None, "theorem signature lacks one top-level result colon and assignment"
    result_colon, assignment = separators
    binders = remainder[:result_colon].strip()
    result = remainder[result_colon + 1 : assignment].strip()
    proof = remainder[assignment + 2 :].strip()
    if not result or _PLACEHOLDER_PROOF.fullmatch(proof) is None:
        return None, "theorem must end in a placeholder proof with a nonempty result"
    proposition = f"∀ {binders}, {result}" if binders else result
    proposition = _generalize_type_stars(proposition).strip()
    if (
        not proposition
        or len(proposition) > 20_000
        or "```" in proposition
        or _DECLARATION.search(proposition)
        or _PROOF_TOKEN.search(proposition)
        or "[anonymous]" in proposition
        or "⋯" in proposition
    ):
        return None, "extracted theorem signature is not a proof-free proposition term"
    return proposition, None


def extract_candidate(
    raw_output: str, *, extraction_contract: str
) -> tuple[str | None, str | None]:
    if extraction_contract == "marked_proposition_v1":
        return extract_proposition(raw_output)
    if extraction_contract == "final_theorem_signature_v1":
        return extract_final_theorem_signature(raw_output)
    raise FormalizerError("unsupported extraction contract at generation time")


def _attempt_id(config: FormalizerConfig, source: SourceRecord, slot: SlotSpec) -> str:
    return stable_id(
        "sft2b_formalizer_attempt",
        {
            "source_id": source.source_id,
            "slot": slot.slot,
            "model_id": config.model_id,
            "model_revision": config.model_revision,
            "snapshot_binding_sha256": config.snapshot_binding_sha256,
            "prompt_sha256": config.prompt_sha256,
            "decoding_sha256": config.decoding_sha256,
            "seed": slot.seed,
        },
    )


def _lineage(config: FormalizerConfig, slot: SlotSpec, attempt_id: str) -> FormalizerLineage:
    return FormalizerLineage(
        origin=config.origin,
        provider="local_transformers",
        model_id=config.model_id,
        model_revision=config.model_revision,
        prompt_sha256=config.prompt_sha256,
        decoding_sha256=config.decoding_sha256,
        seed=slot.seed,
        upstream_call_id=attempt_id,
        upstream_generation_config_sha256=config.config_sha256,
    )


def _candidate(
    config: FormalizerConfig,
    source: SourceRecord,
    slot: SlotSpec,
    attempt_id: str,
    proposition: str,
) -> CandidateRecord:
    lineage = _lineage(config, slot, attempt_id)
    signature_hash = sha256_hex(proposition.encode("utf-8"))
    candidate_id = stable_id(
        "sft2b_candidate",
        {
            "source_id": source.source_id,
            "slot": slot.slot,
            "signature_sha256": signature_hash,
            "source_context_id": source.compile_context.source_context_id,
            "lineage": lineage.model_dump(mode="json"),
        },
    )
    return CandidateRecord(
        candidate_id=candidate_id,
        source_id=source.source_id,
        slot=slot.slot,
        raw_proof_free_signature=proposition,
        signature_sha256=signature_hash,
        source_context_id=source.compile_context.source_context_id,
        lineage=lineage,
    )


def _load_cached_slot(
    root: Path, slot: SlotSpec
) -> tuple[FormalizerAttempt, CandidateRecord | None] | None:
    cell = root / "slots" / slot.slot.value
    attempt_path = cell / "attempt.json"
    if not attempt_path.exists():
        if cell.exists() and any(cell.iterdir()):
            raise FormalizerError(f"partial formalizer slot cache: {slot.slot.value}")
        return None
    attempt = read_model(attempt_path, FormalizerAttempt)
    raw_path = Path(attempt.raw_output_path)
    if hash_file(raw_path) != attempt.raw_output_sha256:
        raise FormalizerError(f"formalizer raw output drift: {slot.slot.value}")
    candidate_path = cell / "candidate.json"
    if attempt.extraction_status == "candidate":
        candidate = read_model(candidate_path, CandidateRecord)
        if candidate.candidate_id != attempt.candidate_id or candidate.slot != slot.slot:
            raise FormalizerError(f"formalizer candidate identity drift: {slot.slot.value}")
        return attempt, candidate
    if candidate_path.exists():
        raise FormalizerError(f"invalid formalizer slot has candidate: {slot.slot.value}")
    return attempt, None


def _four_attempts(
    values: list[FormalizerAttempt] | tuple[FormalizerAttempt, ...],
) -> tuple[FormalizerAttempt, FormalizerAttempt, FormalizerAttempt, FormalizerAttempt]:
    if len(values) != 4:
        raise FormalizerError("formalizer run does not contain exactly four attempts")
    return values[0], values[1], values[2], values[3]


def _generate_one(
    *,
    config: FormalizerConfig,
    source: SourceRecord,
    slot: SlotSpec,
    root: Path,
    tokenizer: Any,
    model: Any,
    torch: Any,
    transformers_version: str,
) -> tuple[FormalizerAttempt, CandidateRecord | None]:
    prompt = render_generation_prompt(config, source)
    encoded = tokenizer(prompt, return_tensors="pt")
    inputs = {str(key): value.to(config.device) for key, value in encoded.items()}
    prompt_tokens = int(inputs["input_ids"].shape[-1])
    torch.manual_seed(slot.seed)
    torch.cuda.manual_seed_all(slot.seed)
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    started = time.monotonic()
    with torch.inference_mode():
        generated = model.generate(**inputs, **config.decoding)
    torch.cuda.synchronize()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    completion = generated[0, prompt_tokens:]
    completion_tokens = int(completion.shape[-1])
    raw_output = cast(str, tokenizer.decode(completion, skip_special_tokens=True))
    if not isinstance(raw_output, str):
        raise FormalizerError("ReForm tokenizer returned a non-string completion")
    attempt_id = _attempt_id(config, source, slot)
    cell = root / "slots" / slot.slot.value
    raw_path = cell / "raw_output.txt"
    immutable_write(raw_path, raw_output.encode("utf-8"))
    proposition, failure = extract_candidate(
        raw_output, extraction_contract=config.extraction_contract
    )
    candidate = (
        _candidate(config, source, slot, attempt_id, proposition)
        if proposition is not None
        else None
    )
    attempt = FormalizerAttempt(
        attempt_id=attempt_id,
        source_id=source.source_id,
        slot=slot.slot,
        lineage=_lineage(config, slot, attempt_id),
        prompt_input_sha256=sha256_hex(prompt.encode("utf-8")),
        raw_output_path=str(raw_path),
        raw_output_sha256=hash_file(raw_path),
        extraction_status="candidate" if candidate is not None else "invalid",
        candidate_id=candidate.candidate_id if candidate is not None else None,
        failure_class=None if candidate is not None else "formalizer_output_contract",
        failure_detail=None if candidate is not None else cast(str, failure),
        elapsed_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        peak_cuda_allocated_bytes=int(torch.cuda.max_memory_allocated()),
        peak_cuda_reserved_bytes=int(torch.cuda.max_memory_reserved()),
        torch_version=str(torch.__version__),
        transformers_version=transformers_version,
    )
    if candidate is not None:
        write_model(cell / "candidate.json", candidate)
    write_model(cell / "attempt.json", attempt)
    return attempt, candidate


def run_formalizer_generation(
    repo_root: Path,
    *,
    config: FormalizerConfig,
    source: SourceRecord,
) -> FormalizerRunResult:
    """Generate four deterministic slots with one model load and cache-safe resume."""

    run_id = stable_id(
        "sft2b_generation_run",
        {
            "source_id": source.source_id,
            "formalizer_config_sha256": config.config_sha256,
            "snapshot_binding_sha256": config.snapshot_binding_sha256,
        },
    )
    root = config.staging_root / "generation" / config.staging_subdir / run_id
    cached = [_load_cached_slot(root, slot) for slot in config.slots]
    missing = [slot for slot, result in zip(config.slots, cached, strict=True) if result is None]
    if not missing:
        complete = cast(
            list[tuple[FormalizerAttempt, CandidateRecord | None]],
            cached,
        )
        return FormalizerRunResult(
            run_id=run_id,
            root=root,
            attempts=_four_attempts([item[0] for item in complete]),
            candidates=tuple(item[1] for item in complete if item[1] is not None),
            model_calls=0,
            model_loaded=False,
        )
    # No staging write occurs before all snapshot bytes have been verified.
    root.mkdir(parents=True, exist_ok=True)
    write_json(
        root / "input.json",
        {
            "schema_version": "sft2b_formalizer_input_v1",
            "run_id": run_id,
            "source": source.model_dump(mode="json"),
            "formalizer_config_sha256": config.config_sha256,
            "snapshot_binding_sha256": config.snapshot_binding_sha256,
            "prompt": render_generation_prompt(config, source),
        },
    )
    reservation = claim_resources(
        task="SFT2B",
        lean_workers=0,
        lean_rss_gib=0.0,
        gpu=True,
        pid=os.getpid(),
        owner_session=config.owner_session,
        worktree=repo_root,
    )
    model: Any = None
    tokenizer: Any = None
    torch: Any = None
    try:
        import torch as torch_module
        import transformers

        torch = torch_module
        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            raise FormalizerError("configured CUDA device is unavailable")
        tokenizer = transformers.AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            config.snapshot_path,
            local_files_only=config.local_files_only,
            trust_remote_code=config.trust_remote_code,
        )
        model = transformers.AutoModelForCausalLM.from_pretrained(
            config.snapshot_path,
            local_files_only=config.local_files_only,
            trust_remote_code=config.trust_remote_code,
            dtype=torch.bfloat16,
            device_map={"": config.device},
            low_cpu_mem_usage=True,
        )
        model.eval()
        results: dict[CandidateSlot, tuple[FormalizerAttempt, CandidateRecord | None]] = {
            slot.slot: result
            for slot, result in zip(config.slots, cached, strict=True)
            if result is not None
        }
        calls = 0
        for slot in missing:
            results[slot.slot] = _generate_one(
                config=config,
                source=source,
                slot=slot,
                root=root,
                tokenizer=tokenizer,
                model=model,
                torch=torch,
                transformers_version=str(transformers.__version__),
            )
            calls += 1
    finally:
        if model is not None:
            model.to("cpu")
        del model
        del tokenizer
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
        released = release_resources(task="SFT2B")
        if released != reservation:
            raise FormalizerError("released GPU claim differs from acquired claim")
    ordered = [results[slot.slot] for slot in config.slots]
    return FormalizerRunResult(
        run_id=run_id,
        root=root,
        attempts=_four_attempts([item[0] for item in ordered]),
        candidates=tuple(item[1] for item in ordered if item[1] is not None),
        model_calls=calls,
        model_loaded=True,
    )


def run_reform_8b_generation(
    repo_root: Path,
    *,
    config_path: Path,
    source: SourceRecord,
) -> FormalizerRunResult:
    """Load a fully pinned ReForm-8B config and run its four slots."""

    config = load_formalizer_config(repo_root, config_path)
    return run_formalizer_generation(repo_root, config=config, source=source)

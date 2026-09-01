"""Pinned, restart-safe vLLM generation for the ReForm-32B bounded gates."""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.durable import (
    atomic_write,
    immutable_write,
    read_model,
    write_json,
    write_model,
)
from leanfaith.sft2b.formalizer import (
    FormalizerConfig,
    SlotSpec,
    extract_candidate,
    render_generation_prompt,
)
from leanfaith.sft2b.reform_32b import load_reform_32b_config
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    CandidateSlot,
    FormalizerAttempt,
    FormalizerLineage,
    Sha256,
    SourceRecord,
    StableId,
    stable_id,
)


class VllmBackendError(RuntimeError):
    """Raised when the vLLM contract, response, or durable cache is incoherent."""


class PortableReleaseConfig(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    release_id: Annotated[str, Field(min_length=1)]
    release_manifest_path: Annotated[str, Field(min_length=1)]
    release_manifest_sha256: Sha256
    smoke_sources_path: Annotated[str, Field(min_length=1)]
    smoke_sources_sha256: Sha256
    probe_sources_path: Annotated[str, Field(min_length=1)]
    probe_sources_sha256: Sha256


class VllmProfile(StrictModel):
    profile_id: Annotated[str, Field(min_length=1)]
    visible_devices: tuple[Annotated[int, Field(ge=0)], ...]
    data_parallel_size: Annotated[int, Field(ge=1)]
    tensor_parallel_size: Annotated[int, Field(ge=1)]
    port: Annotated[int, Field(ge=1, le=65535)]
    max_model_len: Annotated[int, Field(ge=4096)]
    max_num_seqs: Annotated[int, Field(ge=1)]
    gpu_memory_utilization: Annotated[float, Field(gt=0.0, le=1.0)]
    prefix_caching: bool
    concurrency: Annotated[int, Field(ge=1)]
    source_ids: tuple[StableId, ...]
    slots: tuple[CandidateSlot, ...]

    @model_validator(mode="after")
    def validate_topology(self) -> VllmProfile:
        world_size = self.data_parallel_size * self.tensor_parallel_size
        if len(self.visible_devices) != world_size:
            raise ValueError("vLLM profile device count differs from DP*TP")
        if len(set(self.visible_devices)) != len(self.visible_devices):
            raise ValueError("vLLM profile contains duplicate visible devices")
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("vLLM profile source IDs must be nonempty and unique")
        if not self.slots or len(set(self.slots)) != len(self.slots):
            raise ValueError("vLLM profile slots must be nonempty and unique")
        expected_requests = len(self.source_ids) * len(self.slots)
        if self.concurrency > expected_requests:
            raise ValueError("vLLM concurrency exceeds the profile request count")
        return self


class VllmLaunchConfig(StrictModel):
    host: Literal["127.0.0.1"]
    load_format: Literal["safetensors"]
    safetensors_load_strategy: Literal["eager"]
    distributed_executor_backend: Literal["mp"]
    data_parallel_backend: Literal["mp"]
    enable_request_id_headers: Literal[True]
    disable_uvicorn_access_log: Literal[True]


class VllmBackendSpec(StrictModel):
    schema_version: Literal["sft2b_reform_32b_vllm_v1"]
    status: Literal["bounded_probe_authorized"]
    placement_config_path: Annotated[str, Field(min_length=1)]
    placement_config_sha256: Sha256
    model_id: Literal["GuoxinChen/ReForm-32B"]
    model_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    snapshot_binding_sha256: Sha256
    checkpoint_dtype: Literal["bfloat16"]
    quantization: None
    trust_remote_code: Literal[False]
    served_model_name: Annotated[str, Field(min_length=1)]
    provider: Literal["local_vllm_openai"]
    api_route: Literal["/v1/completions"]
    request_timeout_seconds: Annotated[int, Field(ge=1)]
    telemetry_interval_seconds: Annotated[float, Field(gt=0.0)]
    portable_release: PortableReleaseConfig
    source_prompt_tokens: dict[StableId, Annotated[int, Field(ge=1)]]
    profiles: dict[str, VllmProfile]
    launch: VllmLaunchConfig
    staging_root: Path
    owner_session: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def validate_profiles(self) -> VllmBackendSpec:
        if set(self.profiles) != {"smoke_dp1_tp2", "probe_dp4_tp2_c8"}:
            raise ValueError("unexpected vLLM profile set")
        all_source_ids = {item for profile in self.profiles.values() for item in profile.source_ids}
        if all_source_ids != set(self.source_prompt_tokens):
            raise ValueError("vLLM prompt-token map differs from the profile source set")
        for profile in self.profiles.values():
            required = max(self.source_prompt_tokens[item] for item in profile.source_ids) + 4096
            if profile.max_model_len != required:
                raise ValueError("vLLM profile does not use the smallest validated model length")
        return self


class VllmRequestMetrics(StrictModel):
    schema_version: Literal["sft2b_vllm_request_metrics_v1"] = "sft2b_vllm_request_metrics_v1"
    request_key: Sha256
    attempt_id: StableId
    source_id: StableId
    slot: CandidateSlot
    profile_id: Annotated[str, Field(min_length=1)]
    endpoint_url: Annotated[str, Field(min_length=1)]
    request_payload_sha256: Sha256
    response_id: Annotated[str, Field(min_length=1)]
    response_request_id: Annotated[str, Field(min_length=1)] | None = None
    raw_response_path: Annotated[str, Field(min_length=1)]
    raw_response_sha256: Sha256
    raw_output_path: Annotated[str, Field(min_length=1)]
    raw_output_sha256: Sha256
    elapsed_ms: Annotated[int, Field(ge=0)]
    time_to_first_token_ms: Annotated[int, Field(ge=0)]
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    finish_reason: Annotated[str, Field(min_length=1)]
    http_status: Annotated[int, Field(ge=100, le=599)]
    vllm_version: Annotated[str, Field(min_length=1)]


class VllmRequestTerminal(StrictModel):
    schema_version: Literal["sft2b_vllm_request_terminal_v1"] = "sft2b_vllm_request_terminal_v1"
    request_key: Sha256
    attempt: FormalizerAttempt
    candidate: CandidateRecord | None
    metrics: VllmRequestMetrics
    artifact_sha256: dict[str, Sha256]

    @model_validator(mode="after")
    def validate_terminal(self) -> VllmRequestTerminal:
        if self.request_key != self.metrics.request_key:
            raise ValueError("vLLM terminal request key mismatch")
        if self.attempt.attempt_id != self.metrics.attempt_id:
            raise ValueError("vLLM terminal attempt identity mismatch")
        if self.attempt.extraction_status == "candidate":
            if self.candidate is None or self.candidate.candidate_id != self.attempt.candidate_id:
                raise ValueError("vLLM candidate terminal is incomplete")
        elif self.candidate is not None:
            raise ValueError("vLLM invalid terminal unexpectedly contains a candidate")
        return self


@dataclass(frozen=True, slots=True)
class LoadedVllmBackend:
    spec: VllmBackendSpec
    config_path: Path
    config_sha256: str
    placement: FormalizerConfig
    release_root: Path


@dataclass(frozen=True, slots=True)
class StreamCompletion:
    raw_response: bytes
    output_text: str
    response_id: str
    response_request_id: str | None
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    elapsed_ms: int
    time_to_first_token_ms: int
    http_status: int


CompletionTransport = Callable[[str, dict[str, object], str, float], StreamCompletion]


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    source: SourceRecord
    slot: SlotSpec
    prompt: str
    profile: VllmProfile
    request_key: str
    attempt_id: str
    endpoint_url: str
    payload: dict[str, object]
    cell: Path


@dataclass(frozen=True, slots=True)
class VllmProfileResult:
    run_id: str
    root: Path
    terminals: tuple[VllmRequestTerminal, ...]
    model_calls: int
    cache_hits: int
    wall_time_ms: int

    @property
    def total_completion_tokens(self) -> int:
        return sum(item.metrics.completion_tokens for item in self.terminals)


@dataclass(frozen=True, slots=True)
class VllmCacheInspection:
    """Read-only profile cache state used to avoid unnecessary GPU startup."""

    run_id: str
    root: Path
    request_count: int
    cached_terminals: tuple[VllmRequestTerminal, ...]
    missing_request_keys: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_request_keys


def load_vllm_spec(repo_root: Path, config_path: Path) -> VllmBackendSpec:
    """Load the additive backend config and bind it to the frozen placement config."""

    try:
        raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        spec = VllmBackendSpec.model_validate(raw)
    except Exception as exc:
        raise VllmBackendError(f"invalid vLLM backend config: {exc}") from exc
    placement_path = repo_root / spec.placement_config_path
    if hash_file(placement_path) != spec.placement_config_sha256:
        raise VllmBackendError("vLLM placement config hash mismatch")
    return spec


def verify_vllm_dependencies(
    repo_root: Path,
    *,
    config_path: Path,
    snapshot_path: Path,
    release_root: Path,
) -> LoadedVllmBackend:
    """Verify model bytes and portable evidence before any endpoint request or cache write."""

    spec = load_vllm_spec(repo_root, config_path)
    placement, _ = load_reform_32b_config(
        repo_root,
        placement_path=repo_root / spec.placement_config_path,
        snapshot_path=snapshot_path,
    )
    if placement.model_id != spec.model_id or placement.model_revision != spec.model_revision:
        raise VllmBackendError("vLLM model identity differs from the placement contract")
    if placement.snapshot_binding_sha256 != spec.snapshot_binding_sha256:
        raise VllmBackendError("vLLM snapshot binding differs from the placement contract")
    if spec.quantization is not None or placement.dtype != "bfloat16":
        raise VllmBackendError("vLLM backend must use the unquantized BF16 checkpoint")
    release = spec.portable_release
    allowed_release_names = {release.revision, f"remote_{release.revision}"}
    if release_root.name not in allowed_release_names or not release_root.is_dir():
        raise VllmBackendError("portable release root/revision mismatch")
    manifest_path = release_root / release.release_manifest_path
    if hash_file(manifest_path) != release.release_manifest_sha256:
        raise VllmBackendError("portable release manifest hash mismatch")
    manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise VllmBackendError("portable release manifest is not an object")
    if (
        manifest.get("release_id") != release.release_id
        or manifest.get("repo_id") != release.repo_id
    ):
        raise VllmBackendError("portable release identity mismatch")
    _verify_release_file(
        release_root, manifest, release.smoke_sources_path, release.smoke_sources_sha256
    )
    _verify_release_file(
        release_root, manifest, release.probe_sources_path, release.probe_sources_sha256
    )
    return LoadedVllmBackend(
        spec=spec,
        config_path=config_path,
        config_sha256=hash_file(config_path),
        placement=placement,
        release_root=release_root,
    )


def _verify_release_file(
    root: Path, manifest: dict[object, object], relative: str, expected_hash: str
) -> None:
    path = root / relative
    if hash_file(path) != expected_hash:
        raise VllmBackendError(f"portable release file hash mismatch: {relative}")
    payload_files = manifest.get("payload_files")
    if not isinstance(payload_files, dict):
        raise VllmBackendError("portable release lacks payload file bindings")
    binding = payload_files.get(relative)
    if not isinstance(binding, dict) or binding.get("sha256") != expected_hash:
        raise VllmBackendError(f"portable release manifest binding mismatch: {relative}")


def load_profile_sources(backend: LoadedVllmBackend, profile_name: str) -> tuple[SourceRecord, ...]:
    """Load only source rows checksum-bound by the verified portable release."""

    try:
        profile = backend.spec.profiles[profile_name]
    except KeyError as exc:
        raise VllmBackendError(f"unknown vLLM profile: {profile_name}") from exc
    release = backend.spec.portable_release
    relative = (
        release.smoke_sources_path
        if profile_name == "smoke_dp1_tp2"
        else release.probe_sources_path
    )
    rows: list[SourceRecord] = []
    with (backend.release_root / relative).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(SourceRecord.model_validate_json(line))
            except Exception as exc:
                raise VllmBackendError(f"invalid portable source row {relative}:{number}") from exc
    by_id = {row.source_id: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != set(profile.source_ids):
        raise VllmBackendError("portable source IDs differ from the vLLM profile")
    return tuple(by_id[source_id] for source_id in profile.source_ids)


def visible_devices_csv(profile: VllmProfile) -> str:
    return ",".join(str(item) for item in profile.visible_devices)


def build_vllm_serve_command(backend: LoadedVllmBackend, *, profile_name: str) -> tuple[str, ...]:
    """Return the complete local-only serve command for an already verified snapshot."""

    try:
        profile = backend.spec.profiles[profile_name]
    except KeyError as exc:
        raise VllmBackendError(f"unknown vLLM profile: {profile_name}") from exc
    launch = backend.spec.launch
    prefix_flag = (
        "--enable-prefix-caching" if profile.prefix_caching else "--no-enable-prefix-caching"
    )
    snapshot = str(backend.placement.snapshot_path)
    return (
        "vllm",
        "serve",
        snapshot,
        "--served-model-name",
        backend.spec.served_model_name,
        "--dtype",
        backend.spec.checkpoint_dtype,
        "--no-trust-remote-code",
        "--generation-config",
        snapshot,
        "--load-format",
        launch.load_format,
        "--safetensors-load-strategy",
        launch.safetensors_load_strategy,
        "--distributed-executor-backend",
        launch.distributed_executor_backend,
        "--data-parallel-backend",
        launch.data_parallel_backend,
        "--data-parallel-size",
        str(profile.data_parallel_size),
        "--tensor-parallel-size",
        str(profile.tensor_parallel_size),
        "--max-model-len",
        str(profile.max_model_len),
        "--max-num-seqs",
        str(profile.max_num_seqs),
        "--gpu-memory-utilization",
        str(profile.gpu_memory_utilization),
        prefix_flag,
        "--enable-request-id-headers",
        "--disable-uvicorn-access-log",
        "--seed",
        "0",
        "--host",
        launch.host,
        "--port",
        str(profile.port),
    )


def profile_endpoint(backend: LoadedVllmBackend, profile_name: str) -> str:
    profile = backend.spec.profiles[profile_name]
    return f"http://{backend.spec.launch.host}:{profile.port}{backend.spec.api_route}"


def _request_payload(
    backend: LoadedVllmBackend, source: SourceRecord, slot: SlotSpec
) -> dict[str, object]:
    decoding = backend.placement.decoding
    prompt = render_generation_prompt(backend.placement, source)
    return {
        "model": backend.spec.served_model_name,
        "prompt": prompt,
        "n": 1,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": decoding["max_new_tokens"],
        "temperature": decoding["temperature"],
        "top_k": decoding["top_k"],
        "top_p": decoding["top_p"],
        "repetition_penalty": decoding["repetition_penalty"],
        "seed": slot.seed,
    }


def _prepare_requests(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
) -> tuple[str, Path, tuple[PreparedRequest, ...]]:
    profile = backend.spec.profiles[profile_name]
    run_id = stable_id(
        "sft2b_vllm_run",
        {
            "profile_id": profile.profile_id,
            "backend_config_sha256": backend.config_sha256,
            "model_revision": backend.placement.model_revision,
            "snapshot_binding_sha256": backend.placement.snapshot_binding_sha256,
            "source_ids": profile.source_ids,
            "slots": profile.slots,
        },
    )
    root = backend.spec.staging_root / "generation/vllm" / profile.profile_id / run_id
    slots_by_id = {slot.slot: slot for slot in backend.placement.slots}
    prepared: list[PreparedRequest] = []
    for source in sources:
        prompt = render_generation_prompt(backend.placement, source)
        if source.reference_proposition in prompt:
            raise VllmBackendError("vLLM prompt leaks the trusted reference")
        for slot_id in profile.slots:
            slot = slots_by_id[slot_id]
            payload = _request_payload(backend, source, slot)
            request_key = hash_canonical(
                {
                    "schema_version": "sft2b_vllm_request_key_v1",
                    "profile_id": profile.profile_id,
                    "backend_config_sha256": backend.config_sha256,
                    "source_id": source.source_id,
                    "slot": slot.slot,
                    "seed": slot.seed,
                    "model_revision": backend.placement.model_revision,
                    "snapshot_binding_sha256": backend.placement.snapshot_binding_sha256,
                    "prompt_input_sha256": sha256_hex(prompt.encode("utf-8")),
                    "prompt_template_sha256": backend.placement.prompt_sha256,
                    "decoding_sha256": backend.placement.decoding_sha256,
                }
            )
            attempt_id = stable_id(
                "sft2b_formalizer_attempt",
                {
                    "request_key": request_key,
                    "provider": backend.spec.provider,
                },
            )
            prepared.append(
                PreparedRequest(
                    source=source,
                    slot=slot,
                    prompt=prompt,
                    profile=profile,
                    request_key=request_key,
                    attempt_id=attempt_id,
                    endpoint_url=endpoint_url,
                    payload=payload,
                    cell=root / "requests" / request_key,
                )
            )
    return run_id, root, tuple(prepared)


def _lineage(backend: LoadedVllmBackend, request: PreparedRequest) -> FormalizerLineage:
    return FormalizerLineage(
        origin=backend.placement.origin,
        provider=backend.spec.provider,
        model_id=backend.placement.model_id,
        model_revision=backend.placement.model_revision,
        prompt_sha256=backend.placement.prompt_sha256,
        decoding_sha256=backend.placement.decoding_sha256,
        seed=request.slot.seed,
        upstream_call_id=request.attempt_id,
        upstream_generation_config_sha256=backend.config_sha256,
    )


def _candidate(
    backend: LoadedVllmBackend,
    request: PreparedRequest,
    proposition: str,
) -> CandidateRecord:
    lineage = _lineage(backend, request)
    signature_hash = sha256_hex(proposition.encode("utf-8"))
    candidate_id = stable_id(
        "sft2b_candidate",
        {
            "source_id": request.source.source_id,
            "slot": request.slot.slot,
            "signature_sha256": signature_hash,
            "source_context_id": request.source.compile_context.source_context_id,
            "lineage": lineage.model_dump(mode="json"),
        },
    )
    return CandidateRecord(
        candidate_id=candidate_id,
        source_id=request.source.source_id,
        slot=request.slot.slot,
        raw_proof_free_signature=proposition,
        signature_sha256=signature_hash,
        source_context_id=request.source.compile_context.source_context_id,
        lineage=lineage,
    )


def _cache_terminal(request: PreparedRequest) -> VllmRequestTerminal | None:
    terminal_path = request.cell / "terminal.json"
    started_path = request.cell / "request_started.json"
    if not terminal_path.exists():
        if started_path.exists():
            raise VllmBackendError(
                f"ambiguous in-flight vLLM request; refusing duplicate: {request.request_key}"
            )
        return None
    terminal = read_model(terminal_path, VllmRequestTerminal)
    if terminal.request_key != request.request_key:
        raise VllmBackendError("vLLM cache key mismatch")
    expected_paths = {
        "request": request.cell / "request.json",
        "request_started": started_path,
        "raw_response": Path(terminal.metrics.raw_response_path),
        "raw_output": Path(terminal.metrics.raw_output_path),
        "attempt": request.cell / "attempt.json",
        "metrics": request.cell / "metrics.json",
    }
    if terminal.candidate is not None:
        expected_paths["candidate"] = request.cell / "candidate.json"
    if set(expected_paths) != set(terminal.artifact_sha256):
        raise VllmBackendError("vLLM cache artifact set mismatch")
    for name, path in expected_paths.items():
        if not path.is_file() or hash_file(path) != terminal.artifact_sha256[name]:
            raise VllmBackendError(f"vLLM cached artifact drift: {name}")
    cached_attempt = read_model(expected_paths["attempt"], FormalizerAttempt)
    if cached_attempt != terminal.attempt:
        raise VllmBackendError("vLLM cached attempt differs from terminal")
    if terminal.candidate is not None:
        cached_candidate = read_model(expected_paths["candidate"], CandidateRecord)
        if cached_candidate != terminal.candidate:
            raise VllmBackendError("vLLM cached candidate differs from terminal")
    return terminal


def _write_request_artifact(request: PreparedRequest) -> None:
    write_json(
        request.cell / "request.json",
        {
            "schema_version": "sft2b_vllm_request_v1",
            "request_key": request.request_key,
            "attempt_id": request.attempt_id,
            "profile_id": request.profile.profile_id,
            "source_id": request.source.source_id,
            "slot": request.slot.slot,
            "seed": request.slot.seed,
            "endpoint_url": request.endpoint_url,
            "payload": request.payload,
        },
    )


def _execute_request(
    backend: LoadedVllmBackend,
    request: PreparedRequest,
    transport: CompletionTransport,
) -> VllmRequestTerminal:
    _write_request_artifact(request)
    write_json(
        request.cell / "request_started.json",
        {
            "schema_version": "sft2b_vllm_request_started_v1",
            "request_key": request.request_key,
        },
    )
    completion = transport(
        request.endpoint_url,
        request.payload,
        request.request_key,
        float(backend.spec.request_timeout_seconds),
    )
    expected_prompt_tokens = backend.spec.source_prompt_tokens[request.source.source_id]
    if completion.prompt_tokens != expected_prompt_tokens:
        raise VllmBackendError(
            "vLLM prompt token accounting differs from the frozen tokenizer measurement"
        )
    raw_response_path = request.cell / "raw_response.sse"
    raw_output_path = request.cell / "raw_output.txt"
    immutable_write(raw_response_path, completion.raw_response)
    immutable_write(raw_output_path, completion.output_text.encode("utf-8"))
    proposition, failure = extract_candidate(
        completion.output_text,
        extraction_contract=backend.placement.extraction_contract,
    )
    candidate = _candidate(backend, request, proposition) if proposition is not None else None
    attempt = FormalizerAttempt(
        attempt_id=request.attempt_id,
        source_id=request.source.source_id,
        slot=request.slot.slot,
        lineage=_lineage(backend, request),
        prompt_input_sha256=sha256_hex(request.prompt.encode("utf-8")),
        raw_output_path=str(raw_output_path),
        raw_output_sha256=hash_file(raw_output_path),
        extraction_status="candidate" if candidate is not None else "invalid",
        candidate_id=candidate.candidate_id if candidate is not None else None,
        failure_class=None if candidate is not None else "formalizer_output_contract",
        failure_detail=None if candidate is not None else cast(str, failure),
        elapsed_ms=completion.elapsed_ms,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        peak_cuda_allocated_bytes=0,
        peak_cuda_reserved_bytes=0,
        torch_version=metadata.version("torch"),
        transformers_version=metadata.version("transformers"),
    )
    metrics = VllmRequestMetrics(
        request_key=request.request_key,
        attempt_id=request.attempt_id,
        source_id=request.source.source_id,
        slot=request.slot.slot,
        profile_id=request.profile.profile_id,
        endpoint_url=request.endpoint_url,
        request_payload_sha256=hash_canonical(request.payload),
        response_id=completion.response_id,
        response_request_id=completion.response_request_id,
        raw_response_path=str(raw_response_path),
        raw_response_sha256=hash_file(raw_response_path),
        raw_output_path=str(raw_output_path),
        raw_output_sha256=hash_file(raw_output_path),
        elapsed_ms=completion.elapsed_ms,
        time_to_first_token_ms=completion.time_to_first_token_ms,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        finish_reason=completion.finish_reason,
        http_status=completion.http_status,
        vllm_version=metadata.version("vllm"),
    )
    write_model(request.cell / "attempt.json", attempt)
    write_model(request.cell / "metrics.json", metrics)
    if candidate is not None:
        write_model(request.cell / "candidate.json", candidate)
    artifact_paths = {
        "request": request.cell / "request.json",
        "request_started": request.cell / "request_started.json",
        "raw_response": raw_response_path,
        "raw_output": raw_output_path,
        "attempt": request.cell / "attempt.json",
        "metrics": request.cell / "metrics.json",
    }
    if candidate is not None:
        artifact_paths["candidate"] = request.cell / "candidate.json"
    terminal = VllmRequestTerminal(
        request_key=request.request_key,
        attempt=attempt,
        candidate=candidate,
        metrics=metrics,
        artifact_sha256={name: hash_file(path) for name, path in artifact_paths.items()},
    )
    write_model(request.cell / "terminal.json", terminal)
    return terminal


def _append_journal(root: Path, terminal: VllmRequestTerminal) -> None:
    path = root / "journal/requests.jsonl"
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            rows: list[dict[str, object]] = []
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    value: object = json.loads(line)
                    if not isinstance(value, dict):
                        raise VllmBackendError("vLLM journal contains a non-object")
                    rows.append(cast(dict[str, object], value))
            by_key = {str(row.get("request_key")): row for row in rows}
            terminal_path = Path(terminal.metrics.raw_output_path).parent / "terminal.json"
            terminal_hash = hash_file(terminal_path)
            prior = by_key.get(terminal.request_key)
            if prior is not None:
                if prior.get("terminal_sha256") != terminal_hash:
                    raise VllmBackendError("vLLM journal terminal hash drift")
                return
            row = {
                "schema_version": "sft2b_vllm_journal_event_v1",
                "sequence": len(rows),
                "request_key": terminal.request_key,
                "attempt_id": terminal.attempt.attempt_id,
                "source_id": terminal.attempt.source_id,
                "slot": terminal.attempt.slot,
                "terminal_path": str(terminal_path),
                "terminal_sha256": terminal_hash,
            }
            with path.open("ab") as handle:
                handle.write(canonical_json_bytes(row) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_profile_sources(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
) -> None:
    try:
        profile = backend.spec.profiles[profile_name]
    except KeyError as exc:
        raise VllmBackendError(f"unknown vLLM profile: {profile_name}") from exc
    source_ids = tuple(source.source_id for source in sources)
    if len(set(source_ids)) != len(source_ids) or source_ids != profile.source_ids:
        raise VllmBackendError("supplied source order/IDs differ from the vLLM profile")


def inspect_vllm_sources_cache(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
) -> VllmCacheInspection:
    """Validate completed cells without sending a request or starting a model server."""

    _validate_profile_sources(backend, profile_name=profile_name, sources=sources)
    run_id, root, requests = _prepare_requests(
        backend,
        profile_name=profile_name,
        sources=sources,
        endpoint_url=endpoint_url,
    )
    cached: list[VllmRequestTerminal] = []
    missing: list[str] = []
    for request in requests:
        terminal = _cache_terminal(request)
        if terminal is None:
            missing.append(request.request_key)
        else:
            cached.append(terminal)
    return VllmCacheInspection(
        run_id=run_id,
        root=root,
        request_count=len(requests),
        cached_terminals=tuple(cached),
        missing_request_keys=tuple(missing),
    )


def run_vllm_sources(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
    transport: CompletionTransport | None = None,
) -> VllmProfileResult:
    """Run a supplied frozen source set, reusing only verified immutable terminals."""

    _validate_profile_sources(backend, profile_name=profile_name, sources=sources)
    profile = backend.spec.profiles[profile_name]
    run_id, root, requests = _prepare_requests(
        backend,
        profile_name=profile_name,
        sources=sources,
        endpoint_url=endpoint_url,
    )
    if len(requests) != len(profile.source_ids) * len(profile.slots):
        raise VllmBackendError("vLLM prepared request count mismatch")
    selected_transport = transport or stream_openai_completion
    started = time.monotonic()
    terminals: dict[str, VllmRequestTerminal] = {}
    missing: list[PreparedRequest] = []
    for request in requests:
        cached = _cache_terminal(request)
        if cached is None:
            missing.append(request)
        else:
            terminals[request.request_key] = cached
            _append_journal(root, cached)
    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=profile.concurrency) as pool:
            futures = {
                pool.submit(_execute_request, backend, request, selected_transport): request
                for request in missing
            }
            for future in concurrent.futures.as_completed(futures):
                request = futures[future]
                try:
                    terminal = future.result()
                except Exception as exc:
                    identity = f"{request.source.source_id}/{request.slot.slot}"
                    raise VllmBackendError(f"vLLM request failed for {identity}: {exc}") from exc
                terminals[request.request_key] = terminal
                _append_journal(root, terminal)
    ordered = tuple(terminals[request.request_key] for request in requests)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return VllmProfileResult(
        run_id=run_id,
        root=root,
        terminals=ordered,
        model_calls=len(missing),
        cache_hits=len(requests) - len(missing),
        wall_time_ms=elapsed_ms,
    )


def run_vllm_profile(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    endpoint_url: str,
    transport: CompletionTransport | None = None,
) -> VllmProfileResult:
    """Run one release-backed profile, reusing verified immutable terminals."""

    return run_vllm_sources(
        backend,
        profile_name=profile_name,
        sources=load_profile_sources(backend, profile_name),
        endpoint_url=endpoint_url,
        transport=transport,
    )


def stream_openai_completion(
    endpoint_url: str,
    payload: dict[str, object],
    request_key: str,
    timeout_seconds: float,
) -> StreamCompletion:
    """Send one raw-prompt completion and preserve the exact SSE response bytes."""

    request = urllib.request.Request(
        endpoint_url,
        data=canonical_json_bytes(payload),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Request-Id": request_key,
        },
        method="POST",
    )
    started = time.monotonic()
    raw = bytearray()
    text_parts: list[str] = []
    response_id: str | None = None
    response_request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    first_token_ms: int | None = None
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            response_request_id = response.headers.get("X-Request-Id")
            while True:
                line = response.readline()
                if not line:
                    break
                raw.extend(line)
                stripped = line.strip()
                if not stripped.startswith(b"data:"):
                    continue
                data = stripped[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    event: object = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise VllmBackendError("vLLM returned malformed SSE JSON") from exc
                if not isinstance(event, dict):
                    raise VllmBackendError("vLLM returned a non-object SSE event")
                event_map = cast(dict[str, object], event)
                event_id = event_map.get("id")
                if isinstance(event_id, str) and event_id:
                    if response_id is not None and response_id != event_id:
                        raise VllmBackendError("vLLM response ID changed during the stream")
                    response_id = event_id
                choices = event_map.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise VllmBackendError("vLLM stream choice is not an object")
                        choice_map = cast(dict[str, object], choice)
                        part = choice_map.get("text")
                        if isinstance(part, str) and part:
                            if first_token_ms is None:
                                first_token_ms = round((time.monotonic() - started) * 1000)
                            text_parts.append(part)
                        reason = choice_map.get("finish_reason")
                        if isinstance(reason, str) and reason:
                            finish_reason = reason
                usage = event_map.get("usage")
                if isinstance(usage, dict):
                    prompt_value = usage.get("prompt_tokens")
                    completion_value = usage.get("completion_tokens")
                    if isinstance(prompt_value, int) and isinstance(completion_value, int):
                        prompt_tokens = prompt_value
                        completion_tokens = completion_value
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise VllmBackendError(f"vLLM HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise VllmBackendError(f"vLLM endpoint unavailable: {exc.reason}") from exc
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if status != 200:
        raise VllmBackendError(f"unexpected vLLM HTTP status: {status}")
    if response_id is None or not response_id:
        raise VllmBackendError("vLLM response lacks an ID")
    if prompt_tokens is None or completion_tokens is None:
        raise VllmBackendError("vLLM response lacks final token usage")
    if first_token_ms is None:
        raise VllmBackendError("vLLM response contains no completion token")
    if finish_reason is None:
        raise VllmBackendError("vLLM response lacks a finish reason")
    return StreamCompletion(
        raw_response=bytes(raw),
        output_text="".join(text_parts),
        response_id=response_id,
        response_request_id=response_request_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        elapsed_ms=elapsed_ms,
        time_to_first_token_ms=first_token_ms,
        http_status=status,
    )


def verify_openai_server(endpoint_url: str, *, served_model_name: str) -> dict[str, object]:
    """Require a healthy endpoint exposing exactly the configured served-model identity."""

    suffix = "/v1/completions"
    if not endpoint_url.endswith(suffix):
        raise VllmBackendError("vLLM endpoint URL does not end in /v1/completions")
    base = endpoint_url[: -len(suffix)]
    observations: dict[str, object] = {}
    for label, url in (("health", f"{base}/health"), ("models", f"{base}/v1/models")):
        try:
            with urllib.request.urlopen(url, timeout=10.0) as response:
                payload = response.read()
                status = int(response.status)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise VllmBackendError(f"vLLM {label} endpoint is unavailable: {exc}") from exc
        if status != 200:
            raise VllmBackendError(f"vLLM {label} returned HTTP {status}")
        observations[f"{label}_status"] = status
        if label == "models":
            try:
                model_response: object = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise VllmBackendError("vLLM model list is not JSON") from exc
            if not isinstance(model_response, dict) or not isinstance(
                model_response.get("data"), list
            ):
                raise VllmBackendError("vLLM model list has an unexpected schema")
            model_ids = {
                str(item.get("id"))
                for item in model_response["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if model_ids != {served_model_name}:
                raise VllmBackendError("vLLM served-model identity mismatch")
            observations["model_ids"] = sorted(model_ids)
    return observations


def summarize_profile(result: VllmProfileResult) -> dict[str, object]:
    """Return a compact deterministic-shape result for receipts and handoff."""

    latencies = [item.metrics.elapsed_ms for item in result.terminals]
    ttfts = [item.metrics.time_to_first_token_ms for item in result.terminals]
    invalid = [item for item in result.terminals if item.candidate is None]
    wall_seconds = result.wall_time_ms / 1000
    return {
        "schema_version": "sft2b_vllm_profile_summary_v1",
        "run_id": result.run_id,
        "root": str(result.root),
        "requests": len(result.terminals),
        "model_calls": result.model_calls,
        "cache_hits": result.cache_hits,
        "wall_time_ms": result.wall_time_ms,
        "prompt_tokens": sum(item.metrics.prompt_tokens for item in result.terminals),
        "completion_tokens": result.total_completion_tokens,
        "aggregate_output_tokens_per_second": (
            result.total_completion_tokens / wall_seconds if wall_seconds > 0 else 0.0
        ),
        "requests_per_second": len(result.terminals) / wall_seconds if wall_seconds > 0 else 0.0,
        "latency_ms": {"min": min(latencies), "max": max(latencies), "values": latencies},
        "time_to_first_token_ms": {"min": min(ttfts), "max": max(ttfts), "values": ttfts},
        "extracted_candidates": len(result.terminals) - len(invalid),
        "formalizer_invalid": len(invalid),
        "request_keys": [item.request_key for item in result.terminals],
        "response_ids": [item.metrics.response_id for item in result.terminals],
        "raw_response_sha256": [item.metrics.raw_response_sha256 for item in result.terminals],
        "raw_output_sha256": [item.metrics.raw_output_sha256 for item in result.terminals],
        "candidate_ids": [
            item.candidate.candidate_id if item.candidate is not None else None
            for item in result.terminals
        ],
    }


def write_profile_receipt(path: Path, values: Iterable[dict[str, object]]) -> None:
    """Write a caller-owned receipt atomically without weakening immutable request caches."""

    payload = b"".join(canonical_json_bytes(value) + b"\n" for value in values)
    atomic_write(path, payload)

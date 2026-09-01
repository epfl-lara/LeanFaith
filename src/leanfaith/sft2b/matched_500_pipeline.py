"""One-command matched-500 ReForm-32B generation and private Hub publication.

The command is intentionally fail-closed.  It verifies the immutable Hub input,
the local SFT2B/REPR contracts, the complete model snapshot, tokenizer replay,
the eight-GPU topology, and the durable request cache before it starts vLLM.
It performs generation only: no Lean process, judge, semantic label, core row,
training job, or public release is created here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.sft2b.durable import immutable_write, write_json, write_jsonl
from leanfaith.sft2b.formalizer import FormalizerConfig
from leanfaith.sft2b.pilot_source_freeze import FreezeResult, verify_bundle
from leanfaith.sft2b.pins import verify_runtime_pins
from leanfaith.sft2b.reform_32b import load_reform_32b_config
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    CandidateSlot,
    FormalizerAttempt,
    FormalizerInvalidAttemptView,
    Sha256,
    SourceRecord,
)
from leanfaith.sft2b.vllm_backend import (
    LoadedVllmBackend,
    PortableReleaseConfig,
    VllmBackendError,
    VllmBackendSpec,
    VllmLaunchConfig,
    VllmProfile,
    VllmProfileResult,
    VllmRequestMetrics,
    VllmRequestTerminal,
    build_vllm_serve_command,
    inspect_vllm_sources_cache,
    profile_endpoint,
    run_vllm_sources,
    verify_openai_server,
    visible_devices_csv,
)
from leanfaith.sft2b.vllm_telemetry import TelemetryMonitor

CONFIG_SCHEMA = "sft2b_reform_32b_matched_500_pipeline_v1"
PROFILE_NAME = "probe_dp4_tp2_c8"
OUTPUT_NAMES = (
    "SHA256SUMS",
    "candidates.jsonl",
    "formalizer_attempts.jsonl",
    "formalizer_invalid_attempts.jsonl",
    "generation_manifest.json",
    "raw_generations.jsonl",
    "request_metrics.jsonl",
    "request_terminals.jsonl",
    "requests_journal.jsonl",
    "telemetry.jsonl",
    "vllm_server.log",
)


class Matched500PipelineError(RuntimeError):
    """A frozen input, runtime, generation, or publication contract drifted."""


class InputSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    path: Annotated[str, Field(min_length=1)]
    files: dict[str, Sha256]
    source_config_path: Annotated[str, Field(min_length=1)]
    source_config_sha256: Sha256
    expected_rows: Literal[500]
    source_mix: dict[str, Annotated[int, Field(ge=1)]]
    maximum_prompt_tokens: Annotated[int, Field(ge=1)]
    required_max_model_len: Annotated[int, Field(ge=4096)]


class ModelSpec(StrictModel):
    model_id: Literal["GuoxinChen/ReForm-32B"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    placement_config_path: Annotated[str, Field(min_length=1)]
    placement_config_sha256: Sha256
    snapshot_binding_sha256: Sha256
    checkpoint_dtype: Literal["bfloat16"]
    quantization: None
    trust_remote_code: Literal[False]
    served_model_name: Annotated[str, Field(min_length=1)]


class GenerationSpec(StrictModel):
    profile_id: Annotated[str, Field(min_length=1)]
    visible_devices: tuple[Annotated[int, Field(ge=0)], ...]
    data_parallel_size: Literal[4]
    tensor_parallel_size: Literal[2]
    port: Annotated[int, Field(ge=1, le=65535)]
    max_model_len: Annotated[int, Field(ge=4096)]
    max_num_seqs: Annotated[int, Field(ge=1)]
    gpu_memory_utilization: Annotated[float, Field(gt=0.0, le=1.0)]
    prefix_caching: Literal[False]
    concurrency: Annotated[int, Field(ge=1)]
    slots: tuple[CandidateSlot, ...]
    seeds: tuple[Annotated[int, Field(ge=0)], ...]
    expected_requests: Literal[2000]
    request_timeout_seconds: Annotated[int, Field(ge=1)]
    telemetry_interval_seconds: Annotated[float, Field(gt=0.0)]
    server_startup_timeout_seconds: Annotated[int, Field(ge=60)]
    server_shutdown_timeout_seconds: Annotated[int, Field(ge=10)]


class RuntimeSpec(StrictModel):
    work_root: Path
    reservation_root: Path
    vllm_version: Annotated[str, Field(min_length=1)]
    minimum_gpu_memory_mib: Annotated[int, Field(ge=1)]
    allowed_gpu_name_fragments: tuple[Annotated[str, Field(min_length=1)], ...]


class PublicationSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    path_prefix: Annotated[str, Field(min_length=1)]
    commit_message: Annotated[str, Field(min_length=1)]


class ReprSpec(StrictModel):
    freeze_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    spec_sha256: Sha256
    implementation_set_sha256: Sha256
    api_sha256: Sha256


class CodePin(StrictModel):
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256


class Matched500PipelineSpec(StrictModel):
    schema_version: Literal["sft2b_reform_32b_matched_500_pipeline_v1"]
    status: Literal["matched_500_generation_authorized"]
    input: InputSpec
    model: ModelSpec
    generation: GenerationSpec
    runtime: RuntimeSpec
    publication: PublicationSpec
    repr: ReprSpec
    code_pins: tuple[CodePin, ...]
    owner_session: Annotated[str, Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class VerifiedInput:
    root: Path
    rows: tuple[SourceRecord, ...]
    prompt_tokens: dict[str, int]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GpuRecord:
    index: int
    name: str
    uuid: str
    memory_total_mib: int
    memory_used_mib: int


@dataclass(frozen=True, slots=True)
class OutputBundle:
    root: Path
    run_id: str
    hashes: dict[str, str]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    revision: str
    remote_prefix: str
    remote_paths: tuple[str, ...]
    fresh_verification: bool


def _object(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Matched500PipelineError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def load_pipeline_spec(repo_root: Path, config_path: Path) -> tuple[Matched500PipelineSpec, str]:
    """Load the one-command config and verify every task-owned code pin."""

    try:
        spec = Matched500PipelineSpec.model_validate(_object(config_path))
    except Exception as exc:
        raise Matched500PipelineError(f"invalid matched-500 pipeline config: {exc}") from exc
    if len(spec.code_pins) < 4:
        raise Matched500PipelineError("matched-500 pipeline lacks complete task-owned code pins")
    for pin in spec.code_pins:
        path = repo_root / pin.path
        if not path.is_file() or hash_file(path) != pin.sha256:
            raise Matched500PipelineError(f"task-owned code/config hash mismatch: {pin.path}")
    if (
        spec.input.required_max_model_len != (spec.input.maximum_prompt_tokens + 4096)
        or spec.generation.max_model_len != spec.input.required_max_model_len
    ):
        raise Matched500PipelineError("matched-500 maximum model length drifted")
    if spec.generation.visible_devices != tuple(range(8)):
        raise Matched500PipelineError("matched-500 generation must use GPU indices 0 through 7")
    if len(spec.generation.visible_devices) != (
        spec.generation.data_parallel_size * spec.generation.tensor_parallel_size
    ):
        raise Matched500PipelineError("visible GPUs differ from DP*TP")
    if spec.generation.slots != tuple(CandidateSlot) or spec.generation.seeds != (0, 1, 2, 3):
        raise Matched500PipelineError("four matched candidate slots/seeds drifted")
    if spec.generation.expected_requests != spec.input.expected_rows * len(spec.generation.slots):
        raise Matched500PipelineError("expected request count is not 500 times four")
    if sum(spec.input.source_mix.values()) != spec.input.expected_rows:
        raise Matched500PipelineError("source-mix counts do not sum to 500")
    return spec, hash_file(config_path)


def _hf_snapshot_download(
    *,
    repo_id: str,
    repo_type: str,
    revision: str,
    cache_dir: Path,
    allow_patterns: Sequence[str] | None = None,
) -> Path:
    from huggingface_hub import snapshot_download

    cache_dir.mkdir(parents=True, exist_ok=True)
    downloaded = snapshot_download(
        repo_id=repo_id,
        repo_type=repo_type,
        revision=revision,
        cache_dir=str(cache_dir),
        allow_patterns=list(allow_patterns) if allow_patterns is not None else None,
        max_workers=16,
    )
    result = Path(downloaded)
    if result.name != revision:
        raise Matched500PipelineError(
            f"Hub snapshot resolved to {result.name}, expected immutable revision {revision}"
        )
    return result


def download_input(spec: Matched500PipelineSpec, work_root: Path) -> Path:
    """Fetch only the four frozen input files from the exact private revision."""

    from huggingface_hub import HfApi

    info = HfApi().repo_info(repo_id=spec.input.repo_id, repo_type=spec.input.repo_type)
    if spec.input.private_required and not bool(info.private):
        raise Matched500PipelineError("matched-500 input repository is no longer private")
    snapshot = _hf_snapshot_download(
        repo_id=spec.input.repo_id,
        repo_type=spec.input.repo_type,
        revision=spec.input.revision,
        cache_dir=work_root / "hub" / "pilot_input",
        allow_patterns=[f"{spec.input.path}/*"],
    )
    return snapshot / spec.input.path


def _read_sources(path: Path) -> tuple[SourceRecord, ...]:
    rows: list[SourceRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(SourceRecord.model_validate_json(line))
            except Exception as exc:
                raise Matched500PipelineError(
                    f"invalid SourceRecord at {path}:{line_number}: {exc}"
                ) from exc
    return tuple(rows)


def verify_input_without_model(
    repo_root: Path,
    *,
    spec: Matched500PipelineSpec,
    bundle_root: Path,
) -> VerifiedInput:
    """Reject missing data or contract drift before downloading/starting the model."""

    observed_names = {item.name for item in bundle_root.iterdir() if item.is_file()}
    if observed_names != set(spec.input.files):
        raise Matched500PipelineError(
            f"matched-500 input file set drifted: {sorted(observed_names)}"
        )
    for name, expected in spec.input.files.items():
        if hash_file(bundle_root / name) != expected:
            raise Matched500PipelineError(f"matched-500 input hash mismatch: {name}")
    checksum_rows: dict[str, str] = {}
    for line in (bundle_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise Matched500PipelineError("matched-500 SHA256SUMS is malformed")
        checksum_rows[parts[1]] = parts[0]
    expected_covered = set(spec.input.files).difference({"SHA256SUMS"})
    if set(checksum_rows) != expected_covered:
        raise Matched500PipelineError("matched-500 SHA256SUMS coverage drifted")
    for name, digest in checksum_rows.items():
        if digest != spec.input.files[name]:
            raise Matched500PipelineError(f"matched-500 checksum binding drifted: {name}")

    rows = _read_sources(bundle_root / "sources.jsonl")
    source_ids = tuple(row.source_id for row in rows)
    if len(rows) != spec.input.expected_rows or len(set(source_ids)) != len(rows):
        raise Matched500PipelineError("input is not exactly 500 unique SourceRecords")
    if any(
        not row.standalone_nl or not row.trusted_reference or not row.training_eligible
        for row in rows
    ):
        raise Matched500PipelineError("input contains a non-audited or non-trusted source")
    if any(
        "shadowbench" in row.provenance.source_url.casefold()
        or "shadowbench" in row.provenance.source_path.casefold()
        for row in rows
    ):
        raise Matched500PipelineError("ShadowBench leaked into the matched-500 input")

    token_payload = _object(bundle_root / "prompt_token_counts.json")
    raw_token_rows = token_payload.get("rows")
    if not isinstance(raw_token_rows, list) or len(raw_token_rows) != len(rows):
        raise Matched500PipelineError("input prompt-token rows are not exactly 500")
    prompt_tokens: dict[str, int] = {}
    ordered_token_ids: list[str] = []
    for raw in raw_token_rows:
        if not isinstance(raw, dict):
            raise Matched500PipelineError("input prompt-token row is not an object")
        source_id = raw.get("source_id")
        count = raw.get("prompt_tokens")
        if not isinstance(source_id, str) or not isinstance(count, int) or count < 1:
            raise Matched500PipelineError("input prompt-token row has invalid fields")
        ordered_token_ids.append(source_id)
        prompt_tokens[source_id] = count
    if tuple(ordered_token_ids) != source_ids or len(prompt_tokens) != len(rows):
        raise Matched500PipelineError("source and prompt-token ordering/IDs drifted")
    if max(prompt_tokens.values()) != spec.input.maximum_prompt_tokens or (
        token_payload.get("required_max_model_len") != spec.input.required_max_model_len
    ):
        raise Matched500PipelineError("frozen prompt maximum/model length drifted")
    if (
        token_payload.get("model_id") != spec.model.model_id
        or token_payload.get("model_revision") != spec.model.revision
    ):
        raise Matched500PipelineError("prompt-token model identity drifted")

    manifest = _object(bundle_root / "source_manifest.json")
    source_mix = cast(dict[str, Any], cast(dict[str, Any], manifest["source_mix"])["selected"])
    contamination = cast(dict[str, Any], manifest["contamination"])
    placement = cast(dict[str, Any], manifest["placement"])
    repr_manifest = cast(dict[str, Any], manifest["repr"])
    if (
        manifest.get("source_count") != spec.input.expected_rows
        or source_mix != spec.input.source_mix
    ):
        raise Matched500PipelineError("source manifest count/mix drifted")
    if not (
        contamination.get("selected_exact_hits") == 0
        and contamination.get("selected_near_hits") == 0
        and contamination.get("selected_problem_identity_hits") == 0
        and contamination.get("selected_existing_301_hits") == 0
        and contamination.get("selected_internal_duplicates") == 0
        and contamination.get("shadowbench") == "excluded_reference_free_test_only_126_rows"
    ):
        raise Matched500PipelineError("source contamination/exclusion result drifted")
    if (
        placement.get("model_revision") != spec.model.revision
        or placement.get("required_max_model_len") != spec.input.required_max_model_len
        or repr_manifest.get("freeze_commit") != spec.repr.freeze_commit
        or repr_manifest.get("spec_sha256") != spec.repr.spec_sha256
        or repr_manifest.get("implementation_set_sha256") != spec.repr.implementation_set_sha256
        or repr_manifest.get("api_sha256") != spec.repr.api_sha256
    ):
        raise Matched500PipelineError("source manifest placement/REPR pins drifted")
    source_config = repo_root / spec.input.source_config_path
    if not source_config.is_file() or hash_file(source_config) != spec.input.source_config_sha256:
        raise Matched500PipelineError("matched-500 source config hash drifted")
    placement_config = repo_root / spec.model.placement_config_path
    if (
        not placement_config.is_file()
        or hash_file(placement_config) != spec.model.placement_config_sha256
    ):
        raise Matched500PipelineError("ReForm-32B placement config hash drifted")
    return VerifiedInput(
        root=bundle_root,
        rows=rows,
        prompt_tokens=prompt_tokens,
        manifest=manifest,
    )


def download_and_verify_model(
    repo_root: Path,
    *,
    spec: Matched500PipelineSpec,
    verified_input: VerifiedInput,
    work_root: Path,
    snapshot_override: Path | None = None,
) -> tuple[Path, FreezeResult, FormalizerConfig]:
    """Fetch and hash every model byte, then replay all 500 prompt token counts."""

    snapshot = snapshot_override or _hf_snapshot_download(
        repo_id=spec.model.model_id,
        repo_type="model",
        revision=spec.model.revision,
        cache_dir=work_root / "hub" / "model",
    )
    placement, _ = load_reform_32b_config(
        repo_root,
        placement_path=repo_root / spec.model.placement_config_path,
        snapshot_path=snapshot,
    )
    if placement.snapshot_binding_sha256 != spec.model.snapshot_binding_sha256:
        raise Matched500PipelineError("ReForm-32B snapshot binding drifted")
    replay = verify_bundle(
        repo_root,
        config_path=repo_root / spec.input.source_config_path,
        bundle_dir=verified_input.root,
        tokenizer_snapshot_path=snapshot,
    )
    if (
        replay.rows != verified_input.rows
        or replay.maximum_prompt_tokens != spec.input.maximum_prompt_tokens
        or replay.required_max_model_len != spec.input.required_max_model_len
    ):
        raise Matched500PipelineError("full tokenizer replay differs from the frozen input")
    return snapshot, replay, placement


def _gpu_inventory(spec: Matched500PipelineSpec) -> tuple[GpuRecord, ...]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,uuid,memory.total,memory.used",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise Matched500PipelineError(f"nvidia-smi failed: {completed.stderr.strip()}")
    records: list[GpuRecord] = []
    for line in completed.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 5:
            raise Matched500PipelineError("nvidia-smi returned an unexpected schema")
        records.append(
            GpuRecord(
                index=int(fields[0]),
                name=fields[1],
                uuid=fields[2],
                memory_total_mib=int(fields[3]),
                memory_used_mib=int(fields[4]),
            )
        )
    if tuple(item.index for item in records) != spec.generation.visible_devices:
        raise Matched500PipelineError("host does not expose exactly the frozen eight GPU indices")
    for item in records:
        if item.memory_total_mib < spec.runtime.minimum_gpu_memory_mib:
            raise Matched500PipelineError(f"GPU {item.index} has insufficient memory")
        if not any(fragment in item.name for fragment in spec.runtime.allowed_gpu_name_fragments):
            raise Matched500PipelineError(
                f"GPU {item.index} is not an allowed A100/H100: {item.name}"
            )
    return tuple(records)


def _verify_runtime_packages(spec: Matched500PipelineSpec) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("vllm", "torch", "transformers", "huggingface-hub"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError as exc:
            raise Matched500PipelineError(
                f"required runtime package is missing: {package}"
            ) from exc
    try:
        versions["flash-attn"] = metadata.version("flash-attn")
    except metadata.PackageNotFoundError:
        versions["flash-attn"] = "not-installed"
    if versions["vllm"] != spec.runtime.vllm_version:
        raise Matched500PipelineError(
            f"vLLM version drifted: expected {spec.runtime.vllm_version}, "
            f"observed {versions['vllm']}"
        )
    return versions


def _build_backend(
    *,
    spec: Matched500PipelineSpec,
    config_path: Path,
    config_hash: str,
    placement: FormalizerConfig,
    verified_input: VerifiedInput,
    work_root: Path,
) -> LoadedVllmBackend:
    source_ids = tuple(row.source_id for row in verified_input.rows)
    full_profile = VllmProfile(
        profile_id=spec.generation.profile_id,
        visible_devices=spec.generation.visible_devices,
        data_parallel_size=spec.generation.data_parallel_size,
        tensor_parallel_size=spec.generation.tensor_parallel_size,
        port=spec.generation.port,
        max_model_len=spec.generation.max_model_len,
        max_num_seqs=spec.generation.max_num_seqs,
        gpu_memory_utilization=spec.generation.gpu_memory_utilization,
        prefix_caching=spec.generation.prefix_caching,
        concurrency=spec.generation.concurrency,
        source_ids=source_ids,
        slots=spec.generation.slots,
    )
    first_source = source_ids[0]
    smoke_profile = VllmProfile(
        profile_id=f"{spec.generation.profile_id}_structural_smoke",
        visible_devices=(0, 1),
        data_parallel_size=1,
        tensor_parallel_size=2,
        port=spec.generation.port + 1,
        max_model_len=verified_input.prompt_tokens[first_source] + 4096,
        max_num_seqs=1,
        gpu_memory_utilization=spec.generation.gpu_memory_utilization,
        prefix_caching=False,
        concurrency=1,
        source_ids=(first_source,),
        slots=(CandidateSlot.SLOT_0,),
    )
    portable = PortableReleaseConfig(
        repo_id=spec.input.repo_id,
        revision=spec.input.revision,
        release_id=f"sft2b_matched_500:{spec.input.revision}",
        release_manifest_path="source_manifest.json",
        release_manifest_sha256=spec.input.files["source_manifest.json"],
        smoke_sources_path="sources.jsonl",
        smoke_sources_sha256=spec.input.files["sources.jsonl"],
        probe_sources_path="sources.jsonl",
        probe_sources_sha256=spec.input.files["sources.jsonl"],
    )
    backend_spec = VllmBackendSpec(
        schema_version="sft2b_reform_32b_vllm_v1",
        status="bounded_probe_authorized",
        placement_config_path=spec.model.placement_config_path,
        placement_config_sha256=spec.model.placement_config_sha256,
        model_id=spec.model.model_id,
        model_revision=spec.model.revision,
        snapshot_binding_sha256=spec.model.snapshot_binding_sha256,
        checkpoint_dtype=spec.model.checkpoint_dtype,
        quantization=spec.model.quantization,
        trust_remote_code=spec.model.trust_remote_code,
        served_model_name=spec.model.served_model_name,
        provider="local_vllm_openai",
        api_route="/v1/completions",
        request_timeout_seconds=spec.generation.request_timeout_seconds,
        telemetry_interval_seconds=spec.generation.telemetry_interval_seconds,
        portable_release=portable,
        source_prompt_tokens=verified_input.prompt_tokens,
        profiles={"smoke_dp1_tp2": smoke_profile, PROFILE_NAME: full_profile},
        launch=VllmLaunchConfig(
            host="127.0.0.1",
            load_format="safetensors",
            safetensors_load_strategy="eager",
            distributed_executor_backend="mp",
            data_parallel_backend="mp",
            enable_request_id_headers=True,
            disable_uvicorn_access_log=True,
        ),
        staging_root=work_root,
        owner_session=spec.owner_session,
    )
    return LoadedVllmBackend(
        spec=backend_spec,
        config_path=config_path,
        config_sha256=config_hash,
        placement=placement,
        release_root=verified_input.root,
    )


def _server_log_tail(path: Path, limit: int = 12000) -> str:
    if not path.is_file():
        return ""
    payload = path.read_bytes()
    return payload[-limit:].decode("utf-8", errors="replace")


def _wait_for_server(
    process: subprocess.Popen[bytes],
    *,
    endpoint_url: str,
    served_model_name: str,
    timeout_seconds: int,
    log_path: Path,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "server has not answered"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise Matched500PipelineError(
                f"vLLM exited during startup with code {code}: {_server_log_tail(log_path)}"
            )
        try:
            return verify_openai_server(endpoint_url, served_model_name=served_model_name)
        except VllmBackendError as exc:
            last_error = str(exc)
        time.sleep(2.0)
    raise Matched500PipelineError(
        f"vLLM did not become ready in {timeout_seconds}s ({last_error}): "
        f"{_server_log_tail(log_path)}"
    )


def _stop_server(process: subprocess.Popen[bytes], timeout_seconds: int) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=60)


def generate_or_resume(
    repo_root: Path,
    *,
    spec: Matched500PipelineSpec,
    backend: LoadedVllmBackend,
    verified_input: VerifiedInput,
    runtime_versions: Mapping[str, str],
    work_root: Path,
) -> tuple[VllmProfileResult, Path, Path, dict[str, object], tuple[GpuRecord, ...]]:
    """Skip GPU startup on a complete cache; otherwise fill only missing cells."""

    endpoint_url = profile_endpoint(backend, PROFILE_NAME)
    inspection = inspect_vllm_sources_cache(
        backend,
        profile_name=PROFILE_NAME,
        sources=verified_input.rows,
        endpoint_url=endpoint_url,
    )
    if inspection.request_count != spec.generation.expected_requests:
        raise Matched500PipelineError("prepared request count is not exactly 2,000")
    receipt_root = inspection.root / "receipts"
    telemetry_path = receipt_root / "matched_500_telemetry.jsonl"
    server_log_path = receipt_root / "matched_500_vllm_server.log"
    server_observation: dict[str, object] = {"cache_complete_at_start": inspection.complete}
    gpu_records: tuple[GpuRecord, ...] = ()
    if inspection.complete:
        if not telemetry_path.is_file() or not server_log_path.is_file():
            raise Matched500PipelineError(
                "complete generation cache lacks its telemetry or vLLM server log"
            )
        result = run_vllm_sources(
            backend,
            profile_name=PROFILE_NAME,
            sources=verified_input.rows,
            endpoint_url=endpoint_url,
        )
        if result.model_calls != 0 or result.cache_hits != spec.generation.expected_requests:
            raise Matched500PipelineError("complete-cache replay attempted a model call")
        return result, telemetry_path, server_log_path, server_observation, gpu_records

    gpu_records = _gpu_inventory(spec)
    claim_resources(
        root=spec.runtime.reservation_root,
        task="SFT2B",
        lean_workers=0,
        lean_rss_gib=0.0,
        gpu=True,
        pid=os.getpid(),
        owner_session=spec.owner_session,
        worktree=repo_root,
    )
    process: subprocess.Popen[bytes] | None = None
    monitor: TelemetryMonitor | None = None
    receipt_root.mkdir(parents=True, exist_ok=True)
    try:
        command = build_vllm_serve_command(backend, profile_name=PROFILE_NAME)
        environment = os.environ.copy()
        environment["CUDA_VISIBLE_DEVICES"] = visible_devices_csv(
            backend.spec.profiles[PROFILE_NAME]
        )
        with server_log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            server_observation.update(
                _wait_for_server(
                    process,
                    endpoint_url=endpoint_url,
                    served_model_name=spec.model.served_model_name,
                    timeout_seconds=spec.generation.server_startup_timeout_seconds,
                    log_path=server_log_path,
                )
            )
            monitor = TelemetryMonitor(
                endpoint_url=endpoint_url,
                interval_seconds=spec.generation.telemetry_interval_seconds,
                server_pid=process.pid,
            )
            monitor.start()
            try:
                result = run_vllm_sources(
                    backend,
                    profile_name=PROFILE_NAME,
                    sources=verified_input.rows,
                    endpoint_url=endpoint_url,
                )
            finally:
                monitor.stop()
                monitor.write(telemetry_path)
                server_observation["telemetry"] = monitor.summary()
                server_observation["runtime_versions"] = dict(runtime_versions)
                _stop_server(process, spec.generation.server_shutdown_timeout_seconds)
                process = None
    finally:
        if process is not None:
            _stop_server(process, spec.generation.server_shutdown_timeout_seconds)
        release_resources(root=spec.runtime.reservation_root, task="SFT2B")
    final = inspect_vllm_sources_cache(
        backend,
        profile_name=PROFILE_NAME,
        sources=verified_input.rows,
        endpoint_url=endpoint_url,
    )
    if not final.complete or len(result.terminals) != spec.generation.expected_requests:
        raise Matched500PipelineError(
            f"generation stopped with {len(final.missing_request_keys)} incomplete cells"
        )
    return result, telemetry_path, server_log_path, server_observation, gpu_records


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _formalizer_invalid(row: Any) -> dict[str, object]:
    view = FormalizerInvalidAttemptView(
        attempt_id=row.attempt_id,
        source_id=row.source_id,
        slot=row.slot,
        validity_label=False,
        failure_class=cast(str, row.failure_class),
        failure_detail=cast(str, row.failure_detail),
        raw_output_sha256=row.raw_output_sha256,
    )
    return view.model_dump(mode="json")


def _verify_output_bundle(
    root: Path,
    *,
    expected_run_id: str,
    expected_requests: int,
) -> OutputBundle:
    observed = {item.name for item in root.iterdir() if item.is_file()}
    if observed != set(OUTPUT_NAMES):
        raise Matched500PipelineError(f"output bundle file set drifted: {sorted(observed)}")
    checksums: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise Matched500PipelineError("output SHA256SUMS is malformed")
        checksums[parts[1]] = parts[0]
    covered = set(OUTPUT_NAMES).difference({"SHA256SUMS"})
    if set(checksums) != covered:
        raise Matched500PipelineError("output SHA256SUMS coverage drifted")
    for name, digest in checksums.items():
        if hash_file(root / name) != digest:
            raise Matched500PipelineError(f"output hash mismatch: {name}")
    manifest = _object(root / "generation_manifest.json")
    raw_counts = manifest.get("counts")
    raw_source_ids = manifest.get("source_ids")
    if not isinstance(raw_counts, dict) or not isinstance(raw_source_ids, list):
        raise Matched500PipelineError("output manifest lacks counts or source IDs")
    counts = cast(dict[str, int], raw_counts)
    source_ids = tuple(str(item) for item in raw_source_ids)
    if len(source_ids) != 500 or len(set(source_ids)) != 500:
        raise Matched500PipelineError("output manifest does not bind 500 unique source IDs")
    if manifest.get("run_id") != expected_run_id or counts.get("attempts") != expected_requests:
        raise Matched500PipelineError("output run identity/request count drifted")
    line_counts = {
        "attempts": len((root / "formalizer_attempts.jsonl").read_text().splitlines()),
        "candidates": len((root / "candidates.jsonl").read_text().splitlines()),
        "formalizer_invalid": len(
            (root / "formalizer_invalid_attempts.jsonl").read_text().splitlines()
        ),
        "metrics": len((root / "request_metrics.jsonl").read_text().splitlines()),
        "terminals": len((root / "request_terminals.jsonl").read_text().splitlines()),
        "raw_generations": len((root / "raw_generations.jsonl").read_text().splitlines()),
    }
    if line_counts != {key: counts[key] for key in line_counts}:
        raise Matched500PipelineError("output JSONL counts differ from the manifest")
    if not (
        counts.get("attempts")
        == counts.get("metrics")
        == counts.get("terminals")
        == counts.get("raw_generations")
        == expected_requests
        and counts.get("candidates", 0) + counts.get("formalizer_invalid", 0) == expected_requests
        and counts.get("lean_calls") == counts.get("judge_calls") == counts.get("core_rows") == 0
    ):
        raise Matched500PipelineError("output routing/count invariant failed")
    attempts = tuple(
        FormalizerAttempt.model_validate_json(line)
        for line in (root / "formalizer_attempts.jsonl").read_text().splitlines()
    )
    expected_cells = {(source_id, slot) for source_id in source_ids for slot in CandidateSlot}
    observed_cells = {(item.source_id, item.slot) for item in attempts}
    if observed_cells != expected_cells or len(observed_cells) != expected_requests:
        raise Matched500PipelineError("output attempts are not the exact 500x4 Cartesian product")
    candidates = tuple(
        CandidateRecord.model_validate_json(line)
        for line in (root / "candidates.jsonl").read_text().splitlines()
    )
    invalid = tuple(
        FormalizerInvalidAttemptView.model_validate_json(line)
        for line in (root / "formalizer_invalid_attempts.jsonl").read_text().splitlines()
    )
    metrics = tuple(
        VllmRequestMetrics.model_validate_json(line)
        for line in (root / "request_metrics.jsonl").read_text().splitlines()
    )
    terminals = tuple(
        VllmRequestTerminal.model_validate_json(line)
        for line in (root / "request_terminals.jsonl").read_text().splitlines()
    )
    if (
        {item.candidate_id for item in candidates}
        != {item.candidate_id for item in attempts if item.candidate_id is not None}
        or {item.attempt_id for item in invalid}
        != {item.attempt_id for item in attempts if item.extraction_status == "invalid"}
        or [item.attempt_id for item in metrics] != [item.attempt_id for item in attempts]
        or [item.attempt.attempt_id for item in terminals] != [item.attempt_id for item in attempts]
    ):
        raise Matched500PipelineError("output candidate/invalid/metrics/terminal joins drifted")
    hashes = {name: hash_file(root / name) for name in OUTPUT_NAMES}
    return OutputBundle(root=root, run_id=expected_run_id, hashes=hashes, counts=counts)


def compact_generation(
    repo_root: Path,
    *,
    spec: Matched500PipelineSpec,
    config_path: Path,
    config_hash: str,
    verified_input: VerifiedInput,
    backend: LoadedVllmBackend,
    result: VllmProfileResult,
    telemetry_path: Path,
    server_log_path: Path,
    server_observation: Mapping[str, object],
    gpu_records: tuple[GpuRecord, ...],
    runtime_versions: Mapping[str, str],
    work_root: Path,
) -> OutputBundle:
    """Create a deterministic generation-only view; never synthesize semantic labels."""

    output_root = work_root / "publication" / result.run_id.split(":", 1)[-1]
    if (output_root / "generation_manifest.json").is_file():
        return _verify_output_bundle(
            output_root,
            expected_run_id=result.run_id,
            expected_requests=spec.generation.expected_requests,
        )
    output_root.mkdir(parents=True, exist_ok=True)
    attempts: list[object] = [item.attempt.model_dump(mode="json") for item in result.terminals]
    candidates: list[object] = [
        item.candidate.model_dump(mode="json")
        for item in result.terminals
        if item.candidate is not None
    ]
    formalizer_invalid: list[object] = [
        _formalizer_invalid(item.attempt) for item in result.terminals if item.candidate is None
    ]
    metrics: list[object] = [item.metrics.model_dump(mode="json") for item in result.terminals]
    terminals: list[object] = [item.model_dump(mode="json") for item in result.terminals]
    raw_generations: list[object] = []
    for terminal in result.terminals:
        raw_output_path = Path(terminal.metrics.raw_output_path)
        raw_response_path = Path(terminal.metrics.raw_response_path)
        raw_generations.append(
            {
                "schema_version": "sft2b_raw_generation_v1",
                "request_key": terminal.request_key,
                "attempt_id": terminal.attempt.attempt_id,
                "source_id": terminal.attempt.source_id,
                "slot": terminal.attempt.slot,
                "response_id": terminal.metrics.response_id,
                "raw_output": raw_output_path.read_text(encoding="utf-8"),
                "raw_output_sha256": terminal.metrics.raw_output_sha256,
                "raw_response_base64": base64.b64encode(raw_response_path.read_bytes()).decode(
                    "ascii"
                ),
                "raw_response_sha256": terminal.metrics.raw_response_sha256,
            }
        )
    write_jsonl(output_root / "formalizer_attempts.jsonl", attempts)
    write_jsonl(output_root / "candidates.jsonl", candidates)
    write_jsonl(output_root / "formalizer_invalid_attempts.jsonl", formalizer_invalid)
    write_jsonl(output_root / "request_metrics.jsonl", metrics)
    write_jsonl(output_root / "request_terminals.jsonl", terminals)
    write_jsonl(output_root / "raw_generations.jsonl", raw_generations)
    immutable_write(output_root / "telemetry.jsonl", telemetry_path.read_bytes())
    immutable_write(output_root / "vllm_server.log", server_log_path.read_bytes())
    journal_path = result.root / "journal" / "requests.jsonl"
    immutable_write(output_root / "requests_journal.jsonl", journal_path.read_bytes())
    prompt_token_total = sum(item.metrics.prompt_tokens for item in result.terminals)
    completion_token_total = sum(item.metrics.completion_tokens for item in result.terminals)
    counts = {
        "sources": len(verified_input.rows),
        "attempts": len(attempts),
        "candidates": len(candidates),
        "formalizer_invalid": len(formalizer_invalid),
        "metrics": len(metrics),
        "terminals": len(terminals),
        "raw_generations": len(raw_generations),
        "lean_calls": 0,
        "judge_calls": 0,
        "core_rows": 0,
        "semantic_labels": 0,
    }
    manifest = {
        "schema_version": "sft2b_reform_32b_matched_500_generation_manifest_v1",
        "run_id": result.run_id,
        "git_commit": _git_head(repo_root),
        "pipeline_config_path": str(config_path.relative_to(repo_root)),
        "pipeline_config_sha256": config_hash,
        "input": {
            "repo_id": spec.input.repo_id,
            "revision": spec.input.revision,
            "path": spec.input.path,
            "files": spec.input.files,
            "source_manifest_sha256": spec.input.files["source_manifest.json"],
        },
        "model": {
            "model_id": spec.model.model_id,
            "revision": spec.model.revision,
            "snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
            "checkpoint_dtype": spec.model.checkpoint_dtype,
            "quantization": spec.model.quantization,
        },
        "generation": spec.generation.model_dump(mode="json"),
        "source_ids": [row.source_id for row in verified_input.rows],
        "request_keys": [item.request_key for item in result.terminals],
        "candidate_ids": [
            item.candidate.candidate_id if item.candidate is not None else None
            for item in result.terminals
        ],
        "counts": counts,
        "tokens": {
            "prompt": prompt_token_total,
            "completion": completion_token_total,
            "maximum_prompt": spec.input.maximum_prompt_tokens,
            "max_model_len": spec.input.required_max_model_len,
        },
        "runtime_versions": dict(runtime_versions),
        "gpu_inventory": [asdict(item) for item in gpu_records],
        "server_observation": dict(server_observation),
        "repr": spec.repr.model_dump(mode="json"),
        "routing": {
            "candidate": "candidates.jsonl; validity and semantics not yet established",
            "formalizer_invalid": "formalizer_invalid_attempts.jsonl; never semantic false",
            "core": "absent until Lean validity and three blinded votes",
        },
        "forbidden_stages_executed": [],
    }
    write_json(output_root / "generation_manifest.json", manifest)
    checksum_names = sorted(set(OUTPUT_NAMES).difference({"SHA256SUMS"}))
    checksum_payload = "".join(
        f"{hash_file(output_root / name)}  {name}\n" for name in checksum_names
    ).encode("utf-8")
    immutable_write(output_root / "SHA256SUMS", checksum_payload)
    return _verify_output_bundle(
        output_root,
        expected_run_id=result.run_id,
        expected_requests=spec.generation.expected_requests,
    )


def _fresh_verify_remote(
    *,
    spec: Matched500PipelineSpec,
    bundle: OutputBundle,
    revision: str,
    remote_prefix: str,
    work_root: Path,
) -> None:
    fresh_parent = work_root / "fresh_hf_verification"
    fresh_parent.mkdir(parents=True, exist_ok=True)
    fresh_cache = Path(tempfile.mkdtemp(prefix="matched500.", dir=fresh_parent))
    snapshot = _hf_snapshot_download(
        repo_id=spec.publication.repo_id,
        repo_type=spec.publication.repo_type,
        revision=revision,
        cache_dir=fresh_cache,
        allow_patterns=[f"{remote_prefix}/*"],
    )
    remote_root = snapshot / remote_prefix
    verified = _verify_output_bundle(
        remote_root,
        expected_run_id=bundle.run_id,
        expected_requests=spec.generation.expected_requests,
    )
    if verified.hashes != bundle.hashes:
        raise Matched500PipelineError("fresh Hub output hashes differ from local publication")


def publish_output(
    *,
    spec: Matched500PipelineSpec,
    bundle: OutputBundle,
    run_root: Path,
    work_root: Path,
) -> PublicationReceipt:
    """Upload additively, record an immutable revision, and redownload into a fresh cache."""

    from huggingface_hub import CommitOperationAdd, HfApi

    receipt_path = run_root / "receipts" / "matched_500_publication.json"
    remote_prefix = f"{spec.publication.path_prefix}/{bundle.run_id.split(':', 1)[-1]}"
    api = HfApi()
    if receipt_path.is_file():
        receipt = _object(receipt_path)
        revision = str(receipt.get("revision", ""))
        if receipt.get("remote_prefix") != remote_prefix or len(revision) != 40:
            raise Matched500PipelineError("local publication receipt drifted")
        _fresh_verify_remote(
            spec=spec,
            bundle=bundle,
            revision=revision,
            remote_prefix=remote_prefix,
            work_root=work_root,
        )
        return PublicationReceipt(
            revision=revision,
            remote_prefix=remote_prefix,
            remote_paths=tuple(str(item) for item in receipt["remote_paths"]),
            fresh_verification=True,
        )
    info = api.repo_info(repo_id=spec.publication.repo_id, repo_type=spec.publication.repo_type)
    if spec.publication.private_required and not bool(info.private):
        raise Matched500PipelineError("refusing to publish SFT2B generations to a public repo")
    parent_revision = str(info.sha)
    existing_files = set(
        api.list_repo_files(
            repo_id=spec.publication.repo_id,
            repo_type=spec.publication.repo_type,
            revision=parent_revision,
        )
    )
    remote_paths = tuple(f"{remote_prefix}/{name}" for name in sorted(OUTPUT_NAMES))
    existing_target = {name for name in existing_files if name.startswith(f"{remote_prefix}/")}
    if existing_target and existing_target != set(remote_paths):
        raise Matched500PipelineError(
            "remote output prefix is partially occupied; refusing overwrite"
        )
    if existing_target:
        revision = parent_revision
        _fresh_verify_remote(
            spec=spec,
            bundle=bundle,
            revision=revision,
            remote_prefix=remote_prefix,
            work_root=work_root,
        )
    else:
        operations = [
            CommitOperationAdd(path_in_repo=remote, path_or_fileobj=bundle.root / name)
            for name, remote in zip(sorted(OUTPUT_NAMES), remote_paths, strict=True)
        ]
        commit = api.create_commit(
            repo_id=spec.publication.repo_id,
            repo_type=spec.publication.repo_type,
            operations=operations,
            commit_message=spec.publication.commit_message,
            parent_commit=parent_revision,
        )
        revision = str(commit.oid)
        if len(revision) != 40:
            raise Matched500PipelineError("Hub publication did not return an immutable revision")
        _fresh_verify_remote(
            spec=spec,
            bundle=bundle,
            revision=revision,
            remote_prefix=remote_prefix,
            work_root=work_root,
        )
    write_json(
        receipt_path,
        {
            "schema_version": "sft2b_matched_500_publication_receipt_v1",
            "repo_id": spec.publication.repo_id,
            "revision": revision,
            "remote_prefix": remote_prefix,
            "remote_paths": list(remote_paths),
            "file_sha256": bundle.hashes,
            "fresh_verification": True,
        },
    )
    return PublicationReceipt(
        revision=revision,
        remote_prefix=remote_prefix,
        remote_paths=remote_paths,
        fresh_verification=True,
    )


def run_pipeline(
    repo_root: Path,
    *,
    config_path: Path,
    work_root_override: Path | None = None,
    input_bundle_override: Path | None = None,
    snapshot_override: Path | None = None,
) -> dict[str, object]:
    """Execute the complete frozen input -> generation -> private publication path."""

    spec, config_hash = load_pipeline_spec(repo_root, config_path)
    pins = verify_runtime_pins(
        repo_root,
        helper_path=repo_root / "src/leanfaith/sft2b/lean_helper.lean",
    )
    if pins.repr_freeze_commit != spec.repr.freeze_commit or (
        pins.repr_spec_hash,
        pins.repr_implementation_set_hash,
        pins.repr_api_hash,
    ) != (spec.repr.spec_sha256, spec.repr.implementation_set_sha256, spec.repr.api_sha256):
        raise Matched500PipelineError("live REPR pins differ from the pipeline freeze")
    work_root = (work_root_override or spec.runtime.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    input_root = input_bundle_override or download_input(spec, work_root)
    verified_input = verify_input_without_model(
        repo_root,
        spec=spec,
        bundle_root=input_root.resolve(),
    )
    runtime_versions = _verify_runtime_packages(spec)
    _snapshot, _, placement = download_and_verify_model(
        repo_root,
        spec=spec,
        verified_input=verified_input,
        work_root=work_root,
        snapshot_override=snapshot_override,
    )
    backend = _build_backend(
        spec=spec,
        config_path=config_path,
        config_hash=config_hash,
        placement=placement,
        verified_input=verified_input,
        work_root=work_root,
    )
    result, telemetry, server_log, server_observation, gpu_records = generate_or_resume(
        repo_root,
        spec=spec,
        backend=backend,
        verified_input=verified_input,
        runtime_versions=runtime_versions,
        work_root=work_root,
    )
    bundle = compact_generation(
        repo_root,
        spec=spec,
        config_path=config_path,
        config_hash=config_hash,
        verified_input=verified_input,
        backend=backend,
        result=result,
        telemetry_path=telemetry,
        server_log_path=server_log,
        server_observation=server_observation,
        gpu_records=gpu_records,
        runtime_versions=runtime_versions,
        work_root=work_root,
    )
    receipt = publish_output(
        spec=spec,
        bundle=bundle,
        run_root=result.root,
        work_root=work_root,
    )
    return {
        "schema_version": "sft2b_matched_500_pipeline_result_v1",
        "run_id": result.run_id,
        "requests": len(result.terminals),
        "model_calls_this_process": result.model_calls,
        "cache_hits_this_process": result.cache_hits,
        "output_root": str(bundle.root),
        "output_sha256": bundle.hashes,
        "hub_repo": spec.publication.repo_id,
        "hub_revision": receipt.revision,
        "hub_prefix": receipt.remote_prefix,
        "fresh_verification": receipt.fresh_verification,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/reform_32b_matched_500_pipeline_v1.json"),
    )
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--input-bundle", type=Path)
    parser.add_argument("--snapshot-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    result = run_pipeline(
        repo_root,
        config_path=config_path.resolve(),
        work_root_override=args.work_root,
        input_bundle_override=args.input_bundle,
        snapshot_override=args.snapshot_path,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

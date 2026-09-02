"""Gated, resumable consumption of an additive SFT2B full-source release.

The hardened consumer uses the existing ReForm/vLLM request implementation;
it does not delegate generation to an unpinned shell command.  Provider calls,
immutable terminals, append-only journals, deterministic compaction, server
shutdown, and resource release are reconciled explicitly across restarts.

Launch is deliberately fail-closed.  A single self-attested pilot receipt is
never sufficient: the verifier opens the frozen pilot input plus the actual
runtime, quality, output, journal, replay, shutdown, and resource artifacts and
cross-binds their hashes and identities.  Core and legacy-tail authorization
are independent and mutually exclusive.  The checked-in v2 A100 profile keeps
both disabled and leaves final run IDs undefined until authorization is frozen.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.host_resources import claim_resources, list_reservations, release_resources
from leanfaith.sft2b import vllm_backend as frozen_vllm_backend
from leanfaith.sft2b.durable import atomic_write, immutable_write
from leanfaith.sft2b.formalizer import extract_candidate
from leanfaith.sft2b.matched_500_pipeline import (
    Matched500PipelineSpec,
    VerifiedInput,
    load_pipeline_spec,
    verify_input_without_model,
)
from leanfaith.sft2b.reform_32b import load_reform_32b_config
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateRecord,
    CandidateSlot,
    FormalizerAttempt,
    FormalizerInvalidAttemptView,
    Sha256,
    SourceRecord,
    StableId,
    stable_id,
)
from leanfaith.sft2b.vllm_backend import (
    CompletionTransport,
    LoadedVllmBackend,
    PortableReleaseConfig,
    PreparedRequest,
    VllmBackendSpec,
    VllmLaunchConfig,
    VllmProfile,
    VllmProfileResult,
    VllmRequestMetrics,
    VllmRequestTerminal,
    build_vllm_serve_command,
    profile_endpoint,
    stream_openai_completion,
    verify_openai_server,
    visible_devices_csv,
)
from leanfaith.sft2b.vllm_telemetry import TelemetryMonitor, load_samples

CONFIG_SCHEMA_V1 = "sft2b_reform_diverse_full_consumer_v1"
CONFIG_SCHEMA_V2 = "sft2b_reform_diverse_full_consumer_v2"
type ShardId = Literal["corrected_core_50000", "legacy_tail"]
type MissingPilotFact = Literal[
    "clean_shutdown",
    "process_absent",
    "resource_claim",
    "resource_release",
    "zero_call_cache_replay",
    "explicit_quality_acceptance",
    "fresh_download_publication_receipt",
]
CORE_SHARD: Literal["corrected_core_50000"] = "corrected_core_50000"
TAIL_SHARD: Literal["legacy_tail"] = "legacy_tail"
SHARD_IDS = (CORE_SHARD, TAIL_SHARD)
EXPECTED_SLOTS = tuple(CandidateSlot)
EXPECTED_SEEDS = (0, 1, 2, 3)

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_ID_RE = re.compile(r"^sft2b_source:[0-9a-f]{64}$")
_TMUX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")

PILOT_OUTPUT_FILES = (
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
PILOT_EVIDENCE_FILES = (
    *PILOT_OUTPUT_FILES,
    "resource_claim.json",
    "runtime_report.json",
    "quality_report.json",
    "replay_report.json",
    "server_shutdown.json",
    "resource_release.json",
)
FULL_PROFILE_NAME = "probe_dp4_tp2_c8"
SOURCE_CHUNK_SIZE = 128
MAX_PROVIDER_ATTEMPTS = 3
_EXPECTED_GPU_INDICES = tuple(range(8))
_PILOT_RUNTIME_PACKAGES = {"flash-attn", "huggingface-hub", "torch", "transformers", "vllm"}
_SERVER_READY_RE = re.compile(
    r"(?:Application startup complete|Uvicorn running|Available routes|vLLM API server)",
    re.IGNORECASE,
)
_SERVER_PID_RE = re.compile(r"Started server process\s*\[(?P<pid>[0-9]+)\]", re.IGNORECASE)


class FullSourceConsumerError(RuntimeError):
    """A source, resume, compaction, or launch contract drifted."""


class ReleaseFilePin(StrictModel):
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256 | None


class SourceShardSpec(StrictModel):
    shard_id: ShardId
    id_view_path: Annotated[str, Field(min_length=1)]
    id_view_sha256: Sha256 | None
    expected_rows: Annotated[int, Field(ge=1)] | None


class FullSourceInputSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    path_prefix: Annotated[str, Field(pattern=r"^source_inputs/reform_diverse_full_v[23]$")]
    files: tuple[ReleaseFilePin, ...]
    expected_source_rows: Annotated[int, Field(ge=50000)] | None
    shards: tuple[SourceShardSpec, SourceShardSpec]


class SlotSeedSpec(StrictModel):
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]


class ModelSpec(StrictModel):
    model_id: Literal["GuoxinChen/ReForm-32B"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    placement_config_path: Annotated[str, Field(min_length=1)]
    placement_config_sha256: Sha256
    prompt_path: Annotated[str, Field(min_length=1)]
    prompt_sha256: Sha256
    tokenizer_sha256: Sha256
    snapshot_binding_sha256: Sha256 | None = None
    served_model_name: Annotated[str, Field(min_length=1)] = "reform-32b-80e9d9d83998"
    slots: tuple[SlotSeedSpec, SlotSeedSpec, SlotSeedSpec, SlotSeedSpec]


class PilotArtifactPin(StrictModel):
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256


class Matched500GateSpec(StrictModel):
    """Pilot evidence location; legacy receipt fields are read-only tombstones."""

    evidence_state: Literal[
        "pending",
        "outputs_frozen_incomplete_receipts",
        "artifacts_frozen",
    ] = "pending"
    published_repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"] | None = None
    published_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    published_path_prefix: Annotated[str, Field(min_length=1)] | None = None
    artifact_root: Path | None = None
    pilot_input_root: Path | None = None
    pipeline_config_path: Annotated[str, Field(min_length=1)] = (
        "configs/sft2b/reform_32b_matched_500_pipeline_v1.json"
    )
    pipeline_config_sha256: Sha256 | None = None
    artifact_files: tuple[PilotArtifactPin, ...] = ()
    observed_runtime_code_pins: tuple[PilotArtifactPin, ...] = ()
    # Kept only so the committed v1 config still parses.  A non-null legacy
    # receipt is never accepted by the hardened verifier.
    receipt_path: Annotated[str, Field(min_length=1)] | None
    receipt_sha256: Sha256 | None
    decision: Literal["pending", "pass"]
    expected_sources: Literal[500]
    expected_requests: Literal[2000]


class ExecutorSpec(StrictModel):
    """Integrated vLLM executor contract.

    ``argv`` is a disabled compatibility tombstone for the superseded v1
    config.  The v2 launch path rejects it and calls the imported vLLM backend
    directly.
    """

    kind: Literal["legacy_disabled", "integrated_vllm"] = "legacy_disabled"
    argv: tuple[Annotated[str, Field(min_length=1)], ...] | None = None
    visible_devices: tuple[Annotated[int, Field(ge=0)], ...] = ()
    data_parallel_size: Annotated[int, Field(ge=1)] = 4
    tensor_parallel_size: Annotated[int, Field(ge=1)] = 2
    port: Annotated[int, Field(ge=1, le=65535)] = 8102
    max_model_len: Annotated[int, Field(ge=4096)] | None = None
    max_num_seqs: Annotated[int, Field(ge=1)] = 16
    gpu_memory_utilization: Annotated[float, Field(gt=0.0, le=1.0)] = 0.9
    prefix_caching: Literal[False] = False
    concurrency: Annotated[int, Field(ge=1)] = 64
    request_timeout_seconds: Annotated[int, Field(ge=1)] = 900
    telemetry_interval_seconds: Annotated[float, Field(gt=0.0)] = 0.5
    journal_fsync_every: Annotated[int, Field(ge=1, le=1024)] = 64
    server_startup_timeout_seconds: Annotated[int, Field(ge=60)] = 2400
    server_shutdown_timeout_seconds: Annotated[int, Field(ge=10)] = 300


class ShardAuthorizationSpec(StrictModel):
    frozen: bool = False
    core_enabled: bool = False
    tail_enabled: bool = False
    authorized_by: Annotated[str, Field(min_length=1)] | None = None
    authorized_at: Annotated[str, Field(min_length=1)] | None = None
    pilot_evidence_binding_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _validate_authorization(self) -> Self:
        if self.core_enabled and self.tail_enabled:
            raise ValueError("core and legacy tail may never be authorized together")
        if not self.frozen and (self.core_enabled or self.tail_enabled):
            raise ValueError("unfrozen authorization cannot enable a shard")
        if self.core_enabled or self.tail_enabled:
            if not (
                self.authorized_by and self.authorized_at and self.pilot_evidence_binding_sha256
            ):
                raise ValueError("enabled shard authorization lacks frozen decision evidence")
        elif any(
            value is not None
            for value in (
                self.authorized_by,
                self.authorized_at,
                self.pilot_evidence_binding_sha256,
            )
        ):
            raise ValueError("disabled authorization cannot claim decision evidence")
        return self


class RuntimeSpec(StrictModel):
    cache_root: Path
    run_root: Path
    reservation_root: Path
    reservation_task: Literal["SFT2B"]
    tmux_session_prefix: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
    startup_health_timeout_seconds: Annotated[int, Field(ge=5, le=3600)]
    owner_session: Annotated[str, Field(min_length=1)]
    host_profile: Literal["legacy_local", "eight_a100_scratch"] = "legacy_local"
    scratch_root: Path | None = None
    model_snapshot_path: Path | None = None


class FullSourceConsumerSpec(StrictModel):
    schema_version: Literal[
        "sft2b_reform_diverse_full_consumer_v1",
        "sft2b_reform_diverse_full_consumer_v2",
    ]
    status: Literal["waiting_matched_500_report", "active", "scale_authorized"]
    input: FullSourceInputSpec
    model: ModelSpec
    matched_500_gate: Matched500GateSpec
    executor: ExecutorSpec
    runtime: RuntimeSpec
    authorization: ShardAuthorizationSpec = ShardAuthorizationSpec()
    code_pins: tuple[PilotArtifactPin, ...] = ()

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if tuple(item.shard_id for item in self.input.shards) != SHARD_IDS:
            raise ValueError("source shards must be corrected core followed by legacy tail")
        core, tail = self.input.shards
        if core.expected_rows != 50000:
            raise ValueError("corrected core must contain exactly 50,000 sources")
        if (
            tail.expected_rows is not None
            and self.input.expected_source_rows is not None
            and core.expected_rows + tail.expected_rows != self.input.expected_source_rows
        ):
            raise ValueError("core and tail counts do not cover the full release")
        if tuple(item.slot for item in self.model.slots) != EXPECTED_SLOTS:
            raise ValueError("all four candidate slots must appear in frozen order")
        if tuple(item.seed for item in self.model.slots) != EXPECTED_SEEDS:
            raise ValueError("candidate slot seeds must be 0, 1, 2, and 3")
        required_paths = {
            "SHA256SUMS",
            "sources.jsonl",
            "source_manifest.json",
            core.id_view_path,
            tail.id_view_path,
        }
        if self.schema_version == CONFIG_SCHEMA_V2:
            required_paths.add("prompt_token_counts.json")
        file_paths = [item.path for item in self.input.files]
        if len(file_paths) != len(set(file_paths)) or not required_paths.issubset(file_paths):
            raise ValueError("input file pins omit a required consumer artifact or repeat a path")
        if self.schema_version == CONFIG_SCHEMA_V1:
            if self.status != "waiting_matched_500_report":
                raise ValueError("legacy consumer config is permanently launch-disabled")
            if self.authorization.frozen or self.executor.argv:
                raise ValueError("legacy consumer cannot carry launch authorization")
            return self
        required_code_pins = {
            "src/leanfaith/sft2b/full_source_consumer.py",
            "src/leanfaith/sft2b/vllm_backend.py",
            "src/leanfaith/sft2b/vllm_telemetry.py",
            "src/leanfaith/sft2b/formalizer.py",
            "src/leanfaith/sft2b/durable.py",
            "src/leanfaith/sft2b/reform_32b.py",
            "src/leanfaith/sft2b/schemas.py",
        }
        observed_code_pins = {item.path for item in self.code_pins}
        if (
            len(observed_code_pins) != len(self.code_pins)
            or observed_code_pins != required_code_pins
        ):
            raise ValueError("hardened consumer code-pin set is not exact")
        shard_flags = (self.authorization.core_enabled, self.authorization.tail_enabled)
        if not self.authorization.frozen:
            if self.status != "active" or any(shard_flags):
                raise ValueError("active consumer must remain unfrozen with both shards disabled")
        elif self.status != "scale_authorized" or sum(shard_flags) != 1:
            raise ValueError(
                "frozen consumer must be scale_authorized for exactly one independent shard"
            )
        if self.executor.kind != "integrated_vllm" or self.executor.argv is not None:
            raise ValueError("hardened consumer requires the integrated vLLM executor")
        if self.executor.visible_devices != tuple(range(8)):
            raise ValueError("A100 executor must expose GPU indices 0 through 7")
        if len(self.executor.visible_devices) != (
            self.executor.data_parallel_size * self.executor.tensor_parallel_size
        ):
            raise ValueError("A100 device count differs from DP*TP")
        if self.runtime.host_profile != "eight_a100_scratch":
            raise ValueError("hardened consumer requires the eight-A100 host profile")
        if self.runtime.scratch_root is None or self.runtime.model_snapshot_path is None:
            raise ValueError("A100 host profile lacks scratch/model roots")
        scratch = self.runtime.scratch_root
        for path in (
            self.runtime.cache_root,
            self.runtime.run_root,
            self.runtime.model_snapshot_path,
        ):
            if not path.is_relative_to(scratch):
                raise ValueError("A100 runtime path escapes its /scratch root")
        if str(scratch) != "/scratch/milikic/data/leanfaith":
            raise ValueError("A100 scratch root drifted")
        expected_reservation_root = scratch / "value_first/host_reservations"
        if self.runtime.reservation_root != expected_reservation_root:
            raise ValueError("A100 resource claims must use the frozen /scratch reservation root")
        if self.matched_500_gate.receipt_path or self.matched_500_gate.receipt_sha256:
            raise ValueError("hardened config forbids the legacy self-attested receipt")
        if self.matched_500_gate.decision != "pending":
            raise ValueError("pilot pass is derived by artifact verification, not config assertion")
        gate = self.matched_500_gate
        publication_fields = (
            gate.published_repo_id,
            gate.published_revision,
            gate.published_path_prefix,
        )
        artifact_paths = {item.path for item in gate.artifact_files}
        required_observed_runtime_paths = {
            "src/leanfaith/sft2b/vllm_backend.py",
            "src/leanfaith/sft2b/vllm_telemetry.py",
            "src/leanfaith/sft2b/formalizer.py",
            "src/leanfaith/sft2b/durable.py",
            "src/leanfaith/sft2b/reform_32b.py",
            "src/leanfaith/sft2b/schemas.py",
            "src/leanfaith/config/hashing.py",
            "src/leanfaith/config/models.py",
            "src/leanfaith/host_resources.py",
        }
        observed_runtime_paths = {item.path for item in gate.observed_runtime_code_pins}
        if (
            len(observed_runtime_paths) != len(gate.observed_runtime_code_pins)
            or observed_runtime_paths != required_observed_runtime_paths
        ):
            raise ValueError("pilot runtime transitive code-pin set is not exact")
        if len(artifact_paths) != len(gate.artifact_files):
            raise ValueError("matched-pilot artifact pins repeat a path")
        if gate.evidence_state == "pending":
            if any(publication_fields) or gate.artifact_files:
                raise ValueError("pending pilot evidence cannot carry publication claims")
        elif gate.evidence_state == "outputs_frozen_incomplete_receipts":
            if not all(publication_fields) or artifact_paths != set(PILOT_OUTPUT_FILES):
                raise ValueError("partial pilot evidence requires exact published eleven-file pins")
        elif not all(publication_fields) or artifact_paths != set(PILOT_EVIDENCE_FILES):
            raise ValueError(
                "full pilot evidence requires publication identity and exact complete artifacts"
            )
        if self.authorization.frozen:
            if self.input.revision is None or self.input.expected_source_rows is None:
                raise ValueError("scale authorization requires an immutable full-release pin")
            if any(item.sha256 is None for item in self.input.files):
                raise ValueError("scale authorization requires every input file hash")
            if any(item.id_view_sha256 is None for item in self.input.shards):
                raise ValueError("scale authorization requires both ID-view hashes")
            if self.executor.max_model_len is None or self.model.snapshot_binding_sha256 is None:
                raise ValueError("frozen authorization requires model/context pins")
            if (
                gate.evidence_state != "artifacts_frozen"
                or gate.artifact_root is None
                or gate.pilot_input_root is None
                or gate.pipeline_config_sha256 is None
            ):
                raise ValueError(
                    "scale authorization requires complete matched-500 artifact evidence"
                )
        return self


class PilotRuntimeReport(StrictModel):
    schema_version: Literal["sft2b_matched_500_runtime_report_v1"]
    run_id: StableId
    source_count: Literal[500]
    request_count: Literal[2000]
    request_keys_sha256: Sha256
    output_manifest_sha256: Sha256
    request_metrics_sha256: Sha256
    requests_journal_sha256: Sha256
    telemetry_sha256: Sha256
    server_log_sha256: Sha256
    telemetry_samples: Annotated[int, Field(ge=2)]
    telemetry_first_unix_ns: Annotated[int, Field(gt=0)]
    telemetry_last_unix_ns: Annotated[int, Field(gt=0)]
    telemetry_summary_sha256: Sha256
    server_observation_sha256: Sha256
    runtime_versions_sha256: Sha256
    gpu_inventory_sha256: Sha256
    server_pid: Annotated[int, Field(gt=0)]
    wall_time_ms: Annotated[int, Field(gt=0)]
    prompt_tokens: Annotated[int, Field(gt=0)]
    completion_tokens: Annotated[int, Field(gt=0)]
    requests_per_second: Annotated[float, Field(gt=0.0)]
    output_tokens_per_second: Annotated[float, Field(gt=0.0)]
    failure_taxonomy: dict[Annotated[str, Field(min_length=1)], Annotated[int, Field(ge=0)]]


class PilotQualityAcceptanceDecision(StrictModel):
    """An explicit human decision, separate from mechanical output facts."""

    schema_version: Literal["sft2b_matched_500_quality_acceptance_v2"]
    run_id: StableId
    source_count: Literal[500]
    request_count: Literal[2000]
    observed_partial_evidence_binding_sha256: Sha256
    quality_metrics_sha256: Sha256
    reviewed_by: Annotated[str, Field(pattern=r"^human:.+")]
    reviewed_at: Annotated[str, Field(min_length=1)]
    rationale: Annotated[str, Field(min_length=1)]
    decision: Literal["accept_as_pilot_evidence"]


class ObservedPilotQualityMetrics(StrictModel):
    """Mechanical facts only; this is deliberately not a quality decision."""

    measurement_scope: Literal[
        "formalizer_output_contract_only_not_lean_validity_or_semantic_quality"
    ]
    output_contract_admitted: Annotated[int, Field(ge=0, le=2000)]
    output_contract_rejected: Annotated[int, Field(ge=0, le=2000)]
    output_contract_admission_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    candidates_by_slot: dict[str, Annotated[int, Field(ge=0, le=500)]]
    sources_by_candidate_count: dict[str, Annotated[int, Field(ge=0, le=500)]]
    unique_candidate_signatures: Annotated[int, Field(ge=0, le=2000)]
    duplicate_candidate_signatures: Annotated[int, Field(ge=0, le=2000)]
    finish_reasons: dict[str, Annotated[int, Field(ge=0, le=2000)]]
    truncated_requests: Annotated[int, Field(ge=0, le=2000)]
    failure_taxonomy: dict[str, Annotated[int, Field(ge=0, le=2000)]]
    selection_mix: dict[str, Annotated[int, Field(ge=0, le=500)]]
    provenance_origin_mix: dict[str, Annotated[int, Field(ge=0, le=500)]]


class ObservedPilotReceipt(StrictModel):
    """Typed verification of the eleven files the frozen runner really emitted."""

    schema_version: Literal["sft2b_matched_500_observed_partial_receipt_v1"]
    evidence_state: Literal["mechanically_verified_partial"]
    published_repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"] | None
    published_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    published_path_prefix: Annotated[str, Field(min_length=1)] | None
    consumer_spec_sha256: Sha256
    verifier_sha256: Sha256
    run_id: StableId
    source_count: Literal[500]
    request_count: Literal[2000]
    source_ids_sha256: Sha256
    request_keys_sha256: Sha256
    artifact_sha256: dict[str, Sha256]
    quality_metrics: ObservedPilotQualityMetrics
    runtime_observed: Literal[True]
    quality_decision: Literal["not_authorized"]
    gate_passed: Literal[False]
    missing_or_unverifiable: tuple[MissingPilotFact, ...]
    evidence_binding_sha256: Sha256


class PilotReplayReport(StrictModel):
    schema_version: Literal["sft2b_matched_500_replay_report_v1"]
    run_id: StableId
    request_count: Literal[2000]
    request_keys_sha256: Sha256
    model_calls: Literal[0]
    cache_hits: Literal[2000]
    complete_cartesian_product: Literal[True]
    deterministic_output_sha256: dict[str, Sha256]


class PilotShutdownReceipt(StrictModel):
    schema_version: Literal["sft2b_matched_500_server_shutdown_v1"]
    run_id: StableId
    server_started: Literal[True]
    server_pid: Annotated[int, Field(gt=0)]
    server_log_sha256: Sha256
    telemetry_sha256: Sha256
    server_observation_sha256: Sha256
    stopped: Literal[True]
    clean_shutdown: Literal[True]
    kill_escalated: Literal[False]
    return_code: int
    process_absent_after_shutdown: Literal[True]


class PilotResourceClaim(StrictModel):
    schema_version: Literal["sft2b_matched_500_resource_claim_v1"]
    run_id: StableId
    reservation_root: Annotated[str, Field(min_length=1)]
    task: Literal["SFT2B"]
    lean_workers: Literal[0]
    lean_rss_gib: Annotated[float, Field(ge=0.0, le=0.0)]
    gpu: Literal[True]
    pid: Annotated[int, Field(gt=0)]
    owner_session: Annotated[str, Field(min_length=1)]
    hostname: Annotated[str, Field(min_length=1)]
    worktree: Annotated[str, Field(min_length=1)]
    created_at: Annotated[str, Field(min_length=1)]


class PilotResourceReleaseReceipt(StrictModel):
    schema_version: Literal["sft2b_matched_500_resource_release_v1"]
    run_id: StableId
    task: Literal["SFT2B"]
    reservation_root: Annotated[str, Field(min_length=1)]
    claim_acquired: Literal[True]
    claim_artifact_path: Literal["resource_claim.json"]
    claim_sha256: Sha256
    supervisor_pid: Annotated[int, Field(gt=0)]
    released: Literal[True]
    active_task_claims_after_release: Literal[0]


@dataclass(frozen=True, slots=True)
class VerifiedPilotEvidence:
    run_id: str
    source_ids: tuple[str, ...]
    request_keys: tuple[str, ...]
    evidence_binding_sha256: str
    failure_taxonomy: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class VerifiedPilotRequestContract:
    run_id: str
    pipeline_config_sha256: str
    prompt_template_sha256: str
    request_keys: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    prompt_input_sha256: Mapping[str, str]
    prompt_tokens: Mapping[str, int]
    request_payload_sha256: tuple[str, ...]
    request_artifact_sha256: tuple[str, ...]
    request_started_sha256: tuple[str, ...]
    decoding_sha256: str
    extraction_contract: str


@dataclass(frozen=True, slots=True)
class VerifiedSourceViews:
    rows: tuple[SourceRecord, ...]
    source_ids: tuple[str, ...]
    shard_source_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class WorkCell:
    ordinal: int
    shard_id: ShardId
    source_id: str
    slot: CandidateSlot
    seed: int
    cell_id: str


@dataclass(frozen=True, slots=True)
class FullSourceRunPlan:
    run_id: str
    shard_id: ShardId
    source_ids: tuple[str, ...]
    cells: tuple[WorkCell, ...]
    input_binding_sha256: str


class FullSourceTerminal(StrictModel):
    schema_version: Literal["sft2b_full_source_terminal_v1"] = "sft2b_full_source_terminal_v1"
    run_id: StableId
    cell_id: StableId
    shard_id: ShardId
    source_id: StableId
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]
    payload: dict[str, Any]


class FullSourceJournalEvent(StrictModel):
    schema_version: Literal["sft2b_full_source_journal_event_v1"] = (
        "sft2b_full_source_journal_event_v1"
    )
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    cell_id: StableId
    shard_id: ShardId
    source_id: StableId
    slot: CandidateSlot
    seed: Annotated[int, Field(ge=0)]
    terminal_path: Annotated[str, Field(min_length=1)]
    terminal_sha256: Sha256


class FullSourceRuntimeSessionStart(StrictModel):
    schema_version: Literal["sft2b_full_source_runtime_session_start_v1"] = (
        "sft2b_full_source_runtime_session_start_v1"
    )
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    session_id: Annotated[str, Field(pattern=r"^[0-9]+-[0-9]+$")]
    server_pid: Annotated[int, Field(gt=0)]
    served_model_name: Annotated[str, Field(min_length=1)]
    started_unix_ns: Annotated[int, Field(gt=0)]
    backend_config_sha256: Sha256
    claim_artifact_path: Annotated[str, Field(min_length=1)]
    claim_sha256: Sha256
    session_start_path: Annotated[str, Field(min_length=1)]


class FullSourceRuntimeSessionReceipt(StrictModel):
    schema_version: Literal["sft2b_full_source_runtime_session_v1"] = (
        "sft2b_full_source_runtime_session_v1"
    )
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    session_id: Annotated[str, Field(pattern=r"^[0-9]+-[0-9]+$")]
    server_pid: Annotated[int, Field(gt=0)]
    served_model_name: Annotated[str, Field(min_length=1)]
    started_unix_ns: Annotated[int, Field(gt=0)]
    ended_unix_ns: Annotated[int, Field(gt=0)]
    server_log_path: Annotated[str, Field(min_length=1)]
    server_log_sha256: Sha256
    telemetry_path: Annotated[str, Field(min_length=1)]
    telemetry_sha256: Sha256
    telemetry_summary: dict[str, Any]
    shutdown_path: Annotated[str, Field(min_length=1)]
    shutdown_sha256: Sha256
    session_start_sha256: Sha256


class FullSourceRuntimeSessionReconciliation(StrictModel):
    schema_version: Literal["sft2b_full_source_runtime_reconciliation_v1"] = (
        "sft2b_full_source_runtime_reconciliation_v1"
    )
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    session_id: Annotated[str, Field(pattern=r"^[0-9]+-[0-9]+$")]
    server_pid: Annotated[int, Field(gt=0)]
    reconciled_unix_ns: Annotated[int, Field(gt=0)]
    session_start_sha256: Sha256
    process_absent: Literal[True]
    port_closed: Literal[True]
    reason: Literal["dead_same_run_runtime"]
    reconciliation_artifact_path: Annotated[str, Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class CompactionResult:
    path: Path
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DetachedLaunch:
    session_name: str
    command: tuple[str, ...]
    status_path: Path
    log_path: Path
    run_id: str
    shard_id: ShardId
    launch_nonce: str
    launched_unix_ns: int
    provider_journal_path: Path
    consumer_journal_path: Path
    compacted_output_path: Path


@dataclass(frozen=True, slots=True)
class DetachedHealth:
    session_name: str
    pane_pid: int | None
    state: str
    durable_advancement: bool
    healthy: bool


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FullSourceConsumerError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FullSourceConsumerError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def load_consumer_spec(config_path: Path) -> tuple[FullSourceConsumerSpec, str]:
    """Load the strict consumer config and return its exact content hash."""

    try:
        spec = FullSourceConsumerSpec.model_validate(_json_object(config_path))
    except Exception as exc:
        raise FullSourceConsumerError(f"invalid full-source consumer config: {exc}") from exc
    if spec.schema_version == CONFIG_SCHEMA_V2:
        repo_root = config_path.resolve().parents[2]
        for pin in spec.code_pins:
            path = repo_root / pin.path
            if not path.is_file() or path.is_symlink() or hash_file(path) != pin.sha256:
                raise FullSourceConsumerError(f"full-source consumer code pin drifted: {pin.path}")
        placement_path = repo_root / spec.model.placement_config_path
        prompt_path = repo_root / spec.model.prompt_path
        pipeline_path = repo_root / spec.matched_500_gate.pipeline_config_path
        if (
            not placement_path.is_file()
            or hash_file(placement_path) != spec.model.placement_config_sha256
            or not prompt_path.is_file()
            or hash_file(prompt_path) != spec.model.prompt_sha256
            or spec.matched_500_gate.pipeline_config_sha256 is None
            or not pipeline_path.is_file()
            or hash_file(pipeline_path) != spec.matched_500_gate.pipeline_config_sha256
        ):
            raise FullSourceConsumerError("full-source startup config/prompt pins drifted")
        placement = _json_object(placement_path)
        remote_files = placement.get("remote_files")
        tokenizer_rows = (
            [
                item
                for item in remote_files
                if isinstance(item, dict) and item.get("path") == "tokenizer.json"
            ]
            if isinstance(remote_files, list)
            else []
        )
        expected_slots = [item.model_dump(mode="json") for item in spec.model.slots]
        if (
            placement.get("model_id") != spec.model.model_id
            or placement.get("model_revision") != spec.model.revision
            or placement.get("prompt_path") != spec.model.prompt_path
            or placement.get("prompt_sha256") != spec.model.prompt_sha256
            or placement.get("candidate_slots") != expected_slots
            or len(tokenizer_rows) != 1
            or tokenizer_rows[0].get("hash_kind") != "sha256"
            or tokenizer_rows[0].get("hash") != spec.model.tokenizer_sha256
        ):
            raise FullSourceConsumerError("full-source startup model/tokenizer pins drifted")
    return spec, hash_file(config_path)


def _file_pin(spec: FullSourceConsumerSpec, relative_path: str) -> ReleaseFilePin:
    by_path = {item.path: item for item in spec.input.files}
    try:
        return by_path[relative_path]
    except KeyError as exc:
        raise FullSourceConsumerError(f"release file is not pinned: {relative_path}") from exc


def _require_pinned_input(spec: FullSourceConsumerSpec) -> None:
    if spec.input.revision is None or spec.input.expected_source_rows is None:
        raise FullSourceConsumerError("pinned full-source input revision/count are still pending")
    if any(item.sha256 is None for item in spec.input.files):
        raise FullSourceConsumerError("pinned full-source input file hashes are still pending")
    if any(item.id_view_sha256 is None for item in spec.input.shards):
        raise FullSourceConsumerError("pinned full-source ID-view hashes are still pending")


def _read_source_rows(path: Path) -> tuple[SourceRecord, ...]:
    rows: list[SourceRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise FullSourceConsumerError(
                    f"blank line in strict SourceRecord JSONL at {path}:{line_number}"
                )
            try:
                rows.append(SourceRecord.model_validate_json(line))
            except Exception as exc:
                raise FullSourceConsumerError(
                    f"invalid SourceRecord at {path}:{line_number}: {exc}"
                ) from exc
    return tuple(rows)


def _read_id_view(path: Path, *, expected_rows: int | None) -> tuple[str, ...]:
    value = _json_object(path)
    raw_ids = value.get("source_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise FullSourceConsumerError(f"ID view has invalid source_ids: {path}")
    source_ids = tuple(cast(list[str], raw_ids))
    if any(_SOURCE_ID_RE.fullmatch(item) is None for item in source_ids):
        raise FullSourceConsumerError(f"ID view contains an invalid source ID: {path}")
    if len(source_ids) != len(set(source_ids)):
        raise FullSourceConsumerError(f"ID view contains duplicate source IDs: {path}")
    if value.get("source_count") != len(source_ids):
        raise FullSourceConsumerError(f"ID view source_count drifted: {path}")
    if expected_rows is not None and len(source_ids) != expected_rows:
        raise FullSourceConsumerError(
            f"ID view row count drifted: expected {expected_rows}, observed {len(source_ids)}"
        )
    return source_ids


def verify_source_views(spec: FullSourceConsumerSpec, *, bundle_root: Path) -> VerifiedSourceViews:
    """Verify local release bytes and the exact core/tail partition."""

    _require_pinned_input(spec)
    expected_files = {item.path for item in spec.input.files}
    observed_files = {
        str(path.relative_to(bundle_root)) for path in bundle_root.rglob("*") if path.is_file()
    }
    if observed_files != expected_files:
        raise FullSourceConsumerError("full-source release file set differs from frozen pins")
    for pin in spec.input.files:
        path = bundle_root / pin.path
        if not path.is_file() or pin.sha256 is None or hash_file(path) != pin.sha256:
            raise FullSourceConsumerError(f"full-source release hash mismatch: {pin.path}")

    checksums: dict[str, str] = {}
    for line in (bundle_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise FullSourceConsumerError("full-source SHA256SUMS is malformed")
        if parts[1] in checksums:
            raise FullSourceConsumerError("full-source SHA256SUMS repeats a path")
        checksums[parts[1]] = parts[0]
    expected_covered = {item.path for item in spec.input.files}.difference({"SHA256SUMS"})
    if set(checksums) != expected_covered:
        raise FullSourceConsumerError("full-source SHA256SUMS coverage drifted")
    for pin in spec.input.files:
        if pin.path == "SHA256SUMS":
            continue
        if checksums.get(pin.path) != pin.sha256:
            raise FullSourceConsumerError(f"SHA256SUMS binding drifted: {pin.path}")

    rows = _read_source_rows(bundle_root / "sources.jsonl")
    source_ids = tuple(row.source_id for row in rows)
    if len(rows) != spec.input.expected_source_rows or len(source_ids) != len(set(source_ids)):
        raise FullSourceConsumerError("full source rows are not the exact unique pinned count")
    by_shard: dict[str, tuple[str, ...]] = {}
    for shard in spec.input.shards:
        pin = _file_pin(spec, shard.id_view_path)
        if pin.sha256 != shard.id_view_sha256:
            raise FullSourceConsumerError(f"ID-view pin is inconsistent: {shard.shard_id}")
        by_shard[shard.shard_id] = _read_id_view(
            bundle_root / shard.id_view_path, expected_rows=shard.expected_rows
        )

    core_ids = by_shard[CORE_SHARD]
    tail_ids = by_shard[TAIL_SHARD]
    core_set = set(core_ids)
    tail_set = set(tail_ids)
    source_set = set(source_ids)
    if core_set & tail_set:
        raise FullSourceConsumerError("corrected core and legacy tail overlap")
    if core_set | tail_set != source_set:
        raise FullSourceConsumerError("corrected core and legacy tail do not cover sources exactly")
    if tail_ids != tuple(item for item in source_ids if item not in core_set):
        raise FullSourceConsumerError("legacy tail is not the ordered full-release remainder")
    return VerifiedSourceViews(rows=rows, source_ids=source_ids, shard_source_ids=by_shard)


def _input_binding(spec: FullSourceConsumerSpec) -> str:
    _require_pinned_input(spec)
    return hash_canonical(
        {
            "schema_version": "sft2b_full_source_input_binding_v1",
            "repo_id": spec.input.repo_id,
            "revision": spec.input.revision,
            "path_prefix": spec.input.path_prefix,
            "files": {item.path: item.sha256 for item in spec.input.files},
            "shards": [item.model_dump(mode="json") for item in spec.input.shards],
        }
    )


def build_run_plan(
    spec: FullSourceConsumerSpec,
    *,
    config_sha256: str,
    shard_id: str,
    source_ids: Sequence[str],
) -> FullSourceRunPlan:
    """Expand one authorized ID view into its complete four-slot product.

    Final run and cell IDs intentionally do not exist before the independent
    shard authorization record is frozen.
    """

    if shard_id not in SHARD_IDS:
        raise FullSourceConsumerError(f"unknown full-source shard: {shard_id}")
    if spec.schema_version == CONFIG_SCHEMA_V2:
        if not spec.authorization.frozen:
            raise FullSourceConsumerError("final run ID is deferred until authorization is frozen")
        if shard_id == CORE_SHARD and not spec.authorization.core_enabled:
            raise FullSourceConsumerError("corrected core is not authorized")
        if shard_id == TAIL_SHARD and not spec.authorization.tail_enabled:
            raise FullSourceConsumerError("legacy tail is not independently authorized")
    typed_shard_id = shard_id
    if re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None:
        raise FullSourceConsumerError("consumer config hash is invalid")
    input_binding = _input_binding(spec)
    shard = next(item for item in spec.input.shards if item.shard_id == typed_shard_id)
    ordered_ids = tuple(source_ids)
    if len(ordered_ids) != len(set(ordered_ids)) or any(
        _SOURCE_ID_RE.fullmatch(item) is None for item in ordered_ids
    ):
        raise FullSourceConsumerError("run plan source IDs are invalid or duplicated")
    if shard.expected_rows is not None and len(ordered_ids) != shard.expected_rows:
        raise FullSourceConsumerError("run plan row count differs from its pinned ID view")

    run_identity: dict[str, object] = {
        "schema_version": "sft2b_full_reform_run_identity_v1",
        "consumer_config_sha256": config_sha256,
        "input_binding_sha256": input_binding,
        "shard_id": typed_shard_id,
        "source_ids_sha256": hash_canonical(ordered_ids),
        "model_id": spec.model.model_id,
        "model_revision": spec.model.revision,
        "placement_config_sha256": spec.model.placement_config_sha256,
        "prompt_sha256": spec.model.prompt_sha256,
        "tokenizer_sha256": spec.model.tokenizer_sha256,
        "slots": [item.model_dump(mode="json") for item in spec.model.slots],
    }
    if spec.schema_version == CONFIG_SCHEMA_V2:
        run_identity.update(
            {
                "schema_version": "sft2b_full_reform_run_identity_v2",
                "authorization_sha256": hash_canonical(spec.authorization.model_dump(mode="json")),
                "pilot_evidence_binding_sha256": (spec.authorization.pilot_evidence_binding_sha256),
            }
        )
    run_id = stable_id("sft2b_full_reform_run", run_identity)
    cells: list[WorkCell] = []
    for source_id in ordered_ids:
        for slot_spec in spec.model.slots:
            cell_id = stable_id(
                "sft2b_full_reform_cell",
                {
                    "run_id": run_id,
                    "source_id": source_id,
                    "slot": slot_spec.slot,
                    "seed": slot_spec.seed,
                },
            )
            cells.append(
                WorkCell(
                    ordinal=len(cells),
                    shard_id=typed_shard_id,
                    source_id=source_id,
                    slot=slot_spec.slot,
                    seed=slot_spec.seed,
                    cell_id=cell_id,
                )
            )
    if len(cells) != len(ordered_ids) * 4 or len({item.cell_id for item in cells}) != len(cells):
        raise FullSourceConsumerError("four-slot Cartesian-product construction failed")
    return FullSourceRunPlan(
        run_id=run_id,
        shard_id=typed_shard_id,
        source_ids=ordered_ids,
        cells=tuple(cells),
        input_binding_sha256=input_binding,
    )


def terminal_cache_path(cache_root: Path, plan: FullSourceRunPlan, cell: WorkCell) -> Path:
    """Return the content-addressed immutable terminal location for one cell."""

    if (
        cell.ordinal < 0
        or cell.ordinal >= len(plan.cells)
        or plan.cells[cell.ordinal] != cell
        or cell.shard_id != plan.shard_id
    ):
        raise FullSourceConsumerError("cell does not belong to the supplied run plan")
    digest = cell.cell_id.split(":", 1)[1]
    return (
        cache_root
        / "generation"
        / "reform_full_v1"
        / plan.run_id
        / plan.shard_id
        / "requests"
        / digest[:2]
        / digest
        / "terminal.json"
    )


def write_cached_terminal(
    cache_root: Path,
    plan: FullSourceRunPlan,
    cell: WorkCell,
    *,
    payload: Mapping[str, Any],
) -> Path:
    """Write or verify one immutable terminal envelope."""

    terminal = FullSourceTerminal(
        run_id=plan.run_id,
        cell_id=cell.cell_id,
        shard_id=cell.shard_id,
        source_id=cell.source_id,
        slot=cell.slot,
        seed=cell.seed,
        payload=dict(payload),
    )
    path = terminal_cache_path(cache_root, plan, cell)
    immutable_write(path, canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n")
    return path


class FullSourceJournal:
    """Locked append-only terminal journal with exact-cell duplicate suppression."""

    def __init__(
        self,
        path: Path,
        *,
        plan: FullSourceRunPlan,
        cache_root: Path,
        fsync_every: int = 1,
    ) -> None:
        if fsync_every < 1:
            raise FullSourceConsumerError("journal fsync interval must be positive")
        self.path = path
        self.plan = plan
        self.cache_root = cache_root
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._cells = {item.cell_id: item for item in plan.cells}
        self._events_cache: list[FullSourceJournalEvent] | None = None
        self._event_by_cell: dict[str, FullSourceJournalEvent] = {}
        self._cached_size = -1
        self._fsync_every = fsync_every
        self._unsynced = 0

    def _events(self) -> list[FullSourceJournalEvent]:
        observed_size = _file_size(self.path)
        if self._events_cache is not None and observed_size == self._cached_size:
            return self._events_cache
        if observed_size == 0:
            self._events_cache = []
            self._event_by_cell = {}
            self._cached_size = 0
            return self._events_cache
        events: list[FullSourceJournalEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(FullSourceJournalEvent.model_validate_json(line))
                except Exception as exc:
                    raise FullSourceConsumerError(
                        f"invalid full-source journal event at line {line_number}: {exc}"
                    ) from exc
        if [item.sequence for item in events] != list(range(len(events))):
            raise FullSourceConsumerError("full-source journal sequence is not contiguous")
        if any(item.run_id != self.plan.run_id for item in events):
            raise FullSourceConsumerError("full-source journal run identity drifted")
        event_ids = [item.cell_id for item in events]
        if len(event_ids) != len(set(event_ids)):
            raise FullSourceConsumerError("full-source journal contains duplicate terminal cells")
        for event in events:
            cell = self._cells.get(event.cell_id)
            if cell is None or (
                event.shard_id != cell.shard_id
                or event.source_id != cell.source_id
                or event.slot != cell.slot
                or event.seed != cell.seed
            ):
                raise FullSourceConsumerError("full-source journal contains a foreign cell")
            expected = terminal_cache_path(self.cache_root, self.plan, cell)
            if Path(event.terminal_path) != expected:
                raise FullSourceConsumerError("journal terminal is outside its content cache cell")
        self._events_cache = events
        self._event_by_cell = {item.cell_id: item for item in events}
        self._cached_size = observed_size
        return self._events_cache

    def append_terminal(self, cell: WorkCell, terminal_path: Path) -> bool:
        """Append one verified terminal; return False for an identical replay."""

        expected_path = terminal_cache_path(self.cache_root, self.plan, cell)
        if terminal_path != expected_path or not terminal_path.is_file():
            raise FullSourceConsumerError("terminal is absent or outside its content cache cell")
        terminal = FullSourceTerminal.model_validate_json(terminal_path.read_text(encoding="utf-8"))
        if (
            terminal.run_id != self.plan.run_id
            or terminal.cell_id != cell.cell_id
            or terminal.shard_id != cell.shard_id
            or terminal.source_id != cell.source_id
            or terminal.slot != cell.slot
            or terminal.seed != cell.seed
        ):
            raise FullSourceConsumerError("terminal envelope differs from its planned cell")
        terminal_hash = hash_file(terminal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                events = self._events()
                prior = self._event_by_cell.get(cell.cell_id)
                if prior is not None:
                    if prior.terminal_sha256 != terminal_hash:
                        raise FullSourceConsumerError("terminal replay changed immutable content")
                    return False
                event = FullSourceJournalEvent(
                    sequence=len(events),
                    run_id=self.plan.run_id,
                    cell_id=cell.cell_id,
                    shard_id=cell.shard_id,
                    source_id=cell.source_id,
                    slot=cell.slot,
                    seed=cell.seed,
                    terminal_path=str(terminal_path),
                    terminal_sha256=terminal_hash,
                )
                with self.path.open("ab") as handle:
                    handle.write(canonical_json_bytes(event.model_dump(mode="json")) + b"\n")
                    handle.flush()
                    self._unsynced += 1
                    if self._unsynced >= self._fsync_every:
                        os.fsync(handle.fileno())
                        self._unsynced = 0
                events.append(event)
                self._event_by_cell[cell.cell_id] = event
                self._cached_size = self.path.stat().st_size
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def sync(self) -> None:
        if self._unsynced == 0 or not self.path.is_file():
            return
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self.path.open("ab") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                self._unsynced = 0
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def events(self) -> tuple[FullSourceJournalEvent, ...]:
        return tuple(self._events())

    def missing_cells(self) -> tuple[WorkCell, ...]:
        self._events()
        return tuple(item for item in self.plan.cells if item.cell_id not in self._event_by_cell)

    def completed_count(self) -> int:
        self._events()
        return len(self._event_by_cell)

    def is_complete(self) -> bool:
        return self.completed_count() == len(self.plan.cells)


def compact_completed(journal: FullSourceJournal, output_path: Path) -> CompactionResult:
    """Require all cells, then compact terminals in deterministic plan order."""

    events = {item.cell_id: item for item in journal.events()}
    expected_ids = {item.cell_id for item in journal.plan.cells}
    if set(events) != expected_ids:
        missing = len(expected_ids.difference(events))
        extra = len(set(events).difference(expected_ids))
        raise FullSourceConsumerError(
            f"cannot compact incomplete Cartesian product: missing={missing}, extra={extra}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_symlink():
        raise FullSourceConsumerError("refusing symlink compacted output")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for cell in journal.plan.cells:
                event = events[cell.cell_id]
                terminal_path = Path(event.terminal_path)
                if not terminal_path.is_file() or hash_file(terminal_path) != event.terminal_sha256:
                    raise FullSourceConsumerError(
                        f"terminal content drifted before compaction: {cell.cell_id}"
                    )
                terminal = FullSourceTerminal.model_validate_json(
                    terminal_path.read_text(encoding="utf-8")
                )
                if terminal.cell_id != cell.cell_id:
                    raise FullSourceConsumerError(
                        "terminal ordering identity drifted before compaction"
                    )
                row = canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n"
                handle.write(row)
                digest.update(row)
            handle.flush()
            os.fsync(handle.fileno())
        observed_sha = digest.hexdigest()
        if output_path.exists():
            if not output_path.is_file() or hash_file(output_path) != observed_sha:
                raise FullSourceConsumerError("immutable compacted output conflicts")
        else:
            os.replace(temporary, output_path)
            directory_fd = os.open(output_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    result = CompactionResult(
        path=output_path,
        rows=len(journal.plan.cells),
        sha256=observed_sha,
    )
    return result


def verify_compaction(
    plan: FullSourceRunPlan, path: Path, *, expected_sha256: str
) -> CompactionResult:
    """Replay the deterministic compacted ordering and completeness contract."""

    if not path.is_file() or hash_file(path) != expected_sha256:
        raise FullSourceConsumerError("compacted output hash mismatch")
    row_count = 0
    cells = iter(plan.cells)
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                raise FullSourceConsumerError(f"compacted output contains a blank line at {number}")
            try:
                cell = next(cells)
            except StopIteration as exc:
                raise FullSourceConsumerError(
                    "compacted output has more than the planned Cartesian product"
                ) from exc
            terminal = FullSourceTerminal.model_validate_json(line)
            if (
                terminal.run_id != plan.run_id
                or terminal.cell_id != cell.cell_id
                or terminal.source_id != cell.source_id
                or terminal.slot != cell.slot
                or terminal.seed != cell.seed
            ):
                raise FullSourceConsumerError("compacted output order or cell identity drifted")
            row_count += 1
    try:
        next(cells)
    except StopIteration:
        pass
    else:
        raise FullSourceConsumerError("compacted output is not the complete Cartesian product")
    return CompactionResult(path=path, rows=row_count, sha256=expected_sha256)


def _resolve_from_repo(repo_root: Path, configured: Path | str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else repo_root / path


def _read_models[ModelT: StrictModel](path: Path, model_type: type[ModelT]) -> tuple[ModelT, ...]:
    rows: list[ModelT] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(model_type.model_validate_json(line))
            except Exception as exc:
                raise FullSourceConsumerError(
                    f"invalid {model_type.__name__} at {path}:{number}: {exc}"
                ) from exc
    return tuple(rows)


def _verify_pilot_artifact_hashes(
    gate: Matched500GateSpec,
    artifact_root: Path,
    *,
    expected_files: tuple[str, ...],
) -> dict[str, str]:
    pins = {item.path: item.sha256 for item in gate.artifact_files}
    if len(pins) != len(gate.artifact_files) or set(pins) != set(expected_files):
        raise FullSourceConsumerError("matched-500 evidence pin set is not exact")
    for name, expected in pins.items():
        path = artifact_root / name
        # Standard Hugging Face snapshots are symlink forests into the Hub's
        # content-addressed blob store.  The exact immutable revision and file
        # hashes are the trust boundary, so resolved regular files are valid.
        if not path.is_file() or not path.resolve().is_file() or hash_file(path) != expected:
            raise FullSourceConsumerError(f"matched-500 evidence hash mismatch: {name}")

    ledger: dict[str, str] = {}
    for line in (artifact_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise FullSourceConsumerError("matched-500 output SHA256SUMS is malformed")
        if parts[1] in ledger:
            raise FullSourceConsumerError("matched-500 output SHA256SUMS repeats a path")
        ledger[parts[1]] = parts[0]
    covered = set(PILOT_OUTPUT_FILES).difference({"SHA256SUMS"})
    if set(ledger) != covered:
        raise FullSourceConsumerError("matched-500 output SHA256SUMS coverage drifted")
    for name, expected in ledger.items():
        if pins[name] != expected or hash_file(artifact_root / name) != expected:
            raise FullSourceConsumerError(f"matched-500 checksum binding drifted: {name}")
    return pins


def _manifest_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FullSourceConsumerError(f"matched-500 manifest {label} is not an object")
    return cast(dict[str, Any], value)


def _verify_pilot_request_contract(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    pipeline: Matched500PipelineSpec,
    pipeline_hash: str,
    verified_input: VerifiedInput,
) -> VerifiedPilotRequestContract:
    """Recompute the exact matched request identities from independently pinned inputs."""

    code_pins = {item.path: item.sha256 for item in pipeline.code_pins}
    if len(code_pins) != len(pipeline.code_pins):
        raise FullSourceConsumerError("matched-500 pipeline repeats a code pin")
    for relative, expected in code_pins.items():
        path = repo_root / relative
        if not path.is_file() or path.is_symlink() or hash_file(path) != expected:
            raise FullSourceConsumerError(f"matched-500 code pin drifted: {relative}")

    source_config = _json_object(repo_root / pipeline.input.source_config_path)
    placement_config = _json_object(repo_root / pipeline.model.placement_config_path)
    source_prompt = _manifest_mapping(source_config.get("prompt"), label="source prompt")
    source_tokenizer = _manifest_mapping(source_config.get("tokenizer"), label="source tokenizer")
    source_placement = _manifest_mapping(source_config.get("placement"), label="source placement")
    manifest_prompt = _manifest_mapping(verified_input.manifest.get("prompt"), label="input prompt")
    manifest_tokenizer = _manifest_mapping(
        verified_input.manifest.get("tokenizer"), label="input tokenizer"
    )
    manifest_placement = _manifest_mapping(
        verified_input.manifest.get("placement"), label="input placement"
    )

    prompt_path_value = placement_config.get("prompt_path")
    if not isinstance(prompt_path_value, str):
        raise FullSourceConsumerError("matched-500 placement lacks a prompt path")
    prompt_path = repo_root / prompt_path_value
    if not prompt_path.is_file() or prompt_path.is_symlink():
        raise FullSourceConsumerError("matched-500 prompt is absent or a symlink")
    prompt_sha = hash_file(prompt_path)
    tokenizer_files = source_tokenizer.get("files")
    if not isinstance(tokenizer_files, dict):
        raise FullSourceConsumerError("matched-500 tokenizer file pins are malformed")
    remote_files = placement_config.get("remote_files")
    if not isinstance(remote_files, list):
        raise FullSourceConsumerError("matched-500 placement remote files are malformed")
    remote_tokenizers = [
        item
        for item in remote_files
        if isinstance(item, dict) and item.get("path") == "tokenizer.json"
    ]
    if len(remote_tokenizers) != 1:
        raise FullSourceConsumerError("matched-500 placement tokenizer pin is not unique")
    remote_tokenizer = cast(dict[str, Any], remote_tokenizers[0])
    tokenizer_sha = source_tokenizer.get("primary_sha256")
    expected_slots = [
        {"slot": slot.value, "seed": seed}
        for slot, seed in zip(
            pipeline.generation.slots,
            pipeline.generation.seeds,
            strict=True,
        )
    ]
    if (
        tuple(pipeline.generation.slots) != tuple(CandidateSlot)
        or tuple(pipeline.generation.seeds) != EXPECTED_SEEDS
        or placement_config.get("schema_version") != "sft2b_reform_32b_placement_v1"
        or placement_config.get("model_id") != pipeline.model.model_id
        or placement_config.get("model_revision") != pipeline.model.revision
        or placement_config.get("candidate_slots") != expected_slots
        or placement_config.get("extraction_contract") != "final_theorem_signature_v1"
        or source_placement.get("path") != pipeline.model.placement_config_path
        or source_placement.get("sha256") != pipeline.model.placement_config_sha256
        or source_placement.get("model_id") != pipeline.model.model_id
        or source_placement.get("model_revision") != pipeline.model.revision
        or source_placement.get("slots") != expected_slots
        or manifest_placement.get("path") != pipeline.model.placement_config_path
        or manifest_placement.get("sha256") != pipeline.model.placement_config_sha256
        or manifest_placement.get("model_id") != pipeline.model.model_id
        or manifest_placement.get("model_revision") != pipeline.model.revision
        or manifest_placement.get("candidate_slots") != expected_slots
    ):
        raise FullSourceConsumerError("matched-500 placement/slot identity drifted")

    decoding = _manifest_mapping(placement_config.get("decoding"), label="decoding")
    decoding_sha = hash_canonical(decoding)
    if (
        source_placement.get("max_new_tokens") != decoding.get("max_new_tokens")
        or manifest_placement.get("decoding") != decoding
        or manifest_placement.get("required_max_model_len") != pipeline.input.required_max_model_len
        or pipeline.generation.max_model_len != pipeline.input.required_max_model_len
        or decoding.get("max_new_tokens")
        != pipeline.input.required_max_model_len - pipeline.input.maximum_prompt_tokens
    ):
        raise FullSourceConsumerError("matched-500 decoding/model-length identity drifted")

    if (
        prompt_path_value != source_prompt.get("path")
        or prompt_path_value != manifest_prompt.get("path")
        or prompt_sha != placement_config.get("prompt_sha256")
        or prompt_sha != source_prompt.get("sha256")
        or prompt_sha != manifest_prompt.get("sha256")
        or prompt_sha != manifest_prompt.get("observed_sha256")
        or code_pins.get(prompt_path_value) != prompt_sha
        or verified_input.manifest.get("source_config_path") != pipeline.input.source_config_path
        or verified_input.manifest.get("source_config_sha256")
        != pipeline.input.source_config_sha256
    ):
        raise FullSourceConsumerError("matched-500 prompt/source-config binding drifted")
    if (
        not isinstance(tokenizer_sha, str)
        or source_tokenizer.get("model_id") != pipeline.model.model_id
        or source_tokenizer.get("revision") != pipeline.model.revision
        or source_tokenizer.get("primary_file") != "tokenizer.json"
        or tokenizer_files.get("tokenizer.json") != tokenizer_sha
        or manifest_tokenizer != source_tokenizer
        or remote_tokenizer.get("hash_kind") != "sha256"
        or remote_tokenizer.get("hash") != tokenizer_sha
    ):
        raise FullSourceConsumerError("matched-500 tokenizer binding drifted")

    token_payload = _json_object(verified_input.root / "prompt_token_counts.json")
    if (
        token_payload.get("schema_version") != "sft2b_prompt_token_counts_v1"
        or token_payload.get("source_count") != 500
        or token_payload.get("model_id") != pipeline.model.model_id
        or token_payload.get("model_revision") != pipeline.model.revision
        or token_payload.get("prompt_path") != prompt_path_value
        or token_payload.get("prompt_sha256") != prompt_sha
        or token_payload.get("tokenizer_model_id") != pipeline.model.model_id
        or token_payload.get("tokenizer_revision") != pipeline.model.revision
        or token_payload.get("tokenizer_sha256") != tokenizer_sha
        or token_payload.get("maximum_prompt_tokens") != pipeline.input.maximum_prompt_tokens
        or token_payload.get("max_new_tokens") != decoding.get("max_new_tokens")
        or token_payload.get("required_max_model_len") != pipeline.input.required_max_model_len
    ):
        raise FullSourceConsumerError("matched-500 prompt/tokenizer summary drifted")
    if (
        spec.model.model_id != pipeline.model.model_id
        or spec.model.revision != pipeline.model.revision
        or spec.model.placement_config_path != pipeline.model.placement_config_path
        or spec.model.placement_config_sha256 != pipeline.model.placement_config_sha256
        or spec.model.prompt_path != prompt_path_value
        or spec.model.prompt_sha256 != prompt_sha
        or spec.model.tokenizer_sha256 != tokenizer_sha
        or spec.model.snapshot_binding_sha256 != pipeline.model.snapshot_binding_sha256
        or [(item.slot.value, item.seed) for item in spec.model.slots]
        != [(item["slot"], item["seed"]) for item in expected_slots]
    ):
        raise FullSourceConsumerError("full-source model contract differs from matched pilot")

    template = prompt_path.read_text(encoding="utf-8")
    if template.count("{{NL}}") != 1:
        raise FullSourceConsumerError("matched-500 prompt placeholder contract drifted")
    raw_rows = token_payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(verified_input.rows):
        raise FullSourceConsumerError("matched-500 prompt-token rows are not exactly 500")
    prompt_input_sha: dict[str, str] = {}
    prompt_tokens: dict[str, int] = {}
    expected_request_keys: list[str] = []
    expected_attempt_ids: list[str] = []
    expected_payload_hashes: list[str] = []
    expected_request_artifact_hashes: list[str] = []
    expected_started_hashes: list[str] = []
    for source, raw in zip(verified_input.rows, raw_rows, strict=True):
        if not isinstance(raw, dict) or set(raw) != {
            "source_id",
            "prompt_sha256",
            "prompt_tokens",
        }:
            raise FullSourceConsumerError("matched-500 prompt-token row schema drifted")
        prompt = template.replace("{{NL}}", source.nl_statement)
        rendered_sha = sha256_hex(prompt.encode("utf-8"))
        token_count = raw.get("prompt_tokens")
        if (
            raw.get("source_id") != source.source_id
            or raw.get("prompt_sha256") != rendered_sha
            or source.reference_proposition in prompt
            or not isinstance(token_count, int)
            or token_count < 1
            or verified_input.prompt_tokens.get(source.source_id) != token_count
        ):
            raise FullSourceConsumerError("matched-500 rendered prompt row drifted")
        prompt_input_sha[source.source_id] = rendered_sha
        prompt_tokens[source.source_id] = token_count
        for slot, seed in zip(
            pipeline.generation.slots,
            pipeline.generation.seeds,
            strict=True,
        ):
            request_key = hash_canonical(
                {
                    "schema_version": "sft2b_vllm_request_key_v1",
                    "profile_id": pipeline.generation.profile_id,
                    "backend_config_sha256": pipeline_hash,
                    "source_id": source.source_id,
                    "slot": slot,
                    "seed": seed,
                    "model_revision": pipeline.model.revision,
                    "snapshot_binding_sha256": pipeline.model.snapshot_binding_sha256,
                    "prompt_input_sha256": rendered_sha,
                    "prompt_template_sha256": prompt_sha,
                    "decoding_sha256": decoding_sha,
                }
            )
            expected_request_keys.append(request_key)
            expected_attempt_ids.append(
                stable_id(
                    "sft2b_formalizer_attempt",
                    {"request_key": request_key, "provider": "local_vllm_openai"},
                )
            )
            payload: dict[str, object] = {
                "model": pipeline.model.served_model_name,
                "prompt": prompt,
                "n": 1,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": decoding["max_new_tokens"],
                "temperature": decoding["temperature"],
                "top_k": decoding["top_k"],
                "top_p": decoding["top_p"],
                "repetition_penalty": decoding["repetition_penalty"],
                "seed": seed,
            }
            expected_payload_hashes.append(hash_canonical(payload))
            expected_request_artifact_hashes.append(
                sha256_hex(
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_vllm_request_v1",
                            "request_key": request_key,
                            "attempt_id": expected_attempt_ids[-1],
                            "profile_id": pipeline.generation.profile_id,
                            "source_id": source.source_id,
                            "slot": slot,
                            "seed": seed,
                            "endpoint_url": (
                                f"http://127.0.0.1:{pipeline.generation.port}/v1/completions"
                            ),
                            "payload": payload,
                        }
                    )
                    + b"\n"
                )
            )
            expected_started_hashes.append(
                sha256_hex(
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_vllm_request_started_v1",
                            "request_key": request_key,
                        }
                    )
                    + b"\n"
                )
            )
    run_id = stable_id(
        "sft2b_vllm_run",
        {
            "profile_id": pipeline.generation.profile_id,
            "backend_config_sha256": pipeline_hash,
            "model_revision": pipeline.model.revision,
            "snapshot_binding_sha256": pipeline.model.snapshot_binding_sha256,
            "source_ids": tuple(row.source_id for row in verified_input.rows),
            "slots": pipeline.generation.slots,
        },
    )
    return VerifiedPilotRequestContract(
        run_id=run_id,
        pipeline_config_sha256=pipeline_hash,
        prompt_template_sha256=prompt_sha,
        request_keys=tuple(expected_request_keys),
        attempt_ids=tuple(expected_attempt_ids),
        prompt_input_sha256=prompt_input_sha,
        prompt_tokens=prompt_tokens,
        request_payload_sha256=tuple(expected_payload_hashes),
        request_artifact_sha256=tuple(expected_request_artifact_hashes),
        request_started_sha256=tuple(expected_started_hashes),
        decoding_sha256=decoding_sha,
        extraction_contract="final_theorem_signature_v1",
    )


@dataclass(frozen=True, slots=True)
class _ReplayedSse:
    response_id: str
    model_id: str
    output_text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str


@dataclass(frozen=True, slots=True)
class _VerifiedPilotRows:
    request_keys: tuple[str, ...]
    failure_taxonomy: Mapping[str, int]
    metrics: tuple[VllmRequestMetrics, ...]
    quality_metrics: ObservedPilotQualityMetrics


def _replay_sse(payload: bytes) -> _ReplayedSse:
    """Replay preserved OpenAI-compatible SSE bytes without a provider call."""

    response_id: str | None = None
    model_id: str | None = None
    text_parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    saw_done = False
    for line_number, line in enumerate(payload.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if saw_done:
            raise FullSourceConsumerError("pilot SSE contains data after its terminal DONE")
        if not stripped.startswith(b"data:"):
            raise FullSourceConsumerError(f"pilot SSE contains a non-data line at {line_number}")
        data = stripped[5:].strip()
        if data == b"[DONE]":
            saw_done = True
            continue
        try:
            event: object = json.loads(data)
        except json.JSONDecodeError as exc:
            raise FullSourceConsumerError("pilot SSE contains malformed JSON") from exc
        event_map = _manifest_mapping(event, label="SSE event")
        event_model = event_map.get("model")
        if not isinstance(event_model, str) or not event_model:
            raise FullSourceConsumerError("pilot SSE event lacks a model identity")
        if model_id is not None and model_id != event_model:
            raise FullSourceConsumerError("pilot SSE model identity changed")
        model_id = event_model
        event_id = event_map.get("id")
        if isinstance(event_id, str) and event_id:
            if response_id is not None and response_id != event_id:
                raise FullSourceConsumerError("pilot SSE response ID changed")
            response_id = event_id
        choices = event_map.get("choices")
        if choices is not None and not isinstance(choices, list):
            raise FullSourceConsumerError("pilot SSE choices is not a list")
        for choice in cast(list[object], choices or []):
            choice_map = _manifest_mapping(choice, label="SSE choice")
            part = choice_map.get("text")
            if part is not None and not isinstance(part, str):
                raise FullSourceConsumerError("pilot SSE text fragment is not a string")
            if isinstance(part, str):
                text_parts.append(part)
            reason = choice_map.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    raise FullSourceConsumerError("pilot SSE finish reason is invalid")
                finish_reason = reason
        usage = event_map.get("usage")
        if usage is not None:
            usage_map = _manifest_mapping(usage, label="SSE usage")
            prompt_value = usage_map.get("prompt_tokens")
            completion_value = usage_map.get("completion_tokens")
            if (
                not isinstance(prompt_value, int)
                or isinstance(prompt_value, bool)
                or prompt_value < 1
                or not isinstance(completion_value, int)
                or isinstance(completion_value, bool)
                or completion_value < 1
            ):
                raise FullSourceConsumerError("pilot SSE usage is invalid")
            prompt_tokens = prompt_value
            completion_tokens = completion_value
    if (
        not saw_done
        or response_id is None
        or model_id is None
        or not text_parts
        or prompt_tokens is None
        or completion_tokens is None
        or finish_reason is None
    ):
        raise FullSourceConsumerError("pilot SSE lacks a terminal ID/text/usage/reason/DONE")
    return _ReplayedSse(
        response_id=response_id,
        model_id=model_id,
        output_text="".join(text_parts),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )


def _verify_pilot_rows(
    artifact_root: Path,
    *,
    pipeline: Matched500PipelineSpec,
    sources: tuple[SourceRecord, ...],
    selection_mix: Mapping[str, int],
    manifest: Mapping[str, Any],
    contract: VerifiedPilotRequestContract,
) -> _VerifiedPilotRows:
    attempts = _read_models(artifact_root / "formalizer_attempts.jsonl", FormalizerAttempt)
    candidates = _read_models(artifact_root / "candidates.jsonl", CandidateRecord)
    invalid = _read_models(
        artifact_root / "formalizer_invalid_attempts.jsonl",
        FormalizerInvalidAttemptView,
    )
    metrics = _read_models(artifact_root / "request_metrics.jsonl", VllmRequestMetrics)
    terminals = _read_models(artifact_root / "request_terminals.jsonl", VllmRequestTerminal)
    source_ids = tuple(item.source_id for item in sources)
    source_by_id = {item.source_id: item for item in sources}
    expected_cells = tuple((source_id, slot) for source_id in source_ids for slot in CandidateSlot)
    if (
        len(expected_cells) != 2000
        or tuple((item.source_id, item.slot) for item in attempts) != expected_cells
    ):
        raise FullSourceConsumerError("pilot attempts are not the ordered exact 500x4 product")
    if not (
        len(attempts) == len(metrics) == len(terminals) == 2000
        and [item.attempt_id for item in attempts] == [item.attempt_id for item in metrics]
        and [item.attempt_id for item in attempts]
        == [item.attempt.attempt_id for item in terminals]
    ):
        raise FullSourceConsumerError("pilot attempt/metric/terminal joins drifted")
    request_keys = tuple(item.request_key for item in terminals)
    if (
        request_keys != contract.request_keys
        or tuple(item.request_key for item in metrics) != request_keys
    ):
        raise FullSourceConsumerError("pilot request keys differ from exact frozen recomputation")
    manifest_keys = manifest.get("request_keys")
    if not isinstance(manifest_keys, list) or tuple(manifest_keys) != request_keys:
        raise FullSourceConsumerError("pilot manifest request-key order drifted")
    if tuple(item.attempt_id for item in attempts) != contract.attempt_ids:
        raise FullSourceConsumerError("pilot attempt IDs differ from exact request derivation")

    expected_endpoint = f"http://127.0.0.1:{pipeline.generation.port}/v1/completions"
    with (artifact_root / "raw_generations.jsonl").open(encoding="utf-8") as raw_handle:
        for ordinal, (attempt, metric, terminal) in enumerate(
            zip(attempts, metrics, terminals, strict=True)
        ):
            try:
                raw_line = next(raw_handle)
            except StopIteration as exc:
                raise FullSourceConsumerError(
                    "pilot raw-generation stream ended before the exact 500x4 product"
                ) from exc
            if not raw_line.strip():
                raise FullSourceConsumerError("pilot raw-generation JSONL contains a blank line")
            try:
                raw_value: object = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise FullSourceConsumerError("pilot raw-generation JSON is malformed") from exc
            raw = _manifest_mapping(raw_value, label=f"raw generation {ordinal + 1}")
            if (
                set(raw)
                != {
                    "schema_version",
                    "request_key",
                    "attempt_id",
                    "source_id",
                    "slot",
                    "response_id",
                    "raw_output",
                    "raw_output_sha256",
                    "raw_response_base64",
                    "raw_response_sha256",
                }
                or raw.get("schema_version") != "sft2b_raw_generation_v1"
            ):
                raise FullSourceConsumerError("pilot raw-generation schema drifted")
            encoded_response = raw.get("raw_response_base64")
            if not isinstance(encoded_response, str):
                raise FullSourceConsumerError("pilot raw response is not a base64 string")
            try:
                response_bytes = base64.b64decode(encoded_response, validate=True)
            except Exception as exc:
                raise FullSourceConsumerError("pilot raw response is not strict base64") from exc
            replayed = _replay_sse(response_bytes)
            slot_seed = pipeline.generation.seeds[ordinal % len(pipeline.generation.seeds)]
            expected_prompt_sha = contract.prompt_input_sha256[attempt.source_id]
            expected_prompt_tokens = contract.prompt_tokens[attempt.source_id]
            lineage = attempt.lineage
            raw_output = raw.get("raw_output")
            source = source_by_id[attempt.source_id]
            runtime_versions = _manifest_mapping(
                manifest.get("runtime_versions"), label="runtime versions"
            )
            if (
                attempt.prompt_input_sha256 != expected_prompt_sha
                or attempt.prompt_tokens != expected_prompt_tokens
                or lineage.origin != CandidateOrigin.REFORM_32B
                or lineage.provider != "local_vllm_openai"
                or lineage.model_id != pipeline.model.model_id
                or lineage.model_revision != pipeline.model.revision
                or lineage.prompt_sha256 != contract.prompt_template_sha256
                or lineage.decoding_sha256 != contract.decoding_sha256
                or lineage.seed != slot_seed
                or lineage.upstream_call_id != attempt.attempt_id
                or lineage.upstream_generation_config_sha256 != contract.pipeline_config_sha256
                or attempt.raw_output_path != metric.raw_output_path
                or attempt.raw_output_sha256 != metric.raw_output_sha256
                or attempt.elapsed_ms != metric.elapsed_ms
                or attempt.prompt_tokens != metric.prompt_tokens
                or attempt.completion_tokens != metric.completion_tokens
                or attempt.peak_cuda_allocated_bytes != 0
                or attempt.peak_cuda_reserved_bytes != 0
                or attempt.torch_version != runtime_versions.get("torch")
                or attempt.transformers_version != runtime_versions.get("transformers")
            ):
                raise FullSourceConsumerError("pilot attempt lineage/prompt binding drifted")
            if (
                terminal.attempt != attempt
                or terminal.metrics != metric
                or metric.profile_id != pipeline.generation.profile_id
                or metric.source_id != attempt.source_id
                or metric.slot != attempt.slot
                or metric.endpoint_url != expected_endpoint
                or metric.request_payload_sha256 != contract.request_payload_sha256[ordinal]
                or metric.prompt_tokens != expected_prompt_tokens
                or metric.response_request_id != metric.request_key
                or metric.http_status != 200
                or metric.time_to_first_token_ms > metric.elapsed_ms
                or not isinstance(raw_output, str)
                or raw.get("request_key") != terminal.request_key
                or raw.get("attempt_id") != attempt.attempt_id
                or raw.get("source_id") != attempt.source_id
                or raw.get("slot") != attempt.slot.value
                or raw.get("response_id") != metric.response_id
                or raw.get("raw_output_sha256") != sha256_hex(raw_output.encode())
                or raw.get("raw_response_sha256") != sha256_hex(response_bytes)
                or raw.get("raw_output_sha256") != metric.raw_output_sha256
                or raw.get("raw_response_sha256") != metric.raw_response_sha256
                or replayed.response_id != metric.response_id
                or replayed.model_id != pipeline.model.served_model_name
                or replayed.output_text != raw_output
                or replayed.prompt_tokens != metric.prompt_tokens
                or replayed.completion_tokens != metric.completion_tokens
                or replayed.finish_reason != metric.finish_reason
            ):
                raise FullSourceConsumerError("pilot terminal metric/request binding drifted")
            proposition, failure = extract_candidate(
                raw_output,
                extraction_contract=contract.extraction_contract,
            )
            if attempt.extraction_status == "candidate":
                candidate = terminal.candidate
                if (
                    proposition is None
                    or failure is not None
                    or candidate is None
                    or candidate.raw_proof_free_signature != proposition
                    or candidate.source_context_id != source.compile_context.source_context_id
                    or attempt.failure_class is not None
                    or attempt.failure_detail is not None
                ):
                    raise FullSourceConsumerError(
                        "pilot deterministic candidate extraction drifted"
                    )
            elif (
                proposition is not None
                or failure is None
                or terminal.candidate is not None
                or attempt.failure_class != "formalizer_output_contract"
                or attempt.failure_detail != failure
            ):
                raise FullSourceConsumerError("pilot deterministic invalid extraction drifted")
            expected_artifacts = {
                "request": contract.request_artifact_sha256[ordinal],
                "request_started": contract.request_started_sha256[ordinal],
                "raw_response": sha256_hex(response_bytes),
                "raw_output": sha256_hex(raw_output.encode()),
                "attempt": sha256_hex(
                    canonical_json_bytes(attempt.model_dump(mode="json")) + b"\n"
                ),
                "metrics": sha256_hex(canonical_json_bytes(metric.model_dump(mode="json")) + b"\n"),
            }
            if terminal.candidate is not None:
                expected_artifacts["candidate"] = sha256_hex(
                    canonical_json_bytes(terminal.candidate.model_dump(mode="json")) + b"\n"
                )
            if terminal.artifact_sha256 != expected_artifacts:
                raise FullSourceConsumerError("pilot terminal artifact hash map drifted")
            # Do not retain base64 strings, decoded responses, or replay buffers
            # after this order-locked row join.
            del encoded_response, response_bytes, replayed, raw, raw_value, raw_line
        if next(raw_handle, None) is not None:
            raise FullSourceConsumerError(
                "pilot raw-generation stream exceeds the exact 500x4 product"
            )

    candidate_attempt_ids = {
        item.attempt_id for item in attempts if item.extraction_status == "candidate"
    }
    invalid_attempt_ids = {
        item.attempt_id for item in attempts if item.extraction_status == "invalid"
    }
    if (
        len(candidates) != len(candidate_attempt_ids)
        or len(invalid) != len(invalid_attempt_ids)
        or len({item.candidate_id for item in candidates}) != len(candidates)
        or len({item.attempt_id for item in invalid}) != len(invalid)
        or len({item.request_key for item in terminals}) != len(terminals)
        or len({item.attempt_id for item in attempts}) != len(attempts)
        or {item.candidate_id for item in candidates}
        != {item.candidate_id for item in attempts if item.candidate_id is not None}
        or {item.attempt_id for item in invalid} != invalid_attempt_ids
        or len(candidate_attempt_ids) + len(invalid_attempt_ids) != 2000
    ):
        raise FullSourceConsumerError("pilot candidate/invalid routing drifted")
    candidates_by_id = {item.candidate_id: item for item in candidates}
    invalid_by_attempt = {item.attempt_id: item for item in invalid}
    for attempt, terminal in zip(attempts, terminals, strict=True):
        if attempt.extraction_status == "candidate":
            candidate = candidates_by_id.get(cast(str, attempt.candidate_id))
            if (
                candidate is None
                or terminal.candidate != candidate
                or candidate.source_id != attempt.source_id
                or candidate.slot != attempt.slot
                or candidate.lineage != attempt.lineage
                or candidate.source_context_id
                != source_by_id[attempt.source_id].compile_context.source_context_id
            ):
                raise FullSourceConsumerError("pilot candidate lineage/routing drifted")
        else:
            view = invalid_by_attempt.get(attempt.attempt_id)
            if (
                terminal.candidate is not None
                or view is None
                or view.source_id != attempt.source_id
                or view.slot != attempt.slot
                or view.failure_class != attempt.failure_class
                or view.failure_detail != attempt.failure_detail
                or view.raw_output_sha256 != attempt.raw_output_sha256
            ):
                raise FullSourceConsumerError("pilot invalid-attempt routing drifted")
    failure_taxonomy = Counter(
        "candidate" if item.extraction_status == "candidate" else cast(str, item.failure_class)
        for item in attempts
    )

    journal_rows: list[dict[str, Any]] = []
    with (artifact_root / "requests_journal.jsonl").open(encoding="utf-8") as handle:
        journal_lines = tuple(handle)
    for number, line in enumerate(journal_lines):
        if not line.strip():
            raise FullSourceConsumerError("pilot request journal contains a blank line")
        try:
            journal_value: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FullSourceConsumerError("pilot request journal is malformed") from exc
        row = _manifest_mapping(journal_value, label=f"journal row {number}")
        if (
            set(row)
            != {
                "schema_version",
                "sequence",
                "request_key",
                "attempt_id",
                "source_id",
                "slot",
                "terminal_path",
                "terminal_sha256",
            }
            or row.get("schema_version") != "sft2b_vllm_journal_event_v1"
        ):
            raise FullSourceConsumerError("pilot request journal schema drifted")
        journal_rows.append(row)
    if len(journal_rows) != 2000 or [row.get("sequence") for row in journal_rows] != list(
        range(2000)
    ):
        raise FullSourceConsumerError("pilot request journal sequence/count drifted")
    journal_by_key = {str(row.get("request_key")): row for row in journal_rows}
    if len(journal_by_key) != 2000 or set(journal_by_key) != set(request_keys):
        raise FullSourceConsumerError("pilot journal request-key coverage drifted")
    terminals_by_key = {item.request_key: item for item in terminals}
    for key, row in journal_by_key.items():
        terminal = terminals_by_key[key]
        # ``write_model`` writes canonical JSON plus one newline; hash the exact
        # bytes rather than accepting a journal's self-description.
        terminal_bytes_sha = hashlib.sha256(
            canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n"
        ).hexdigest()
        if (
            row.get("attempt_id") != terminal.attempt.attempt_id
            or row.get("source_id") != terminal.attempt.source_id
            or row.get("slot") != terminal.attempt.slot.value
            or row.get("terminal_path")
            != str(Path(terminal.metrics.raw_output_path).parent / "terminal.json")
            or row.get("terminal_sha256") != terminal_bytes_sha
        ):
            raise FullSourceConsumerError("pilot journal terminal binding drifted")

    counts = _manifest_mapping(manifest.get("counts"), label="counts")
    expected_counts = {
        "sources": 500,
        "attempts": 2000,
        "candidates": len(candidates),
        "formalizer_invalid": len(invalid),
        "metrics": 2000,
        "terminals": 2000,
        "raw_generations": 2000,
        "lean_calls": 0,
        "judge_calls": 0,
        "core_rows": 0,
        "semantic_labels": 0,
    }
    if any(counts.get(key) != value for key, value in expected_counts.items()):
        raise FullSourceConsumerError("pilot output counts drifted")
    if manifest.get("generation") != pipeline.generation.model_dump(mode="json"):
        raise FullSourceConsumerError("pilot generation identity differs from its frozen config")
    candidate_ids = [item.candidate_id for item in attempts]
    if manifest.get("candidate_ids") != candidate_ids:
        raise FullSourceConsumerError("pilot manifest candidate IDs drifted")
    candidates_by_slot = Counter(item.slot.value for item in candidates)
    candidates_by_source = Counter(item.source_id for item in candidates)
    source_candidate_histogram = Counter(
        candidates_by_source.get(source_id, 0) for source_id in source_ids
    )
    unique_signatures = len({item.signature_sha256 for item in candidates})
    finish_reasons = Counter(item.finish_reason for item in metrics)
    failure_counts = dict(failure_taxonomy)
    quality_metrics = ObservedPilotQualityMetrics(
        measurement_scope=("formalizer_output_contract_only_not_lean_validity_or_semantic_quality"),
        output_contract_admitted=len(candidates),
        output_contract_rejected=len(invalid),
        output_contract_admission_rate=len(candidates) / 2000,
        candidates_by_slot={slot.value: candidates_by_slot[slot.value] for slot in CandidateSlot},
        sources_by_candidate_count={
            str(count): source_candidate_histogram[count] for count in range(5)
        },
        unique_candidate_signatures=unique_signatures,
        duplicate_candidate_signatures=len(candidates) - unique_signatures,
        finish_reasons=dict(finish_reasons),
        truncated_requests=finish_reasons.get("length", 0),
        failure_taxonomy=failure_counts,
        selection_mix=dict(selection_mix),
        provenance_origin_mix=dict(Counter(item.provenance.source_family for item in sources)),
    )
    return _VerifiedPilotRows(
        request_keys=request_keys,
        failure_taxonomy=failure_counts,
        metrics=metrics,
        quality_metrics=quality_metrics,
    )


def _telemetry_summary(samples: Sequence[Any], *, errors: Sequence[str] = ()) -> dict[str, object]:
    gpu_indices = sorted({gpu.index for sample in samples for gpu in sample.gpus})
    running = [sample.requests_running for sample in samples if sample.requests_running is not None]
    waiting = [sample.requests_waiting for sample in samples if sample.requests_waiting is not None]
    rss = [
        sample.server_process_tree_rss_bytes
        for sample in samples
        if sample.server_process_tree_rss_bytes is not None
    ]
    return {
        "schema_version": "sft2b_vllm_telemetry_summary_v1",
        "samples": len(samples),
        "errors": list(errors),
        "peak_by_gpu": {
            str(index): {
                "memory_used_mib": max(
                    gpu.memory_used_mib
                    for sample in samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
                "utilization_gpu_percent": max(
                    gpu.utilization_gpu_percent
                    for sample in samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
                "power_draw_watts": max(
                    gpu.power_draw_watts
                    for sample in samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
            }
            for index in gpu_indices
        },
        "max_requests_running": max(running) if running else None,
        "max_requests_waiting": max(waiting) if waiting else None,
        "peak_server_process_tree_rss_bytes": max(rss) if rss else None,
        "peak_system_ram_used_bytes": max(item.system_ram_used_bytes for item in samples),
        "minimum_system_ram_available_bytes": min(
            item.system_ram_available_bytes for item in samples
        ),
    }


@dataclass(frozen=True, slots=True)
class _VerifiedPilotRuntimeArtifacts:
    telemetry_summary: Mapping[str, object]
    server_observation: Mapping[str, object]
    runtime_versions: Mapping[str, str]
    gpu_inventory: tuple[Mapping[str, object], ...]
    first_unix_ns: int
    last_unix_ns: int
    server_log_pids: frozenset[int]


def _verify_pilot_runtime_artifacts(
    *,
    artifact_root: Path,
    pipeline: Matched500PipelineSpec,
    manifest: Mapping[str, Any],
) -> _VerifiedPilotRuntimeArtifacts:
    try:
        samples = load_samples(artifact_root / "telemetry.jsonl")
    except Exception as exc:
        raise FullSourceConsumerError(f"matched-500 telemetry schema failed: {exc}") from exc
    if len(samples) < 2:
        raise FullSourceConsumerError("matched-500 telemetry requires a real multi-sample timeline")
    monotonic = [item.monotonic_ns for item in samples]
    unix_times = [item.unix_time_ns for item in samples]
    if any(right <= left for left, right in pairwise(monotonic)) or any(
        right <= left for left, right in pairwise(unix_times)
    ):
        raise FullSourceConsumerError("matched-500 telemetry timeline is not strictly increasing")

    raw_inventory = manifest.get("gpu_inventory")
    if not isinstance(raw_inventory, list) or len(raw_inventory) != 8:
        raise FullSourceConsumerError("matched-500 GPU inventory is not exactly eight records")
    inventory: list[Mapping[str, object]] = []
    uuids: set[str] = set()
    for expected_index, raw in enumerate(raw_inventory):
        if not isinstance(raw, dict) or set(raw) != {
            "index",
            "name",
            "uuid",
            "memory_total_mib",
            "memory_used_mib",
        }:
            raise FullSourceConsumerError("matched-500 GPU inventory schema drifted")
        name = raw.get("name")
        uuid = raw.get("uuid")
        total = raw.get("memory_total_mib")
        used = raw.get("memory_used_mib")
        if (
            raw.get("index") != expected_index
            or not isinstance(name, str)
            or not any(fragment in name for fragment in pipeline.runtime.allowed_gpu_name_fragments)
            or not isinstance(uuid, str)
            or not uuid
            or uuid in uuids
            or not isinstance(total, int)
            or total < pipeline.runtime.minimum_gpu_memory_mib
            or not isinstance(used, int)
            or used < 0
            or used > total
        ):
            raise FullSourceConsumerError("matched-500 GPU inventory hardware identity drifted")
        uuids.add(uuid)
        inventory.append(cast(Mapping[str, object], raw))
    inventory_by_index = {cast(int, item["index"]): item for item in inventory}
    for sample in samples:
        if tuple(gpu.index for gpu in sample.gpus) != _EXPECTED_GPU_INDICES:
            raise FullSourceConsumerError("matched-500 telemetry GPU coverage/order drifted")
        for gpu in sample.gpus:
            record = inventory_by_index[gpu.index]
            if (
                gpu.uuid != record["uuid"]
                or gpu.memory_total_mib != record["memory_total_mib"]
                or gpu.memory_used_mib > gpu.memory_total_mib
            ):
                raise FullSourceConsumerError("matched-500 telemetry hardware identity drifted")
    if not any(
        (sample.requests_running or 0.0) > 0
        or any(gpu.utilization_gpu_percent > 0 for gpu in sample.gpus)
        for sample in samples
    ) or not any((sample.server_process_tree_rss_bytes or 0) > 0 for sample in samples):
        raise FullSourceConsumerError("matched-500 telemetry contains no observed server activity")

    summary = _telemetry_summary(samples)
    observation = _manifest_mapping(manifest.get("server_observation"), label="server observation")
    versions = _manifest_mapping(manifest.get("runtime_versions"), label="runtime versions")
    if (
        set(versions) != _PILOT_RUNTIME_PACKAGES
        or any(not isinstance(value, str) or not value for value in versions.values())
        or versions.get("vllm") != pipeline.runtime.vllm_version
        or observation.get("health_status") != 200
        or observation.get("models_status") != 200
        or observation.get("model_ids") != [pipeline.model.served_model_name]
        or observation.get("runtime_versions") != versions
        or observation.get("telemetry") != summary
    ):
        raise FullSourceConsumerError("matched-500 runtime/server observation drifted")

    log_path = artifact_root / "vllm_server.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if (
        log_path.stat().st_size < 64
        or len([line for line in log_text.splitlines() if line.strip()]) < 2
        or _SERVER_READY_RE.search(log_text) is None
    ):
        raise FullSourceConsumerError("matched-500 server log lacks substantive readiness evidence")
    log_pids = frozenset(int(match.group("pid")) for match in _SERVER_PID_RE.finditer(log_text))
    if not log_pids:
        raise FullSourceConsumerError("matched-500 server log lacks a server PID observation")
    return _VerifiedPilotRuntimeArtifacts(
        telemetry_summary=summary,
        server_observation=observation,
        runtime_versions=cast(Mapping[str, str], versions),
        gpu_inventory=tuple(inventory),
        first_unix_ns=unix_times[0],
        last_unix_ns=unix_times[-1],
        server_log_pids=log_pids,
    )


@dataclass(frozen=True, slots=True)
class _ObservedPilotVerification:
    receipt: ObservedPilotReceipt
    artifact_root: Path
    pipeline: Matched500PipelineSpec
    manifest: Mapping[str, Any]
    contract: VerifiedPilotRequestContract
    rows: _VerifiedPilotRows
    runtime: _VerifiedPilotRuntimeArtifacts


def _verify_manifest_git_snapshot(
    repo_root: Path,
    *,
    git_commit: str,
    pipeline: Matched500PipelineSpec,
    pipeline_config_path: str,
    pipeline_config_sha256: str,
    runtime_code_pins: tuple[PilotArtifactPin, ...],
) -> None:
    """Prove the reported commit contains every hash-pinned runner input."""

    if _SHA40_RE.fullmatch(git_commit) is None:
        raise FullSourceConsumerError("matched-500 manifest git commit is invalid")
    commit_check = subprocess.run(
        ("git", "cat-file", "-e", f"{git_commit}^{{commit}}"),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if commit_check.returncode != 0:
        raise FullSourceConsumerError("matched-500 manifest git commit is unavailable")
    expected_by_path: dict[str, str] = {}
    for pin in pipeline.code_pins:
        expected_by_path[pin.path] = pin.sha256
    for runtime_pin in runtime_code_pins:
        prior = expected_by_path.setdefault(runtime_pin.path, runtime_pin.sha256)
        if prior != runtime_pin.sha256:
            raise FullSourceConsumerError(f"pilot code pins conflict for {runtime_pin.path}")
    if not Path(pipeline_config_path).is_absolute():
        expected_by_path[pipeline_config_path] = pipeline_config_sha256
    for path, expected_sha256 in expected_by_path.items():
        snapshot = subprocess.run(
            ("git", "show", f"{git_commit}:{path}"),
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if snapshot.returncode != 0 or sha256_hex(snapshot.stdout) != expected_sha256:
            raise FullSourceConsumerError(
                f"matched-500 manifest commit does not contain pinned input: {path}"
            )


def _verify_observed_pilot(
    repo_root: Path,
    spec: FullSourceConsumerSpec,
    *,
    artifact_root: Path,
    pilot_input_root: Path,
    expected_files: tuple[str, ...],
) -> _ObservedPilotVerification:
    """Open and replay the real output bundle without inventing missing receipts."""

    gate = spec.matched_500_gate
    consumer_spec_sha256 = hash_canonical(
        {
            "schema_version": spec.schema_version,
            "model": spec.model.model_dump(mode="json"),
            "pipeline_config_path": gate.pipeline_config_path,
            "pipeline_config_sha256": gate.pipeline_config_sha256,
            "observed_runtime_code_pins": [
                item.model_dump(mode="json") for item in gate.observed_runtime_code_pins
            ],
            "consumer_code_pins": [item.model_dump(mode="json") for item in spec.code_pins],
        }
    )
    verifier_pins = {item.path: item.sha256 for item in spec.code_pins}
    verifier_sha256 = verifier_pins.get("src/leanfaith/sft2b/full_source_consumer.py")
    if verifier_sha256 is None:
        raise FullSourceConsumerError("consumer spec lacks its verifier implementation pin")
    pipeline_path = _resolve_from_repo(repo_root, gate.pipeline_config_path).resolve()
    if (
        gate.pipeline_config_sha256 is None
        or not pipeline_path.is_file()
        or hash_file(pipeline_path) != gate.pipeline_config_sha256
    ):
        raise FullSourceConsumerError("matched-500 pipeline config hash drifted")
    try:
        pipeline, pipeline_hash = load_pipeline_spec(repo_root, pipeline_path)
        verified_input = verify_input_without_model(
            repo_root,
            spec=pipeline,
            bundle_root=pilot_input_root,
        )
    except Exception as exc:
        raise FullSourceConsumerError(
            f"matched-500 frozen input verification failed: {exc}"
        ) from exc
    if pipeline_hash != gate.pipeline_config_sha256:
        raise FullSourceConsumerError("matched-500 pipeline config binding drifted")
    contract = _verify_pilot_request_contract(
        repo_root,
        spec=spec,
        pipeline=pipeline,
        pipeline_hash=pipeline_hash,
        verified_input=verified_input,
    )
    artifact_hashes = _verify_pilot_artifact_hashes(
        gate,
        artifact_root,
        expected_files=expected_files,
    )
    manifest = _json_object(artifact_root / "generation_manifest.json")
    expected_manifest_fields = {
        "candidate_ids",
        "counts",
        "forbidden_stages_executed",
        "generation",
        "git_commit",
        "gpu_inventory",
        "input",
        "model",
        "pipeline_config_path",
        "pipeline_config_sha256",
        "repr",
        "request_keys",
        "routing",
        "run_id",
        "runtime_versions",
        "schema_version",
        "server_observation",
        "source_ids",
        "tokens",
    }
    if set(manifest) != expected_manifest_fields:
        raise FullSourceConsumerError("matched-500 output manifest field set drifted")
    run_id = manifest.get("run_id")
    source_ids = tuple(row.source_id for row in verified_input.rows)
    manifest_input = _manifest_mapping(manifest.get("input"), label="input")
    manifest_model = _manifest_mapping(manifest.get("model"), label="model")
    if (
        run_id != contract.run_id
        or manifest.get("schema_version") != "sft2b_reform_32b_matched_500_generation_manifest_v1"
        or manifest.get("pipeline_config_path") != gate.pipeline_config_path
        or manifest.get("pipeline_config_sha256") != pipeline_hash
        or manifest.get("source_ids") != list(source_ids)
        or manifest.get("forbidden_stages_executed") != []
        or manifest.get("repr") != pipeline.repr.model_dump(mode="json")
        or manifest.get("routing")
        != {
            "candidate": "candidates.jsonl; validity and semantics not yet established",
            "core": "absent until Lean validity and three blinded votes",
            "formalizer_invalid": ("formalizer_invalid_attempts.jsonl; never semantic false"),
        }
    ):
        raise FullSourceConsumerError("matched-500 output manifest identity drifted")
    git_commit = manifest.get("git_commit")
    if not isinstance(git_commit, str):
        raise FullSourceConsumerError("matched-500 manifest lacks a git commit")
    _verify_manifest_git_snapshot(
        repo_root,
        git_commit=git_commit,
        pipeline=pipeline,
        pipeline_config_path=gate.pipeline_config_path,
        pipeline_config_sha256=pipeline_hash,
        runtime_code_pins=gate.observed_runtime_code_pins,
    )
    if (
        set(manifest_input) != {"repo_id", "revision", "path", "files", "source_manifest_sha256"}
        or manifest_input.get("repo_id") != pipeline.input.repo_id
        or manifest_input.get("revision") != pipeline.input.revision
        or manifest_input.get("path") != pipeline.input.path
        or manifest_input.get("files") != pipeline.input.files
        or manifest_input.get("source_manifest_sha256")
        != pipeline.input.files["source_manifest.json"]
        or set(manifest_model)
        != {
            "model_id",
            "revision",
            "snapshot_binding_sha256",
            "checkpoint_dtype",
            "quantization",
        }
        or manifest_model.get("model_id") != pipeline.model.model_id
        or manifest_model.get("revision") != pipeline.model.revision
        or manifest_model.get("snapshot_binding_sha256") != pipeline.model.snapshot_binding_sha256
        or manifest_model.get("checkpoint_dtype") != "bfloat16"
        or manifest_model.get("quantization") is not None
    ):
        raise FullSourceConsumerError("matched-500 input/model binding drifted")
    source_mix_manifest = _manifest_mapping(
        verified_input.manifest.get("source_mix"), label="input source mix"
    )
    selected_mix_raw = _manifest_mapping(
        source_mix_manifest.get("selected"), label="input selected source mix"
    )
    if (
        any(
            not isinstance(key, str) or not isinstance(value, int)
            for key, value in selected_mix_raw.items()
        )
        or selected_mix_raw != pipeline.input.source_mix
        or sum(cast(int, value) for value in selected_mix_raw.values()) != 500
    ):
        raise FullSourceConsumerError("matched-500 selected source mix drifted")
    selection_mix = {key: cast(int, value) for key, value in selected_mix_raw.items()}
    rows = _verify_pilot_rows(
        artifact_root,
        pipeline=pipeline,
        sources=verified_input.rows,
        selection_mix=selection_mix,
        manifest=manifest,
        contract=contract,
    )
    runtime = _verify_pilot_runtime_artifacts(
        artifact_root=artifact_root,
        pipeline=pipeline,
        manifest=manifest,
    )
    prompt_tokens = sum(item.prompt_tokens for item in rows.metrics)
    completion_tokens = sum(item.completion_tokens for item in rows.metrics)
    token_summary = manifest.get("tokens")
    if token_summary != {
        "prompt": prompt_tokens,
        "completion": completion_tokens,
        "maximum_prompt": pipeline.input.maximum_prompt_tokens,
        "max_model_len": pipeline.generation.max_model_len,
    }:
        raise FullSourceConsumerError("matched-500 manifest token summary drifted")
    missing: tuple[MissingPilotFact, ...] = (
        "clean_shutdown",
        "process_absent",
        "resource_claim",
        "resource_release",
        "zero_call_cache_replay",
        "explicit_quality_acceptance",
        "fresh_download_publication_receipt",
    )
    output_hashes = {name: artifact_hashes[name] for name in PILOT_OUTPUT_FILES}
    binding_payload = {
        "schema_version": "sft2b_matched_500_observed_partial_binding_v1",
        "publication": {
            "repo_id": gate.published_repo_id,
            "revision": gate.published_revision,
            "path_prefix": gate.published_path_prefix,
        },
        "consumer_spec_sha256": consumer_spec_sha256,
        "verifier_sha256": verifier_sha256,
        "pipeline_config_sha256": pipeline_hash,
        "pilot_input": {
            "revision": pipeline.input.revision,
            "path": pipeline.input.path,
            "files": pipeline.input.files,
        },
        "artifacts": output_hashes,
        "run_id": run_id,
        "source_ids_sha256": hash_canonical(source_ids),
        "request_keys_sha256": hash_canonical(rows.request_keys),
        "quality_metrics": rows.quality_metrics.model_dump(mode="json"),
        "telemetry_summary": runtime.telemetry_summary,
        "server_observation": runtime.server_observation,
        "runtime_versions": runtime.runtime_versions,
        "gpu_inventory": runtime.gpu_inventory,
        "missing_or_unverifiable": missing,
    }
    receipt = ObservedPilotReceipt(
        schema_version="sft2b_matched_500_observed_partial_receipt_v1",
        evidence_state="mechanically_verified_partial",
        published_repo_id=gate.published_repo_id,
        published_revision=gate.published_revision,
        published_path_prefix=gate.published_path_prefix,
        consumer_spec_sha256=consumer_spec_sha256,
        verifier_sha256=verifier_sha256,
        run_id=cast(str, run_id),
        source_count=500,
        request_count=2000,
        source_ids_sha256=hash_canonical(source_ids),
        request_keys_sha256=hash_canonical(rows.request_keys),
        artifact_sha256=output_hashes,
        quality_metrics=rows.quality_metrics,
        runtime_observed=True,
        quality_decision="not_authorized",
        gate_passed=False,
        missing_or_unverifiable=missing,
        evidence_binding_sha256=hash_canonical(binding_payload),
    )
    return _ObservedPilotVerification(
        receipt=receipt,
        artifact_root=artifact_root,
        pipeline=pipeline,
        manifest=manifest,
        contract=contract,
        rows=rows,
        runtime=runtime,
    )


def verify_observed_pilot(
    repo_root: Path,
    spec: FullSourceConsumerSpec,
    *,
    artifact_root: Path,
    pilot_input_root: Path,
) -> ObservedPilotReceipt:
    """Verify the immutable eleven-file publication as incomplete pilot evidence."""

    if spec.schema_version != CONFIG_SCHEMA_V2:
        raise FullSourceConsumerError("legacy consumer cannot verify observed pilot output")
    gate = spec.matched_500_gate
    if gate.evidence_state != "outputs_frozen_incomplete_receipts":
        raise FullSourceConsumerError("observed pilot publication is not hash-pinned as partial")
    return _verify_observed_pilot(
        repo_root,
        spec,
        artifact_root=artifact_root.resolve(),
        pilot_input_root=pilot_input_root.resolve(),
        expected_files=PILOT_OUTPUT_FILES,
    ).receipt


def verify_matched_500_gate(repo_root: Path, spec: FullSourceConsumerSpec) -> VerifiedPilotEvidence:
    """Verify actual matched-pilot artifacts; never trust a pass-shaped receipt."""

    if spec.schema_version != CONFIG_SCHEMA_V2:
        raise FullSourceConsumerError("legacy self-attested matched receipt is superseded")
    gate = spec.matched_500_gate
    if (
        gate.evidence_state != "artifacts_frozen"
        or gate.artifact_root is None
        or gate.pilot_input_root is None
        or gate.pipeline_config_sha256 is None
    ):
        raise FullSourceConsumerError("matched-500 artifact evidence is still pending")
    artifact_root = _resolve_from_repo(repo_root, gate.artifact_root).resolve()
    pilot_input_root = _resolve_from_repo(repo_root, gate.pilot_input_root).resolve()
    observed = _verify_observed_pilot(
        repo_root,
        spec,
        artifact_root=artifact_root,
        pilot_input_root=pilot_input_root,
        expected_files=PILOT_EVIDENCE_FILES,
    )
    pipeline = observed.pipeline
    pipeline_hash = observed.contract.pipeline_config_sha256
    manifest = observed.manifest
    run_id = observed.receipt.run_id
    source_ids = tuple(cast(list[str], manifest["source_ids"]))
    request_keys = observed.rows.request_keys
    failure_taxonomy = dict(observed.rows.failure_taxonomy)
    metrics = observed.rows.metrics
    runtime_artifacts = observed.runtime
    artifact_hashes = {item.path: item.sha256 for item in gate.artifact_files}
    request_keys_sha = hash_canonical(request_keys)
    manifest_sha = artifact_hashes["generation_manifest.json"]
    try:
        runtime = PilotRuntimeReport.model_validate(
            _json_object(artifact_root / "runtime_report.json")
        )
        quality = PilotQualityAcceptanceDecision.model_validate(
            _json_object(artifact_root / "quality_report.json")
        )
        replay = PilotReplayReport.model_validate(
            _json_object(artifact_root / "replay_report.json")
        )
        shutdown = PilotShutdownReceipt.model_validate(
            _json_object(artifact_root / "server_shutdown.json")
        )
        release = PilotResourceReleaseReceipt.model_validate(
            _json_object(artifact_root / "resource_release.json")
        )
        claim = PilotResourceClaim.model_validate(
            _json_object(artifact_root / "resource_claim.json")
        )
    except Exception as exc:
        raise FullSourceConsumerError(f"matched-500 evidence schema failed: {exc}") from exc
    prompt_tokens = sum(item.prompt_tokens for item in metrics)
    completion_tokens = sum(item.completion_tokens for item in metrics)
    expected_rps = 2_000_000.0 / runtime.wall_time_ms
    expected_tps = completion_tokens * 1000.0 / runtime.wall_time_ms
    telemetry_span_ms = (
        runtime_artifacts.last_unix_ns - runtime_artifacts.first_unix_ns
    ) / 1_000_000.0
    claim_sha = artifact_hashes["resource_claim.json"]
    common_bad = (
        runtime.run_id != run_id
        or quality.run_id != run_id
        or replay.run_id != run_id
        or shutdown.run_id != run_id
        or release.run_id != run_id
        or claim.run_id != run_id
        or release.reservation_root != str(pipeline.runtime.reservation_root)
        or claim.reservation_root != str(pipeline.runtime.reservation_root)
        or release.claim_sha256 != claim_sha
        or release.supervisor_pid != claim.pid
        or shutdown.server_pid not in runtime_artifacts.server_log_pids
        or runtime.server_pid != shutdown.server_pid
        or runtime.telemetry_sha256 != artifact_hashes["telemetry.jsonl"]
        or runtime.server_log_sha256 != artifact_hashes["vllm_server.log"]
        or shutdown.telemetry_sha256 != artifact_hashes["telemetry.jsonl"]
        or shutdown.server_log_sha256 != artifact_hashes["vllm_server.log"]
        or runtime.telemetry_samples != runtime_artifacts.telemetry_summary["samples"]
        or runtime.telemetry_first_unix_ns != runtime_artifacts.first_unix_ns
        or runtime.telemetry_last_unix_ns != runtime_artifacts.last_unix_ns
        or runtime.telemetry_summary_sha256 != hash_canonical(runtime_artifacts.telemetry_summary)
        or runtime.server_observation_sha256 != hash_canonical(runtime_artifacts.server_observation)
        or shutdown.server_observation_sha256
        != hash_canonical(runtime_artifacts.server_observation)
        or runtime.runtime_versions_sha256 != hash_canonical(runtime_artifacts.runtime_versions)
        or runtime.gpu_inventory_sha256 != hash_canonical(runtime_artifacts.gpu_inventory)
        or telemetry_span_ms <= 0
        or telemetry_span_ms > runtime.wall_time_ms + max(30_000, runtime.wall_time_ms * 0.2)
        or shutdown.return_code not in {0, -signal.SIGTERM}
        or runtime.request_keys_sha256 != request_keys_sha
        or replay.request_keys_sha256 != request_keys_sha
        or runtime.output_manifest_sha256 != manifest_sha
        or runtime.request_metrics_sha256 != artifact_hashes["request_metrics.jsonl"]
        or runtime.requests_journal_sha256 != artifact_hashes["requests_journal.jsonl"]
        or runtime.prompt_tokens != prompt_tokens
        or runtime.completion_tokens != completion_tokens
        or runtime.failure_taxonomy != failure_taxonomy
        or quality.observed_partial_evidence_binding_sha256
        != observed.receipt.evidence_binding_sha256
        or quality.quality_metrics_sha256
        != hash_canonical(observed.receipt.quality_metrics.model_dump(mode="json"))
        or abs(runtime.requests_per_second - expected_rps) > max(1e-9, expected_rps * 1e-6)
        or abs(runtime.output_tokens_per_second - expected_tps) > max(1e-9, expected_tps * 1e-6)
    )
    if common_bad:
        raise FullSourceConsumerError(
            "matched-500 runtime metrics or explicit quality acceptance do not replay"
        )
    output_hashes = {name: artifact_hashes[name] for name in PILOT_OUTPUT_FILES}
    if replay.deterministic_output_sha256 != output_hashes:
        raise FullSourceConsumerError("matched-500 complete-cache replay hashes drifted")
    binding = hash_canonical(
        {
            "schema_version": "sft2b_matched_500_verified_evidence_binding_v1",
            "pipeline_config_sha256": pipeline_hash,
            "pilot_input": {
                "revision": pipeline.input.revision,
                "path": pipeline.input.path,
                "files": pipeline.input.files,
            },
            "artifacts": artifact_hashes,
            "run_id": run_id,
            "source_ids_sha256": hash_canonical(source_ids),
            "request_keys_sha256": request_keys_sha,
            "failure_taxonomy": failure_taxonomy,
            "observed_partial_evidence": observed.receipt.model_dump(mode="json"),
            "quality_acceptance": quality.model_dump(mode="json"),
            "shutdown": shutdown.model_dump(mode="json"),
            "resource_release": release.model_dump(mode="json"),
            "resource_claim_sha256": claim_sha,
            "telemetry_summary": runtime_artifacts.telemetry_summary,
            "server_observation": runtime_artifacts.server_observation,
            "runtime_versions": runtime_artifacts.runtime_versions,
            "gpu_inventory": runtime_artifacts.gpu_inventory,
        }
    )
    return VerifiedPilotEvidence(
        run_id=run_id,
        source_ids=source_ids,
        request_keys=request_keys,
        evidence_binding_sha256=binding,
        failure_taxonomy=failure_taxonomy,
    )


def _read_prompt_tokens(
    spec: FullSourceConsumerSpec,
    *,
    bundle_root: Path,
    source_ids: tuple[str, ...],
) -> dict[str, int]:
    pin = _file_pin(spec, "prompt_token_counts.json")
    path = bundle_root / pin.path
    if pin.sha256 is None or not path.is_file() or hash_file(path) != pin.sha256:
        raise FullSourceConsumerError("full-source prompt-token artifact is not pinned")
    payload = _json_object(path)
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("source_count") != len(source_ids):
        raise FullSourceConsumerError("full-source prompt-token row count drifted")
    ordered_ids: list[str] = []
    token_counts: dict[str, int] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise FullSourceConsumerError("full-source prompt-token row is not an object")
        source_id = raw.get("source_id")
        count = raw.get("prompt_tokens")
        if not isinstance(source_id, str) or not isinstance(count, int) or count < 1:
            raise FullSourceConsumerError("full-source prompt-token row is invalid")
        ordered_ids.append(source_id)
        token_counts[source_id] = count
    if tuple(ordered_ids) != source_ids or len(token_counts) != len(source_ids):
        raise FullSourceConsumerError("prompt-token rows differ from full-source ordering")
    maximum = max(token_counts.values())
    if (
        payload.get("maximum_prompt_tokens") != maximum
        or payload.get("required_max_model_len") != maximum + 4096
        or payload.get("required_max_model_len") != spec.executor.max_model_len
        or payload.get("model_id") != spec.model.model_id
        or payload.get("model_revision") != spec.model.revision
        or payload.get("prompt_sha256") != spec.model.prompt_sha256
        or payload.get("tokenizer_sha256") != spec.model.tokenizer_sha256
    ):
        raise FullSourceConsumerError("full-source prompt/model/tokenizer binding drifted")
    return token_counts


def build_integrated_vllm_backend(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    config_sha256: str,
    bundle_root: Path,
    verified: VerifiedSourceViews,
    plan: FullSourceRunPlan,
) -> tuple[LoadedVllmBackend, tuple[SourceRecord, ...]]:
    """Construct the real full-source backend from the matched-run machinery."""

    if (
        spec.schema_version != CONFIG_SCHEMA_V2
        or spec.executor.kind != "integrated_vllm"
        or spec.runtime.model_snapshot_path is None
        or spec.model.snapshot_binding_sha256 is None
        or spec.input.revision is None
        or spec.executor.max_model_len is None
    ):
        raise FullSourceConsumerError("integrated vLLM executor is not fully pinned")
    placement_path = repo_root / spec.model.placement_config_path
    if hash_file(placement_path) != spec.model.placement_config_sha256:
        raise FullSourceConsumerError("full-source placement config hash drifted")
    try:
        placement, _ = load_reform_32b_config(
            repo_root,
            placement_path=placement_path,
            snapshot_path=spec.runtime.model_snapshot_path,
        )
    except Exception as exc:
        raise FullSourceConsumerError(
            f"full-source model snapshot verification failed: {exc}"
        ) from exc
    if (
        placement.model_id != spec.model.model_id
        or placement.model_revision != spec.model.revision
        or placement.snapshot_binding_sha256 != spec.model.snapshot_binding_sha256
        or placement.prompt_sha256 != spec.model.prompt_sha256
        or placement.dtype != "bfloat16"
        or placement.trust_remote_code
    ):
        raise FullSourceConsumerError("full-source placement/model identity drifted")
    all_tokens = _read_prompt_tokens(spec, bundle_root=bundle_root, source_ids=verified.source_ids)
    by_id = {row.source_id: row for row in verified.rows}
    sources = tuple(by_id[source_id] for source_id in plan.source_ids)
    selected_tokens = {source_id: all_tokens[source_id] for source_id in plan.source_ids}
    selected_max_model_len = max(selected_tokens.values()) + 4096
    if selected_max_model_len > spec.executor.max_model_len:
        raise FullSourceConsumerError("shard prompt length exceeds the frozen full-release bound")
    full_profile = VllmProfile(
        profile_id=(
            f"sft2b_reform_32b_{plan.shard_id}_dp4_tp2_{plan.run_id.split(':', 1)[1][:16]}"
        ),
        visible_devices=spec.executor.visible_devices,
        data_parallel_size=spec.executor.data_parallel_size,
        tensor_parallel_size=spec.executor.tensor_parallel_size,
        port=spec.executor.port,
        max_model_len=selected_max_model_len,
        max_num_seqs=spec.executor.max_num_seqs,
        gpu_memory_utilization=spec.executor.gpu_memory_utilization,
        prefix_caching=spec.executor.prefix_caching,
        concurrency=min(spec.executor.concurrency, len(sources) * len(CandidateSlot)),
        source_ids=plan.source_ids,
        slots=tuple(CandidateSlot),
    )
    first_id = plan.source_ids[0]
    smoke_profile = VllmProfile(
        profile_id=f"{full_profile.profile_id}_structural_smoke",
        visible_devices=(0, 1),
        data_parallel_size=1,
        tensor_parallel_size=2,
        port=spec.executor.port + 1,
        max_model_len=selected_tokens[first_id] + 4096,
        max_num_seqs=1,
        gpu_memory_utilization=spec.executor.gpu_memory_utilization,
        prefix_caching=False,
        concurrency=1,
        source_ids=(first_id,),
        slots=(CandidateSlot.SLOT_0,),
    )
    source_manifest_pin = _file_pin(spec, "source_manifest.json")
    sources_pin = _file_pin(spec, "sources.jsonl")
    if source_manifest_pin.sha256 is None or sources_pin.sha256 is None:
        raise FullSourceConsumerError("full-source portable release pins are incomplete")
    portable = PortableReleaseConfig(
        repo_id=spec.input.repo_id,
        revision=spec.input.revision,
        release_id=f"sft2b_full_source:{spec.input.revision}",
        release_manifest_path="source_manifest.json",
        release_manifest_sha256=source_manifest_pin.sha256,
        smoke_sources_path="sources.jsonl",
        smoke_sources_sha256=sources_pin.sha256,
        probe_sources_path="sources.jsonl",
        probe_sources_sha256=sources_pin.sha256,
    )
    backend_spec = VllmBackendSpec(
        schema_version="sft2b_reform_32b_vllm_v1",
        status="bounded_probe_authorized",
        placement_config_path=spec.model.placement_config_path,
        placement_config_sha256=spec.model.placement_config_sha256,
        model_id=spec.model.model_id,
        model_revision=spec.model.revision,
        snapshot_binding_sha256=spec.model.snapshot_binding_sha256,
        checkpoint_dtype="bfloat16",
        quantization=None,
        trust_remote_code=False,
        served_model_name=spec.model.served_model_name,
        provider="local_vllm_openai",
        api_route="/v1/completions",
        request_timeout_seconds=spec.executor.request_timeout_seconds,
        telemetry_interval_seconds=spec.executor.telemetry_interval_seconds,
        portable_release=portable,
        source_prompt_tokens=selected_tokens,
        profiles={"smoke_dp1_tp2": smoke_profile, FULL_PROFILE_NAME: full_profile},
        launch=VllmLaunchConfig(
            host="127.0.0.1",
            load_format="safetensors",
            safetensors_load_strategy="eager",
            distributed_executor_backend="mp",
            data_parallel_backend="mp",
            enable_request_id_headers=True,
            disable_uvicorn_access_log=True,
        ),
        staging_root=spec.runtime.cache_root / "vllm",
        owner_session=spec.runtime.owner_session,
    )
    return (
        LoadedVllmBackend(
            spec=backend_spec,
            config_path=config_path,
            config_sha256=config_sha256,
            placement=placement,
            release_root=bundle_root,
        ),
        sources,
    )


def _server_log_tail(path: Path, limit: int = 12_000) -> str:
    if not path.is_file():
        return ""
    return path.read_bytes()[-limit:].decode("utf-8", errors="replace")


def _wait_for_vllm(
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
        return_code = process.poll()
        if return_code is not None:
            raise FullSourceConsumerError(
                f"vLLM exited during startup with code {return_code}: {_server_log_tail(log_path)}"
            )
        try:
            return verify_openai_server(endpoint_url, served_model_name=served_model_name)
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2.0)
    raise FullSourceConsumerError(
        f"vLLM startup timed out ({last_error}): {_server_log_tail(log_path)}"
    )


def _stop_vllm(process: subprocess.Popen[bytes], *, timeout_seconds: int) -> tuple[bool, int]:
    if process.poll() is not None:
        return False, cast(int, process.returncode)
    os.killpg(process.pid, signal.SIGTERM)
    escalated = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        escalated = True
        os.killpg(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=60)
    return escalated, return_code


def _runtime_session_journal(run_root: Path, plan: FullSourceRunPlan) -> Path:
    return run_root / plan.shard_id / plan.run_id / "runtime_sessions.jsonl"


def _runtime_session_start_journal(run_root: Path, plan: FullSourceRunPlan) -> Path:
    return run_root / plan.shard_id / plan.run_id / "runtime_session_starts.jsonl"


def _runtime_session_reconciliation_journal(run_root: Path, plan: FullSourceRunPlan) -> Path:
    return run_root / plan.shard_id / plan.run_id / "runtime_session_reconciliations.jsonl"


def _verify_runtime_sessions(
    run_root: Path,
    plan: FullSourceRunPlan,
    *,
    require_nonempty: bool,
    allow_open_session_id: str | None = None,
) -> tuple[FullSourceRuntimeSessionReceipt, ...]:
    journal_path = _runtime_session_journal(run_root, plan)
    starts_path = _runtime_session_start_journal(run_root, plan)
    reconciliations_path = _runtime_session_reconciliation_journal(run_root, plan)
    sessions_root = journal_path.parent / "runtime_sessions"
    starts = (
        _read_models(starts_path, FullSourceRuntimeSessionStart) if starts_path.is_file() else ()
    )
    receipts = (
        _read_models(journal_path, FullSourceRuntimeSessionReceipt)
        if journal_path.is_file()
        else ()
    )
    reconciliations = (
        _read_models(reconciliations_path, FullSourceRuntimeSessionReconciliation)
        if reconciliations_path.is_file()
        else ()
    )
    if require_nonempty and not receipts:
        raise FullSourceConsumerError("complete provider cache lacks closed runtime evidence")
    if tuple(item.sequence for item in starts) != tuple(range(len(starts))):
        raise FullSourceConsumerError("runtime-session start journal sequence drifted")
    if tuple(item.sequence for item in receipts) != tuple(range(len(receipts))):
        raise FullSourceConsumerError("runtime-session journal sequence drifted")
    if tuple(item.sequence for item in reconciliations) != tuple(range(len(reconciliations))):
        raise FullSourceConsumerError("runtime-session reconciliation sequence drifted")
    start_by_id = {item.session_id: item for item in starts}
    receipt_by_id = {item.session_id: item for item in receipts}
    reconciliation_by_id = {item.session_id: item for item in reconciliations}
    if (
        len(start_by_id) != len(starts)
        or len(receipt_by_id) != len(receipts)
        or len(reconciliation_by_id) != len(reconciliations)
    ):
        raise FullSourceConsumerError("runtime-session identity repeats")
    observed_directories = (
        {item.name for item in sessions_root.iterdir() if item.is_dir()}
        if sessions_root.is_dir()
        else set()
    )
    if observed_directories != set(start_by_id):
        raise FullSourceConsumerError("runtime-session directory/start-event coverage drifted")
    for start in starts:
        expected_start_path = sessions_root / start.session_id / "session_start.json"
        claim_path = Path(start.claim_artifact_path)
        if (
            start.run_id != plan.run_id
            or Path(start.session_start_path) != expected_start_path
            or not expected_start_path.is_file()
            or FullSourceRuntimeSessionStart.model_validate(_json_object(expected_start_path))
            != start
            or not claim_path.is_file()
            or hash_file(claim_path) != start.claim_sha256
        ):
            raise FullSourceConsumerError("runtime-session start/claim evidence drifted")
    if set(receipt_by_id).intersection(reconciliation_by_id):
        raise FullSourceConsumerError("runtime session is both closed and reconciled")
    for reconciliation in reconciliations:
        reconciled_start = start_by_id.get(reconciliation.session_id)
        expected_path = (
            sessions_root / reconciliation.session_id / "dead_runtime_reconciliation.json"
        )
        if (
            reconciled_start is None
            or reconciliation.run_id != plan.run_id
            or reconciliation.server_pid != reconciled_start.server_pid
            or reconciliation.session_start_sha256
            != hash_file(Path(reconciled_start.session_start_path))
            or Path(reconciliation.reconciliation_artifact_path) != expected_path
            or not expected_path.is_file()
            or expected_path.is_symlink()
            or FullSourceRuntimeSessionReconciliation.model_validate(_json_object(expected_path))
            != reconciliation
        ):
            raise FullSourceConsumerError("runtime-session reconciliation evidence drifted")
    open_sessions = set(start_by_id).difference(receipt_by_id, reconciliation_by_id)
    allowed_open = {allow_open_session_id} if allow_open_session_id is not None else set()
    if (
        open_sessions != allowed_open
        or not set(receipt_by_id).issubset(start_by_id)
        or not set(reconciliation_by_id).issubset(start_by_id)
    ):
        raise FullSourceConsumerError(
            "runtime session is unclosed; manual reconciliation is required"
        )
    seen: set[str] = set()
    for receipt in receipts:
        session_root = sessions_root / receipt.session_id
        start = start_by_id[receipt.session_id]
        expected_paths = {
            "session_start": session_root / "session_start.json",
            "server_log": session_root / "vllm_server.log",
            "telemetry": session_root / "telemetry.jsonl",
            "shutdown": session_root / "server_shutdown.json",
        }
        if receipt.run_id != plan.run_id or receipt.session_id in seen:
            raise FullSourceConsumerError("runtime-session identity drifted or repeated")
        seen.add(receipt.session_id)
        if (
            Path(start.session_start_path) != expected_paths["session_start"]
            or not expected_paths["session_start"].is_file()
            or FullSourceRuntimeSessionStart.model_validate(
                _json_object(expected_paths["session_start"])
            )
            != start
            or receipt.session_start_sha256 != hash_file(expected_paths["session_start"])
            or start.run_id != plan.run_id
            or start.server_pid != receipt.server_pid
            or start.served_model_name != receipt.served_model_name
            or start.started_unix_ns != receipt.started_unix_ns
            or Path(receipt.server_log_path) != expected_paths["server_log"]
            or Path(receipt.telemetry_path) != expected_paths["telemetry"]
            or Path(receipt.shutdown_path) != expected_paths["shutdown"]
            or receipt.ended_unix_ns < receipt.started_unix_ns
        ):
            raise FullSourceConsumerError("runtime-session paths/times drifted")
        for name, expected_hash in (
            ("server_log", receipt.server_log_sha256),
            ("telemetry", receipt.telemetry_sha256),
            ("shutdown", receipt.shutdown_sha256),
        ):
            path = expected_paths[name]
            if not path.is_file() or path.is_symlink() or hash_file(path) != expected_hash:
                raise FullSourceConsumerError(f"runtime-session {name} artifact drifted")
        samples = load_samples(expected_paths["telemetry"])
        summary = receipt.telemetry_summary
        first_gpu_identity = (
            tuple((gpu.index, gpu.uuid, gpu.memory_total_mib) for gpu in samples[0].gpus)
            if samples
            else ()
        )
        if (
            len(samples) < 2
            or summary != _telemetry_summary(samples)
            or summary.get("schema_version") != "sft2b_vllm_telemetry_summary_v1"
            or summary.get("samples") != len(samples)
            or summary.get("errors") != []
            or set(cast(dict[str, Any], summary.get("peak_by_gpu", {})))
            != {str(index) for index in range(8)}
            or any(
                tuple(gpu.index for gpu in sample.gpus) != _EXPECTED_GPU_INDICES
                for sample in samples
            )
            or any(
                tuple((gpu.index, gpu.uuid, gpu.memory_total_mib) for gpu in sample.gpus)
                != first_gpu_identity
                for sample in samples
            )
            or any(
                right.monotonic_ns <= left.monotonic_ns or right.unix_time_ns <= left.unix_time_ns
                for left, right in pairwise(samples)
            )
            or samples[0].unix_time_ns < receipt.started_unix_ns
            or samples[-1].unix_time_ns > receipt.ended_unix_ns
            or not any(
                (sample.requests_running or 0.0) > 0
                or any(gpu.utilization_gpu_percent > 0 for gpu in sample.gpus)
                for sample in samples
            )
            or not any((sample.server_process_tree_rss_bytes or 0) > 0 for sample in samples)
        ):
            raise FullSourceConsumerError("runtime-session telemetry coverage drifted")
        shutdown = _json_object(expected_paths["shutdown"])
        observation = shutdown.get("server_observation")
        log_text = expected_paths["server_log"].read_text(encoding="utf-8", errors="replace")
        if (
            shutdown.get("schema_version") != "sft2b_full_source_server_shutdown_v1"
            or shutdown.get("run_id") != plan.run_id
            or shutdown.get("session_id") != receipt.session_id
            or shutdown.get("server_pid") != receipt.server_pid
            or shutdown.get("stopped") is not True
            or not isinstance(shutdown.get("return_code"), int)
            or shutdown.get("return_code") not in {0, -signal.SIGTERM}
            or shutdown.get("clean_shutdown") is not True
            or shutdown.get("kill_escalated") is not False
            or shutdown.get("process_absent_after_shutdown") is not True
            or not isinstance(observation, dict)
            or observation.get("health_status") != 200
            or observation.get("models_status") != 200
            or observation.get("model_ids") != [receipt.served_model_name]
            or len([line for line in log_text.splitlines() if line.strip()]) < 2
            or _SERVER_READY_RE.search(log_text) is None
            or receipt.server_pid
            not in {int(match.group("pid")) for match in _SERVER_PID_RE.finditer(log_text)}
        ):
            raise FullSourceConsumerError("runtime-session shutdown evidence drifted")
    return receipts


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def _append_dead_runtime_reconciliation(
    *,
    run_root: Path,
    plan: FullSourceRunPlan,
    session_id: str,
    host: str,
    port: int,
) -> FullSourceRuntimeSessionReconciliation:
    starts = _read_models(
        _runtime_session_start_journal(run_root, plan), FullSourceRuntimeSessionStart
    )
    start_by_id = {item.session_id: item for item in starts}
    start = start_by_id.get(session_id)
    if start is None or start.run_id != plan.run_id:
        raise FullSourceConsumerError("dead runtime reconciliation is foreign to this run")
    if Path(f"/proc/{start.server_pid}").exists():
        raise FullSourceConsumerError("runtime server PID is still live; refusing reconciliation")
    if _port_is_open(host, port):
        raise FullSourceConsumerError(
            "runtime provider port is still open; refusing reconciliation"
        )
    journal_path = _runtime_session_reconciliation_journal(run_root, plan)
    artifact_path = (
        journal_path.parent / "runtime_sessions" / session_id / "dead_runtime_reconciliation.json"
    )
    lock_path = journal_path.with_suffix(".jsonl.lock")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            prior = (
                _read_models(journal_path, FullSourceRuntimeSessionReconciliation)
                if journal_path.is_file()
                else ()
            )
            by_id = {item.session_id: item for item in prior}
            if session_id in by_id:
                return by_id[session_id]
            if len(by_id) != len(prior):
                raise FullSourceConsumerError("runtime reconciliation identity repeats")
            start_path = Path(start.session_start_path)
            reconciliation = FullSourceRuntimeSessionReconciliation(
                sequence=len(prior),
                run_id=plan.run_id,
                session_id=session_id,
                server_pid=start.server_pid,
                reconciled_unix_ns=time.time_ns(),
                session_start_sha256=hash_file(start_path),
                process_absent=True,
                port_closed=True,
                reason="dead_same_run_runtime",
                reconciliation_artifact_path=str(artifact_path),
            )
            payload = canonical_json_bytes(reconciliation.model_dump(mode="json")) + b"\n"
            immutable_write(artifact_path, payload)
            with journal_path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    _verify_runtime_sessions(run_root, plan, require_nonempty=False)
    return reconciliation


def _append_runtime_session_start(
    *,
    run_root: Path,
    plan: FullSourceRunPlan,
    session_id: str,
    server_pid: int,
    served_model_name: str,
    started_unix_ns: int,
    backend_config_sha256: str,
    claim_path: Path,
) -> FullSourceRuntimeSessionStart:
    shard_root = run_root / plan.shard_id / plan.run_id
    if not claim_path.is_file():
        raise FullSourceConsumerError("runtime session start lacks an immutable resource claim")
    starts_path = _runtime_session_start_journal(run_root, plan)
    starts_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = starts_path.with_suffix(".jsonl.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            prior = (
                _read_models(starts_path, FullSourceRuntimeSessionStart)
                if starts_path.is_file()
                else ()
            )
            session_start_path = shard_root / "runtime_sessions" / session_id / "session_start.json"
            start = FullSourceRuntimeSessionStart(
                sequence=len(prior),
                run_id=plan.run_id,
                session_id=session_id,
                server_pid=server_pid,
                served_model_name=served_model_name,
                started_unix_ns=started_unix_ns,
                backend_config_sha256=backend_config_sha256,
                claim_artifact_path=str(claim_path),
                claim_sha256=hash_file(claim_path),
                session_start_path=str(session_start_path),
            )
            immutable_write(
                session_start_path,
                canonical_json_bytes(start.model_dump(mode="json")) + b"\n",
            )
            with starts_path.open("ab") as handle:
                handle.write(canonical_json_bytes(start.model_dump(mode="json")) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    _verify_runtime_sessions(
        run_root,
        plan,
        require_nonempty=False,
        allow_open_session_id=session_id,
    )
    return start


def _read_jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                raise FullSourceConsumerError(f"blank append-only event at {path}:{number}")
            try:
                value: object = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FullSourceConsumerError(f"invalid append-only event at {path}") from exc
            if not isinstance(value, dict):
                raise FullSourceConsumerError(f"non-object append-only event at {path}")
            rows.append(cast(dict[str, Any], value))
    return tuple(rows)


def _append_resource_record(
    *,
    shard_root: Path,
    kind: Literal["claim", "release"],
    claim_id: str,
    payload: Mapping[str, object],
) -> Path:
    journal_path = shard_root / f"resource_{kind}s.jsonl"
    artifact_path = shard_root / "resource_sessions" / claim_id / f"{kind}.json"
    lock_path = journal_path.with_suffix(".jsonl.lock")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            prior = _read_jsonl_objects(journal_path)
            record = dict(payload)
            record["sequence"] = len(prior)
            record["claim_id"] = claim_id
            record[f"{kind}_artifact_path"] = str(artifact_path)
            immutable_write(artifact_path, canonical_json_bytes(record) + b"\n")
            with journal_path.open("ab") as handle:
                handle.write(canonical_json_bytes(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return artifact_path


def _verify_full_resource_release(
    shard_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    plan: FullSourceRunPlan,
) -> None:
    claims = _read_jsonl_objects(shard_root / "resource_claims.jsonl")
    releases = _read_jsonl_objects(shard_root / "resource_releases.jsonl")
    if not claims or len(claims) != len(releases):
        raise FullSourceConsumerError("complete cache lacks exact claim/release coverage")
    if [item.get("sequence") for item in claims] != list(range(len(claims))) or [
        item.get("sequence") for item in releases
    ] != list(range(len(releases))):
        raise FullSourceConsumerError("resource claim/release sequence drifted")
    claims_by_id = {item.get("claim_id"): item for item in claims}
    releases_by_id = {item.get("claim_id"): item for item in releases}
    if (
        len(claims_by_id) != len(claims)
        or len(releases_by_id) != len(releases)
        or set(claims_by_id) != set(releases_by_id)
    ):
        raise FullSourceConsumerError("resource claim/release identity coverage drifted")
    for claim_id, claim in claims_by_id.items():
        if not isinstance(claim_id, str) or re.fullmatch(r"[0-9]+-[0-9]+", claim_id) is None:
            raise FullSourceConsumerError("resource claim ID is invalid")
        release = releases_by_id[claim_id]
        raw_claim_path = claim.get("claim_artifact_path")
        raw_release_path = release.get("release_artifact_path")
        if not isinstance(raw_claim_path, str) or not isinstance(raw_release_path, str):
            raise FullSourceConsumerError("resource artifact path is invalid")
        claim_path = Path(raw_claim_path)
        release_path = Path(raw_release_path)
        expected_root = shard_root / "resource_sessions" / claim_id
        reservation = claim.get("reservation")
        if (
            set(claim)
            != {
                "schema_version",
                "sequence",
                "claim_id",
                "claim_artifact_path",
                "run_id",
                "reservation_root",
                "launch_nonce",
                "launched_unix_ns",
                "reservation",
            }
            or set(release)
            != {
                "schema_version",
                "sequence",
                "claim_id",
                "release_artifact_path",
                "run_id",
                "task",
                "launch_nonce",
                "launched_unix_ns",
                "claim_artifact_path",
                "claim_sha256",
                "supervisor_pid",
                "released",
                "active_task_claims_after_release",
            }
            or claim.get("schema_version") != "sft2b_full_source_resource_claim_v2"
            or claim.get("run_id") != plan.run_id
            or claim.get("reservation_root") != str(spec.runtime.reservation_root)
            or not isinstance(claim.get("launch_nonce"), str)
            or re.fullmatch(r"[0-9a-f]{64}", cast(str, claim.get("launch_nonce"))) is None
            or not isinstance(claim.get("launched_unix_ns"), int)
            or isinstance(claim.get("launched_unix_ns"), bool)
            or cast(int, claim.get("launched_unix_ns")) <= 0
            or not isinstance(reservation, dict)
            or set(reservation)
            != {
                "task",
                "lean_workers",
                "lean_rss_gib",
                "gpu",
                "pid",
                "owner_session",
                "hostname",
                "worktree",
                "created_at",
            }
            or reservation.get("task") != spec.runtime.reservation_task
            or reservation.get("lean_workers") != 0
            or reservation.get("lean_rss_gib") != 0.0
            or reservation.get("gpu") is not True
            or reservation.get("pid") != int(claim_id.rsplit("-", 1)[1])
            or f"run_id={plan.run_id}" not in str(reservation.get("owner_session"))
            or not all(
                isinstance(reservation.get(name), str) and reservation.get(name)
                for name in ("hostname", "worktree", "created_at")
            )
            or claim_path != expected_root / "claim.json"
            or not claim_path.is_file()
            or _json_object(claim_path) != claim
            or release.get("schema_version") != "sft2b_full_source_resource_release_v2"
            or release.get("run_id") != plan.run_id
            or release.get("task") != spec.runtime.reservation_task
            or release.get("launch_nonce") != claim.get("launch_nonce")
            or release.get("launched_unix_ns") != claim.get("launched_unix_ns")
            or release.get("claim_artifact_path") != str(claim_path)
            or release.get("claim_sha256") != hash_file(claim_path)
            or release.get("supervisor_pid") != reservation.get("pid")
            or release.get("released") is not True
            or release.get("active_task_claims_after_release") != 0
            or release_path != expected_root / "release.json"
            or not release_path.is_file()
            or _json_object(release_path) != release
        ):
            raise FullSourceConsumerError("full-source resource claim/release evidence drifted")


def _reconcile_stale_same_run_reservation(
    *,
    repo_root: Path,
    spec: FullSourceConsumerSpec,
    plan: FullSourceRunPlan,
    run_root: Path,
    shard_root: Path,
    host: str,
    port: int,
) -> bool:
    active = [
        item
        for item in list_reservations(spec.runtime.reservation_root)
        if item.task == spec.runtime.reservation_task
    ]
    if not active:
        return False
    if len(active) != 1:
        raise FullSourceConsumerError("multiple SFT2B reservations violate atomic ownership")
    reservation = active[0]
    if reservation.hostname != socket.gethostname():
        raise FullSourceConsumerError("foreign-host SFT2B reservation cannot be reconciled")
    if Path(f"/proc/{reservation.pid}").exists():
        raise FullSourceConsumerError("live SFT2B reservation cannot be reconciled")
    if Path(reservation.worktree).resolve() != repo_root.resolve():
        raise FullSourceConsumerError("foreign-worktree SFT2B reservation cannot be reconciled")
    if f"run_id={plan.run_id}" not in reservation.owner_session:
        raise FullSourceConsumerError("foreign-run SFT2B reservation cannot be reconciled")
    if _port_is_open(host, port):
        raise FullSourceConsumerError("provider port remains open; refusing reservation release")
    claims = _read_jsonl_objects(shard_root / "resource_claims.jsonl")
    releases = _read_jsonl_objects(shard_root / "resource_releases.jsonl")
    released_ids = {item.get("claim_id") for item in releases}
    unmatched = [item for item in claims if item.get("claim_id") not in released_ids]
    if len(unmatched) != 1:
        raise FullSourceConsumerError(
            "stale same-run reservation lacks one unmatched durable claim"
        )
    claim = unmatched[0]
    claim_id = claim.get("claim_id")
    claim_path_value = claim.get("claim_artifact_path")
    if not isinstance(claim_id, str) or not isinstance(claim_path_value, str):
        raise FullSourceConsumerError("stale resource claim identity is invalid")
    claim_path = Path(claim_path_value)
    if (
        claim.get("schema_version") != "sft2b_full_source_resource_claim_v2"
        or claim.get("run_id") != plan.run_id
        or claim.get("reservation_root") != str(spec.runtime.reservation_root)
        or claim.get("reservation") != asdict(reservation)
        or claim_path != shard_root / "resource_sessions" / claim_id / "claim.json"
        or not claim_path.is_file()
        or claim_path.is_symlink()
        or _json_object(claim_path) != claim
    ):
        raise FullSourceConsumerError("stale resource claim evidence drifted")
    runtime_reconciliations = (
        _read_models(
            _runtime_session_reconciliation_journal(run_root, plan),
            FullSourceRuntimeSessionReconciliation,
        )
        if _runtime_session_reconciliation_journal(run_root, plan).is_file()
        else ()
    )
    starts = (
        _read_models(
            _runtime_session_start_journal(run_root, plan),
            FullSourceRuntimeSessionStart,
        )
        if _runtime_session_start_journal(run_root, plan).is_file()
        else ()
    )
    start_ids_for_claim = {
        item.session_id for item in starts if Path(item.claim_artifact_path) == claim_path
    }
    reconciled_ids = {item.session_id for item in runtime_reconciliations}
    if start_ids_for_claim and not start_ids_for_claim.issubset(reconciled_ids):
        raise FullSourceConsumerError("stale claim still owns an unreconciled runtime")
    reconciliation_root = shard_root / "resource_sessions" / claim_id
    intent_path = reconciliation_root / "stale_release_intent.json"
    intent = {
        "schema_version": "sft2b_full_source_stale_release_intent_v1",
        "run_id": plan.run_id,
        "claim_id": claim_id,
        "claim_artifact_path": str(claim_path),
        "claim_sha256": hash_file(claim_path),
        "reservation": asdict(reservation),
        "reservation_pid_absent": True,
        "provider_port_closed": True,
        "runtime_reconciliation_sha256": {
            item.session_id: hash_file(Path(item.reconciliation_artifact_path))
            for item in runtime_reconciliations
            if item.session_id in start_ids_for_claim
        },
        "reason": "dead_same_run_supervisor",
    }
    immutable_write(intent_path, canonical_json_bytes(intent) + b"\n")
    released = release_resources(
        root=spec.runtime.reservation_root, task=spec.runtime.reservation_task
    )
    active_after = sum(
        item.task == spec.runtime.reservation_task
        for item in list_reservations(spec.runtime.reservation_root)
    )
    if released != reservation or active_after != 0:
        raise FullSourceConsumerError("stale release differs from its exact reservation")
    completion_path = reconciliation_root / "stale_release_completed.json"
    immutable_write(
        completion_path,
        canonical_json_bytes(
            {
                "schema_version": "sft2b_full_source_stale_release_completed_v1",
                "run_id": plan.run_id,
                "claim_id": claim_id,
                "intent_path": str(intent_path),
                "intent_sha256": hash_file(intent_path),
                "released_reservation": asdict(released),
                "active_task_claims_after_release": active_after,
                "reconciled_unix_ns": time.time_ns(),
            }
        )
        + b"\n",
    )
    _append_resource_record(
        shard_root=shard_root,
        kind="release",
        claim_id=claim_id,
        payload={
            "schema_version": "sft2b_full_source_resource_release_v2",
            "run_id": plan.run_id,
            "task": spec.runtime.reservation_task,
            "launch_nonce": claim["launch_nonce"],
            "launched_unix_ns": claim["launched_unix_ns"],
            "claim_artifact_path": str(claim_path),
            "claim_sha256": hash_file(claim_path),
            "supervisor_pid": reservation.pid,
            "released": True,
            "active_task_claims_after_release": active_after,
        },
    )
    return True


def _append_runtime_session(
    *,
    run_root: Path,
    plan: FullSourceRunPlan,
    session_id: str,
    server_pid: int,
    served_model_name: str,
    started_unix_ns: int,
    server_log_path: Path,
    telemetry_path: Path,
    telemetry_summary: Mapping[str, Any],
    shutdown_path: Path,
) -> FullSourceRuntimeSessionReceipt:
    journal_path = _runtime_session_journal(run_root, plan)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(".jsonl.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            prior = _verify_runtime_sessions(
                run_root,
                plan,
                require_nonempty=False,
                allow_open_session_id=session_id,
            )
            start_path = (
                journal_path.parent / "runtime_sessions" / session_id / "session_start.json"
            )
            if not start_path.is_file():
                raise FullSourceConsumerError("closing runtime session lacks its start artifact")
            receipt = FullSourceRuntimeSessionReceipt(
                sequence=len(prior),
                run_id=plan.run_id,
                session_id=session_id,
                server_pid=server_pid,
                served_model_name=served_model_name,
                started_unix_ns=started_unix_ns,
                ended_unix_ns=time.time_ns(),
                server_log_path=str(server_log_path),
                server_log_sha256=hash_file(server_log_path),
                telemetry_path=str(telemetry_path),
                telemetry_sha256=hash_file(telemetry_path),
                telemetry_summary=dict(telemetry_summary),
                shutdown_path=str(shutdown_path),
                shutdown_sha256=hash_file(shutdown_path),
                session_start_sha256=hash_file(start_path),
            )
            with journal_path.open("ab") as handle:
                handle.write(canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    _verify_runtime_sessions(run_root, plan, require_nonempty=True)
    return receipt


class _ScalableVllmJournal:
    """One-pass verified provider journal with O(1) duplicate suppression."""

    def __init__(self, root: Path, requests: tuple[PreparedRequest, ...] = ()) -> None:
        self.root = root
        self.path = root / "journal/requests.jsonl"
        self.lock_path = self.path.with_suffix(".jsonl.lock")
        self.requests = {item.request_key: item for item in requests}
        if len(self.requests) != len(requests):
            raise FullSourceConsumerError("prepared vLLM requests contain duplicate keys")
        self.rows: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self.file_size = 0
        self.unsynced = 0
        self._load_once()

    def _load_once(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise FullSourceConsumerError(
                        f"blank line in provider journal at {self.path}:{number}"
                    )
                try:
                    value: object = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise FullSourceConsumerError("provider journal contains invalid JSON") from exc
                row = _manifest_mapping(value, label=f"provider journal row {number}")
                key = row.get("request_key")
                if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
                    raise FullSourceConsumerError("provider journal request key is invalid")
                key_value = key
                request = self.requests.get(key_value)
                terminal_path = self.root / "requests" / key_value / "terminal.json"
                if (
                    row.get("schema_version") != "sft2b_vllm_journal_event_v1"
                    or row.get("sequence") != number - 1
                    or key in self.rows
                ):
                    raise FullSourceConsumerError("provider journal identity/order drifted")
                if request is not None and (
                    row.get("attempt_id") != request.attempt_id
                    or row.get("source_id") != request.source.source_id
                    or row.get("slot") != request.slot.slot.value
                ):
                    raise FullSourceConsumerError("provider journal request binding drifted")
                if (
                    row.get("terminal_path") != str(terminal_path)
                    or not terminal_path.is_file()
                    or terminal_path.is_symlink()
                    or row.get("terminal_sha256") != hash_file(terminal_path)
                ):
                    raise FullSourceConsumerError("provider journal terminal binding drifted")
                self.rows[key_value] = row
        self.sequence = len(self.rows)
        self.file_size = self.path.stat().st_size

    def append(self, terminal: VllmRequestTerminal, *, fsync_every: int) -> bool:
        request = self.requests.get(terminal.request_key)
        terminal_path = Path(terminal.metrics.raw_output_path).parent / "terminal.json"
        expected_path = self.root / "requests" / terminal.request_key / "terminal.json"
        if terminal_path != expected_path or (
            request is not None and terminal_path != request.cell / "terminal.json"
        ):
            raise FullSourceConsumerError("provider terminal is foreign to the prepared run")
        terminal_sha = hash_file(terminal_path)
        prior = self.rows.get(terminal.request_key)
        if prior is not None:
            if (
                prior.get("terminal_sha256") != terminal_sha
                or prior.get("attempt_id") != terminal.attempt.attempt_id
                or prior.get("source_id") != terminal.attempt.source_id
                or prior.get("slot") != terminal.attempt.slot.value
            ):
                raise FullSourceConsumerError("provider journal terminal replay changed")
            return False
        row = {
            "schema_version": "sft2b_vllm_journal_event_v1",
            "sequence": self.sequence,
            "request_key": terminal.request_key,
            "attempt_id": terminal.attempt.attempt_id,
            "source_id": terminal.attempt.source_id,
            "slot": terminal.attempt.slot,
            "terminal_path": str(terminal_path),
            "terminal_sha256": terminal_sha,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if _file_size(self.path) != self.file_size:
                    raise FullSourceConsumerError("provider journal changed outside its executor")
                with self.path.open("ab") as handle:
                    handle.write(canonical_json_bytes(row) + b"\n")
                    handle.flush()
                    self.unsynced += 1
                    if self.unsynced >= fsync_every:
                        os.fsync(handle.fileno())
                        self.unsynced = 0
                self.file_size = self.path.stat().st_size
                self.rows[terminal.request_key] = row
                self.sequence += 1
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    def sync(self) -> None:
        if self.unsynced == 0 or not self.path.is_file():
            return
        with self.path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self.unsynced = 0


@dataclass(frozen=True, slots=True)
class _ScalableCacheInspection:
    run_id: str
    root: Path
    request_count: int
    cached_terminals: int
    missing_requests: int
    ambiguous_request_keys: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.missing_requests == 0 and not self.ambiguous_request_keys


@dataclass(frozen=True, slots=True)
class _ScalableExecutionResult:
    run_id: str
    root: Path
    request_count: int
    request_keys_sha256: str
    model_calls: int
    cache_hits: int
    compacted: CompactionResult


@dataclass(frozen=True, slots=True)
class _ProviderExecutionResult:
    terminal: VllmRequestTerminal
    model_calls: int


def _prepared_chunks(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
) -> Iterator[tuple[str, Path, tuple[PreparedRequest, ...]]]:
    """Lazily prepare prompt-bearing requests in bounded source chunks."""

    profile = backend.spec.profiles[profile_name]
    run_id = stable_id(
        "sft2b_vllm_run",
        {
            "schema_version": "sft2b_vllm_semantic_run_v2",
            "provider": backend.spec.provider,
            "model_id": backend.placement.model_id,
            "model_revision": backend.placement.model_revision,
            "snapshot_binding_sha256": backend.placement.snapshot_binding_sha256,
            "source_ids": profile.source_ids,
            "slots": profile.slots,
            "prompt_template_sha256": backend.placement.prompt_sha256,
            "decoding_sha256": backend.placement.decoding_sha256,
        },
    )
    root = backend.spec.staging_root / "generation/vllm" / profile.profile_id / run_id
    for start in range(0, len(sources), SOURCE_CHUNK_SIZE):
        _, _, legacy_requests = frozen_vllm_backend._prepare_requests(
            backend,
            profile_name=profile_name,
            sources=sources[start : start + SOURCE_CHUNK_SIZE],
            endpoint_url=endpoint_url,
        )
        semantic_requests: list[PreparedRequest] = []
        for request in legacy_requests:
            request_key = hash_canonical(
                {
                    "schema_version": "sft2b_vllm_request_key_v2",
                    "provider": backend.spec.provider,
                    "model_id": backend.placement.model_id,
                    "model_revision": backend.placement.model_revision,
                    "snapshot_binding_sha256": backend.placement.snapshot_binding_sha256,
                    "source_id": request.source.source_id,
                    "slot": request.slot.slot,
                    "seed": request.slot.seed,
                    "request_payload_sha256": hash_canonical(request.payload),
                }
            )
            attempt_id = stable_id(
                "sft2b_formalizer_attempt",
                {"request_key": request_key, "provider": backend.spec.provider},
            )
            semantic_requests.append(
                PreparedRequest(
                    source=request.source,
                    slot=request.slot,
                    prompt=request.prompt,
                    profile=request.profile,
                    request_key=request_key,
                    attempt_id=attempt_id,
                    endpoint_url=request.endpoint_url,
                    payload=request.payload,
                    cell=root / "requests" / request_key,
                )
            )
        yield run_id, root, tuple(semantic_requests)


def _provider_attempt_root(request: PreparedRequest, attempt_number: int) -> Path:
    return request.cell / "transport_attempts" / f"{attempt_number:02d}"


def _provider_attempt_rows(request: PreparedRequest) -> tuple[dict[str, Any], ...]:
    attempts_root = request.cell / "transport_attempts"
    if not attempts_root.is_dir():
        return ()
    directories = sorted(item for item in attempts_root.iterdir() if item.is_dir())
    expected_names = [f"{index:02d}" for index in range(1, len(directories) + 1)]
    if [item.name for item in directories] != expected_names:
        raise FullSourceConsumerError("provider transport-attempt sequence drifted")
    rows: list[dict[str, Any]] = []
    for attempt_number, root in enumerate(directories, start=1):
        started_path = root / "started.json"
        if not started_path.is_file() or started_path.is_symlink():
            raise FullSourceConsumerError("provider transport attempt lacks immutable start")
        started = _json_object(started_path)
        expected_attempt_id = stable_id(
            "sft2b_provider_transport_attempt",
            {"request_key": request.request_key, "attempt_number": attempt_number},
        )
        if started != {
            "schema_version": "sft2b_provider_transport_attempt_started_v1",
            "request_key": request.request_key,
            "attempt_id": expected_attempt_id,
            "attempt_number": attempt_number,
        }:
            raise FullSourceConsumerError("provider transport-attempt start drifted")
        outcomes = [
            name
            for name in ("success.json", "failure.json", "abandoned.json")
            if (root / name).is_file()
        ]
        if len(outcomes) > 1:
            raise FullSourceConsumerError("provider transport attempt has multiple outcomes")
        outcome = _json_object(root / outcomes[0]) if outcomes else None
        rows.append(
            {
                "attempt_number": attempt_number,
                "attempt_id": expected_attempt_id,
                "root": root,
                "outcome_name": outcomes[0] if outcomes else None,
                "outcome": outcome,
            }
        )
    return tuple(rows)


def _persisted_provider_completion(row: Mapping[str, Any]) -> Any:
    root = cast(Path, row["root"])
    outcome = cast(dict[str, Any], row["outcome"])
    raw_response_path = root / "raw_response.sse"
    raw_output_path = root / "raw_output.txt"
    if (
        outcome.get("schema_version") != "sft2b_provider_transport_attempt_success_v1"
        or outcome.get("attempt_id") != row["attempt_id"]
        or outcome.get("attempt_number") != row["attempt_number"]
        or not raw_response_path.is_file()
        or raw_response_path.is_symlink()
        or not raw_output_path.is_file()
        or raw_output_path.is_symlink()
        or outcome.get("raw_response_sha256") != hash_file(raw_response_path)
        or outcome.get("raw_output_sha256") != hash_file(raw_output_path)
    ):
        raise FullSourceConsumerError("persisted provider completion drifted")
    return frozen_vllm_backend.StreamCompletion(
        raw_response=raw_response_path.read_bytes(),
        output_text=raw_output_path.read_text(encoding="utf-8"),
        response_id=cast(str, outcome["response_id"]),
        response_request_id=cast(str | None, outcome["response_request_id"]),
        prompt_tokens=cast(int, outcome["prompt_tokens"]),
        completion_tokens=cast(int, outcome["completion_tokens"]),
        finish_reason=cast(str, outcome["finish_reason"]),
        elapsed_ms=cast(int, outcome["elapsed_ms"]),
        time_to_first_token_ms=cast(int, outcome["time_to_first_token_ms"]),
        http_status=cast(int, outcome["http_status"]),
    )


def _retryable_provider_failure(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    detail = str(exc).lower()
    return any(
        marker in detail
        for marker in (
            "endpoint unavailable",
            "connection refused",
            "connection reset",
            "timed out",
            "vllm http 408",
            "vllm http 429",
            "vllm http 500",
            "vllm http 502",
            "vllm http 503",
            "vllm http 504",
        )
    )


def _validated_runtime_reconciliation_reference(
    *, artifact_path: object, artifact_sha256: object, session_id: object
) -> FullSourceRuntimeSessionReconciliation:
    if not isinstance(artifact_path, str) or not isinstance(artifact_sha256, str):
        raise FullSourceConsumerError("provider reconciliation lacks runtime evidence")
    path = Path(artifact_path)
    if not path.is_file() or path.is_symlink() or hash_file(path) != artifact_sha256:
        raise FullSourceConsumerError("provider runtime-reconciliation binding drifted")
    runtime = FullSourceRuntimeSessionReconciliation.model_validate(_json_object(path))
    if runtime.session_id != session_id or Path(runtime.reconciliation_artifact_path) != path:
        raise FullSourceConsumerError("provider runtime-reconciliation identity drifted")
    return runtime


def _request_reconciliation(request: PreparedRequest) -> dict[str, Any] | None:
    path = request.cell / "request_reconciled.json"
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise FullSourceConsumerError("provider request-reconciliation artifact is invalid")
    row = _json_object(path)
    if (
        row.get("schema_version") != "sft2b_provider_request_reconciliation_v1"
        or row.get("request_key") != request.request_key
        or row.get("request_started_sha256") != hash_file(request.cell / "request_started.json")
        or row.get("reason") != "dead_runtime_before_transport_attempt"
        or row.get("provider_call_occurred") is not False
    ):
        raise FullSourceConsumerError("provider request-reconciliation evidence drifted")
    _validated_runtime_reconciliation_reference(
        artifact_path=row.get("runtime_reconciliation_path"),
        artifact_sha256=row.get("runtime_reconciliation_sha256"),
        session_id=row.get("runtime_session_id"),
    )
    return row


def _semantic_request_can_resume(request: PreparedRequest) -> bool:
    rows = _provider_attempt_rows(request)
    if not rows:
        return _request_reconciliation(request) is not None
    last = rows[-1]
    outcome_name = last["outcome_name"]
    outcome = cast(dict[str, Any] | None, last["outcome"])
    if outcome_name == "success.json":
        _persisted_provider_completion(last)
        return True
    if outcome_name == "abandoned.json":
        if (
            outcome is None
            or outcome.get("schema_version") != "sft2b_provider_transport_attempt_abandoned_v1"
            or outcome.get("request_key") != request.request_key
            or outcome.get("attempt_id") != last["attempt_id"]
            or outcome.get("attempt_number") != last["attempt_number"]
            or outcome.get("reason") != "dead_same_run_runtime"
            or outcome.get("provider_call_may_have_occurred") is not True
        ):
            raise FullSourceConsumerError("provider abandoned-attempt evidence drifted")
        _validated_runtime_reconciliation_reference(
            artifact_path=outcome.get("runtime_reconciliation_path"),
            artifact_sha256=outcome.get("runtime_reconciliation_sha256"),
            session_id=outcome.get("runtime_session_id"),
        )
        return len(rows) < MAX_PROVIDER_ATTEMPTS
    if outcome_name != "failure.json" or outcome is None:
        return False
    if (
        outcome.get("schema_version") != "sft2b_provider_transport_attempt_failure_v1"
        or outcome.get("attempt_id") != last["attempt_id"]
        or outcome.get("attempt_number") != last["attempt_number"]
        or not isinstance(outcome.get("retryable"), bool)
    ):
        raise FullSourceConsumerError("provider transport failure evidence drifted")
    return cast(bool, outcome["retryable"]) and len(rows) < MAX_PROVIDER_ATTEMPTS


def _reconcile_dead_same_run_runtime_and_requests(
    *,
    backend: LoadedVllmBackend,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
    run_root: Path,
    plan: FullSourceRunPlan,
    host: str,
    port: int,
) -> tuple[FullSourceRuntimeSessionReconciliation, ...]:
    starts_path = _runtime_session_start_journal(run_root, plan)
    receipts_path = _runtime_session_journal(run_root, plan)
    reconciliations_path = _runtime_session_reconciliation_journal(run_root, plan)
    starts = (
        _read_models(starts_path, FullSourceRuntimeSessionStart) if starts_path.is_file() else ()
    )
    receipts = (
        _read_models(receipts_path, FullSourceRuntimeSessionReceipt)
        if receipts_path.is_file()
        else ()
    )
    reconciliations = (
        _read_models(reconciliations_path, FullSourceRuntimeSessionReconciliation)
        if reconciliations_path.is_file()
        else ()
    )
    closed = {item.session_id for item in receipts}.union(
        item.session_id for item in reconciliations
    )
    for start in starts:
        if start.session_id not in closed:
            _append_dead_runtime_reconciliation(
                run_root=run_root,
                plan=plan,
                session_id=start.session_id,
                host=host,
                port=port,
            )
    reconciliations = (
        _read_models(reconciliations_path, FullSourceRuntimeSessionReconciliation)
        if reconciliations_path.is_file()
        else ()
    )
    if not reconciliations:
        return ()
    runtime_by_time = tuple(sorted(reconciliations, key=lambda item: item.reconciled_unix_ns))
    for _, _, requests in _prepared_chunks(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=sources,
        endpoint_url=endpoint_url,
    ):
        for request in requests:
            started_path = request.cell / "request_started.json"
            if (request.cell / "terminal.json").is_file() or not started_path.is_file():
                continue
            eligible = tuple(
                item
                for item in runtime_by_time
                if item.reconciled_unix_ns >= started_path.stat().st_mtime_ns
            )
            if not eligible:
                continue
            runtime = eligible[0]
            runtime_path = Path(runtime.reconciliation_artifact_path)
            rows = _provider_attempt_rows(request)
            if not rows:
                immutable_write(
                    request.cell / "request_reconciled.json",
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_provider_request_reconciliation_v1",
                            "request_key": request.request_key,
                            "request_started_sha256": hash_file(started_path),
                            "runtime_session_id": runtime.session_id,
                            "runtime_reconciliation_path": str(runtime_path),
                            "runtime_reconciliation_sha256": hash_file(runtime_path),
                            "reason": "dead_runtime_before_transport_attempt",
                            "provider_call_occurred": False,
                        }
                    )
                    + b"\n",
                )
                continue
            last = rows[-1]
            if last["outcome_name"] is not None:
                continue
            attempt_root = cast(Path, last["root"])
            immutable_write(
                attempt_root / "abandoned.json",
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_provider_transport_attempt_abandoned_v1",
                        "request_key": request.request_key,
                        "attempt_id": last["attempt_id"],
                        "attempt_number": last["attempt_number"],
                        "runtime_session_id": runtime.session_id,
                        "runtime_reconciliation_path": str(runtime_path),
                        "runtime_reconciliation_sha256": hash_file(runtime_path),
                        "reason": "dead_same_run_runtime",
                        "provider_call_may_have_occurred": True,
                    }
                )
                + b"\n",
            )
    _verify_runtime_sessions(run_root, plan, require_nonempty=False)
    return reconciliations


def _cache_semantic_terminal(request: PreparedRequest) -> VllmRequestTerminal | None:
    terminal_path = request.cell / "terminal.json"
    if terminal_path.is_file():
        return frozen_vllm_backend._cache_terminal(request)
    started_path = request.cell / "request_started.json"
    if not started_path.exists():
        return None
    if _semantic_request_can_resume(request):
        return None
    raise FullSourceConsumerError(
        f"ambiguous in-flight vLLM request; refusing duplicate: {request.request_key}"
    )


def _execute_semantic_request_with_retries(
    backend: LoadedVllmBackend,
    request: PreparedRequest,
    transport: CompletionTransport,
) -> _ProviderExecutionResult:
    model_calls = 0

    def retrying_transport(
        endpoint_url: str,
        payload: dict[str, object],
        request_key: str,
        timeout_seconds: float,
    ) -> Any:
        nonlocal model_calls
        prior = _provider_attempt_rows(request)
        if prior:
            last = prior[-1]
            if last["outcome_name"] == "success.json":
                return _persisted_provider_completion(last)
            if not _semantic_request_can_resume(request):
                raise FullSourceConsumerError(
                    "provider transport attempts are terminal or ambiguous"
                )
        for attempt_number in range(len(prior) + 1, MAX_PROVIDER_ATTEMPTS + 1):
            attempt_id = stable_id(
                "sft2b_provider_transport_attempt",
                {"request_key": request_key, "attempt_number": attempt_number},
            )
            attempt_root = _provider_attempt_root(request, attempt_number)
            immutable_write(
                attempt_root / "started.json",
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_provider_transport_attempt_started_v1",
                        "request_key": request_key,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                    }
                )
                + b"\n",
            )
            model_calls += 1
            try:
                completion = transport(endpoint_url, payload, request_key, timeout_seconds)
            except Exception as exc:
                retryable = _retryable_provider_failure(exc)
                immutable_write(
                    attempt_root / "failure.json",
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_provider_transport_attempt_failure_v1",
                            "request_key": request_key,
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "retryable": retryable,
                            "error_class": type(exc).__name__,
                            "error_detail": str(exc)[:2000] or type(exc).__name__,
                        }
                    )
                    + b"\n",
                )
                if retryable and attempt_number < MAX_PROVIDER_ATTEMPTS:
                    continue
                raise
            raw_response_path = attempt_root / "raw_response.sse"
            raw_output_path = attempt_root / "raw_output.txt"
            immutable_write(raw_response_path, completion.raw_response)
            immutable_write(raw_output_path, completion.output_text.encode("utf-8"))
            immutable_write(
                attempt_root / "success.json",
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_provider_transport_attempt_success_v1",
                        "request_key": request_key,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "response_id": completion.response_id,
                        "response_request_id": completion.response_request_id,
                        "prompt_tokens": completion.prompt_tokens,
                        "completion_tokens": completion.completion_tokens,
                        "finish_reason": completion.finish_reason,
                        "elapsed_ms": completion.elapsed_ms,
                        "time_to_first_token_ms": completion.time_to_first_token_ms,
                        "http_status": completion.http_status,
                        "raw_response_sha256": hash_file(raw_response_path),
                        "raw_output_sha256": hash_file(raw_output_path),
                    }
                )
                + b"\n",
            )
            return completion
        raise FullSourceConsumerError("provider retry ceiling exhausted")

    terminal = frozen_vllm_backend._execute_request(
        backend,
        request,
        retrying_transport,
    )
    return _ProviderExecutionResult(terminal=terminal, model_calls=model_calls)


def _inspect_scalable_cache(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
) -> _ScalableCacheInspection:
    profile = backend.spec.profiles[profile_name]
    source_ids = tuple(item.source_id for item in sources)
    if source_ids != profile.source_ids or len(set(source_ids)) != len(source_ids):
        raise FullSourceConsumerError("supplied source order differs from the vLLM profile")
    observed_run: str | None = None
    observed_root: Path | None = None
    request_count = 0
    cached = 0
    missing = 0
    ambiguous: list[str] = []
    seen: set[str] = set()
    for run_id, root, requests in _prepared_chunks(
        backend,
        profile_name=profile_name,
        sources=sources,
        endpoint_url=endpoint_url,
    ):
        if observed_run is None:
            observed_run, observed_root = run_id, root
        elif run_id != observed_run or root != observed_root:
            raise FullSourceConsumerError("chunked vLLM run identity drifted")
        for request in requests:
            if request.request_key in seen:
                raise FullSourceConsumerError("chunked vLLM request key repeated")
            seen.add(request.request_key)
            request_count += 1
            terminal_path = request.cell / "terminal.json"
            started_path = request.cell / "request_started.json"
            if (
                not terminal_path.exists()
                and started_path.exists()
                and not _semantic_request_can_resume(request)
            ):
                ambiguous.append(request.request_key)
            elif _cache_semantic_terminal(request) is None:
                missing += 1
            else:
                cached += 1
    if observed_run is None or observed_root is None:
        raise FullSourceConsumerError("vLLM profile has no prepared requests")
    if request_count != len(profile.source_ids) * len(profile.slots):
        raise FullSourceConsumerError("chunked vLLM request count mismatch")
    return _ScalableCacheInspection(
        run_id=observed_run,
        root=observed_root,
        request_count=request_count,
        cached_terminals=cached,
        missing_requests=missing,
        ambiguous_request_keys=tuple(ambiguous),
    )


def _append_canonical_string_hash(digest: Any, value: str, *, first: bool) -> None:
    if first:
        digest.update(b"[")
    else:
        digest.update(b",")
    digest.update(canonical_json_bytes(value))


def _write_ambiguous_reconciliation(
    *,
    inspection: _ScalableCacheInspection,
    plan: FullSourceRunPlan,
    run_root: Path,
) -> None:
    immutable_write(
        run_root / plan.shard_id / plan.run_id / "provider_reconciliation_failure.json",
        canonical_json_bytes(
            {
                "schema_version": "sft2b_full_source_provider_reconciliation_failure_v1",
                "run_id": plan.run_id,
                "vllm_run_id": inspection.run_id,
                "request_count": inspection.request_count,
                "cached_terminals": inspection.cached_terminals,
                "missing_requests": inspection.missing_requests,
                "ambiguous_request_keys": list(inspection.ambiguous_request_keys),
                "duplicate_provider_calls_permitted": False,
            }
        )
        + b"\n",
    )


def _run_scalable_and_reconcile(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
    plan: FullSourceRunPlan,
    cache_root: Path,
    run_root: Path,
    journal_fsync_every: int,
    transport: CompletionTransport | None,
    status_path: Path | None,
) -> _ScalableExecutionResult:
    """Keep one bounded request window full while preparing sources in chunks."""

    profile = backend.spec.profiles[profile_name]
    provider_journal: _ScalableVllmJournal | None = None
    consumer_journal = FullSourceJournal(
        run_root / plan.shard_id / plan.run_id / "journal/requests.jsonl",
        plan=plan,
        cache_root=cache_root,
        fsync_every=journal_fsync_every,
    )
    selected_transport = transport or stream_openai_completion
    observed_run: str | None = None
    observed_root: Path | None = None
    request_count = 0
    processed_count = 0
    model_calls = 0
    cache_hits = 0
    key_digest = hashlib.sha256()
    seen_keys: set[str] = set()
    checkpoint_interval = SOURCE_CHUNK_SIZE * len(profile.slots)

    def prepared_requests() -> Iterator[tuple[int, PreparedRequest]]:
        nonlocal observed_run, observed_root, provider_journal, request_count
        for run_id, root, requests in _prepared_chunks(
            backend,
            profile_name=profile_name,
            sources=sources,
            endpoint_url=endpoint_url,
        ):
            if observed_run is None:
                observed_run, observed_root = run_id, root
                provider_journal = _ScalableVllmJournal(root)
            elif run_id != observed_run or root != observed_root:
                raise FullSourceConsumerError("chunked vLLM execution identity drifted")
            for request in requests:
                if request.request_key in seen_keys:
                    raise FullSourceConsumerError("chunked vLLM execution request key repeated")
                seen_keys.add(request.request_key)
                ordinal = request_count
                _append_canonical_string_hash(
                    key_digest,
                    request.request_key,
                    first=request_count == 0,
                )
                request_count += 1
                yield ordinal, request

    def reconcile_terminal(
        ordinal: int,
        request: PreparedRequest,
        terminal: VllmRequestTerminal,
    ) -> None:
        nonlocal processed_count
        cell = plan.cells[ordinal]
        if (
            request.source.source_id != cell.source_id
            or request.slot.slot != cell.slot
            or request.slot.seed != cell.seed
            or cell.source_id != terminal.attempt.source_id
            or cell.slot != terminal.attempt.slot
            or cell.seed != terminal.attempt.lineage.seed
            or terminal.metrics.source_id != cell.source_id
            or terminal.metrics.slot != cell.slot
        ):
            raise FullSourceConsumerError("vLLM terminal does not map to its planned cell")
        cast(_ScalableVllmJournal, provider_journal).append(
            terminal, fsync_every=journal_fsync_every
        )
        terminal_path = write_cached_terminal(
            cache_root,
            plan,
            cell,
            payload={
                "schema_version": "sft2b_full_source_vllm_payload_v1",
                "request_key": terminal.request_key,
                "attempt_id": terminal.attempt.attempt_id,
                "response_id": terminal.metrics.response_id,
                "provider_artifacts": _provider_artifact_binding(terminal),
                "vllm_terminal": terminal.model_dump(mode="json"),
            },
        )
        consumer_journal.append_terminal(cell, terminal_path)
        processed_count += 1
        if processed_count % checkpoint_interval == 0:
            cast(_ScalableVllmJournal, provider_journal).sync()
            consumer_journal.sync()
            if status_path is not None:
                _write_durable_checkpoint(
                    status_path=status_path,
                    plan=plan,
                    provider_journal=cast(_ScalableVllmJournal, provider_journal).path,
                    consumer_journal=consumer_journal.path,
                    output_path=(
                        run_root / plan.shard_id / plan.run_id / "outputs/request_terminals.jsonl"
                    ),
                    sequence=processed_count,
                )

    request_iterator = iter(prepared_requests())
    exhausted = False
    pending: dict[
        concurrent.futures.Future[_ProviderExecutionResult], tuple[int, PreparedRequest]
    ] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=profile.concurrency) as pool:

        def fill_window() -> None:
            nonlocal exhausted, cache_hits
            while not exhausted and len(pending) < profile.concurrency:
                try:
                    ordinal, request = next(request_iterator)
                except StopIteration:
                    exhausted = True
                    break
                terminal = _cache_semantic_terminal(request)
                if terminal is not None:
                    cache_hits += 1
                    reconcile_terminal(ordinal, request, terminal)
                    continue
                future = pool.submit(
                    _execute_semantic_request_with_retries,
                    backend,
                    request,
                    selected_transport,
                )
                pending[future] = (ordinal, request)

        fill_window()
        while pending:
            done, _ = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                ordinal, request = pending.pop(future)
                try:
                    execution = future.result()
                except Exception as exc:
                    for other in pending:
                        other.cancel()
                    raise FullSourceConsumerError(
                        "vLLM request failed for "
                        f"{request.source.source_id}/{request.slot.slot}: {exc}"
                    ) from exc
                model_calls += execution.model_calls
                reconcile_terminal(ordinal, request, execution.terminal)
            fill_window()
    cast(_ScalableVllmJournal, provider_journal).sync()
    consumer_journal.sync()
    key_digest.update(b"]")
    if (
        observed_run is None
        or observed_root is None
        or request_count != len(plan.cells)
        or processed_count != len(plan.cells)
        or not consumer_journal.is_complete()
    ):
        raise FullSourceConsumerError("provider reconciliation did not close every planned cell")
    output_path = run_root / plan.shard_id / plan.run_id / "outputs/request_terminals.jsonl"
    compacted = compact_completed(consumer_journal, output_path)
    reconciliation = {
        "schema_version": "sft2b_full_source_provider_reconciliation_v2",
        "run_id": plan.run_id,
        "vllm_run_id": observed_run,
        "request_count": request_count,
        "request_keys_sha256": key_digest.hexdigest(),
        "request_started_artifacts": request_count,
        "verified_terminals": request_count,
        "ambiguous_inflight_requests": 0,
        "duplicate_provider_calls_permitted": False,
        "consumer_journal_rows": consumer_journal.completed_count(),
        "compacted_output_sha256": compacted.sha256,
        "complete": True,
    }
    immutable_write(
        run_root / plan.shard_id / plan.run_id / "provider_reconciliation.json",
        canonical_json_bytes(reconciliation) + b"\n",
    )
    return _ScalableExecutionResult(
        run_id=observed_run,
        root=observed_root,
        request_count=request_count,
        request_keys_sha256=key_digest.hexdigest(),
        model_calls=model_calls,
        cache_hits=cache_hits,
        compacted=compacted,
    )


def _run_scalable_vllm_sources(
    backend: LoadedVllmBackend,
    *,
    profile_name: str,
    sources: tuple[SourceRecord, ...],
    endpoint_url: str,
    journal_fsync_every: int,
    transport: CompletionTransport | None = None,
) -> VllmProfileResult:
    """Execute with bounded in-flight futures and a single indexed journal replay."""

    profile = backend.spec.profiles[profile_name]
    source_ids = tuple(item.source_id for item in sources)
    if source_ids != profile.source_ids or len(set(source_ids)) != len(source_ids):
        raise FullSourceConsumerError("supplied source order differs from the vLLM profile")
    run_id, root, requests = frozen_vllm_backend._prepare_requests(
        backend,
        profile_name=profile_name,
        sources=sources,
        endpoint_url=endpoint_url,
    )
    if len(requests) != len(profile.source_ids) * len(profile.slots):
        raise FullSourceConsumerError("vLLM prepared request count mismatch")
    journal = _ScalableVllmJournal(root, requests)
    terminals: dict[str, VllmRequestTerminal] = {}
    missing: list[PreparedRequest] = []
    for request in requests:
        terminal = frozen_vllm_backend._cache_terminal(request)
        if terminal is None:
            missing.append(request)
        else:
            terminals[request.request_key] = terminal
            journal.append(terminal, fsync_every=journal_fsync_every)

    selected_transport = transport or stream_openai_completion
    submitted = 0
    started = time.monotonic()
    try:
        if missing:
            missing_iterator = iter(missing)
            with concurrent.futures.ThreadPoolExecutor(max_workers=profile.concurrency) as pool:
                pending: dict[concurrent.futures.Future[VllmRequestTerminal], PreparedRequest] = {}

                def submit_one() -> bool:
                    nonlocal submitted
                    try:
                        request = next(missing_iterator)
                    except StopIteration:
                        return False
                    future = pool.submit(
                        frozen_vllm_backend._execute_request,
                        backend,
                        request,
                        selected_transport,
                    )
                    pending[future] = request
                    submitted += 1
                    return True

                for _ in range(min(profile.concurrency, len(missing))):
                    submit_one()
                while pending:
                    done, _ = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in done:
                        request = pending.pop(future)
                        try:
                            terminal = future.result()
                        except Exception as exc:
                            for other in pending:
                                other.cancel()
                            identity = f"{request.source.source_id}/{request.slot.slot}"
                            raise FullSourceConsumerError(
                                f"vLLM request failed for {identity}: {exc}"
                            ) from exc
                        terminals[request.request_key] = terminal
                        journal.append(terminal, fsync_every=journal_fsync_every)
                        submit_one()
    finally:
        journal.sync()
    ordered = tuple(terminals[request.request_key] for request in requests)
    return VllmProfileResult(
        run_id=run_id,
        root=root,
        terminals=ordered,
        model_calls=submitted,
        cache_hits=len(requests) - len(missing),
        wall_time_ms=round((time.monotonic() - started) * 1000),
    )


def _provider_artifact_binding(terminal: VllmRequestTerminal) -> dict[str, str]:
    cell = Path(terminal.metrics.raw_output_path).parent
    request_path = cell / "request.json"
    started_path = cell / "request_started.json"
    terminal_path = cell / "terminal.json"
    for name, path in (
        ("request", request_path),
        ("request_started", started_path),
        ("terminal", terminal_path),
    ):
        if not path.is_file():
            raise FullSourceConsumerError(f"provider reconciliation lacks {name} artifact")
    request = _json_object(request_path)
    started = _json_object(started_path)
    semantic_request = (cell / "transport_attempts").is_dir()
    if (
        request.get("request_key") != terminal.request_key
        or request.get("attempt_id") != terminal.attempt.attempt_id
        or started
        != {
            "schema_version": "sft2b_vllm_request_started_v1",
            "request_key": terminal.request_key,
        }
        or terminal.metrics.request_payload_sha256 != hash_canonical(request.get("payload"))
        or terminal.metrics.response_request_id != terminal.request_key
    ):
        raise FullSourceConsumerError("provider request/start/response identity drifted")
    binding = {
        "request_sha256": hash_file(request_path),
        "request_started_sha256": hash_file(started_path),
        "terminal_sha256": hash_file(terminal_path),
    }
    if semantic_request:
        attempts_root = cell / "transport_attempts"
        attempt_files = sorted(item for item in attempts_root.rglob("*") if item.is_file())
        if not attempt_files or any(item.is_symlink() for item in attempt_files):
            raise FullSourceConsumerError("semantic provider terminal lacks attempt evidence")
        binding["transport_attempts_sha256"] = hash_canonical(
            {str(path.relative_to(attempts_root)): hash_file(path) for path in attempt_files}
        )
        reconciliation_path = cell / "request_reconciled.json"
        if reconciliation_path.is_file():
            binding["request_reconciliation_sha256"] = hash_file(reconciliation_path)
    return binding


def reconcile_vllm_result(
    *,
    result: VllmProfileResult,
    plan: FullSourceRunPlan,
    cache_root: Path,
    run_root: Path,
    journal_fsync_every: int = 1,
) -> CompactionResult:
    """Reconcile provider terminals into the consumer journal after any crash.

    A crash after a provider terminal but before consumer journaling is safe:
    the imported backend verifies and reuses the immutable terminal, this
    function writes the same content-addressed envelope, and duplicate journal
    entries are suppressed.  A lone ``request_started.json`` remains ambiguous
    and the backend refuses a second provider call.
    """

    by_cell = {(cell.source_id, cell.slot): cell for cell in plan.cells}
    if len(result.terminals) != len(plan.cells):
        raise FullSourceConsumerError("vLLM result is not the complete planned product")
    journal = FullSourceJournal(
        run_root / plan.shard_id / plan.run_id / "journal/requests.jsonl",
        plan=plan,
        cache_root=cache_root,
        fsync_every=journal_fsync_every,
    )
    request_keys: list[str] = []
    seen_request_keys: set[str] = set()
    try:
        for terminal in result.terminals:
            key = (terminal.attempt.source_id, terminal.attempt.slot)
            cell = by_cell.get(key)
            if (
                cell is None
                or terminal.attempt.lineage.seed != cell.seed
                or terminal.metrics.source_id != cell.source_id
                or terminal.metrics.slot != cell.slot
            ):
                raise FullSourceConsumerError("vLLM terminal does not map to its planned cell")
            if terminal.request_key in seen_request_keys:
                raise FullSourceConsumerError("vLLM result contains a duplicate provider request")
            seen_request_keys.add(terminal.request_key)
            provider_artifacts = _provider_artifact_binding(terminal)
            request_keys.append(terminal.request_key)
            terminal_path = write_cached_terminal(
                cache_root,
                plan,
                cell,
                payload={
                    "schema_version": "sft2b_full_source_vllm_payload_v1",
                    "request_key": terminal.request_key,
                    "attempt_id": terminal.attempt.attempt_id,
                    "response_id": terminal.metrics.response_id,
                    "provider_artifacts": provider_artifacts,
                    "vllm_terminal": terminal.model_dump(mode="json"),
                },
            )
            journal.append_terminal(cell, terminal_path)
    finally:
        journal.sync()
    if len(set(request_keys)) != len(plan.cells) or journal.missing_cells():
        raise FullSourceConsumerError("provider reconciliation did not close every planned cell")
    output_path = run_root / plan.shard_id / plan.run_id / "outputs/request_terminals.jsonl"
    compacted = compact_completed(journal, output_path)
    reconciliation = {
        "schema_version": "sft2b_full_source_provider_reconciliation_v1",
        "run_id": plan.run_id,
        "vllm_run_id": result.run_id,
        "request_count": len(request_keys),
        "request_keys_sha256": hash_canonical(request_keys),
        "request_started_artifacts": len(seen_request_keys),
        "verified_terminals": len(seen_request_keys),
        "ambiguous_inflight_requests": 0,
        "duplicate_provider_calls_permitted": False,
        "consumer_journal_rows": len(journal.events()),
        "compacted_output_sha256": compacted.sha256,
        "complete": True,
    }
    immutable_write(
        run_root / plan.shard_id / plan.run_id / "provider_reconciliation.json",
        canonical_json_bytes(reconciliation) + b"\n",
    )
    return compacted


def run_integrated_executor(
    *,
    spec: FullSourceConsumerSpec,
    backend: LoadedVllmBackend,
    sources: tuple[SourceRecord, ...],
    plan: FullSourceRunPlan,
    cache_root: Path,
    run_root: Path,
    transport: CompletionTransport | None = None,
    status_path: Path | None = None,
    launch_nonce: str | None = None,
    launched_unix_ns: int | None = None,
    resource_claim_path: Path | None = None,
) -> CompactionResult:
    """Fill missing provider cells, then reconcile and compact exactly once."""

    endpoint = profile_endpoint(backend, FULL_PROFILE_NAME)
    inspection = _inspect_scalable_cache(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=sources,
        endpoint_url=endpoint,
    )
    if inspection.ambiguous_request_keys:
        _write_ambiguous_reconciliation(
            inspection=inspection,
            plan=plan,
            run_root=run_root,
        )
        raise FullSourceConsumerError(
            "ambiguous in-flight vLLM request(s); refusing duplicate provider calls"
        )
    if inspection.request_count != len(plan.cells):
        raise FullSourceConsumerError("integrated vLLM request count differs from run plan")
    runtime_attestation_error: str | None = None
    try:
        _verify_runtime_sessions(run_root, plan, require_nonempty=False)
    except FullSourceConsumerError as exc:
        if not inspection.complete:
            raise
        runtime_attestation_error = str(exc)
    server_process: subprocess.Popen[bytes] | None = None
    monitor: TelemetryMonitor | None = None
    server_log: Path | None = None
    telemetry_path: Path | None = None
    shutdown_path: Path | None = None
    runtime_session_id: str | None = None
    runtime_started_ns: int | None = None
    primary_error: Exception | None = None
    server_observation: dict[str, object] = {"cache_complete_at_start": inspection.complete}
    if status_path is not None:
        if launch_nonce is None or launched_unix_ns is None:
            raise FullSourceConsumerError("status tracking lacks detached launch identity")
        atomic_write(
            status_path,
            _status_payload(
                plan,
                state="worker_started",
                launch_nonce=launch_nonce,
                launched_unix_ns=launched_unix_ns,
                supervisor_pid=os.getpid(),
                progress_artifacts=[
                    {
                        "path": str(inspection.root / "journal/requests.jsonl"),
                        "kind": "bytes",
                        "baseline": _file_size(inspection.root / "journal/requests.jsonl"),
                    },
                    {
                        "path": str(
                            run_root / plan.shard_id / plan.run_id / "journal/requests.jsonl"
                        ),
                        "kind": "bytes",
                        "baseline": _file_size(
                            run_root / plan.shard_id / plan.run_id / "journal/requests.jsonl"
                        ),
                    },
                    {
                        "path": str(
                            run_root
                            / plan.shard_id
                            / plan.run_id
                            / "outputs/request_terminals.jsonl"
                        ),
                        "kind": "bytes",
                        "baseline": _file_size(
                            run_root
                            / plan.shard_id
                            / plan.run_id
                            / "outputs/request_terminals.jsonl"
                        ),
                    },
                ],
            ),
        )
    try:
        if not inspection.complete and transport is None:
            if resource_claim_path is None:
                raise FullSourceConsumerError(
                    "provider launch lacks session resource-claim evidence"
                )
            command = build_vllm_serve_command(backend, profile_name=FULL_PROFILE_NAME)
            environment = os.environ.copy()
            environment["CUDA_VISIBLE_DEVICES"] = visible_devices_csv(
                backend.spec.profiles[FULL_PROFILE_NAME]
            )
            runtime_started_ns = time.time_ns()
            runtime_session_id = f"{runtime_started_ns}-{os.getpid()}"
            session_root = (
                run_root / plan.shard_id / plan.run_id / "runtime_sessions" / runtime_session_id
            )
            server_log = session_root / "vllm_server.log"
            telemetry_path = session_root / "telemetry.jsonl"
            shutdown_path = session_root / "server_shutdown.json"
            server_log.parent.mkdir(parents=True, exist_ok=True)
            with server_log.open("ab", buffering=0) as log_handle:
                server_process = subprocess.Popen(
                    command,
                    cwd=backend.config_path.resolve().parents[2],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                log_handle.write(
                    f"[LeanFaith] Started server process [{server_process.pid}]\n".encode()
                )
                _append_runtime_session_start(
                    run_root=run_root,
                    plan=plan,
                    session_id=runtime_session_id,
                    server_pid=server_process.pid,
                    served_model_name=spec.model.served_model_name,
                    started_unix_ns=runtime_started_ns,
                    backend_config_sha256=backend.config_sha256,
                    claim_path=resource_claim_path,
                )
                server_observation.update(
                    _wait_for_vllm(
                        server_process,
                        endpoint_url=endpoint,
                        served_model_name=spec.model.served_model_name,
                        timeout_seconds=spec.executor.server_startup_timeout_seconds,
                        log_path=server_log,
                    )
                )
                monitor = TelemetryMonitor(
                    endpoint_url=endpoint,
                    interval_seconds=spec.executor.telemetry_interval_seconds,
                    server_pid=server_process.pid,
                )
                monitor.start()
        result = _run_scalable_and_reconcile(
            backend,
            profile_name=FULL_PROFILE_NAME,
            sources=sources,
            endpoint_url=endpoint,
            plan=plan,
            cache_root=cache_root,
            run_root=run_root,
            journal_fsync_every=spec.executor.journal_fsync_every,
            transport=transport,
            status_path=status_path,
        )
        if inspection.complete and transport is None:
            sessions: tuple[FullSourceRuntimeSessionReceipt, ...] = ()
            try:
                sessions = _verify_runtime_sessions(run_root, plan, require_nonempty=False)
            except FullSourceConsumerError as exc:
                runtime_attestation_error = str(exc)
            if not sessions or runtime_attestation_error is not None:
                immutable_write(
                    run_root / plan.shard_id / plan.run_id / "cache_recovery.json",
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_full_source_cache_recovery_v1",
                            "run_id": plan.run_id,
                            "vllm_run_id": result.run_id,
                            "request_count": result.request_count,
                            "request_keys_sha256": result.request_keys_sha256,
                            "compacted_output_sha256": result.compacted.sha256,
                            "provider_calls": 0,
                            "cache_hits": result.cache_hits,
                            "cache_recovered_after_unclosed_runtime": True,
                            "clean_shutdown_evidence": False,
                            "authorization_evidence": False,
                            "runtime_attestation_error": runtime_attestation_error,
                        }
                    )
                    + b"\n",
                )
        return result.compacted
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: Exception | None = None
        telemetry_summary: Mapping[str, Any] | None = None
        if monitor is not None and telemetry_path is not None:
            try:
                monitor.stop()
                monitor.write(telemetry_path)
                telemetry_summary = monitor.summary()
            except Exception as exc:
                cleanup_error = exc
        if server_process is not None:
            server_pid = server_process.pid
            try:
                escalated, return_code = _stop_vllm(
                    server_process,
                    timeout_seconds=spec.executor.server_shutdown_timeout_seconds,
                )
                if shutdown_path is None or runtime_session_id is None:
                    raise FullSourceConsumerError("runtime session paths were not initialized")
                immutable_write(
                    shutdown_path,
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_full_source_server_shutdown_v1",
                            "run_id": plan.run_id,
                            "session_id": runtime_session_id,
                            "server_pid": server_pid,
                            "server_observation": server_observation,
                            "stopped": True,
                            "return_code": return_code,
                            "kill_escalated": escalated,
                            "clean_shutdown": not escalated and return_code in {0, -signal.SIGTERM},
                            "process_absent_after_shutdown": server_process.poll() is not None,
                        }
                    )
                    + b"\n",
                )
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if (
                cleanup_error is None
                and telemetry_summary is not None
                and runtime_session_id is not None
                and runtime_started_ns is not None
                and server_log is not None
                and telemetry_path is not None
                and shutdown_path is not None
            ):
                try:
                    _append_runtime_session(
                        run_root=run_root,
                        plan=plan,
                        session_id=runtime_session_id,
                        server_pid=server_pid,
                        served_model_name=spec.model.served_model_name,
                        started_unix_ns=runtime_started_ns,
                        server_log_path=server_log,
                        telemetry_path=telemetry_path,
                        telemetry_summary=telemetry_summary,
                        shutdown_path=shutdown_path,
                    )
                except Exception as exc:
                    cleanup_error = exc
        if cleanup_error is not None:
            message = f"vLLM runtime telemetry/shutdown evidence failed: {cleanup_error}"
            if primary_error is not None:
                message = (
                    f"primary executor failure {type(primary_error).__name__}: {primary_error}; "
                    + message
                )
            raise FullSourceConsumerError(message) from primary_error or cleanup_error


def _session_name(spec: FullSourceConsumerSpec, plan: FullSourceRunPlan) -> str:
    suffix = plan.run_id.split(":", 1)[1][:12]
    name = f"{spec.runtime.tmux_session_prefix}-{plan.shard_id}-{suffix}"
    if _TMUX_NAME_RE.fullmatch(name) is None:
        raise FullSourceConsumerError(f"invalid deterministic tmux session name: {name}")
    return name


def _expected_provider_journal(
    repo_root: Path,
    spec: FullSourceConsumerSpec,
    *,
    plan: FullSourceRunPlan,
) -> Path:
    profile_id = f"sft2b_reform_32b_{plan.shard_id}_dp4_tp2_{plan.run_id.split(':', 1)[1][:16]}"
    placement = _json_object(repo_root / spec.model.placement_config_path)
    decoding = placement.get("decoding")
    if not isinstance(decoding, dict):
        raise FullSourceConsumerError("placement config lacks semantic decoding identity")
    provider_run_id = stable_id(
        "sft2b_vllm_run",
        {
            "schema_version": "sft2b_vllm_semantic_run_v2",
            "provider": "local_vllm_openai",
            "model_id": spec.model.model_id,
            "model_revision": spec.model.revision,
            "snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
            "source_ids": plan.source_ids,
            "slots": tuple(CandidateSlot),
            "prompt_template_sha256": spec.model.prompt_sha256,
            "decoding_sha256": hash_canonical(decoding),
        },
    )
    return (
        spec.runtime.cache_root
        / "vllm/generation/vllm"
        / profile_id
        / provider_run_id
        / "journal/requests.jsonl"
    )


def build_detached_launch(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    bundle_root: Path,
    plan: FullSourceRunPlan,
    run_root: Path,
) -> DetachedLaunch:
    """Build the named tmux command only after the hard launch gate passes."""

    if (
        spec.schema_version == CONFIG_SCHEMA_V2
        and run_root.resolve() != spec.runtime.run_root.resolve()
    ):
        raise FullSourceConsumerError(
            "detached run root differs from the frozen A100 /scratch root"
        )
    evidence = verify_matched_500_gate(repo_root, spec)
    if (
        not spec.authorization.frozen
        or spec.authorization.pilot_evidence_binding_sha256 != evidence.evidence_binding_sha256
    ):
        raise FullSourceConsumerError("frozen authorization does not bind verified pilot evidence")
    if plan.shard_id == CORE_SHARD and not spec.authorization.core_enabled:
        raise FullSourceConsumerError("corrected core launch is not authorized")
    if plan.shard_id == TAIL_SHARD and not spec.authorization.tail_enabled:
        raise FullSourceConsumerError("legacy tail launch is not independently authorized")
    if spec.executor.kind != "integrated_vllm":
        raise FullSourceConsumerError("full-source launch lacks the integrated executor")
    session_name = _session_name(spec, plan)
    launch_time = time.time_ns()
    launch_nonce = hash_canonical(
        {
            "schema_version": "sft2b_detached_launch_nonce_v1",
            "run_id": plan.run_id,
            "shard_id": plan.shard_id,
            "launched_unix_ns": launch_time,
            "launcher_pid": os.getpid(),
        }
    )
    shard_root = run_root / plan.shard_id / plan.run_id
    status_path = shard_root / "launch_status.json"
    log_path = shard_root / "consumer.log"
    supervisor_argv = (
        "uv",
        "run",
        "python",
        "-m",
        "leanfaith.sft2b.full_source_consumer",
        "supervise",
        "--repo-root",
        str(repo_root),
        "--config",
        str(config_path),
        "--bundle-root",
        str(bundle_root),
        "--shard",
        plan.shard_id,
        "--run-root",
        str(run_root),
        "--launch-nonce",
        launch_nonce,
        "--launched-unix-ns",
        str(launch_time),
    )
    shell_command = f"{shlex.join(supervisor_argv)} >> {shlex.quote(str(log_path))} 2>&1 </dev/null"
    command = (
        "tmux",
        "new-session",
        "-d",
        "-s",
        session_name,
        "-c",
        str(repo_root),
        shell_command,
    )
    return DetachedLaunch(
        session_name=session_name,
        command=command,
        status_path=status_path,
        log_path=log_path,
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        launch_nonce=launch_nonce,
        launched_unix_ns=launch_time,
        provider_journal_path=_expected_provider_journal(
            repo_root,
            spec,
            plan=plan,
        ),
        consumer_journal_path=shard_root / "journal/requests.jsonl",
        compacted_output_path=shard_root / "outputs/request_terminals.jsonl",
    )


def inspect_detached_health(launch: DetachedLaunch) -> DetachedHealth:
    """Require liveness plus actual journal/output advancement."""

    pane = subprocess.run(
        (
            "tmux",
            "display-message",
            "-p",
            "-t",
            f"={launch.session_name}",
            "#{pane_pid}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    pane_pid: int | None = None
    if pane.returncode == 0 and pane.stdout.strip().isdigit():
        pane_pid = int(pane.stdout.strip())
    state = "not_started"
    advanced = False
    if launch.status_path.is_file():
        value = _json_object(launch.status_path)
        raw_state = value.get("state")
        supervisor_pid = value.get("supervisor_pid")
        if (
            value.get("schema_version") != "sft2b_full_source_launch_status_v1"
            or value.get("run_id") != launch.run_id
            or value.get("shard_id") != launch.shard_id
            or value.get("launch_nonce") != launch.launch_nonce
            or value.get("launched_unix_ns") != launch.launched_unix_ns
            or launch.status_path.stat().st_mtime_ns < launch.launched_unix_ns
            or not isinstance(raw_state, str)
            or (raw_state == "launch_pending" and supervisor_pid is not None)
            or (
                raw_state != "launch_pending"
                and (
                    not isinstance(supervisor_pid, int)
                    or isinstance(supervisor_pid, bool)
                    or supervisor_pid <= 0
                )
            )
        ):
            raise FullSourceConsumerError("detached launch status identity/schema drifted")
        if (
            pane_pid is not None
            and isinstance(supervisor_pid, int)
            and not _process_descends_from(
                child_pid=supervisor_pid,
                ancestor_pid=pane_pid,
            )
        ):
            raise FullSourceConsumerError("tmux pane does not own the recorded supervisor process")
        state = raw_state
        progress = value.get("progress_artifacts")
        expected_fixed = {
            launch.consumer_journal_path.resolve(): "bytes",
            launch.compacted_output_path.resolve(): "bytes",
        }
        valid_progress = isinstance(progress, list) and len(progress) == 3
        observed_paths: set[Path] = set()
        if valid_progress:
            for raw in cast(list[object], progress):
                if not isinstance(raw, dict) or set(raw) != {"path", "kind", "baseline"}:
                    valid_progress = False
                    break
                path_value = raw.get("path")
                kind = raw.get("kind")
                baseline = raw.get("baseline")
                if (
                    not isinstance(path_value, str)
                    or kind != "bytes"
                    or not isinstance(baseline, int)
                    or isinstance(baseline, bool)
                    or baseline < 0
                ):
                    valid_progress = False
                    break
                path = Path(path_value).resolve()
                observed_paths.add(path)
                if path in expected_fixed:
                    continue
                if path != launch.provider_journal_path.resolve():
                    valid_progress = False
                    break
            if len(observed_paths) != 3 or not set(expected_fixed).issubset(observed_paths):
                valid_progress = False
        checkpoint = value.get("durable_checkpoint")
        if (
            valid_progress
            and isinstance(checkpoint, dict)
            and (
                set(checkpoint)
                == {
                    "schema_version",
                    "sequence",
                    "provider_journal_bytes",
                    "consumer_journal_bytes",
                    "compacted_output_bytes",
                }
                and checkpoint.get("schema_version") == "sft2b_full_source_durable_checkpoint_v1"
                and isinstance(checkpoint.get("sequence"), int)
                and cast(int, checkpoint["sequence"]) > 0
                and checkpoint.get("provider_journal_bytes")
                == _file_size(next(path for path in observed_paths if path not in expected_fixed))
                and checkpoint.get("consumer_journal_bytes")
                == _file_size(launch.consumer_journal_path)
                and checkpoint.get("compacted_output_bytes")
                == _file_size(launch.compacted_output_path)
            )
        ):
            baselines = {
                Path(cast(str, raw["path"])).resolve(): cast(int, raw["baseline"])
                for raw in cast(list[dict[str, object]], progress)
            }
            advanced = any(_file_size(path) > baseline for path, baseline in baselines.items())
        if valid_progress and state == "completed" and value.get("cache_complete_noop") is True:
            advanced = _file_size(launch.compacted_output_path) > 0
    healthy_states = {"worker_started", "completed"}
    healthy = (
        state in healthy_states and advanced and (pane_pid is not None or state == "completed")
    )
    return DetachedHealth(
        session_name=launch.session_name,
        pane_pid=pane_pid,
        state=state,
        durable_advancement=advanced,
        healthy=healthy,
    )


def _process_descends_from(*, child_pid: int, ancestor_pid: int) -> bool:
    """Read the Linux parent chain without trusting a self-reported PID."""

    current = child_pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        if current == ancestor_pid:
            return True
        visited.add(current)
        status_path = Path(f"/proc/{current}/status")
        try:
            lines = status_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        parent_lines = [line for line in lines if line.startswith("PPid:")]
        if len(parent_lines) != 1:
            return False
        raw_parent = parent_lines[0].split(":", 1)[1].strip()
        if not raw_parent.isdigit():
            return False
        current = int(raw_parent)
    return False


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def launch_detached(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    bundle_root: Path,
    plan: FullSourceRunPlan,
    run_root: Path,
) -> DetachedHealth:
    """Start and health-check the authorized detached supervisor.

    The checked-in waiting config always raises before filesystem mutation,
    tmux, or resource claiming.
    """

    launch = build_detached_launch(
        repo_root,
        spec=spec,
        config_path=config_path,
        bundle_root=bundle_root,
        plan=plan,
        run_root=run_root,
    )
    existing = subprocess.run(
        ("tmux", "has-session", "-t", f"={launch.session_name}"),
        check=False,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        raise FullSourceConsumerError(f"tmux session already exists: {launch.session_name}")
    launch.log_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        launch.status_path,
        _status_payload(
            plan,
            state="launch_pending",
            launch_nonce=launch.launch_nonce,
            launched_unix_ns=launch.launched_unix_ns,
            supervisor_pid=None,
        ),
    )
    started = subprocess.run(launch.command, check=False, capture_output=True, text=True)
    if started.returncode != 0:
        raise FullSourceConsumerError(f"tmux launch failed: {started.stderr.strip()}")
    deadline = time.monotonic() + spec.runtime.startup_health_timeout_seconds
    last = DetachedHealth(launch.session_name, None, "not_started", False, False)
    while time.monotonic() < deadline:
        last = inspect_detached_health(launch)
        if last.healthy:
            return last
        if last.state in {"failed", "recovered_unattested"}:
            break
        time.sleep(0.5)
    raise FullSourceConsumerError(
        f"detached launch failed health contract: session={launch.session_name}, state={last.state}"
    )


def _status_payload(
    plan: FullSourceRunPlan,
    *,
    state: str,
    launch_nonce: str,
    launched_unix_ns: int,
    **extra: object,
) -> bytes:
    value = {
        "schema_version": "sft2b_full_source_launch_status_v1",
        "run_id": plan.run_id,
        "shard_id": plan.shard_id,
        "launch_nonce": launch_nonce,
        "launched_unix_ns": launched_unix_ns,
        "state": state,
        **extra,
    }
    return canonical_json_bytes(value) + b"\n"


def _write_durable_checkpoint(
    *,
    status_path: Path,
    plan: FullSourceRunPlan,
    provider_journal: Path,
    consumer_journal: Path,
    output_path: Path,
    sequence: int,
) -> None:
    current = _json_object(status_path)
    progress = current.get("progress_artifacts")
    launch_nonce = current.get("launch_nonce")
    launched_unix_ns = current.get("launched_unix_ns")
    if (
        not isinstance(progress, list)
        or not isinstance(launch_nonce, str)
        or not isinstance(launched_unix_ns, int)
        or isinstance(launched_unix_ns, bool)
    ):
        raise FullSourceConsumerError("launch status lost its progress artifact contract")
    atomic_write(
        status_path,
        _status_payload(
            plan,
            state="worker_started",
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=os.getpid(),
            progress_artifacts=progress,
            durable_checkpoint={
                "schema_version": "sft2b_full_source_durable_checkpoint_v1",
                "sequence": sequence,
                "provider_journal_bytes": _file_size(provider_journal),
                "consumer_journal_bytes": _file_size(consumer_journal),
                "compacted_output_bytes": _file_size(output_path),
            },
        ),
    )


def supervise_shard(
    repo_root: Path,
    *,
    spec: FullSourceConsumerSpec,
    config_path: Path,
    config_sha256: str,
    bundle_root: Path,
    shard_id: str,
    run_root: Path,
    launch_nonce: str,
    launched_unix_ns: int,
) -> int:
    """Claim the A100 host around the integrated executor and release it reliably."""

    if run_root.resolve() != spec.runtime.run_root.resolve():
        raise FullSourceConsumerError("supervisor run root differs from frozen A100 /scratch root")
    evidence = verify_matched_500_gate(repo_root, spec)
    if evidence.evidence_binding_sha256 != spec.authorization.pilot_evidence_binding_sha256:
        raise FullSourceConsumerError("authorization does not bind the verified pilot artifacts")
    verified = verify_source_views(spec, bundle_root=bundle_root)
    source_ids = verified.shard_source_ids[shard_id]
    plan = build_run_plan(
        spec, config_sha256=config_sha256, shard_id=shard_id, source_ids=source_ids
    )
    shard_root = run_root / shard_id / plan.run_id
    status_path = shard_root / "launch_status.json"
    if not status_path.is_file():
        raise FullSourceConsumerError("supervisor lacks its launcher-created status nonce")
    pending = _json_object(status_path)
    if (
        pending.get("schema_version") != "sft2b_full_source_launch_status_v1"
        or pending.get("run_id") != plan.run_id
        or pending.get("shard_id") != plan.shard_id
        or pending.get("state") != "launch_pending"
        or pending.get("launch_nonce") != launch_nonce
        or pending.get("launched_unix_ns") != launched_unix_ns
        or status_path.stat().st_mtime_ns < launched_unix_ns
    ):
        raise FullSourceConsumerError("supervisor launch nonce/status is stale or mismatched")
    atomic_write(
        status_path,
        _status_payload(
            plan,
            state="starting",
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=os.getpid(),
        ),
    )
    backend, sources = build_integrated_vllm_backend(
        repo_root,
        spec=spec,
        config_path=config_path,
        config_sha256=config_sha256,
        bundle_root=bundle_root,
        verified=verified,
        plan=plan,
    )
    endpoint = profile_endpoint(backend, FULL_PROFILE_NAME)
    _reconcile_dead_same_run_runtime_and_requests(
        backend=backend,
        sources=sources,
        endpoint_url=endpoint,
        run_root=run_root,
        plan=plan,
        host=backend.spec.launch.host,
        port=spec.executor.port,
    )
    _reconcile_stale_same_run_reservation(
        repo_root=repo_root,
        spec=spec,
        plan=plan,
        run_root=run_root,
        shard_root=shard_root,
        host=backend.spec.launch.host,
        port=spec.executor.port,
    )
    inspection = _inspect_scalable_cache(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=sources,
        endpoint_url=endpoint,
    )
    if inspection.ambiguous_request_keys:
        _write_ambiguous_reconciliation(
            inspection=inspection,
            plan=plan,
            run_root=run_root,
        )
        raise FullSourceConsumerError(
            "ambiguous in-flight vLLM request(s); refusing resource claim/provider recall"
        )
    output_existed = (shard_root / "outputs/request_terminals.jsonl").is_file()
    claimed = False
    reservation: object | None = None
    claim_id: str | None = None
    claim_path: Path | None = None
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    compacted: CompactionResult | None = None
    try:
        active = [
            item
            for item in list_reservations(spec.runtime.reservation_root)
            if item.task == spec.runtime.reservation_task
        ]
        if active:
            item = active[0]
            live = item.hostname == socket.gethostname() and Path(f"/proc/{item.pid}").exists()
            state = "live" if live else "stale/unverifiable"
            raise FullSourceConsumerError(
                f"existing SFT2B resource claim is {state}; refusing foreign release/restart"
            )
        if not inspection.complete:
            if _port_is_open(backend.spec.launch.host, spec.executor.port):
                raise FullSourceConsumerError(
                    "vLLM port is already open without a same-run runtime receipt"
                )
            reservation = claim_resources(
                root=spec.runtime.reservation_root,
                task=spec.runtime.reservation_task,
                lean_workers=0,
                lean_rss_gib=0.0,
                gpu=True,
                pid=os.getpid(),
                owner_session=f"{spec.runtime.owner_session}; run_id={plan.run_id}",
                worktree=repo_root,
            )
            claimed = True
            claim_id = f"{time.time_ns()}-{os.getpid()}"
            claim_payload = {
                "schema_version": "sft2b_full_source_resource_claim_v2",
                "run_id": plan.run_id,
                "reservation_root": str(spec.runtime.reservation_root),
                "launch_nonce": launch_nonce,
                "launched_unix_ns": launched_unix_ns,
                "reservation": asdict(cast(Any, reservation)),
            }
            claim_path = _append_resource_record(
                shard_root=shard_root,
                kind="claim",
                claim_id=claim_id,
                payload=claim_payload,
            )
            atomic_write(
                status_path,
                _status_payload(
                    plan,
                    state="resource_claimed",
                    launch_nonce=launch_nonce,
                    launched_unix_ns=launched_unix_ns,
                    supervisor_pid=os.getpid(),
                    resource_claim_path=str(claim_path),
                ),
            )
        compacted = run_integrated_executor(
            spec=spec,
            backend=backend,
            sources=sources,
            plan=plan,
            cache_root=spec.runtime.cache_root,
            run_root=run_root,
            status_path=status_path,
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            resource_claim_path=claim_path,
        )
    except Exception as exc:
        failure = exc
    finally:
        if claimed:
            try:
                released = release_resources(
                    root=spec.runtime.reservation_root, task=spec.runtime.reservation_task
                )
                active_after = sum(
                    item.task == spec.runtime.reservation_task
                    for item in list_reservations(spec.runtime.reservation_root)
                )
                if reservation != released or active_after != 0:
                    raise FullSourceConsumerError(
                        "resource release differs from its acquired claim"
                    )
                if claim_id is None or claim_path is None:
                    raise FullSourceConsumerError("resource claim identity was not retained")
                _append_resource_record(
                    shard_root=shard_root,
                    kind="release",
                    claim_id=claim_id,
                    payload={
                        "schema_version": "sft2b_full_source_resource_release_v2",
                        "run_id": plan.run_id,
                        "task": spec.runtime.reservation_task,
                        "launch_nonce": launch_nonce,
                        "launched_unix_ns": launched_unix_ns,
                        "claim_artifact_path": str(claim_path),
                        "claim_sha256": hash_file(claim_path),
                        "supervisor_pid": os.getpid(),
                        "released": True,
                        "active_task_claims_after_release": active_after,
                    },
                )
            except Exception as exc:
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
    status_value = _json_object(status_path)
    progress = status_value.get("progress_artifacts", [])
    if failure is not None or cleanup_errors:
        original = f"{type(failure).__name__}: {failure}" if failure is not None else None
        atomic_write(
            status_path,
            _status_payload(
                plan,
                state="failed",
                launch_nonce=launch_nonce,
                launched_unix_ns=launched_unix_ns,
                supervisor_pid=os.getpid(),
                progress_artifacts=progress,
                error=original,
                cleanup_errors=cleanup_errors,
                resource_released=claimed and not cleanup_errors,
                claim_may_remain=claimed and bool(cleanup_errors),
            ),
        )
        message = "; ".join(item for item in ([original] if original else []) + cleanup_errors)
        combined = FullSourceConsumerError(f"full-source supervisor failed: {message}")
        if failure is not None:
            raise combined from failure
        raise combined
    if compacted is None:
        raise FullSourceConsumerError("integrated executor returned without compaction")
    attestation_error: str | None = None
    try:
        _verify_runtime_sessions(run_root, plan, require_nonempty=True)
        _verify_full_resource_release(shard_root, spec=spec, plan=plan)
    except Exception as exc:
        attestation_error = f"{type(exc).__name__}: {exc}"
    if attestation_error is not None:
        atomic_write(
            status_path,
            _status_payload(
                plan,
                state="recovered_unattested",
                launch_nonce=launch_nonce,
                launched_unix_ns=launched_unix_ns,
                supervisor_pid=os.getpid(),
                progress_artifacts=progress,
                compacted_path=str(compacted.path),
                compacted_sha256=compacted.sha256,
                compacted_rows=compacted.rows,
                provider_calls=0 if inspection.complete else None,
                manual_reconciliation_required=True,
                attestation_error=attestation_error,
                resource_released=claimed and not cleanup_errors,
            ),
        )
        return 2
    atomic_write(
        status_path,
        _status_payload(
            plan,
            state="completed",
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=os.getpid(),
            progress_artifacts=progress,
            compacted_path=str(compacted.path),
            compacted_sha256=compacted.sha256,
            compacted_rows=compacted.rows,
            resource_released=not claimed or not cleanup_errors,
            resource_claim_required=claimed,
            cache_complete_noop=inspection.complete and output_existed,
        ),
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "preflight",
        "verify-observed-pilot",
        "verify-pilot",
        "launch",
        "supervise",
    ):
        child = subparsers.add_parser(name)
        child.add_argument("--repo-root", type=Path, default=Path.cwd())
        child.add_argument(
            "--config",
            type=Path,
            default=Path("configs/sft2b/reform_diverse_full_consumer_v2.json"),
        )
        child.add_argument("--bundle-root", type=Path)
        child.add_argument("--shard", choices=SHARD_IDS, default=CORE_SHARD)
        child.add_argument("--run-root", type=Path)
        child.add_argument("--artifact-root", type=Path)
        child.add_argument("--pilot-input-root", type=Path)
        child.add_argument("--launch-nonce")
        child.add_argument("--launched-unix-ns", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    spec, config_sha256 = load_consumer_spec(config_path.resolve())
    run_root = (args.run_root or spec.runtime.run_root).resolve()
    if (
        spec.schema_version == CONFIG_SCHEMA_V2
        and args.command in {"launch", "supervise"}
        and run_root != spec.runtime.run_root.resolve()
    ):
        raise FullSourceConsumerError("CLI run root differs from frozen A100 /scratch root")
    if args.command == "verify-observed-pilot":
        if args.artifact_root is None or args.pilot_input_root is None:
            raise FullSourceConsumerError(
                "verify-observed-pilot requires --artifact-root and --pilot-input-root"
            )
        receipt = verify_observed_pilot(
            repo_root,
            spec,
            artifact_root=args.artifact_root,
            pilot_input_root=args.pilot_input_root,
        )
        print(receipt.model_dump_json())
        return 0
    if args.command == "verify-pilot":
        evidence = verify_matched_500_gate(repo_root, spec)
        print(
            json.dumps(
                {
                    "schema_version": "sft2b_matched_500_verified_evidence_binding_v1",
                    "run_id": evidence.run_id,
                    "sources": len(evidence.source_ids),
                    "requests": len(evidence.request_keys),
                    "failure_taxonomy": evidence.failure_taxonomy,
                    "evidence_binding_sha256": evidence.evidence_binding_sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "preflight":
        shard = next(item for item in spec.input.shards if item.shard_id == args.shard)
        enabled = (
            spec.authorization.core_enabled
            if args.shard == CORE_SHARD
            else spec.authorization.tail_enabled
        )
        run_id: str | None = None
        requests = shard.expected_rows * 4 if shard.expected_rows is not None else None
        sources = shard.expected_rows
        pilot_evidence_verified = False
        if spec.authorization.frozen:
            if args.bundle_root is None:
                raise FullSourceConsumerError("frozen preflight requires --bundle-root")
            evidence = verify_matched_500_gate(repo_root, spec)
            if evidence.evidence_binding_sha256 != spec.authorization.pilot_evidence_binding_sha256:
                raise FullSourceConsumerError(
                    "frozen authorization does not bind verified pilot evidence"
                )
            pilot_evidence_verified = True
            bundle_root = args.bundle_root.resolve()
            verified = verify_source_views(spec, bundle_root=bundle_root)
            plan = build_run_plan(
                spec,
                config_sha256=config_sha256,
                shard_id=args.shard,
                source_ids=verified.shard_source_ids[args.shard],
            )
            run_id = plan.run_id
            sources = len(plan.source_ids)
            requests = len(plan.cells)
        result = {
            "schema_version": "sft2b_full_source_preflight_v2",
            "status": spec.status,
            "run_id": run_id,
            "run_id_deferred": not spec.authorization.frozen,
            "shard_id": args.shard,
            "sources": sources,
            "requests": requests,
            "authorization_frozen": spec.authorization.frozen,
            "shard_authorized": enabled,
            "pilot_evidence_verified": pilot_evidence_verified,
            "launch_authorized": (
                spec.authorization.frozen and enabled and pilot_evidence_verified
            ),
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.bundle_root is None:
        raise FullSourceConsumerError(f"{args.command} requires --bundle-root")
    bundle_root = args.bundle_root.resolve()
    verified = verify_source_views(spec, bundle_root=bundle_root)
    plan = build_run_plan(
        spec,
        config_sha256=config_sha256,
        shard_id=args.shard,
        source_ids=verified.shard_source_ids[args.shard],
    )
    if args.command == "launch":
        health = launch_detached(
            repo_root,
            spec=spec,
            config_path=config_path.resolve(),
            bundle_root=bundle_root,
            plan=plan,
            run_root=run_root,
        )
        print(json.dumps(asdict(health), sort_keys=True))
        return 0
    if args.launch_nonce is None or args.launched_unix_ns is None:
        raise FullSourceConsumerError("supervise requires launcher nonce and timestamp")
    return supervise_shard(
        repo_root,
        spec=spec,
        config_path=config_path.resolve(),
        config_sha256=config_sha256,
        bundle_root=bundle_root,
        shard_id=args.shard,
        run_root=run_root,
        launch_nonce=args.launch_nonce,
        launched_unix_ns=args.launched_unix_ns,
    )


if __name__ == "__main__":
    raise SystemExit(main())

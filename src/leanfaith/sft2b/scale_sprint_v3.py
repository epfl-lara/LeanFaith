"""Authorized four-shard SFT2B corrected-core scale activation.

This additive driver leaves the hash-bound v1/v2 consumer and all historical
artifacts unchanged.  It verifies the mechanical source-v3 release, freezes
four contiguous views, and delegates each view's actual provider execution to
the hardened retry/reconciliation executor in :mod:`full_source_consumer`.

The first real shard carries the recovery exercise.  The orchestrator kills
only identity-verified same-run supervisor/vLLM processes after 64--128
durable terminals, relaunches the identical semantic run, proves progress past
the 512-terminal checkpoint, and then leaves that shard running.  Later shards
start sequentially only after completion, zero-call replay, private
publication, and fresh-download verification of their predecessor.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.host_resources import claim_resources, list_reservations, release_resources
from leanfaith.sft2b import full_source_consumer as hardened
from leanfaith.sft2b.durable import atomic_write, immutable_write
from leanfaith.sft2b.formalizer import FormalizerConfig
from leanfaith.sft2b.full_source_consumer import (
    CORE_SHARD,
    FULL_PROFILE_NAME,
    FullSourceConsumerSpec,
    FullSourceJournal,
    FullSourceRunPlan,
    WorkCell,
    load_consumer_spec,
    run_integrated_executor,
)
from leanfaith.sft2b.reform_32b import load_reform_32b_config
from leanfaith.sft2b.schemas import (
    CandidateSlot,
    FormalizerInvalidAttemptView,
    Sha256,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.vllm_backend import (
    LoadedVllmBackend,
    PortableReleaseConfig,
    VllmBackendSpec,
    VllmLaunchConfig,
    VllmProfile,
    VllmRequestTerminal,
    profile_endpoint,
)

SCHEMA_VERSION = "sft2b_reform_diverse_core_scale_sprint_v3"
VIEW_SCHEMA_VERSION = "sft2b_sprint_v3_shard_source_view_v1"
INTERNAL_SHARD_ID = CORE_SHARD
SHARD_IDS = ("core_00", "core_01", "core_02", "core_03")
EXPECTED_SLICES = ((0, 12500), (12500, 25000), (25000, 37500), (37500, 50000))
EXPECTED_SOURCE_FILES = {
    "SHA256SUMS",
    "frozen_active_meta_instruction_impact.json",
    "frozen_v2_library_docstring_corrections.jsonl",
    "frozen_v2_source_audit.jsonl",
    "frozen_v2_source_manifest.json",
    "frozen_v2_workbook_discourse_audit.jsonl",
    "legacy_tail_source_ids.json",
    "matched_50000_source_ids.json",
    "mechanical_conservative_receipt.json",
    "prompt_token_counts.json",
    "source_conservation_events.jsonl",
    "source_conservation_receipt.json",
    "source_manifest.json",
    "source_mechanical_evidence.jsonl",
    "source_mix.json",
    "source_quarantine.jsonl",
    "sources.jsonl",
}
OUTPUT_NAMES = {
    "SHA256SUMS",
    "candidates.jsonl",
    "completion.json",
    "formalizer_attempts.jsonl",
    "formalizer_invalid_attempts.jsonl",
    "generation_manifest.json",
    "raw_generations.jsonl",
    "recovery_receipt.json",
    "replay_receipt.json",
    "request_metrics.jsonl",
    "request_terminals.jsonl",
    "shard_source_ids.json",
}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_ID_RE = re.compile(r"^sft2b_source:[0-9a-f]{64}$")
_TMUX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


class ScaleSprintV3Error(RuntimeError):
    """A scale identity, recovery, launch, or publication contract failed."""


class AuthorizationSpec(StrictModel):
    authorized_at: Annotated[str, Field(min_length=1)]
    authorized_by: Literal["user"]
    baseline_git_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    core_enabled: Literal[True]
    tail_enabled: Literal[False]


class InputSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    path_prefix: Literal["source_inputs/reform_diverse_full_v3_mechanical_conservative_v1"]
    expected_source_rows: Literal[54144]
    expected_core_sources: Literal[50000]
    expected_tail_sources: Literal[4144]
    files: dict[str, Sha256]


class ShardSpec(StrictModel):
    shard_id: Literal["core_00", "core_01", "core_02", "core_03"]
    start: Annotated[int, Field(ge=0)]
    stop: Annotated[int, Field(gt=0)]
    expected_sources: Literal[12500]
    source_ids_sha256: Sha256
    artifact_sha256: Sha256


class ModelSpec(StrictModel):
    model_id: Literal["GuoxinChen/ReForm-32B"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    placement_config_path: str
    placement_config_sha256: Sha256
    prompt_path: str
    prompt_sha256: Sha256
    tokenizer_sha256: Sha256
    snapshot_binding_sha256: Sha256
    served_model_name: str
    checkpoint_dtype: Literal["bfloat16"]
    quantization: None
    trust_remote_code: Literal[False]
    visible_devices: tuple[int, ...]
    data_parallel_size: Literal[4]
    tensor_parallel_size: Literal[2]
    concurrency: Literal[64]
    max_model_len: Literal[5063]
    max_num_seqs: Literal[16]
    gpu_memory_utilization: Annotated[float, Field(gt=0.0, le=1.0)]
    prefix_caching: Literal[False]
    port: Annotated[int, Field(ge=1, le=65535)]


class EvidenceSpec(StrictModel):
    matched_generation_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    matched_generation_path_prefix: str
    matched_generation_terminal_count: Literal[2000]
    matched_generation_manifest_sha256: Sha256
    lean_audit_manifest_sha256: Sha256
    lean_audit_gate_passed: Literal[True]
    judge_manifest_sha256: Sha256
    judge_gate_passed: Literal[True]
    waived_historical_receipts_resurrected: Literal[False]


class RecoverySpec(StrictModel):
    kill_min_terminals: Annotated[int, Field(ge=64, le=128)]
    kill_max_terminals: Annotated[int, Field(ge=64, le=128)]
    checkpoint_terminals: Literal[512]
    minimum_post_recovery_terminals: Annotated[int, Field(gt=512)]
    minimum_requests_per_second: Annotated[float, Field(ge=2.0)]
    required_abandoned_attempts: Annotated[int, Field(ge=1)]


class RuntimeSpec(StrictModel):
    base_executor_config_path: str
    base_executor_config_sha256: Sha256
    scratch_root: Path
    input_cache_root: Path
    model_cache_root: Path
    model_snapshot_path: Path
    cache_root: Path
    run_root: Path
    reservation_root: Path
    reservation_task: Literal["SFT2B"]
    orchestrator_session_prefix: str
    shard_session_prefix: str
    minimum_free_scratch_bytes: Annotated[int, Field(ge=500_000_000_000)]
    minimum_gpu_memory_mib: Annotated[int, Field(ge=79000)]
    vllm_version: Literal["0.12.0"]


class PublicationSpec(StrictModel):
    repo_id: Literal["Lemmy00/leanfaith-sft2-autoformalizer-v1"]
    repo_type: Literal["dataset"]
    private_required: Literal[True]
    path_prefix: str
    commit_message_prefix: str


class DownstreamSpec(StrictModel):
    start_after_each_publication: Literal[True]
    lean_max_workers: Literal[2]
    lean_max_host_rss_gib: Annotated[float, Field(ge=40.0, le=40.0)]
    invalid_candidates_are_semantic_negatives: Literal[False]
    judge_config_path: str
    judge_config_sha256: Sha256
    labeling_policy_path: str
    labeling_policy_sha256: Sha256


class ScaleSprintV3Spec(StrictModel):
    schema_version: Literal["sft2b_reform_diverse_core_scale_sprint_v3"]
    authorization: AuthorizationSpec
    input: InputSpec
    shards: tuple[ShardSpec, ShardSpec, ShardSpec, ShardSpec]
    model: ModelSpec
    evidence: EvidenceSpec
    recovery: RecoverySpec
    runtime: RuntimeSpec
    publication: PublicationSpec
    downstream: DownstreamSpec
    code_pins: dict[str, Sha256 | None]

    @model_validator(mode="after")
    def validate_contract(self) -> ScaleSprintV3Spec:
        if set(self.input.files) != EXPECTED_SOURCE_FILES:
            raise ValueError("source release file-pin set is not exact")
        if tuple(item.shard_id for item in self.shards) != SHARD_IDS:
            raise ValueError("four scale shards are not in exact order")
        if tuple((item.start, item.stop) for item in self.shards) != EXPECTED_SLICES:
            raise ValueError("scale shard slices drifted")
        if self.model.visible_devices != tuple(range(8)):
            raise ValueError("scale must expose exactly GPU indices 0 through 7")
        if len(self.model.visible_devices) != (
            self.model.data_parallel_size * self.model.tensor_parallel_size
        ):
            raise ValueError("visible GPU count differs from DP*TP")
        scratch = self.runtime.scratch_root
        if str(scratch) != "/scratch/milikic/data/leanfaith":
            raise ValueError("scale scratch root drifted")
        for path in (
            self.runtime.input_cache_root,
            self.runtime.model_cache_root,
            self.runtime.model_snapshot_path,
            self.runtime.cache_root,
            self.runtime.run_root,
            self.runtime.reservation_root,
        ):
            if not path.is_relative_to(scratch):
                raise ValueError("scale path escapes the frozen scratch root")
        if self.recovery.kill_min_terminals > self.recovery.kill_max_terminals:
            raise ValueError("recovery kill range is inverted")
        if self.recovery.minimum_post_recovery_terminals <= self.recovery.checkpoint_terminals:
            raise ValueError("recovery proof must cross the durable checkpoint")
        if any("receipt" in key for key in self.evidence.model_fields_set):
            allowed = {"waived_historical_receipts_resurrected"}
            observed = {key for key in self.evidence.model_fields_set if "receipt" in key}
            if observed != allowed:
                raise ValueError("waived historical receipt requirements were resurrected")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedShardView:
    spec: ShardSpec
    source_ids: tuple[str, ...]
    payload: bytes


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    root: Path
    rows: tuple[SourceRecord, ...]
    prompt_tokens: dict[str, int]
    core_ids: tuple[str, ...]
    tail_ids: tuple[str, ...]
    views: tuple[VerifiedShardView, ...]


@dataclass(frozen=True, slots=True)
class GpuRecord:
    index: int
    name: str
    uuid: str
    memory_total_mib: int
    memory_used_mib: int


@dataclass(frozen=True, slots=True)
class HostPreflight:
    gpu_inventory: tuple[GpuRecord, ...]
    runtime_versions: dict[str, str]
    scratch_free_bytes: int
    git_revision: str
    model_snapshot_path: str
    port_closed: bool
    no_duplicate_sessions: bool
    no_duplicate_processes: bool
    no_resource_claims: bool


@dataclass(frozen=True, slots=True)
class ShardRuntime:
    shard: VerifiedShardView
    plan: FullSourceRunPlan
    sources: tuple[SourceRecord, ...]
    backend: LoadedVllmBackend
    executor_spec: FullSourceConsumerSpec
    shard_root: Path
    session_name: str
    log_path: Path


@dataclass(frozen=True, slots=True)
class OutputBundle:
    root: Path
    run_id: str
    hashes: dict[str, str]


def _object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ScaleSprintV3Error(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScaleSprintV3Error(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ScaleSprintV3Error(f"blank JSONL line at {path}:{number}")
            value: object = json.loads(line)
            if not isinstance(value, dict):
                raise ScaleSprintV3Error(f"non-object JSONL row at {path}:{number}")
            rows.append(cast(dict[str, Any], value))
    return tuple(rows)


def _read_sources(path: Path) -> tuple[SourceRecord, ...]:
    rows: list[SourceRecord] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ScaleSprintV3Error(f"blank source row at {path}:{number}")
            try:
                rows.append(SourceRecord.model_validate_json(line))
            except Exception as exc:
                raise ScaleSprintV3Error(f"invalid SourceRecord at {path}:{number}: {exc}") from exc
    return tuple(rows)


def _read_id_view(path: Path, *, expected: int) -> tuple[str, ...]:
    payload = _object(path)
    raw = payload.get("source_ids")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ScaleSprintV3Error(f"invalid source ID view: {path}")
    source_ids = tuple(cast(list[str], raw))
    if (
        payload.get("source_count") != expected
        or len(source_ids) != expected
        or len(set(source_ids)) != expected
        or any(_SOURCE_ID_RE.fullmatch(item) is None for item in source_ids)
    ):
        raise ScaleSprintV3Error(f"source ID view count/identity drifted: {path}")
    return source_ids


def load_spec(repo_root: Path, config_path: Path) -> tuple[ScaleSprintV3Spec, str]:
    try:
        spec = ScaleSprintV3Spec.model_validate(_object(config_path))
    except Exception as exc:
        raise ScaleSprintV3Error(f"invalid sprint-v3 config: {exc}") from exc
    for relative, expected in spec.code_pins.items():
        if expected is None:
            raise ScaleSprintV3Error(f"sprint-v3 code pin is not frozen: {relative}")
        path = repo_root / relative
        if not path.is_file() or path.is_symlink() or hash_file(path) != expected:
            raise ScaleSprintV3Error(f"sprint-v3 code pin drifted: {relative}")
    for relative, expected, label in (
        (
            spec.runtime.base_executor_config_path,
            spec.runtime.base_executor_config_sha256,
            "base executor config",
        ),
        (spec.model.placement_config_path, spec.model.placement_config_sha256, "placement"),
        (spec.model.prompt_path, spec.model.prompt_sha256, "prompt"),
        (
            spec.downstream.judge_config_path,
            spec.downstream.judge_config_sha256,
            "judge defaults",
        ),
        (
            spec.downstream.labeling_policy_path,
            spec.downstream.labeling_policy_sha256,
            "labeling defaults",
        ),
    ):
        path = repo_root / relative
        if not path.is_file() or hash_file(path) != expected:
            raise ScaleSprintV3Error(f"{label} pin drifted")
    return spec, hash_file(config_path)


def _view_payload(shard: ShardSpec, core_ids: tuple[str, ...], core_sha256: str) -> bytes:
    source_ids = core_ids[shard.start : shard.stop]
    return (
        canonical_json_bytes(
            {
                "schema_version": VIEW_SCHEMA_VERSION,
                "shard_id": shard.shard_id,
                "slice": {"start": shard.start, "stop": shard.stop},
                "source_count": len(source_ids),
                "source_ids": source_ids,
                "core_view_sha256": core_sha256,
            }
        )
        + b"\n"
    )


def verify_source_bundle(
    spec: ScaleSprintV3Spec,
    *,
    bundle_root: Path,
    view_output_root: Path | None = None,
) -> VerifiedBundle:
    observed = {item.name for item in bundle_root.iterdir() if item.is_file()}
    if observed != set(spec.input.files):
        raise ScaleSprintV3Error("source bundle file set differs from all frozen ledger pins")
    for name, expected in spec.input.files.items():
        path = bundle_root / name
        if not path.is_file() or hash_file(path) != expected:
            raise ScaleSprintV3Error(f"source bundle hash mismatch: {name}")
    ledger: dict[str, str] = {}
    for line in (bundle_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise ScaleSprintV3Error("source SHA256SUMS is malformed")
        if parts[1] in ledger:
            raise ScaleSprintV3Error("source SHA256SUMS repeats a path")
        ledger[parts[1]] = parts[0]
    if set(ledger) != EXPECTED_SOURCE_FILES.difference({"SHA256SUMS"}):
        raise ScaleSprintV3Error("source SHA256SUMS does not cover every release file")
    if any(spec.input.files[name] != digest for name, digest in ledger.items()):
        raise ScaleSprintV3Error("source SHA256SUMS disagrees with config pins")

    rows = _read_sources(bundle_root / "sources.jsonl")
    source_ids = tuple(item.source_id for item in rows)
    if len(rows) != spec.input.expected_source_rows or len(set(source_ids)) != len(rows):
        raise ScaleSprintV3Error("source release is not exactly 54,144 unique rows")
    core_ids = _read_id_view(
        bundle_root / "matched_50000_source_ids.json", expected=spec.input.expected_core_sources
    )
    tail_ids = _read_id_view(
        bundle_root / "legacy_tail_source_ids.json", expected=spec.input.expected_tail_sources
    )
    if set(core_ids) & set(tail_ids) or core_ids + tail_ids != source_ids:
        raise ScaleSprintV3Error("core/tail views do not disjointly concatenate to sources")

    token_payload = _object(bundle_root / "prompt_token_counts.json")
    raw_token_rows = token_payload.get("rows")
    if not isinstance(raw_token_rows, list) or len(raw_token_rows) != len(rows):
        raise ScaleSprintV3Error("prompt-token rows do not cover all source rows")
    prompt_tokens: dict[str, int] = {}
    ordered_token_ids: list[str] = []
    for raw in raw_token_rows:
        if not isinstance(raw, dict):
            raise ScaleSprintV3Error("prompt-token row is not an object")
        source_id = raw.get("source_id")
        count = raw.get("prompt_tokens")
        if not isinstance(source_id, str) or not isinstance(count, int) or count < 1:
            raise ScaleSprintV3Error("prompt-token row is invalid")
        ordered_token_ids.append(source_id)
        prompt_tokens[source_id] = count
    if tuple(ordered_token_ids) != source_ids or len(prompt_tokens) != len(source_ids):
        raise ScaleSprintV3Error("prompt-token source ordering drifted")
    if (
        max(prompt_tokens.values()) != 967
        or token_payload.get("required_max_model_len") != spec.model.max_model_len
        or token_payload.get("model_id") != spec.model.model_id
        or token_payload.get("model_revision") != spec.model.revision
        or token_payload.get("prompt_sha256") != spec.model.prompt_sha256
        or token_payload.get("tokenizer_sha256") != spec.model.tokenizer_sha256
    ):
        raise ScaleSprintV3Error("prompt/model/tokenizer binding drifted")
    manifest = _object(bundle_root / "source_manifest.json")
    if (
        manifest.get("schema_version")
        != "sft2b_diverse_full_source_manifest_v3_mechanical_conservative_v1"
        or manifest.get("release_mode") != "mechanical_conservative_v1"
        or manifest.get("source_count") != spec.input.expected_source_rows
        or manifest.get("core_count") != spec.input.expected_core_sources
        or manifest.get("tail_count") != spec.input.expected_tail_sources
        or manifest.get("quarantine_count") != 762
    ):
        raise ScaleSprintV3Error("mechanical source-v3 manifest contract drifted")

    views: list[VerifiedShardView] = []
    concatenated: list[str] = []
    core_hash = spec.input.files["matched_50000_source_ids.json"]
    for shard in spec.shards:
        ids = core_ids[shard.start : shard.stop]
        payload = _view_payload(shard, core_ids, core_hash)
        if (
            len(ids) != shard.expected_sources
            or hash_canonical(ids) != shard.source_ids_sha256
            or hashlib.sha256(payload).hexdigest() != shard.artifact_sha256
        ):
            raise ScaleSprintV3Error(f"frozen shard view drifted: {shard.shard_id}")
        if view_output_root is not None:
            immutable_write(view_output_root / f"{shard.shard_id}_source_ids.json", payload)
        views.append(VerifiedShardView(spec=shard, source_ids=ids, payload=payload))
        concatenated.extend(ids)
    if tuple(concatenated) != core_ids or len(set(concatenated)) != len(core_ids):
        raise ScaleSprintV3Error("four shard views do not exactly and disjointly rebuild core")
    return VerifiedBundle(
        root=bundle_root,
        rows=rows,
        prompt_tokens=prompt_tokens,
        core_ids=core_ids,
        tail_ids=tail_ids,
        views=tuple(views),
    )


def verify_accepted_evidence(
    spec: ScaleSprintV3Spec,
    *,
    lean_manifest_path: Path | None = None,
    judge_manifest_path: Path | None = None,
) -> dict[str, object]:
    """Bind accepted sprint gates without resurrecting waived pilot receipts."""

    if lean_manifest_path is not None:
        lean = _object(lean_manifest_path)
        if (
            hash_file(lean_manifest_path) != spec.evidence.lean_audit_manifest_sha256
            or lean.get("gate_passed") is not True
            or cast(dict[str, object], lean.get("counts", {})).get("valid_references") != 500
            or cast(dict[str, object], lean.get("counts", {})).get("valid_candidates") != 865
        ):
            raise ScaleSprintV3Error("accepted Lean audit manifest drifted")
    if judge_manifest_path is not None:
        judge = _object(judge_manifest_path)
        if (
            hash_file(judge_manifest_path) != spec.evidence.judge_manifest_sha256
            or judge.get("gate_passed") is not True
            or cast(dict[str, object], judge.get("counts", {})).get("votes") != 300
            or cast(dict[str, object], judge.get("counts", {})).get("unknown") != 0
        ):
            raise ScaleSprintV3Error("accepted judge manifest drifted")
    return {
        "matched_generation_revision": spec.evidence.matched_generation_revision,
        "matched_generation_manifest_sha256": (spec.evidence.matched_generation_manifest_sha256),
        "lean_audit_manifest_sha256": spec.evidence.lean_audit_manifest_sha256,
        "judge_manifest_sha256": spec.evidence.judge_manifest_sha256,
        "waived_receipts_required": False,
    }


def _build_plan(
    *,
    spec: ScaleSprintV3Spec,
    config_sha256: str,
    shard: VerifiedShardView,
) -> FullSourceRunPlan:
    input_binding = hash_canonical(
        {
            "schema_version": "sft2b_sprint_v3_input_binding_v1",
            "repo_id": spec.input.repo_id,
            "revision": spec.input.revision,
            "path_prefix": spec.input.path_prefix,
            "files": spec.input.files,
            "core_view_sha256": spec.input.files["matched_50000_source_ids.json"],
            "shard_id": shard.spec.shard_id,
            "shard_artifact_sha256": shard.spec.artifact_sha256,
        }
    )
    run_id = stable_id(
        "sft2b_full_reform_run",
        {
            "schema_version": "sft2b_full_reform_run_identity_sprint_v3",
            "input_binding_sha256": input_binding,
            "shard_id": shard.spec.shard_id,
            "source_ids_sha256": shard.spec.source_ids_sha256,
            "model_id": spec.model.model_id,
            "model_revision": spec.model.revision,
            "snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
            "prompt_sha256": spec.model.prompt_sha256,
            "slots": [item.value for item in CandidateSlot],
            "seeds": [0, 1, 2, 3],
        },
    )
    cells: list[WorkCell] = []
    for source_id in shard.source_ids:
        for seed, slot in enumerate(CandidateSlot):
            cell_id = stable_id(
                "sft2b_full_reform_cell",
                {
                    "run_id": run_id,
                    "scale_shard_id": shard.spec.shard_id,
                    "source_id": source_id,
                    "slot": slot,
                    "seed": seed,
                },
            )
            cells.append(
                WorkCell(
                    ordinal=len(cells),
                    shard_id=INTERNAL_SHARD_ID,
                    source_id=source_id,
                    slot=slot,
                    seed=seed,
                    cell_id=cell_id,
                )
            )
    if len(cells) != 50000 or len({item.cell_id for item in cells}) != 50000:
        raise ScaleSprintV3Error("shard plan is not the exact 12,500-by-four product")
    # The config hash is provenance and deliberately absent from run/request identity.
    if re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None:
        raise ScaleSprintV3Error("scale config hash is invalid")
    return FullSourceRunPlan(
        run_id=run_id,
        shard_id=INTERNAL_SHARD_ID,
        source_ids=shard.source_ids,
        cells=tuple(cells),
        input_binding_sha256=input_binding,
    )


def _executor_spec(
    repo_root: Path,
    spec: ScaleSprintV3Spec,
) -> FullSourceConsumerSpec:
    path = repo_root / spec.runtime.base_executor_config_path
    base, _ = load_consumer_spec(path)
    return base.model_copy(
        update={
            "executor": base.executor.model_copy(
                update={
                    "visible_devices": spec.model.visible_devices,
                    "data_parallel_size": spec.model.data_parallel_size,
                    "tensor_parallel_size": spec.model.tensor_parallel_size,
                    "port": spec.model.port,
                    "max_model_len": spec.model.max_model_len,
                    "max_num_seqs": spec.model.max_num_seqs,
                    "gpu_memory_utilization": spec.model.gpu_memory_utilization,
                    "prefix_caching": spec.model.prefix_caching,
                    "concurrency": spec.model.concurrency,
                }
            ),
            "runtime": base.runtime.model_copy(
                update={
                    "cache_root": spec.runtime.cache_root,
                    "run_root": spec.runtime.run_root,
                    "reservation_root": spec.runtime.reservation_root,
                    "owner_session": "SFT2B sprint-v3 corrected-core generation",
                    "scratch_root": spec.runtime.scratch_root,
                    "model_snapshot_path": spec.runtime.model_snapshot_path,
                }
            ),
            "model": base.model.model_copy(
                update={
                    "revision": spec.model.revision,
                    "snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
                    "served_model_name": spec.model.served_model_name,
                }
            ),
        }
    )


def _build_backend(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    config_sha256: str,
    bundle: VerifiedBundle,
    shard: VerifiedShardView,
    plan: FullSourceRunPlan,
) -> tuple[LoadedVllmBackend, tuple[SourceRecord, ...], FormalizerConfig]:
    placement, _ = load_reform_32b_config(
        repo_root,
        placement_path=repo_root / spec.model.placement_config_path,
        snapshot_path=spec.runtime.model_snapshot_path,
    )
    if (
        placement.model_id != spec.model.model_id
        or placement.model_revision != spec.model.revision
        or placement.snapshot_binding_sha256 != spec.model.snapshot_binding_sha256
        or placement.prompt_sha256 != spec.model.prompt_sha256
        or placement.dtype != "bfloat16"
        or placement.trust_remote_code
    ):
        raise ScaleSprintV3Error("model snapshot/placement identity drifted")
    by_id = {item.source_id: item for item in bundle.rows}
    sources = tuple(by_id[source_id] for source_id in shard.source_ids)
    selected_tokens = {source_id: bundle.prompt_tokens[source_id] for source_id in shard.source_ids}
    if max(selected_tokens.values()) + 4096 > spec.model.max_model_len:
        raise ScaleSprintV3Error("shard exceeds frozen max_model_len")
    profile_id = (
        f"sft2b_reform_32b_{shard.spec.shard_id}_dp4_tp2_{plan.run_id.split(':', 1)[1][:16]}"
    )
    full_profile = VllmProfile(
        profile_id=profile_id,
        visible_devices=spec.model.visible_devices,
        data_parallel_size=spec.model.data_parallel_size,
        tensor_parallel_size=spec.model.tensor_parallel_size,
        port=spec.model.port,
        max_model_len=spec.model.max_model_len,
        max_num_seqs=spec.model.max_num_seqs,
        gpu_memory_utilization=spec.model.gpu_memory_utilization,
        prefix_caching=spec.model.prefix_caching,
        concurrency=spec.model.concurrency,
        source_ids=shard.source_ids,
        slots=tuple(CandidateSlot),
    )
    first_id = shard.source_ids[0]
    smoke_profile = VllmProfile(
        profile_id=f"{profile_id}_structural_smoke",
        visible_devices=(0, 1),
        data_parallel_size=1,
        tensor_parallel_size=2,
        port=spec.model.port + 1,
        max_model_len=selected_tokens[first_id] + 4096,
        max_num_seqs=1,
        gpu_memory_utilization=spec.model.gpu_memory_utilization,
        prefix_caching=False,
        concurrency=1,
        source_ids=(first_id,),
        slots=(CandidateSlot.SLOT_0,),
    )
    portable = PortableReleaseConfig(
        repo_id=spec.input.repo_id,
        revision=spec.input.revision,
        release_id=f"sft2b_full_source:{spec.input.revision}",
        release_manifest_path="source_manifest.json",
        release_manifest_sha256=spec.input.files["source_manifest.json"],
        smoke_sources_path="sources.jsonl",
        smoke_sources_sha256=spec.input.files["sources.jsonl"],
        probe_sources_path="sources.jsonl",
        probe_sources_sha256=spec.input.files["sources.jsonl"],
    )
    # The frozen backend schema required the *smallest* per-profile length for
    # earlier probes.  Scale authorization deliberately pins one uniform 5,063
    # bound across all four views, so construct the otherwise fully validated
    # field set without re-applying that superseded cross-profile equality.
    backend_spec = VllmBackendSpec.model_construct(
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
        request_timeout_seconds=900,
        telemetry_interval_seconds=0.5,
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
        owner_session=f"SFT2B sprint-v3 {shard.spec.shard_id}",
    )
    backend = LoadedVllmBackend(
        spec=backend_spec,
        config_path=config_path,
        config_sha256=config_sha256,
        placement=placement,
        release_root=bundle.root,
    )
    return backend, sources, placement


def prepare_shard_runtime(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    config_sha256: str,
    bundle: VerifiedBundle,
    shard_id: str,
) -> ShardRuntime:
    shard = next((item for item in bundle.views if item.spec.shard_id == shard_id), None)
    if shard is None:
        raise ScaleSprintV3Error(f"unknown sprint shard: {shard_id}")
    plan = _build_plan(spec=spec, config_sha256=config_sha256, shard=shard)
    backend, sources, _ = _build_backend(
        repo_root,
        spec=spec,
        config_path=config_path,
        config_sha256=config_sha256,
        bundle=bundle,
        shard=shard,
        plan=plan,
    )
    suffix = plan.run_id.split(":", 1)[1][:12]
    session_name = f"{spec.runtime.shard_session_prefix}-{shard_id}-{suffix}"
    if _TMUX_RE.fullmatch(session_name) is None:
        raise ScaleSprintV3Error("deterministic shard tmux name is invalid")
    # The historical executor's validated terminal schemas retain their single
    # corrected-core lane.  Independent sprint shards are separated by their
    # distinct semantic run IDs and mapped back to core_00..03 in sprint manifests.
    shard_root = spec.runtime.run_root / INTERNAL_SHARD_ID / plan.run_id
    return ShardRuntime(
        shard=shard,
        plan=plan,
        sources=sources,
        backend=backend,
        executor_spec=_executor_spec(repo_root, spec),
        shard_root=shard_root,
        session_name=session_name,
        log_path=shard_root / "supervisor.log",
    )


def _run_checked(argv: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(tuple(argv), cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ScaleSprintV3Error(f"command failed ({shlex.join(argv)}): {detail}")
    return completed.stdout.strip()


def _gpu_inventory(spec: ScaleSprintV3Spec) -> tuple[GpuRecord, ...]:
    output = _run_checked(
        (
            "nvidia-smi",
            "--query-gpu=index,name,uuid,memory.total,memory.used",
            "--format=csv,noheader,nounits",
        )
    )
    records: list[GpuRecord] = []
    for line in output.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 5:
            raise ScaleSprintV3Error("nvidia-smi returned an unexpected schema")
        records.append(
            GpuRecord(
                index=int(fields[0]),
                name=fields[1],
                uuid=fields[2],
                memory_total_mib=int(fields[3]),
                memory_used_mib=int(fields[4]),
            )
        )
    if tuple(item.index for item in records) != spec.model.visible_devices:
        raise ScaleSprintV3Error("target does not expose exactly eight GPU indices 0 through 7")
    for item in records:
        if not (spec.runtime.minimum_gpu_memory_mib <= item.memory_total_mib <= 85000):
            raise ScaleSprintV3Error(
                f"GPU {item.index} is not an 80GB-class allocation: {item.memory_total_mib} MiB"
            )
        normalized = item.name.casefold()
        if "a100" not in normalized and "h100" not in normalized:
            raise ScaleSprintV3Error(f"GPU {item.index} is not a supported A100/H100: {item.name}")
        if item.memory_used_mib > 2048:
            raise ScaleSprintV3Error(
                f"GPU {item.index} is not idle enough for exclusive scale: "
                f"{item.memory_used_mib} MiB"
            )
    return tuple(records)


def _runtime_versions(spec: ScaleSprintV3Spec) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("vllm", "torch", "transformers", "huggingface-hub"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError as exc:
            raise ScaleSprintV3Error(f"required runtime package is missing: {package}") from exc
    try:
        versions["flash-attn"] = metadata.version("flash-attn")
    except metadata.PackageNotFoundError:
        versions["flash-attn"] = "not-installed"
    if versions["vllm"] != spec.runtime.vllm_version:
        raise ScaleSprintV3Error(
            f"vLLM version drifted: expected {spec.runtime.vllm_version}, "
            f"observed {versions['vllm']}"
        )
    return versions


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _tmux_sessions() -> tuple[str, ...]:
    completed = subprocess.run(
        ("tmux", "list-sessions", "-F", "#{session_name}"),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).casefold()
        if "no server running" in detail or "no sessions" in detail:
            return ()
        raise ScaleSprintV3Error(f"tmux inventory failed: {completed.stderr.strip()}")
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def _matching_processes(repo_root: Path) -> tuple[int, ...]:
    matches: list[int] = []
    own_pid = os.getpid()
    for item in Path("/proc").iterdir():
        if not item.name.isdigit() or int(item.name) == own_pid:
            continue
        try:
            command = (
                (item / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
        except OSError:
            continue
        if "leanfaith.sft2b.scale_sprint_v3" in command and str(repo_root.resolve()) in command:
            matches.append(int(item.name))
    return tuple(matches)


def _verify_git_pushed(repo_root: Path, *, baseline: str) -> str:
    head = _run_checked(("git", "rev-parse", "HEAD"), cwd=repo_root)
    if _run_checked(("git", "status", "--porcelain"), cwd=repo_root):
        raise ScaleSprintV3Error("scale launch requires a clean committed worktree")
    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", baseline, head),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise ScaleSprintV3Error("activation HEAD does not descend from the authorized baseline")
    branch = _run_checked(("git", "branch", "--show-current"), cwd=repo_root)
    if not branch.startswith("milikic/"):
        raise ScaleSprintV3Error("activation branch is not a pushed milikic/* branch")
    remote = _run_checked(("git", "ls-remote", "origin", f"refs/heads/{branch}"), cwd=repo_root)
    fields = remote.split()
    if len(fields) != 2 or fields[0] != head:
        raise ScaleSprintV3Error("activation HEAD is not yet pushed to its exact remote branch")
    return head


def verify_target_host(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    require_model: bool,
) -> HostPreflight:
    """Fail before launch unless the target is the exact exclusive 8x80GB environment."""

    gpu_records = _gpu_inventory(spec)
    scratch = spec.runtime.scratch_root
    if not scratch.is_dir():
        raise ScaleSprintV3Error("frozen /scratch root is unavailable")
    free = shutil.disk_usage(scratch).free
    if free < spec.runtime.minimum_free_scratch_bytes:
        raise ScaleSprintV3Error(
            f"insufficient scratch: {free} bytes free, "
            f"need {spec.runtime.minimum_free_scratch_bytes}"
        )
    sessions = _tmux_sessions()
    duplicate_sessions = tuple(
        item
        for item in sessions
        if item.startswith(spec.runtime.orchestrator_session_prefix)
        or item.startswith(spec.runtime.shard_session_prefix)
    )
    if duplicate_sessions:
        raise ScaleSprintV3Error(
            f"duplicate scale tmux session(s) exist: {', '.join(duplicate_sessions)}"
        )
    duplicate_processes = _matching_processes(repo_root)
    if duplicate_processes:
        raise ScaleSprintV3Error(f"duplicate scale process(es) exist: {list(duplicate_processes)}")
    reservations = list_reservations(spec.runtime.reservation_root)
    if reservations:
        raise ScaleSprintV3Error(
            "target host already has a resource claim; refusing concurrent eight-GPU work"
        )
    if _port_is_open(spec.model.port):
        raise ScaleSprintV3Error(f"vLLM port {spec.model.port} is already open")
    git_revision = _verify_git_pushed(repo_root, baseline=spec.authorization.baseline_git_revision)
    versions = _runtime_versions(spec)
    if require_model:
        placement, _ = load_reform_32b_config(
            repo_root,
            placement_path=repo_root / spec.model.placement_config_path,
            snapshot_path=spec.runtime.model_snapshot_path,
        )
        if placement.snapshot_binding_sha256 != spec.model.snapshot_binding_sha256:
            raise ScaleSprintV3Error("exact ReForm-32B snapshot binding drifted")
    return HostPreflight(
        gpu_inventory=gpu_records,
        runtime_versions=versions,
        scratch_free_bytes=free,
        git_revision=git_revision,
        model_snapshot_path=str(spec.runtime.model_snapshot_path),
        port_closed=True,
        no_duplicate_sessions=True,
        no_duplicate_processes=True,
        no_resource_claims=True,
    )


def _snapshot_download(
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
        raise ScaleSprintV3Error(f"Hub snapshot resolved to {result.name}, expected {revision}")
    return result


def stage_inputs_and_model(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
) -> tuple[Path, Path]:
    """Download exact private input/model snapshots after allocation-only preflight."""

    from huggingface_hub import HfApi

    info = HfApi().repo_info(repo_id=spec.input.repo_id, repo_type=spec.input.repo_type)
    if spec.input.private_required and not bool(info.private):
        raise ScaleSprintV3Error("source repository is no longer private")
    evidence_snapshot = _snapshot_download(
        repo_id=spec.input.repo_id,
        repo_type=spec.input.repo_type,
        revision=spec.evidence.matched_generation_revision,
        cache_dir=spec.runtime.input_cache_root / "accepted_matched_generation",
        allow_patterns=[f"{spec.evidence.matched_generation_path_prefix}/generation_manifest.json"],
    )
    matched_manifest = (
        evidence_snapshot
        / spec.evidence.matched_generation_path_prefix
        / "generation_manifest.json"
    )
    matched_payload = _object(matched_manifest)
    if (
        hash_file(matched_manifest) != spec.evidence.matched_generation_manifest_sha256
        or cast(dict[str, object], matched_payload.get("counts", {})).get("requests")
        != spec.evidence.matched_generation_terminal_count
    ):
        raise ScaleSprintV3Error("accepted matched-generation publication drifted")
    dataset_snapshot = _snapshot_download(
        repo_id=spec.input.repo_id,
        repo_type=spec.input.repo_type,
        revision=spec.input.revision,
        cache_dir=spec.runtime.input_cache_root,
        allow_patterns=[f"{spec.input.path_prefix}/*"],
    )
    bundle_root = dataset_snapshot / spec.input.path_prefix
    verify_source_bundle(
        spec,
        bundle_root=bundle_root,
        view_output_root=spec.runtime.run_root / "activation/views",
    )
    model_snapshot = _snapshot_download(
        repo_id=spec.model.model_id,
        repo_type="model",
        revision=spec.model.revision,
        cache_dir=spec.runtime.model_cache_root,
    )
    if model_snapshot.resolve() != spec.runtime.model_snapshot_path.resolve():
        raise ScaleSprintV3Error("downloaded model snapshot path differs from frozen target path")
    placement, _ = load_reform_32b_config(
        repo_root,
        placement_path=repo_root / spec.model.placement_config_path,
        snapshot_path=model_snapshot,
    )
    if placement.snapshot_binding_sha256 != spec.model.snapshot_binding_sha256:
        raise ScaleSprintV3Error("downloaded model snapshot binding drifted")
    immutable_write(
        spec.runtime.run_root / "activation/staging_receipt.json",
        canonical_json_bytes(
            {
                "schema_version": "sft2b_sprint_v3_staging_receipt_v1",
                "config_sha256": hash_file(config_path),
                "input_revision": spec.input.revision,
                "input_path_prefix": spec.input.path_prefix,
                "input_ledger_sha256": spec.input.files["SHA256SUMS"],
                "matched_generation_revision": (spec.evidence.matched_generation_revision),
                "matched_generation_manifest_sha256": (
                    spec.evidence.matched_generation_manifest_sha256
                ),
                "model_revision": spec.model.revision,
                "model_snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
                "bundle_root": str(bundle_root),
                "model_snapshot_path": str(model_snapshot),
            }
        )
        + b"\n",
    )
    return bundle_root, model_snapshot


def _append_event(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("ab") as handle:
                handle.write(canonical_json_bytes(dict(payload)) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _pid_command(pid: int) -> str:
    try:
        return (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", errors="replace")
        )
    except OSError:
        return ""


def _process_descends_from(child_pid: int, ancestor_pid: int) -> bool:
    current = child_pid
    seen: set[int] = set()
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text(encoding="utf-8")
        except OSError:
            return False
        parents = [line for line in status.splitlines() if line.startswith("PPid:")]
        if len(parents) != 1 or not parents[0].split(":", 1)[1].strip().isdigit():
            return False
        current = int(parents[0].split(":", 1)[1].strip())
    return False


def _claim_path(runtime: ShardRuntime, claim_id: str) -> Path:
    return runtime.shard_root / "resource_sessions" / claim_id / "claim.json"


def _reconcile_stale_claim(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
) -> bool:
    reservations = list_reservations(spec.runtime.reservation_root)
    if not reservations:
        return False
    if len(reservations) != 1:
        raise ScaleSprintV3Error("multiple resource claims exist; refusing stale cleanup")
    prior = reservations[0]
    if (
        prior.task != spec.runtime.reservation_task
        or prior.hostname != socket.gethostname()
        or Path(prior.worktree).resolve() != repo_root.resolve()
        or runtime.plan.run_id not in prior.owner_session
        or Path(f"/proc/{prior.pid}").exists()
        or _port_is_open(spec.model.port)
    ):
        raise ScaleSprintV3Error("resource claim is live or foreign; refusing stale cleanup")
    released = release_resources(
        root=spec.runtime.reservation_root, task=spec.runtime.reservation_task
    )
    if released != prior:
        raise ScaleSprintV3Error("stale release differs from the exact prior reservation")
    _append_event(
        runtime.shard_root / "stale_claim_cleanup.jsonl",
        {
            "schema_version": "sft2b_sprint_v3_stale_claim_cleanup_v1",
            "run_id": runtime.plan.run_id,
            "scale_shard_id": runtime.shard.spec.shard_id,
            "prior_supervisor_pid": prior.pid,
            "process_absent": True,
            "port_closed": True,
            "released": True,
            "released_unix_ns": time.time_ns(),
        },
    )
    return True


def supervise_shard(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    config_sha256: str,
    bundle: VerifiedBundle,
    shard_id: str,
    launch_nonce: str,
    launched_unix_ns: int,
) -> int:
    runtime = prepare_shard_runtime(
        repo_root,
        spec=spec,
        config_path=config_path,
        config_sha256=config_sha256,
        bundle=bundle,
        shard_id=shard_id,
    )
    status_path = runtime.shard_root / "launch_status.json"
    pending = _object(status_path)
    if (
        pending.get("schema_version") != "sft2b_full_source_launch_status_v1"
        or pending.get("run_id") != runtime.plan.run_id
        or pending.get("state") != "launch_pending"
        or pending.get("launch_nonce") != launch_nonce
        or pending.get("launched_unix_ns") != launched_unix_ns
    ):
        raise ScaleSprintV3Error("supervisor launch nonce/status drifted")
    atomic_write(
        status_path,
        hardened._status_payload(
            runtime.plan,
            state="starting",
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=os.getpid(),
            scale_shard_id=shard_id,
        ),
    )
    _append_event(
        runtime.shard_root / "supervisor_sessions.jsonl",
        {
            "schema_version": "sft2b_sprint_v3_supervisor_session_v1",
            "run_id": runtime.plan.run_id,
            "scale_shard_id": shard_id,
            "supervisor_pid": os.getpid(),
            "launch_nonce": launch_nonce,
            "launched_unix_ns": launched_unix_ns,
            "started_unix_ns": time.time_ns(),
        },
    )
    endpoint = profile_endpoint(runtime.backend, FULL_PROFILE_NAME)
    hardened._reconcile_dead_same_run_runtime_and_requests(
        backend=runtime.backend,
        sources=runtime.sources,
        endpoint_url=endpoint,
        run_root=spec.runtime.run_root,
        plan=runtime.plan,
        host="127.0.0.1",
        port=spec.model.port,
    )
    _reconcile_stale_claim(repo_root, spec=spec, runtime=runtime)
    inspection = hardened._inspect_scalable_cache(
        runtime.backend,
        profile_name=FULL_PROFILE_NAME,
        sources=runtime.sources,
        endpoint_url=endpoint,
    )
    if inspection.ambiguous_request_keys:
        raise ScaleSprintV3Error("ambiguous same-run request remains after exact reconciliation")
    if not inspection.complete and _port_is_open(spec.model.port):
        raise ScaleSprintV3Error("vLLM port is open without an exact live same-run receipt")
    reservation = None
    claim_path: Path | None = None
    claim_id: str | None = None
    primary: Exception | None = None
    result = None
    try:
        if not inspection.complete:
            reservation = claim_resources(
                root=spec.runtime.reservation_root,
                task=spec.runtime.reservation_task,
                lean_workers=0,
                lean_rss_gib=0.0,
                gpu=True,
                pid=os.getpid(),
                owner_session=(f"SFT2B sprint-v3 {shard_id}; run_id={runtime.plan.run_id}"),
                worktree=repo_root,
            )
            claim_id = f"{time.time_ns()}-{os.getpid()}"
            claim_path = _claim_path(runtime, claim_id)
            immutable_write(
                claim_path,
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_sprint_v3_resource_claim_v1",
                        "run_id": runtime.plan.run_id,
                        "scale_shard_id": shard_id,
                        "claim_id": claim_id,
                        "reservation": asdict(reservation),
                    }
                )
                + b"\n",
            )
        result = run_integrated_executor(
            spec=runtime.executor_spec,
            backend=runtime.backend,
            sources=runtime.sources,
            plan=runtime.plan,
            cache_root=spec.runtime.cache_root,
            run_root=spec.runtime.run_root,
            status_path=status_path,
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            resource_claim_path=claim_path,
        )
    except Exception as exc:
        primary = exc
    finally:
        if reservation is not None:
            released = release_resources(
                root=spec.runtime.reservation_root, task=spec.runtime.reservation_task
            )
            if released != reservation or claim_id is None or claim_path is None:
                raise ScaleSprintV3Error("normal resource release identity drifted")
            immutable_write(
                runtime.shard_root / "resource_sessions" / claim_id / "release.json",
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_sprint_v3_resource_release_v1",
                        "run_id": runtime.plan.run_id,
                        "scale_shard_id": shard_id,
                        "claim_id": claim_id,
                        "claim_sha256": hash_file(claim_path),
                        "released": True,
                        "released_unix_ns": time.time_ns(),
                    }
                )
                + b"\n",
            )
    if primary is not None:
        atomic_write(
            status_path,
            hardened._status_payload(
                runtime.plan,
                state="failed",
                launch_nonce=launch_nonce,
                launched_unix_ns=launched_unix_ns,
                supervisor_pid=os.getpid(),
                scale_shard_id=shard_id,
                error=f"{type(primary).__name__}: {primary}",
            ),
        )
        raise ScaleSprintV3Error(f"shard supervisor failed: {primary}") from primary
    if result is None or result.rows != 50000:
        raise ScaleSprintV3Error("hardened executor did not compact exactly 50,000 terminals")
    atomic_write(
        status_path,
        hardened._status_payload(
            runtime.plan,
            state="completed",
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=os.getpid(),
            scale_shard_id=shard_id,
            compacted_path=str(result.path),
            compacted_sha256=result.sha256,
            compacted_rows=result.rows,
            resource_released=True,
        ),
    )
    return 0


def _session_exists(name: str) -> bool:
    return (
        subprocess.run(
            ("tmux", "has-session", "-t", f"={name}"),
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _pane_pid(name: str) -> int:
    value = _run_checked(("tmux", "display-message", "-p", "-t", f"={name}", "#{pane_pid}"))
    if not value.isdigit():
        raise ScaleSprintV3Error("tmux pane PID is invalid")
    return int(value)


def start_shard_tmux(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    bundle_root: Path,
    runtime: ShardRuntime,
) -> tuple[str, int]:
    if _session_exists(runtime.session_name):
        raise ScaleSprintV3Error(f"shard tmux session already exists: {runtime.session_name}")
    if _port_is_open(spec.model.port):
        raise ScaleSprintV3Error("provider port is open before shard launch")
    launched_unix_ns = time.time_ns()
    nonce = hash_canonical(
        {
            "schema_version": "sft2b_sprint_v3_launch_nonce_v1",
            "run_id": runtime.plan.run_id,
            "scale_shard_id": runtime.shard.spec.shard_id,
            "launched_unix_ns": launched_unix_ns,
            "orchestrator_pid": os.getpid(),
        }
    )
    runtime.log_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        runtime.shard_root / "launch_status.json",
        hardened._status_payload(
            runtime.plan,
            state="launch_pending",
            launch_nonce=nonce,
            launched_unix_ns=launched_unix_ns,
            supervisor_pid=None,
            scale_shard_id=runtime.shard.spec.shard_id,
        ),
    )
    argv = (
        "uv",
        "run",
        "--with",
        f"vllm=={spec.runtime.vllm_version}",
        "python",
        "-m",
        "leanfaith.sft2b.scale_sprint_v3",
        "--config",
        str(config_path),
        "--bundle-root",
        str(bundle_root),
        "supervise",
        "--shard",
        runtime.shard.spec.shard_id,
        "--launch-nonce",
        nonce,
        "--launched-unix-ns",
        str(launched_unix_ns),
    )
    shell_command = f"{shlex.join(argv)} >> {shlex.quote(str(runtime.log_path))} 2>&1 </dev/null"
    started = subprocess.run(
        (
            "tmux",
            "new-session",
            "-d",
            "-s",
            runtime.session_name,
            "-c",
            str(repo_root),
            shell_command,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        raise ScaleSprintV3Error(f"shard tmux launch failed: {started.stderr.strip()}")
    return nonce, launched_unix_ns


def _journal(runtime: ShardRuntime, spec: ScaleSprintV3Spec) -> FullSourceJournal:
    return FullSourceJournal(
        runtime.shard_root / "journal/requests.jsonl",
        plan=runtime.plan,
        cache_root=spec.runtime.cache_root,
        fsync_every=64,
    )


def _terminal_request_keys(
    journal: FullSourceJournal,
    *,
    limit: int | None = None,
) -> tuple[str, ...]:
    events = journal.events()
    if limit is not None:
        events = events[:limit]
    keys: list[str] = []
    for event in events:
        terminal = _object(Path(event.terminal_path))
        payload = terminal.get("payload")
        if not isinstance(payload, dict):
            raise ScaleSprintV3Error("consumer terminal payload is invalid")
        key = payload.get("request_key")
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ScaleSprintV3Error("consumer terminal lacks a semantic request key")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ScaleSprintV3Error("semantic request key repeats across logical terminals")
    return tuple(keys)


def _inflight_attempts(provider_root: Path) -> tuple[Path, ...]:
    active: list[Path] = []
    for started in provider_root.glob("requests/*/transport_attempts/*/started.json"):
        root = started.parent
        outcomes = tuple(
            (root / name).is_file() for name in ("success.json", "failure.json", "abandoned.json")
        )
        if not any(outcomes):
            active.append(root)
    return tuple(sorted(active))


def _last_runtime_start(runtime: ShardRuntime) -> dict[str, Any]:
    path = (
        runtime.executor_spec.runtime.run_root
        / INTERNAL_SHARD_ID
        / runtime.plan.run_id
        / "runtime_session_starts.jsonl"
    )
    rows = _jsonl_objects(path)
    if not rows:
        raise ScaleSprintV3Error("same-run runtime has no server-session start evidence")
    return rows[-1]


def _wait_for_absence(*, pids: Sequence[int], session_name: str, port: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (
            all(not Path(f"/proc/{pid}").exists() for pid in pids)
            and not _session_exists(session_name)
            and not _port_is_open(port)
        ):
            return
        time.sleep(0.25)
    raise ScaleSprintV3Error("killed same-run processes/session/port did not disappear")


def run_inline_core00_recovery(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    bundle_root: Path,
    runtime: ShardRuntime,
) -> dict[str, object]:
    if runtime.shard.spec.shard_id != "core_00":
        raise ScaleSprintV3Error("inline recovery may run only in real shard core_00")
    receipt_path = runtime.shard_root / "inline_recovery_receipt.json"
    if receipt_path.is_file():
        existing_receipt = _object(receipt_path)
        if (
            existing_receipt.get("run_id") != runtime.plan.run_id
            or existing_receipt.get("recovery_passed") is not True
            or cast(int, existing_receipt.get("post_recovery_unique_terminals", 0))
            < spec.recovery.minimum_post_recovery_terminals
        ):
            raise ScaleSprintV3Error("existing inline recovery receipt drifted")
        return existing_receipt

    deadline = time.monotonic() + 7200
    provider_root: Path | None = None
    before_count = 0
    while time.monotonic() < deadline:
        journal = _journal(runtime, spec)
        before_count = journal.completed_count()
        if before_count > spec.recovery.kill_max_terminals:
            raise ScaleSprintV3Error("core_00 passed the authorized 64--128 kill window")
        if before_count >= spec.recovery.kill_min_terminals:
            inspection = hardened._inspect_scalable_cache(
                runtime.backend,
                profile_name=FULL_PROFILE_NAME,
                sources=runtime.sources,
                endpoint_url=profile_endpoint(runtime.backend, FULL_PROFILE_NAME),
            )
            provider_root = inspection.root
            if _inflight_attempts(provider_root):
                break
        status_path = runtime.shard_root / "launch_status.json"
        if status_path.is_file() and _object(status_path).get("state") == "failed":
            raise ScaleSprintV3Error("core_00 supervisor failed before recovery kill")
        time.sleep(0.1)
    else:
        raise ScaleSprintV3Error("core_00 did not reach the inline recovery kill gate")
    if provider_root is None:
        raise ScaleSprintV3Error("provider root was not resolved at recovery gate")

    status = _object(runtime.shard_root / "launch_status.json")
    supervisor_pid = status.get("supervisor_pid")
    if not isinstance(supervisor_pid, int) or isinstance(supervisor_pid, bool):
        raise ScaleSprintV3Error("recovery status lacks the exact supervisor PID")
    pane_pid = _pane_pid(runtime.session_name)
    runtime_start = _last_runtime_start(runtime)
    server_pid = runtime_start.get("server_pid")
    if not isinstance(server_pid, int) or isinstance(server_pid, bool):
        raise ScaleSprintV3Error("runtime evidence lacks the exact vLLM PID")
    supervisor_command = _pid_command(supervisor_pid)
    server_command = _pid_command(server_pid)
    if (
        not _process_descends_from(supervisor_pid, pane_pid)
        or not _process_descends_from(server_pid, supervisor_pid)
        or "leanfaith.sft2b.scale_sprint_v3" not in supervisor_command
        or "supervise" not in supervisor_command
        or "core_00" not in supervisor_command
        or "vllm" not in server_command.casefold()
        or str(spec.runtime.model_snapshot_path) not in server_command
        or f"--port {spec.model.port}" not in server_command
    ):
        raise ScaleSprintV3Error("recovery process ancestry/command identity is unprovable")
    inflight = _inflight_attempts(provider_root)
    if not inflight:
        raise ScaleSprintV3Error("recovery kill no longer has an in-flight request")
    server_process_group = os.getpgid(server_pid)
    if server_process_group != server_pid:
        raise ScaleSprintV3Error("vLLM process is not the verified process-group leader")
    before_keys = _terminal_request_keys(_journal(runtime, spec))
    kill_payload = {
        "schema_version": "sft2b_sprint_v3_inline_kill_v1",
        "run_id": runtime.plan.run_id,
        "scale_shard_id": "core_00",
        "unique_terminals_before_kill": before_count,
        "terminal_request_keys_sha256": hash_canonical(before_keys),
        "inflight_attempts": len(inflight),
        "inflight_request_keys": [path.parents[1].name for path in inflight],
        "tmux_session": runtime.session_name,
        "pane_pid": pane_pid,
        "supervisor_pid": supervisor_pid,
        "supervisor_cmdline_sha256": hashlib.sha256(supervisor_command.encode()).hexdigest(),
        "vllm_pid": server_pid,
        "vllm_process_group": server_process_group,
        "vllm_cmdline_sha256": hashlib.sha256(server_command.encode()).hexdigest(),
        "identities_verified": True,
        "killed_unix_ns": time.time_ns(),
    }
    immutable_write(
        runtime.shard_root / "inline_recovery_kill.json",
        canonical_json_bytes(kill_payload) + b"\n",
    )
    os.kill(supervisor_pid, signal.SIGKILL)
    os.killpg(server_process_group, signal.SIGKILL)
    _wait_for_absence(
        pids=(supervisor_pid, server_pid),
        session_name=runtime.session_name,
        port=spec.model.port,
    )
    immutable_write(
        runtime.shard_root / "inline_recovery_absence.json",
        canonical_json_bytes(
            {
                "schema_version": "sft2b_sprint_v3_inline_absence_v1",
                "run_id": runtime.plan.run_id,
                "supervisor_pid": supervisor_pid,
                "vllm_pid": server_pid,
                "processes_absent": True,
                "tmux_session_absent": True,
                "port_closed": True,
                "verified_unix_ns": time.time_ns(),
            }
        )
        + b"\n",
    )

    start_shard_tmux(
        repo_root,
        spec=spec,
        config_path=config_path,
        bundle_root=bundle_root,
        runtime=runtime,
    )
    first_advance_count: int | None = None
    first_advance_time: float | None = None
    post_count = before_count
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        post_count = _journal(runtime, spec).completed_count()
        now = time.monotonic()
        if post_count > before_count and first_advance_time is None:
            first_advance_time = now
            first_advance_count = post_count
        if post_count >= spec.recovery.minimum_post_recovery_terminals:
            break
        status = _object(runtime.shard_root / "launch_status.json")
        if status.get("state") == "failed":
            raise ScaleSprintV3Error("core_00 failed during recovery advancement")
        time.sleep(0.25)
    else:
        raise ScaleSprintV3Error("core_00 did not advance past the 512-terminal checkpoint")
    if first_advance_time is None or first_advance_count is None:
        raise ScaleSprintV3Error("post-recovery throughput interval was not observed")
    elapsed = max(time.monotonic() - first_advance_time, 1e-9)
    throughput = (post_count - first_advance_count) / elapsed
    if throughput < spec.recovery.minimum_requests_per_second:
        raise ScaleSprintV3Error(
            f"post-recovery throughput {throughput:.3f} requests/s is below 2.0"
        )
    post_journal = _journal(runtime, spec)
    post_keys = _terminal_request_keys(post_journal)
    if not set(before_keys).issubset(post_keys):
        raise ScaleSprintV3Error("pre-kill semantic request keys did not survive recovery")
    if post_journal.completed_count() != len(post_keys):
        raise ScaleSprintV3Error("logical terminal/request-key coverage conflicts")
    remaining = len(runtime.plan.cells) - post_journal.completed_count()
    if remaining < 0:
        raise ScaleSprintV3Error("recovery produced more terminals than planned cells")
    abandoned = tuple(provider_root.glob("requests/*/transport_attempts/*/abandoned.json"))
    reconciliations = (
        runtime.executor_spec.runtime.run_root / INTERNAL_SHARD_ID / runtime.plan.run_id
    )
    reconciliation_rows = _jsonl_objects(reconciliations / "runtime_session_reconciliations.jsonl")
    stale_rows = _jsonl_objects(runtime.shard_root / "stale_claim_cleanup.jsonl")
    status = _object(runtime.shard_root / "launch_status.json")
    checkpoint = status.get("durable_checkpoint")
    checkpoint_sequence = checkpoint.get("sequence") if isinstance(checkpoint, dict) else None
    if (
        len(abandoned) < spec.recovery.required_abandoned_attempts
        or not reconciliation_rows
        or not stale_rows
        or not isinstance(checkpoint_sequence, int)
        or checkpoint_sequence < spec.recovery.checkpoint_terminals
    ):
        raise ScaleSprintV3Error("recovery evidence is incomplete")
    new_supervisor_pid = status.get("supervisor_pid")
    new_start = _last_runtime_start(runtime)
    new_server_pid = new_start.get("server_pid")
    if (
        not isinstance(new_supervisor_pid, int)
        or not isinstance(new_server_pid, int)
        or new_supervisor_pid == supervisor_pid
        or new_server_pid == server_pid
        or not Path(f"/proc/{new_supervisor_pid}").exists()
        or not Path(f"/proc/{new_server_pid}").exists()
        or not _port_is_open(spec.model.port)
    ):
        raise ScaleSprintV3Error("recovered core_00 processes are not provably live and new")
    recovery_receipt: dict[str, object] = {
        "schema_version": "sft2b_sprint_v3_inline_recovery_receipt_v1",
        "run_id": runtime.plan.run_id,
        "scale_shard_id": "core_00",
        "same_run_identity": True,
        "same_cache_root": str(spec.runtime.cache_root),
        "pre_kill_unique_terminals": before_count,
        "post_recovery_unique_terminals": post_count,
        "remaining_planned_terminals": remaining,
        "checkpoint_sequence": checkpoint_sequence,
        "pre_kill_request_keys_sha256": hash_canonical(before_keys),
        "post_recovery_request_keys_sha256": hash_canonical(post_keys),
        "pre_kill_keys_preserved": True,
        "duplicate_or_conflicting_cells": 0,
        "abandoned_attempts": len(abandoned),
        "runtime_reconciliations": len(reconciliation_rows),
        "stale_claim_cleanups": len(stale_rows),
        "old_supervisor_pid": supervisor_pid,
        "old_vllm_pid": server_pid,
        "new_supervisor_pid": new_supervisor_pid,
        "new_vllm_pid": new_server_pid,
        "post_recovery_requests_per_second": throughput,
        "shard_left_running": True,
        "recovery_passed": True,
        "verified_unix_ns": time.time_ns(),
    }
    immutable_write(receipt_path, canonical_json_bytes(recovery_receipt) + b"\n")
    return recovery_receipt


def wait_for_shard_completion(
    runtime: ShardRuntime,
    *,
    poll_seconds: float = 5.0,
) -> dict[str, Any]:
    while True:
        path = runtime.shard_root / "launch_status.json"
        if path.is_file():
            status = _object(path)
            state = status.get("state")
            if state == "completed":
                if (
                    status.get("run_id") != runtime.plan.run_id
                    or status.get("compacted_rows") != 50000
                    or status.get("resource_released") is not True
                ):
                    raise ScaleSprintV3Error("completed shard status drifted")
                return status
            if state == "failed":
                raise ScaleSprintV3Error(
                    f"shard {runtime.shard.spec.shard_id} failed: {status.get('error')}"
                )
        if not _session_exists(runtime.session_name):
            raise ScaleSprintV3Error(
                f"shard session vanished without completion: {runtime.session_name}"
            )
        time.sleep(poll_seconds)


def verify_zero_call_replay(
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
) -> dict[str, object]:
    starts_root = runtime.backend.spec.staging_root
    starts_before = len(tuple(starts_root.glob("**/transport_attempts/*/started.json")))
    runtime_starts = (
        runtime.executor_spec.runtime.run_root
        / INTERNAL_SHARD_ID
        / runtime.plan.run_id
        / "runtime_session_starts.jsonl"
    )
    sessions_before = len(_jsonl_objects(runtime_starts)) if runtime_starts.is_file() else 0
    journal_before = hash_file(runtime.shard_root / "journal/requests.jsonl")
    if _port_is_open(spec.model.port):
        raise ScaleSprintV3Error("zero-call replay found an open provider port")
    result = run_integrated_executor(
        spec=runtime.executor_spec,
        backend=runtime.backend,
        sources=runtime.sources,
        plan=runtime.plan,
        cache_root=spec.runtime.cache_root,
        run_root=spec.runtime.run_root,
    )
    starts_after = len(tuple(starts_root.glob("**/transport_attempts/*/started.json")))
    sessions_after = len(_jsonl_objects(runtime_starts)) if runtime_starts.is_file() else 0
    if (
        result.rows != 50000
        or starts_after != starts_before
        or sessions_after != sessions_before
        or hash_file(runtime.shard_root / "journal/requests.jsonl") != journal_before
        or _port_is_open(spec.model.port)
    ):
        raise ScaleSprintV3Error("shard replay made a call or changed durable terminal evidence")
    receipt = {
        "schema_version": "sft2b_sprint_v3_zero_call_replay_v1",
        "run_id": runtime.plan.run_id,
        "scale_shard_id": runtime.shard.spec.shard_id,
        "sources": 12500,
        "terminals": 50000,
        "provider_calls": 0,
        "cache_hits": 50000,
        "new_runtime_sessions": 0,
        "journal_sha256": journal_before,
        "compacted_sha256": result.sha256,
        "verified_unix_ns": time.time_ns(),
    }
    immutable_write(
        runtime.shard_root / "replay_receipt.json",
        canonical_json_bytes(receipt) + b"\n",
    )
    return receipt


def _verify_output_bundle(
    root: Path,
    *,
    runtime: ShardRuntime,
) -> OutputBundle:
    observed = {item.name for item in root.iterdir() if item.is_file()}
    if observed != OUTPUT_NAMES:
        raise ScaleSprintV3Error("generation release file set is not exact")
    ledger: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise ScaleSprintV3Error("generation SHA256SUMS is malformed")
        ledger[parts[1]] = parts[0]
    if set(ledger) != OUTPUT_NAMES.difference({"SHA256SUMS"}):
        raise ScaleSprintV3Error("generation SHA256SUMS coverage drifted")
    if any(hash_file(root / name) != digest for name, digest in ledger.items()):
        raise ScaleSprintV3Error("generation release file hash drifted")
    manifest = _object(root / "generation_manifest.json")
    completion = _object(root / "completion.json")
    view = _object(root / "shard_source_ids.json")
    if (
        manifest.get("run_id") != runtime.plan.run_id
        or manifest.get("scale_shard_id") != runtime.shard.spec.shard_id
        or cast(dict[str, object], manifest.get("counts", {})).get("sources") != 12500
        or cast(dict[str, object], manifest.get("counts", {})).get("terminals") != 50000
        or completion.get("complete") is not True
        or completion.get("zero_call_replay") is not True
        or view.get("source_ids") != list(runtime.shard.source_ids)
        or hashlib.sha256((root / "shard_source_ids.json").read_bytes()).hexdigest()
        != runtime.shard.spec.artifact_sha256
    ):
        raise ScaleSprintV3Error("generation release manifest/completion/view drifted")
    hashes = {"SHA256SUMS": hash_file(root / "SHA256SUMS"), **ledger}
    return OutputBundle(root=root, run_id=runtime.plan.run_id, hashes=hashes)


def build_output_bundle(
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
    git_revision: str,
) -> OutputBundle:
    final_root = runtime.shard_root / "release"
    if final_root.is_dir():
        return _verify_output_bundle(final_root, runtime=runtime)
    final_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".release.", dir=final_root.parent))
    try:
        (temporary / "shard_source_ids.json").write_bytes(runtime.shard.payload)
        compacted = runtime.shard_root / "outputs/request_terminals.jsonl"
        if not compacted.is_file():
            raise ScaleSprintV3Error("completed shard lacks compacted request terminals")
        shutil.copyfile(compacted, temporary / "request_terminals.jsonl")
        counts = {
            "sources": 12500,
            "terminals": 0,
            "candidates": 0,
            "formalizer_invalid": 0,
        }
        prompt_tokens = 0
        completion_tokens = 0
        with (
            compacted.open(encoding="utf-8") as source,
            (temporary / "formalizer_attempts.jsonl").open("wb") as attempts_out,
            (temporary / "candidates.jsonl").open("wb") as candidates_out,
            (temporary / "formalizer_invalid_attempts.jsonl").open("wb") as invalid_out,
            (temporary / "request_metrics.jsonl").open("wb") as metrics_out,
            (temporary / "raw_generations.jsonl").open("wb") as raw_out,
        ):
            for number, line in enumerate(source, start=1):
                try:
                    envelope = hardened.FullSourceTerminal.model_validate_json(line)
                    terminal = VllmRequestTerminal.model_validate(
                        envelope.payload.get("vllm_terminal")
                    )
                except Exception as exc:
                    raise ScaleSprintV3Error(
                        f"invalid compacted terminal at line {number}: {exc}"
                    ) from exc
                attempt = terminal.attempt.model_dump(mode="json")
                candidate = (
                    terminal.candidate.model_dump(mode="json")
                    if terminal.candidate is not None
                    else None
                )
                metrics = terminal.metrics.model_dump(mode="json")
                request_key = terminal.request_key
                raw_path = Path(terminal.metrics.raw_output_path)
                raw_sha = terminal.metrics.raw_output_sha256
                if (
                    not raw_path.is_file()
                    or raw_path.is_symlink()
                    or hash_file(raw_path) != raw_sha
                ):
                    raise ScaleSprintV3Error("raw provider output drifted before release")
                attempts_out.write(canonical_json_bytes(attempt) + b"\n")
                metrics_out.write(canonical_json_bytes(metrics) + b"\n")
                raw_out.write(
                    canonical_json_bytes(
                        {
                            "schema_version": "sft2b_sprint_v3_raw_generation_v1",
                            "request_key": request_key,
                            "source_id": metrics.get("source_id"),
                            "slot": metrics.get("slot"),
                            "raw_output": raw_path.read_text(encoding="utf-8"),
                            "raw_output_sha256": raw_sha,
                        }
                    )
                    + b"\n"
                )
                if isinstance(candidate, dict):
                    candidates_out.write(canonical_json_bytes(candidate) + b"\n")
                    counts["candidates"] += 1
                else:
                    invalid = FormalizerInvalidAttemptView(
                        attempt_id=terminal.attempt.attempt_id,
                        source_id=terminal.attempt.source_id,
                        slot=terminal.attempt.slot,
                        validity_label=False,
                        failure_class=cast(str, terminal.attempt.failure_class),
                        failure_detail=cast(str, terminal.attempt.failure_detail),
                        raw_output_sha256=terminal.attempt.raw_output_sha256,
                    )
                    invalid_out.write(canonical_json_bytes(invalid.model_dump(mode="json")) + b"\n")
                    counts["formalizer_invalid"] += 1
                counts["terminals"] += 1
                prompt_tokens += int(metrics.get("prompt_tokens", 0))
                completion_tokens += int(metrics.get("completion_tokens", 0))
        if counts["terminals"] != 50000 or (
            counts["candidates"] + counts["formalizer_invalid"] != 50000
        ):
            raise ScaleSprintV3Error("release routing does not cover every terminal exactly once")
        replay = _object(runtime.shard_root / "replay_receipt.json")
        (temporary / "replay_receipt.json").write_bytes(canonical_json_bytes(replay) + b"\n")
        recovery_path = runtime.shard_root / "inline_recovery_receipt.json"
        recovery: dict[str, object]
        if runtime.shard.spec.shard_id == "core_00":
            recovery = _object(recovery_path)
            if recovery.get("recovery_passed") is not True:
                raise ScaleSprintV3Error("core_00 release lacks passing recovery evidence")
        else:
            recovery = {
                "schema_version": "sft2b_sprint_v3_recovery_not_applicable_v1",
                "scale_shard_id": runtime.shard.spec.shard_id,
                "reason": "inline recovery is required exactly once in core_00",
            }
        (temporary / "recovery_receipt.json").write_bytes(canonical_json_bytes(recovery) + b"\n")
        manifest = {
            "schema_version": "sft2b_sprint_v3_generation_manifest_v1",
            "run_id": runtime.plan.run_id,
            "scale_shard_id": runtime.shard.spec.shard_id,
            "source_slice": {
                "start": runtime.shard.spec.start,
                "stop": runtime.shard.spec.stop,
            },
            "source_view_sha256": runtime.shard.spec.artifact_sha256,
            "source_ids_sha256": runtime.shard.spec.source_ids_sha256,
            "input": {
                "repo_id": spec.input.repo_id,
                "revision": spec.input.revision,
                "path_prefix": spec.input.path_prefix,
                "checksum_ledger_sha256": spec.input.files["SHA256SUMS"],
                "core_view_sha256": spec.input.files["matched_50000_source_ids.json"],
            },
            "model": {
                "model_id": spec.model.model_id,
                "revision": spec.model.revision,
                "snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
                "dtype": spec.model.checkpoint_dtype,
                "max_model_len": spec.model.max_model_len,
                "data_parallel_size": spec.model.data_parallel_size,
                "tensor_parallel_size": spec.model.tensor_parallel_size,
                "concurrency": spec.model.concurrency,
                "max_num_seqs": spec.model.max_num_seqs,
                "vllm_version": spec.runtime.vllm_version,
            },
            "accepted_evidence": spec.evidence.model_dump(mode="json"),
            "counts": counts,
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            "git_revision": git_revision,
            "routing": {
                "candidates": "Lean validity and semantics not yet established",
                "formalizer_invalid": "auxiliary only; never a semantic negative",
            },
        }
        (temporary / "generation_manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
        completion = {
            "schema_version": "sft2b_sprint_v3_shard_completion_v1",
            "run_id": runtime.plan.run_id,
            "scale_shard_id": runtime.shard.spec.shard_id,
            "sources": 12500,
            "terminals": 50000,
            "four_slots_per_source": True,
            "zero_call_replay": True,
            "resource_released": True,
            "complete": True,
            "completed_unix_ns": time.time_ns(),
        }
        (temporary / "completion.json").write_bytes(canonical_json_bytes(completion) + b"\n")
        covered = sorted(OUTPUT_NAMES.difference({"SHA256SUMS"}))
        (temporary / "SHA256SUMS").write_text(
            "".join(f"{hash_file(temporary / name)}  {name}\n" for name in covered),
            encoding="utf-8",
        )
        os.replace(temporary, final_root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return _verify_output_bundle(final_root, runtime=runtime)


def _fresh_verify_publication(
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
    bundle: OutputBundle,
    revision: str,
    remote_prefix: str,
) -> None:
    fresh_parent = runtime.shard_root / "fresh_publication_verification"
    fresh_parent.mkdir(parents=True, exist_ok=True)
    fresh_cache = Path(tempfile.mkdtemp(prefix="hf.", dir=fresh_parent))
    snapshot = _snapshot_download(
        repo_id=spec.publication.repo_id,
        repo_type=spec.publication.repo_type,
        revision=revision,
        cache_dir=fresh_cache,
        allow_patterns=[f"{remote_prefix}/*"],
    )
    verified = _verify_output_bundle(snapshot / remote_prefix, runtime=runtime)
    if verified.hashes != bundle.hashes:
        raise ScaleSprintV3Error("fresh publication hashes differ from local shard")


def publish_output(
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
    bundle: OutputBundle,
) -> dict[str, object]:
    from huggingface_hub import CommitOperationAdd, HfApi

    receipt_path = runtime.shard_root / "publication_receipt.json"
    remote_prefix = (
        f"{spec.publication.path_prefix}/{runtime.shard.spec.shard_id}/"
        f"{runtime.plan.run_id.split(':', 1)[1]}"
    )
    if receipt_path.is_file():
        existing_receipt = _object(receipt_path)
        revision = existing_receipt.get("revision")
        if not isinstance(revision, str) or existing_receipt.get("remote_prefix") != remote_prefix:
            raise ScaleSprintV3Error("local publication receipt drifted")
        _fresh_verify_publication(
            spec=spec,
            runtime=runtime,
            bundle=bundle,
            revision=revision,
            remote_prefix=remote_prefix,
        )
        return existing_receipt
    api = HfApi()
    info = api.repo_info(repo_id=spec.publication.repo_id, repo_type=spec.publication.repo_type)
    if spec.publication.private_required and not bool(info.private):
        raise ScaleSprintV3Error("refusing generation publication to a public repository")
    parent = str(info.sha)
    existing = set(
        api.list_repo_files(
            repo_id=spec.publication.repo_id,
            repo_type=spec.publication.repo_type,
            revision=parent,
        )
    )
    remote_paths = tuple(f"{remote_prefix}/{name}" for name in sorted(OUTPUT_NAMES))
    occupied = {item for item in existing if item.startswith(f"{remote_prefix}/")}
    if occupied and occupied != set(remote_paths):
        raise ScaleSprintV3Error("remote shard prefix is partially occupied")
    if occupied:
        revision = parent
    else:
        commit = api.create_commit(
            repo_id=spec.publication.repo_id,
            repo_type=spec.publication.repo_type,
            parent_commit=parent,
            commit_message=(
                f"{spec.publication.commit_message_prefix} {runtime.shard.spec.shard_id}"
            ),
            operations=[
                CommitOperationAdd(path_in_repo=remote, path_or_fileobj=bundle.root / name)
                for name, remote in zip(sorted(OUTPUT_NAMES), remote_paths, strict=True)
            ],
        )
        revision = str(commit.oid)
    if _SHA40_RE.fullmatch(revision) is None:
        raise ScaleSprintV3Error("Hub publication did not return an immutable revision")
    _fresh_verify_publication(
        spec=spec,
        runtime=runtime,
        bundle=bundle,
        revision=revision,
        remote_prefix=remote_prefix,
    )
    publication_receipt: dict[str, object] = {
        "schema_version": "sft2b_sprint_v3_publication_receipt_v1",
        "run_id": runtime.plan.run_id,
        "scale_shard_id": runtime.shard.spec.shard_id,
        "repo_id": spec.publication.repo_id,
        "revision": revision,
        "remote_prefix": remote_prefix,
        "remote_paths": list(remote_paths),
        "file_sha256": bundle.hashes,
        "fresh_verification": True,
    }
    immutable_write(receipt_path, canonical_json_bytes(publication_receipt) + b"\n")
    return publication_receipt


def queue_downstream(
    *,
    spec: ScaleSprintV3Spec,
    runtime: ShardRuntime,
    publication: Mapping[str, object],
) -> None:
    """Emit an append-only cross-resource work item as soon as a shard lands.

    Generation never waits for this queue.  The work item binds the active
    persistent-Lean budget and three-judge defaults, and explicitly preserves
    invalid formalizations as auxiliary validity data rather than negatives.
    """

    queue = spec.runtime.run_root / "downstream/queue.jsonl"
    prior = _jsonl_objects(queue) if queue.is_file() else ()
    if any(item.get("scale_shard_id") == runtime.shard.spec.shard_id for item in prior):
        return
    _append_event(
        queue,
        {
            "schema_version": "sft2b_sprint_v3_downstream_work_item_v1",
            "scale_shard_id": runtime.shard.spec.shard_id,
            "generation_run_id": runtime.plan.run_id,
            "generation_revision": publication["revision"],
            "generation_remote_prefix": publication["remote_prefix"],
            "state": "queued_for_separate_resources",
            "lean": {
                "persistent_workers": spec.downstream.lean_max_workers,
                "maximum_host_rss_gib": spec.downstream.lean_max_host_rss_gib,
                "compile_each_novel_candidate_once": True,
            },
            "judges": {
                "config_path": spec.downstream.judge_config_path,
                "config_sha256": spec.downstream.judge_config_sha256,
                "labeling_policy_path": spec.downstream.labeling_policy_path,
                "labeling_policy_sha256": spec.downstream.labeling_policy_sha256,
                "three_independent_votes": True,
            },
            "invalid_candidates_are_semantic_negatives": False,
            "queued_unix_ns": time.time_ns(),
        },
    )


def emit_union_receipt(
    *,
    spec: ScaleSprintV3Spec,
    bundle: VerifiedBundle,
    runtimes: Sequence[ShardRuntime],
    publications: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if len(runtimes) != 4 or len(publications) != 4:
        raise ScaleSprintV3Error("union receipt requires four complete publications")
    source_ids = tuple(item for runtime in runtimes for item in runtime.shard.source_ids)
    if source_ids != bundle.core_ids or len(set(source_ids)) != 50000:
        raise ScaleSprintV3Error("published shard union differs from frozen 50K core")
    cell_ids = tuple(cell.cell_id for runtime in runtimes for cell in runtime.plan.cells)
    if len(cell_ids) != 200000 or len(set(cell_ids)) != 200000:
        raise ScaleSprintV3Error("published shard union has overlapping/duplicate cells")
    receipt: dict[str, object] = {
        "schema_version": "sft2b_sprint_v3_union_receipt_v1",
        "input_revision": spec.input.revision,
        "core_view_sha256": spec.input.files["matched_50000_source_ids.json"],
        "sources": 50000,
        "source_slot_terminals": 200000,
        "slots_per_source": 4,
        "source_overlap": 0,
        "cell_duplicates": 0,
        "exact_core_equality": True,
        "tail_sources_generated": 0,
        "shards": [
            {
                "scale_shard_id": runtime.shard.spec.shard_id,
                "run_id": runtime.plan.run_id,
                "sources": 12500,
                "terminals": 50000,
                "source_view_sha256": runtime.shard.spec.artifact_sha256,
                "publication_revision": publication["revision"],
                "publication_remote_prefix": publication["remote_prefix"],
                "fresh_verification": publication["fresh_verification"],
            }
            for runtime, publication in zip(runtimes, publications, strict=True)
        ],
        "completed_unix_ns": time.time_ns(),
    }
    immutable_write(
        spec.runtime.run_root / "union_receipt.json", canonical_json_bytes(receipt) + b"\n"
    )
    return receipt


def orchestrate(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    config_sha256: str,
    bundle_root: Path,
) -> dict[str, object]:
    """Run core_00..03 sequentially and never serialize downstream consumption."""

    state_path = spec.runtime.run_root / "orchestrator_state.json"
    events_path = spec.runtime.run_root / "orchestrator_events.jsonl"
    bundle = verify_source_bundle(
        spec,
        bundle_root=bundle_root,
        view_output_root=spec.runtime.run_root / "activation/views",
    )
    verify_accepted_evidence(spec)
    git_revision = _run_checked(("git", "rev-parse", "HEAD"), cwd=repo_root)
    runtimes: list[ShardRuntime] = []
    publications: list[Mapping[str, object]] = []
    for index, shard_id in enumerate(SHARD_IDS):
        runtime = prepare_shard_runtime(
            repo_root,
            spec=spec,
            config_path=config_path,
            config_sha256=config_sha256,
            bundle=bundle,
            shard_id=shard_id,
        )
        runtimes.append(runtime)
        status_path = runtime.shard_root / "launch_status.json"
        completed = status_path.is_file() and _object(status_path).get("state") == "completed"
        if not completed and not _session_exists(runtime.session_name):
            start_shard_tmux(
                repo_root,
                spec=spec,
                config_path=config_path,
                bundle_root=bundle_root,
                runtime=runtime,
            )
            _append_event(
                events_path,
                {
                    "schema_version": "sft2b_sprint_v3_orchestrator_event_v1",
                    "sequence_stage": f"launch_{shard_id}",
                    "scale_shard_id": shard_id,
                    "run_id": runtime.plan.run_id,
                    "session_name": runtime.session_name,
                    "launched_unix_ns": time.time_ns(),
                },
            )
        if index == 0 and not completed:
            recovery = run_inline_core00_recovery(
                repo_root,
                spec=spec,
                config_path=config_path,
                bundle_root=bundle_root,
                runtime=runtime,
            )
            raw_throughput = recovery["post_recovery_requests_per_second"]
            raw_terminals = recovery["post_recovery_unique_terminals"]
            if (
                not isinstance(raw_throughput, int | float)
                or isinstance(raw_throughput, bool)
                or not isinstance(raw_terminals, int)
                or isinstance(raw_terminals, bool)
            ):
                raise ScaleSprintV3Error("recovery receipt metrics have invalid types")
            throughput = float(raw_throughput)
            recovery_terminals = raw_terminals
            remaining = 200000 - recovery_terminals
            projection = remaining / throughput
            atomic_write(
                state_path,
                canonical_json_bytes(
                    {
                        "schema_version": "sft2b_sprint_v3_orchestrator_state_v1",
                        "state": "recovery_passed_scaling",
                        "orchestrator_pid": os.getpid(),
                        "active_scale_shard_id": "core_00",
                        "active_run_id": runtime.plan.run_id,
                        "active_session_name": runtime.session_name,
                        "active_log_path": str(runtime.log_path),
                        "first_durable_counts": {
                            "terminals": recovery_terminals,
                            "sources_maximum": (recovery_terminals + 3) // 4,
                        },
                        "measured_requests_per_second": throughput,
                        "projected_remaining_generation_seconds": projection,
                        "core_00_left_running": True,
                        "tail_enabled": False,
                        "updated_unix_ns": time.time_ns(),
                    }
                )
                + b"\n",
            )
            _append_event(
                events_path,
                {
                    "schema_version": "sft2b_sprint_v3_orchestrator_event_v1",
                    "sequence_stage": "core_00_inline_recovery_passed",
                    "run_id": runtime.plan.run_id,
                    "terminals": recovery_terminals,
                    "requests_per_second": throughput,
                    "projected_remaining_generation_seconds": projection,
                    "recorded_unix_ns": time.time_ns(),
                },
            )
        wait_for_shard_completion(runtime)
        replay = verify_zero_call_replay(spec=spec, runtime=runtime)
        output = build_output_bundle(
            spec=spec,
            runtime=runtime,
            git_revision=git_revision,
        )
        publication = publish_output(spec=spec, runtime=runtime, bundle=output)
        publications.append(publication)
        queue_downstream(spec=spec, runtime=runtime, publication=publication)
        _append_event(
            events_path,
            {
                "schema_version": "sft2b_sprint_v3_orchestrator_event_v1",
                "sequence_stage": f"published_{shard_id}",
                "scale_shard_id": shard_id,
                "run_id": runtime.plan.run_id,
                "sources": 12500,
                "terminals": 50000,
                "zero_call_replay": replay["provider_calls"] == 0,
                "publication_revision": publication["revision"],
                "fresh_verification": publication["fresh_verification"],
                "downstream_queued": True,
                "recorded_unix_ns": time.time_ns(),
            },
        )
        next_shard = SHARD_IDS[index + 1] if index + 1 < len(SHARD_IDS) else None
        atomic_write(
            state_path,
            canonical_json_bytes(
                {
                    "schema_version": "sft2b_sprint_v3_orchestrator_state_v1",
                    "state": "shard_published" if next_shard else "generation_complete",
                    "orchestrator_pid": os.getpid(),
                    "last_completed_shard": shard_id,
                    "next_scale_shard_id": next_shard,
                    "published_shards": len(publications),
                    "published_sources": len(publications) * 12500,
                    "published_terminals": len(publications) * 50000,
                    "tail_enabled": False,
                    "updated_unix_ns": time.time_ns(),
                }
            )
            + b"\n",
        )
    union = emit_union_receipt(
        spec=spec,
        bundle=bundle,
        runtimes=runtimes,
        publications=publications,
    )
    atomic_write(
        state_path,
        canonical_json_bytes(
            {
                "schema_version": "sft2b_sprint_v3_orchestrator_state_v1",
                "state": "complete",
                "orchestrator_pid": os.getpid(),
                "sources": 50000,
                "terminals": 200000,
                "published_shards": 4,
                "union_receipt_path": str(spec.runtime.run_root / "union_receipt.json"),
                "union_receipt_sha256": hash_file(spec.runtime.run_root / "union_receipt.json"),
                "tail_enabled": False,
                "updated_unix_ns": time.time_ns(),
            }
        )
        + b"\n",
    )
    return union


def launch_orchestrator(
    repo_root: Path,
    *,
    spec: ScaleSprintV3Spec,
    config_path: Path,
    config_sha256: str,
    bundle_root: Path,
    preflight: HostPreflight,
) -> dict[str, object]:
    suffix = config_sha256[:12]
    session_name = f"{spec.runtime.orchestrator_session_prefix}-{suffix}"
    if _TMUX_RE.fullmatch(session_name) is None or _session_exists(session_name):
        raise ScaleSprintV3Error("orchestrator tmux identity is invalid or already live")
    root = spec.runtime.run_root / "orchestrator"
    log_path = root / "orchestrator.log"
    contract_path = root / "launch_contract.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resume_argv = (
        "uv",
        "run",
        "--with",
        f"vllm=={spec.runtime.vllm_version}",
        "python",
        "-m",
        "leanfaith.sft2b.scale_sprint_v3",
        "--config",
        str(config_path),
        "--bundle-root",
        str(bundle_root),
        "orchestrate",
    )
    contract = {
        "schema_version": "sft2b_sprint_v3_launch_contract_v1",
        "session_name": session_name,
        "config_path": str(config_path),
        "config_sha256": config_sha256,
        "git_revision": preflight.git_revision,
        "input_revision": spec.input.revision,
        "input_path_prefix": spec.input.path_prefix,
        "input_checksum_ledger_sha256": spec.input.files["SHA256SUMS"],
        "model_revision": spec.model.revision,
        "model_snapshot_binding_sha256": spec.model.snapshot_binding_sha256,
        "model_snapshot_path": str(spec.runtime.model_snapshot_path),
        "run_root": str(spec.runtime.run_root),
        "cache_root": str(spec.runtime.cache_root),
        "reservation_root": str(spec.runtime.reservation_root),
        "resource_reservation": "one exclusive eight-GPU generation job; zero Lean workers",
        "journal_paths": [
            str(spec.runtime.run_root / INTERNAL_SHARD_ID / "<run_id>/journal/requests.jsonl"),
            str(spec.runtime.run_root / "orchestrator_events.jsonl"),
        ],
        "log_path": str(log_path),
        "completion_marker": str(spec.runtime.run_root / "union_receipt.json"),
        "resume_command": shlex.join(resume_argv),
        "status_command": (
            f"uv run python -m leanfaith.sft2b.scale_sprint_v3 --config "
            f"{shlex.quote(str(config_path))} status"
        ),
        "stop_conditions": [
            "identity drift",
            "unsafe or unprovable recovery",
            "real GPU allocation failure",
            "exhausted transport retries",
            "persistent infrastructure failure",
            "insufficient storage",
        ],
        "tail_enabled": False,
        "launched_unix_ns": time.time_ns(),
    }
    immutable_write(contract_path, canonical_json_bytes(contract) + b"\n")
    shell_command = f"{shlex.join(resume_argv)} >> {shlex.quote(str(log_path))} 2>&1 </dev/null"
    started = subprocess.run(
        (
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(repo_root),
            shell_command,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        raise ScaleSprintV3Error(f"orchestrator tmux launch failed: {started.stderr.strip()}")
    pane_pid = _pane_pid(session_name)
    return {
        "schema_version": "sft2b_sprint_v3_launch_receipt_v1",
        "session_name": session_name,
        "pane_pid": pane_pid,
        "log_path": str(log_path),
        "launch_contract_path": str(contract_path),
        "resume_command": shlex.join(resume_argv),
        "tail_enabled": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/reform_diverse_core_scale_sprint_v3.json"),
    )
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--lean-manifest", type=Path)
    parser.add_argument("--judge-manifest", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "preflight", "activate", "orchestrate", "status"):
        subparsers.add_parser(name)
    supervise = subparsers.add_parser("supervise")
    supervise.add_argument("--shard", choices=SHARD_IDS, required=True)
    supervise.add_argument("--launch-nonce", required=True)
    supervise.add_argument("--launched-unix-ns", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path.cwd().resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    spec, config_sha256 = load_spec(repo_root, config_path.resolve())
    if args.command == "status":
        state = spec.runtime.run_root / "orchestrator_state.json"
        output: object = (
            _object(state)
            if state.is_file()
            else {
                "schema_version": "sft2b_sprint_v3_status_v1",
                "state": "not_launched",
            }
        )
        print(json.dumps(output, sort_keys=True))
        return 0
    verify_accepted_evidence(
        spec,
        lean_manifest_path=args.lean_manifest,
        judge_manifest_path=args.judge_manifest,
    )
    bundle_root = args.bundle_root.resolve() if args.bundle_root is not None else None
    if args.command == "verify":
        if bundle_root is None:
            raise ScaleSprintV3Error("verify requires --bundle-root")
        bundle = verify_source_bundle(spec, bundle_root=bundle_root)
        print(
            json.dumps(
                {
                    "schema_version": "sft2b_sprint_v3_verification_v1",
                    "sources": len(bundle.rows),
                    "core_sources": len(bundle.core_ids),
                    "tail_sources": len(bundle.tail_ids),
                    "shards": {
                        item.spec.shard_id: {
                            "sources": len(item.source_ids),
                            "artifact_sha256": item.spec.artifact_sha256,
                        }
                        for item in bundle.views
                    },
                    "tail_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "preflight":
        host = verify_target_host(repo_root, spec=spec, require_model=True)
        if bundle_root is None:
            raise ScaleSprintV3Error("preflight requires --bundle-root")
        bundle = verify_source_bundle(spec, bundle_root=bundle_root)
        for shard in bundle.views:
            prepare_shard_runtime(
                repo_root,
                spec=spec,
                config_path=config_path.resolve(),
                config_sha256=config_sha256,
                bundle=bundle,
                shard_id=shard.spec.shard_id,
            )
        print(json.dumps(asdict(host), sort_keys=True))
        return 0
    if args.command == "activate":
        verify_target_host(repo_root, spec=spec, require_model=False)
        if bundle_root is None:
            bundle_root, _ = stage_inputs_and_model(
                repo_root, spec=spec, config_path=config_path.resolve()
            )
        host = verify_target_host(repo_root, spec=spec, require_model=True)
        bundle = verify_source_bundle(
            spec,
            bundle_root=bundle_root,
            view_output_root=spec.runtime.run_root / "activation/views",
        )
        for shard in bundle.views:
            prepare_shard_runtime(
                repo_root,
                spec=spec,
                config_path=config_path.resolve(),
                config_sha256=config_sha256,
                bundle=bundle,
                shard_id=shard.spec.shard_id,
            )
        receipt = launch_orchestrator(
            repo_root,
            spec=spec,
            config_path=config_path.resolve(),
            config_sha256=config_sha256,
            bundle_root=bundle_root,
            preflight=host,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if bundle_root is None:
        raise ScaleSprintV3Error(f"{args.command} requires --bundle-root")
    bundle = verify_source_bundle(spec, bundle_root=bundle_root)
    if args.command == "orchestrate":
        result = orchestrate(
            repo_root,
            spec=spec,
            config_path=config_path.resolve(),
            config_sha256=config_sha256,
            bundle_root=bundle_root,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    return supervise_shard(
        repo_root,
        spec=spec,
        config_path=config_path.resolve(),
        config_sha256=config_sha256,
        bundle=bundle,
        shard_id=args.shard,
        launch_nonce=args.launch_nonce,
        launched_unix_ns=args.launched_unix_ns,
    )


if __name__ == "__main__":
    raise SystemExit(main())

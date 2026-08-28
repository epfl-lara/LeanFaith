"""Durable two-arm Queue-5 S1 retraining on the frozen corpus-v1 artifact.

This module is intentionally an operational wrapper around :mod:`trainer`.  It
does not add approximate mid-epoch resume to the trainer.  Instead, each arm is
written to a fresh immutable attempt directory; a completed attempt is skipped
only after its manifest and checkpoints replay exactly, while a partial arm is
left in place and a later invocation starts a new attempt.

Run this module under tmux or nohup.  It acquires the shared RTX 4090 lock
non-blockingly and performs an ``nvidia-smi`` idle check before either model is
loaded.  Hugging Face and Transformers are forced into local/offline mode.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.train2.trainer import (
    BEST_CHECKPOINT_FILENAME,
    LAST_CHECKPOINT_FILENAME,
    MANIFEST_FILENAME,
    TrainerConfig,
    TrainerResult,
    run_trainer,
)

METHOD_VERSION = "s1_corpus_v1_two_arm_v1"
QUEUE_MANIFEST_FILENAME = "run_manifest.json"
FAILURE_MANIFEST_FILENAME = "failure_manifest.json"
PREFLIGHT_FILENAME = "gpu_preflight.json"
ATTEMPT_STATUS_FILENAME = "attempt_status.json"
QUEUE_LOCK_FILENAME = "queue.lock"

DEFAULT_GPU_LOCK_PATH = Path("/storage/milikic/leanfaith/rtx4090.lock")
DEFAULT_GPU_INDEX = 0
DEFAULT_MAX_IDLE_MEMORY_MIB = 1024
_STORAGE_ROOT = Path("/storage/milikic")
_ARM_ORDER = ("cpt_chunks", "cpt_mixed")
_OUTPUT_MODEL_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


def _blocked_dev_evaluation() -> dict[str, object]:
    """Describe the literal seal blocker without opening the mixed pair file."""

    return {
        "status": "blocked",
        "reason_code": "literal_seal_missing_trusted_dev_only_text_artifact",
        "reason": (
            "golden dev scoring requires pair text, but the only available canonical pair "
            "artifact mixes dev with sealed final_test rows; the current evaluator loads that "
            "whole file before partition filtering"
        ),
        "mixed_canonical_file_opened": False,
        "evaluation_attempted": False,
        "strict_metrics": None,
        "calibrated_metrics": None,
        "required_input": {
            "kind": "trusted_dev_only_golden_pairs_v1",
            "expected_pair_count": 821,
            "required_partition": "dev",
            "must_bind_frozen_partition_manifest": str(
                Path(__file__).resolve().parents[3] / "data/benchmarks/golden_partition_v1.json"
            ),
        },
        "next_action": (
            "an authorized trusted process must supply a hash-bound dev-only text artifact; "
            "evaluate and calibrate must then validate that dev-only binding"
        ),
    }


class S1V1Error(RuntimeError):
    """One frozen-input, output-safety, GPU, or replay invariant failed."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class FrozenFile:
    """One exact local input file."""

    path: Path
    sha256: str

    def binding(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class EncoderArm:
    """One encoder initialization and its frozen source artifacts."""

    name: str
    encoder_dir: Path
    config: FrozenFile
    model: FrozenFile
    run_manifest: FrozenFile

    def binding(self) -> dict[str, object]:
        return {
            "name": self.name,
            "encoder_dir": str(self.encoder_dir),
            "files": {
                "config.json": self.config.binding(),
                "model.safetensors": self.model.binding(),
                "run_manifest.json": self.run_manifest.binding(),
            },
        }


@dataclass(frozen=True, slots=True)
class S1V1Inputs:
    """Frozen corpus, tokenizer, encoder, and expected-count bindings."""

    corpus_root: Path
    corpus_manifest: FrozenFile
    train_records: FrozenFile
    validation_records: FrozenFile
    tokenizer_dir: Path
    tokenizer_files: tuple[FrozenFile, ...]
    arms: tuple[EncoderArm, ...]
    train_count: int
    validation_count: int

    def binding(self) -> dict[str, object]:
        return {
            "corpus_root": str(self.corpus_root),
            "corpus_manifest": self.corpus_manifest.binding(),
            "train_records": self.train_records.binding(),
            "validation_records": self.validation_records.binding(),
            "tokenizer_dir": str(self.tokenizer_dir),
            "tokenizer_files": {
                frozen.path.name: frozen.binding() for frozen in self.tokenizer_files
            },
            "arms": [arm.binding() for arm in self.arms],
            "expected_counts": {
                "train": self.train_count,
                "validation": self.validation_count,
            },
        }


_CORPUS_ROOT = Path("/storage/milikic/leanfaith/corpus2/v1_ed41471")
_CHUNKS_ENCODER = Path("/storage/milikic/leanfaith/cpt/modernbert_lean_v1_run1")
_MIXED_ENCODER = Path("/storage/milikic/leanfaith/cpt/modernbert_lean_v2_mixed")

PRODUCTION_INPUTS = S1V1Inputs(
    corpus_root=_CORPUS_ROOT,
    corpus_manifest=FrozenFile(
        _CORPUS_ROOT / "corpus_v1_manifest.json",
        "22386b7127c80fab6ce70df722ecc155ee3a3520971515ebefee6cb438a20a01",
    ),
    train_records=FrozenFile(
        _CORPUS_ROOT / "records_train_v1.jsonl",
        "51ad67e42d5d350be0219ff26142e24ac1b7f8dfbfc652a1355430e46f5d6c4b",
    ),
    validation_records=FrozenFile(
        _CORPUS_ROOT / "records_validation_v1.jsonl",
        "a5939fee4df3363fec1c3285623ca18509c549fbf65e73f2ec9a741af5505470",
    ),
    tokenizer_dir=_CHUNKS_ENCODER,
    tokenizer_files=(
        FrozenFile(
            _CHUNKS_ENCODER / "tokenizer.json",
            "c7a995f78d60cc3c253902f4b5becfe2f9d0b44f78e6e2f81a343a0cb71789e6",
        ),
        FrozenFile(
            _CHUNKS_ENCODER / "tokenizer_config.json",
            "2966a59b9e9cf122279aec1249e22e5bc7ad8430c754e95031b13fd128d4e560",
        ),
        FrozenFile(
            _CHUNKS_ENCODER / "special_tokens_map.json",
            "ea97ecdbcc73713039d8d64dbb05e3689495c96657fbd9a18f5bed381be81049",
        ),
    ),
    arms=(
        EncoderArm(
            name="cpt_chunks",
            encoder_dir=_CHUNKS_ENCODER,
            config=FrozenFile(
                _CHUNKS_ENCODER / "config.json",
                "a6498f40224133a0917ef46c995a6a50521526d8d45a28f5d13a31b677a3d4d3",
            ),
            model=FrozenFile(
                _CHUNKS_ENCODER / "model.safetensors",
                "f66f3ef56dcc0c4854eac28c507140b3706a94c9ece9994eff93b0d8da31ebb0",
            ),
            run_manifest=FrozenFile(
                _CHUNKS_ENCODER / "run_manifest.json",
                "2099673d78fc5badb68f750ee52293396630e0a088c92df437ee70c9e9612e3b",
            ),
        ),
        EncoderArm(
            name="cpt_mixed",
            encoder_dir=_MIXED_ENCODER,
            config=FrozenFile(
                _MIXED_ENCODER / "config.json",
                "a6498f40224133a0917ef46c995a6a50521526d8d45a28f5d13a31b677a3d4d3",
            ),
            model=FrozenFile(
                _MIXED_ENCODER / "model.safetensors",
                "a549de7318bc0aba613ffcbb97114f7b5aad596830492a090d4cb3217fabe88b",
            ),
            run_manifest=FrozenFile(
                _MIXED_ENCODER / "run_manifest.json",
                "ccc3211471e4f2fafa25f30612d7286e1bdd481b1a374ae6a3a456b570d9eceb",
            ),
        ),
    ),
    train_count=18_760,
    validation_count=2_166,
)

TrainerCallable = Callable[[TrainerConfig], TrainerResult]
GpuProbeCallable = Callable[[int, int], dict[str, object]]


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() or "unknown"


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as stream:
            stream.write(canonical_json_bytes(dict(payload)))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise S1V1Error("invalid_manifest", f"cannot read JSON object {path}: {error}") from error
    if not isinstance(raw, dict):
        raise S1V1Error("invalid_manifest", f"expected one JSON object in {path}")
    return cast(dict[str, Any], raw)


def _require_hash(frozen: FrozenFile) -> None:
    if not frozen.path.is_file():
        raise S1V1Error("missing_frozen_input", f"frozen input is missing: {frozen.path}")
    observed = hash_file(frozen.path)
    if observed != frozen.sha256:
        raise S1V1Error(
            "frozen_input_hash_mismatch",
            f"frozen input hash mismatch for {frozen.path}: {observed} != {frozen.sha256}",
        )


def verify_frozen_inputs(inputs: S1V1Inputs = PRODUCTION_INPUTS) -> None:
    """Hash every corpus, tokenizer, and encoder artifact before GPU loading."""

    if tuple(arm.name for arm in inputs.arms) != _ARM_ORDER:
        raise S1V1Error("arm_order_mismatch", "encoder arms must be cpt_chunks then cpt_mixed")
    frozen_files = [
        inputs.corpus_manifest,
        inputs.train_records,
        inputs.validation_records,
        *inputs.tokenizer_files,
    ]
    for arm in inputs.arms:
        frozen_files.extend((arm.config, arm.model, arm.run_manifest))
    for frozen in frozen_files:
        _require_hash(frozen)

    manifest = _read_json_object(inputs.corpus_manifest.path)
    counts = manifest.get("counts")
    outputs = manifest.get("outputs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("method_version") != "corpus_v1_track_d_merge_v1"
        or not isinstance(counts, dict)
        or not isinstance(outputs, dict)
        or counts.get("split")
        != {
            "test": 2_488,
            "train": inputs.train_count,
            "validation": inputs.validation_count,
        }
    ):
        raise S1V1Error("corpus_manifest_mismatch", "corpus-v1 manifest is not the frozen run")
    for name, frozen in (
        ("records_train_v1.jsonl", inputs.train_records),
        ("records_validation_v1.jsonl", inputs.validation_records),
    ):
        entry = outputs.get(name)
        if not isinstance(entry, dict) or entry.get("sha256") != frozen.sha256:
            raise S1V1Error("corpus_manifest_mismatch", f"corpus-v1 manifest does not bind {name}")


def _trainer_recipe() -> dict[str, object]:
    return {
        "seq_len": 1024,
        "epochs": 2,
        "batch_size": 8,
        "grad_accum": 4,
        "lr": 2e-5,
        "weight_decay": 0.01,
        "warmup_ratio": 0.1,
        "seed": 20260828,
        "device": "cuda",
        "bf16": True,
        "early_stop_metric": "auprc",
        "swap_orientation": "augment",
        "class_balance": "weighted",
        "label_smoothing": 0.0,
        "max_records": None,
        "init_state_safetensors": None,
        "holdout_families": [],
    }


def _trainer_config(inputs: S1V1Inputs, arm: EncoderArm, out_dir: Path) -> TrainerConfig:
    return TrainerConfig.model_validate(
        {
            "records_jsonl": inputs.train_records.path,
            "val_records_jsonl": inputs.validation_records.path,
            "encoder_init_dir": arm.encoder_dir,
            "tokenizer_dir": inputs.tokenizer_dir,
            "out_dir": out_dir,
            **_trainer_recipe(),
        }
    )


def _implementation_sha256() -> str:
    return hash_file(Path(__file__))


def _plan(
    inputs: S1V1Inputs,
    git_revision: str,
    *,
    gpu_lock_path: Path,
    gpu_index: int,
    max_idle_memory_mib: int,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "method_version": METHOD_VERSION,
        "git_revision": git_revision,
        "implementation": {
            "path": str(Path(__file__).resolve()),
            "sha256": _implementation_sha256(),
        },
        "inputs": inputs.binding(),
        "arm_order": list(_ARM_ORDER),
        "trainer_recipe": _trainer_recipe(),
        "gpu_runtime": {
            "lock_path": str(gpu_lock_path),
            "index": gpu_index,
            "max_idle_memory_mib": max_idle_memory_mib,
            "lock_mode": "exclusive_nonblocking",
        },
        "offline_environment": {
            "CUDA_VISIBLE_DEVICES": str(gpu_index),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        },
        "resume_policy": "completed_arm_reverify_partial_arm_new_attempt_v1",
        "private_source_content": True,
        "external_transmission_allowed": False,
    }
    plan["plan_sha256"] = sha256_hex(canonical_json_bytes(plan))
    return plan


def _new_queue_manifest(plan: Mapping[str, object], output_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "running",
        "output_root": str(output_root),
        "plan": dict(plan),
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "finished_at": None,
        "gpu_preflight": None,
        "arms": {
            name: {"status": "pending", "attempts": [], "completed_attempt": None}
            for name in _ARM_ORDER
        },
        "outputs": {},
        "golden_dev_evaluation": _blocked_dev_evaluation(),
    }


def _validate_storage_output(output_root: Path, *, enforce_storage_root: bool) -> Path:
    if output_root.is_symlink():
        raise S1V1Error("unsafe_output_root", "output root must not be a symlink")
    resolved = output_root.resolve()
    if enforce_storage_root and not resolved.is_relative_to(_STORAGE_ROOT):
        raise S1V1Error("unsafe_output_root", "S1 artifacts must be under /storage/milikic")
    return resolved


def _open_queue_root(output_root: Path, plan: Mapping[str, object]) -> dict[str, Any]:
    manifest_path = output_root / QUEUE_MANIFEST_FILENAME
    entries = {path.name for path in output_root.iterdir() if path.name != QUEUE_LOCK_FILENAME}
    if not entries:
        manifest = _new_queue_manifest(plan, output_root)
        _atomic_json(manifest_path, manifest)
        return manifest
    if not manifest_path.is_file():
        raise S1V1Error(
            "unsafe_nonempty_output",
            "nonempty output root lacks this runner's queue manifest; refusing overwrite",
        )
    manifest = _read_json_object(manifest_path)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("method_version") != METHOD_VERSION
        or manifest.get("output_root") != str(output_root)
        or manifest.get("plan") != dict(plan)
    ):
        raise S1V1Error(
            "nonmatching_output_manifest",
            "existing output root belongs to a different code revision, input set, or recipe",
        )
    if manifest.get("golden_dev_evaluation") != _blocked_dev_evaluation():
        raise S1V1Error(
            "evaluation_block_manifest_mismatch",
            "existing output root does not preserve the literal-seal evaluation blocker",
        )
    if not isinstance(manifest.get("arms"), dict):
        raise S1V1Error("invalid_manifest", "queue manifest lacks an arms object")
    return manifest


def _set_queue_status(
    output_root: Path,
    manifest: dict[str, Any],
    status: Literal["running", "completed", "failed"],
) -> None:
    manifest["status"] = status
    manifest["updated_at"] = _utc_now()
    if status == "completed":
        manifest["finished_at"] = _utc_now()
    _atomic_json(output_root / QUEUE_MANIFEST_FILENAME, manifest)


def _record_failure(
    output_root: Path,
    manifest: dict[str, Any],
    error: BaseException,
    *,
    stage: str,
    arm_name: str | None,
) -> None:
    reason_code = error.reason_code if isinstance(error, S1V1Error) else "unexpected_exception"
    failure: dict[str, object] = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "failed",
        "reason_code": reason_code,
        "reason": str(error),
        "exception_type": type(error).__name__,
        "stage": stage,
        "arm": arm_name,
        "failed_at": _utc_now(),
        "plan_sha256": cast(Mapping[str, object], manifest["plan"])["plan_sha256"],
    }
    manifest["latest_failure"] = failure
    _set_queue_status(output_root, manifest, "failed")
    _atomic_json(output_root / FAILURE_MANIFEST_FILENAME, failure)


def probe_idle_gpu(index: int, max_idle_memory_mib: int) -> dict[str, object]:
    """Return a recorded nvidia-smi snapshot or fail before model loading."""

    gpu = subprocess.run(
        [
            "nvidia-smi",
            f"--id={index}",
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    processes = subprocess.run(
        [
            "nvidia-smi",
            f"--id={index}",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if gpu.returncode != 0 or processes.returncode != 0:
        detail = (gpu.stderr or processes.stderr).strip()
        raise S1V1Error("nvidia_smi_failed", f"nvidia-smi preflight failed: {detail}")
    fields = [field.strip() for field in gpu.stdout.strip().split(",")]
    if len(fields) != 6:
        raise S1V1Error("nvidia_smi_parse_failed", "unexpected nvidia-smi GPU query output")
    try:
        used_memory_mib = int(fields[4])
    except ValueError as error:
        raise S1V1Error("nvidia_smi_parse_failed", "invalid used-memory field") from error
    compute_apps = [line.strip() for line in processes.stdout.splitlines() if line.strip()]
    idle = not compute_apps and used_memory_mib <= max_idle_memory_mib
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "checked_at": _utc_now(),
        "gpu_index": index,
        "gpu_uuid": fields[1],
        "gpu_name": fields[2],
        "memory_total_mib": int(fields[3]),
        "memory_used_mib": used_memory_mib,
        "utilization_percent": int(fields[5]),
        "compute_apps": compute_apps,
        "max_idle_memory_mib": max_idle_memory_mib,
        "idle": idle,
    }
    if not idle:
        raise S1V1Error(
            "gpu_not_idle",
            f"GPU {index} is not idle: memory_used={used_memory_mib} MiB, "
            f"compute_apps={len(compute_apps)}",
        )
    return snapshot


def _attempts(arm_state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = arm_state.get("attempts")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise S1V1Error("invalid_manifest", "arm attempts must be a list of objects")
    return cast(list[dict[str, Any]], raw)


def _mark_stale_attempts(manifest: dict[str, Any]) -> None:
    arms = cast(dict[str, Any], manifest["arms"])
    for arm_name in _ARM_ORDER:
        state = cast(dict[str, Any], arms[arm_name])
        for attempt in _attempts(state):
            if attempt.get("status") == "running":
                attempt["status"] = "interrupted"
                attempt["finished_at"] = _utc_now()
                attempt["reason"] = "prior runner ended before a completed trainer manifest"
        if state.get("status") == "running":
            state["status"] = "interrupted"


def _verify_completed_attempt(
    attempt_dir: Path,
    *,
    inputs: S1V1Inputs,
    arm: EncoderArm,
    expected_git_revision: str,
) -> dict[str, object]:
    status_path = attempt_dir / ATTEMPT_STATUS_FILENAME
    trainer_manifest_path = attempt_dir / MANIFEST_FILENAME
    for path in (status_path, trainer_manifest_path):
        if not path.is_file():
            raise S1V1Error("partial_arm_output", f"completed arm is missing {path}")
    status = _read_json_object(status_path)
    trainer_manifest = _read_json_object(trainer_manifest_path)
    config = _trainer_config(inputs, arm, attempt_dir)
    expected_config = config.model_dump(mode="json")
    expected_steps = math.ceil(inputs.train_count / (config.batch_size * config.grad_accum))
    expected_steps *= config.epochs
    expected_warmup = round(expected_steps * config.warmup_ratio)
    if (
        status.get("status") != "completed"
        or status.get("arm") != arm.name
        or status.get("trainer_config") != expected_config
        or trainer_manifest.get("kind") != "m1_sft_run"
        or trainer_manifest.get("config") != expected_config
        or trainer_manifest.get("git_revision") != expected_git_revision
        or trainer_manifest.get("input_sha256")
        != {
            "records_jsonl": inputs.train_records.sha256,
            "val_records_jsonl": inputs.validation_records.sha256,
        }
        or trainer_manifest.get("record_counts")
        != {"train": inputs.train_count, "validation": inputs.validation_count, "holdout": 0}
        or trainer_manifest.get("optimizer_steps") != expected_steps
        or trainer_manifest.get("warmup_steps") != expected_warmup
    ):
        raise S1V1Error("completed_arm_manifest_mismatch", f"arm manifest differs: {arm.name}")
    checkpoint_hashes = trainer_manifest.get("checkpoint_sha256")
    if not isinstance(checkpoint_hashes, dict):
        raise S1V1Error("completed_arm_manifest_mismatch", "checkpoint hashes are missing")
    best = attempt_dir / BEST_CHECKPOINT_FILENAME
    last = attempt_dir / LAST_CHECKPOINT_FILENAME
    if (
        not best.is_file()
        or not last.is_file()
        or checkpoint_hashes.get("best") != hash_file(best)
        or checkpoint_hashes.get("last") != hash_file(last)
    ):
        raise S1V1Error("checkpoint_hash_mismatch", f"checkpoint replay failed: {arm.name}")
    output_files: dict[str, dict[str, str]] = {}
    for name in _OUTPUT_MODEL_FILES:
        path = attempt_dir / name
        if not path.is_file():
            raise S1V1Error("partial_arm_output", f"completed arm is missing {path}")
        output_files[name] = {"path": str(path), "sha256": hash_file(path)}
    return {
        "attempt_dir": str(attempt_dir),
        "trainer_manifest": {
            "path": str(trainer_manifest_path),
            "sha256": hash_file(trainer_manifest_path),
        },
        "best_checkpoint": {"path": str(best), "sha256": hash_file(best)},
        "last_checkpoint": {"path": str(last), "sha256": hash_file(last)},
        "model_files": output_files,
    }


def _completed_output(
    manifest: dict[str, Any],
    *,
    inputs: S1V1Inputs,
    arm: EncoderArm,
    expected_git_revision: str,
) -> dict[str, object] | None:
    arms = cast(dict[str, Any], manifest["arms"])
    state = cast(dict[str, Any], arms[arm.name])
    completed = state.get("completed_attempt")
    if completed is None:
        return None
    if not isinstance(completed, str):
        raise S1V1Error("invalid_manifest", "completed_attempt must be a path string")
    output = _verify_completed_attempt(
        Path(completed), inputs=inputs, arm=arm, expected_git_revision=expected_git_revision
    )
    state["status"] = "completed"
    return output


def _new_attempt(
    output_root: Path,
    manifest: dict[str, Any],
    *,
    inputs: S1V1Inputs,
    arm: EncoderArm,
) -> tuple[Path, dict[str, Any], TrainerConfig]:
    arms = cast(dict[str, Any], manifest["arms"])
    state = cast(dict[str, Any], arms[arm.name])
    attempts = _attempts(state)
    index = len(attempts) + 1
    attempt_dir = output_root / "attempts" / arm.name / f"attempt_{index:03d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    config = _trainer_config(inputs, arm, attempt_dir)
    attempt: dict[str, Any] = {
        "index": index,
        "arm": arm.name,
        "path": str(attempt_dir),
        "status": "running",
        "started_at": _utc_now(),
        "finished_at": None,
        "trainer_config": config.model_dump(mode="json"),
    }
    attempts.append(attempt)
    state["status"] = "running"
    _atomic_json(attempt_dir / ATTEMPT_STATUS_FILENAME, attempt)
    return attempt_dir, attempt, config


def _lock_nonblocking(path: Path, *, reason_code: str) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise S1V1Error(reason_code, f"exclusive lock is already held: {path}") from error
    return handle


def _unlock(handle: Any) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _offline_environment(gpu_index: int) -> None:
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


def run_s1_v1(
    output_root: Path,
    *,
    inputs: S1V1Inputs = PRODUCTION_INPUTS,
    gpu_lock_path: Path = DEFAULT_GPU_LOCK_PATH,
    gpu_index: int = DEFAULT_GPU_INDEX,
    max_idle_memory_mib: int = DEFAULT_MAX_IDLE_MEMORY_MIB,
    trainer: TrainerCallable = run_trainer,
    gpu_probe: GpuProbeCallable = probe_idle_gpu,
    enforce_storage_root: bool = True,
) -> dict[str, Any]:
    """Run or arm-level resume the exact frozen two-arm S1-v1 recipe."""

    output_root = _validate_storage_output(output_root, enforce_storage_root=enforce_storage_root)
    if gpu_index < 0 or max_idle_memory_mib < 0:
        raise S1V1Error("invalid_gpu_config", "GPU index and idle-memory limit must be nonnegative")
    if enforce_storage_root and gpu_lock_path.resolve() != DEFAULT_GPU_LOCK_PATH:
        raise S1V1Error(
            "invalid_gpu_config",
            f"production runs require the shared GPU lock {DEFAULT_GPU_LOCK_PATH}",
        )
    output_root.mkdir(parents=True, exist_ok=True)
    queue_lock = _lock_nonblocking(output_root / QUEUE_LOCK_FILENAME, reason_code="queue_lock_busy")
    manifest: dict[str, Any] | None = None
    stage = "initialize"
    active_arm: str | None = None
    try:
        git_revision = _git_revision()
        plan = _plan(
            inputs,
            git_revision,
            gpu_lock_path=gpu_lock_path,
            gpu_index=gpu_index,
            max_idle_memory_mib=max_idle_memory_mib,
        )
        manifest = _open_queue_root(output_root, plan)
        stage = "verify_frozen_inputs"
        verify_frozen_inputs(inputs)
        _mark_stale_attempts(manifest)

        outputs: dict[str, object] = {}
        remaining: list[EncoderArm] = []
        for arm in inputs.arms:
            completed = _completed_output(
                manifest,
                inputs=inputs,
                arm=arm,
                expected_git_revision=git_revision,
            )
            if completed is None:
                remaining.append(arm)
            else:
                outputs[arm.name] = completed
        manifest["outputs"] = outputs
        if not remaining:
            _set_queue_status(output_root, manifest, "completed")
            return manifest

        stage = "acquire_gpu_lock"
        gpu_lock = _lock_nonblocking(gpu_lock_path, reason_code="gpu_lock_busy")
        try:
            stage = "gpu_preflight"
            snapshot = gpu_probe(gpu_index, max_idle_memory_mib)
            if snapshot.get("idle") is not True:
                raise S1V1Error("gpu_not_idle", "GPU probe did not certify an idle device")
            preflight_path = output_root / PREFLIGHT_FILENAME
            _atomic_json(preflight_path, snapshot)
            manifest["gpu_preflight"] = {
                "path": str(preflight_path),
                "sha256": hash_file(preflight_path),
            }
            _offline_environment(gpu_index)
            _set_queue_status(output_root, manifest, "running")

            for arm in remaining:
                active_arm = arm.name
                stage = "train_arm"
                attempt_dir, attempt, config = _new_attempt(
                    output_root, manifest, inputs=inputs, arm=arm
                )
                _set_queue_status(output_root, manifest, "running")
                try:
                    trainer(config)
                    attempt["status"] = "completed"
                    attempt["finished_at"] = _utc_now()
                    _atomic_json(attempt_dir / ATTEMPT_STATUS_FILENAME, attempt)
                    completed = _verify_completed_attempt(
                        attempt_dir,
                        inputs=inputs,
                        arm=arm,
                        expected_git_revision=git_revision,
                    )
                except BaseException as error:
                    attempt["status"] = "failed"
                    attempt["finished_at"] = _utc_now()
                    attempt["reason"] = str(error)
                    _atomic_json(attempt_dir / ATTEMPT_STATUS_FILENAME, attempt)
                    arms = cast(dict[str, Any], manifest["arms"])
                    cast(dict[str, Any], arms[arm.name])["status"] = "failed"
                    raise
                arms = cast(dict[str, Any], manifest["arms"])
                arm_state = cast(dict[str, Any], arms[arm.name])
                arm_state["status"] = "completed"
                arm_state["completed_attempt"] = str(attempt_dir)
                outputs[arm.name] = completed
                manifest["outputs"] = outputs
                _set_queue_status(output_root, manifest, "running")
                print(
                    f"[s1-v1] completed {arm.name}: "
                    f"{cast(Mapping[str, str], completed['best_checkpoint'])['path']}",
                    flush=True,
                )

            active_arm = None
            stage = "final_verify"
            for arm in inputs.arms:
                completed = _completed_output(
                    manifest,
                    inputs=inputs,
                    arm=arm,
                    expected_git_revision=git_revision,
                )
                if completed is None:
                    raise S1V1Error("partial_queue_output", f"arm did not complete: {arm.name}")
                outputs[arm.name] = completed
            manifest["outputs"] = outputs
            _set_queue_status(output_root, manifest, "completed")
            print(f"[s1-v1] all arms complete: {output_root}", flush=True)
            return manifest
        finally:
            _unlock(gpu_lock)
    except BaseException as error:
        if manifest is not None:
            _record_failure(output_root, manifest, error, stage=stage, arm_name=active_arm)
        raise
    finally:
        _unlock(queue_lock)


def verify_s1_v1(
    output_root: Path,
    *,
    inputs: S1V1Inputs = PRODUCTION_INPUTS,
    enforce_storage_root: bool = True,
) -> dict[str, Any]:
    """Replay a completed queue without acquiring or inspecting the GPU."""

    output_root = _validate_storage_output(output_root, enforce_storage_root=enforce_storage_root)
    manifest = _read_json_object(output_root / QUEUE_MANIFEST_FILENAME)
    plan = manifest.get("plan")
    if not isinstance(plan, dict) or manifest.get("status") != "completed":
        raise S1V1Error("queue_not_completed", "queue manifest is not completed")
    if manifest.get("golden_dev_evaluation") != _blocked_dev_evaluation():
        raise S1V1Error(
            "evaluation_block_manifest_mismatch",
            "completed queue does not preserve the literal-seal dev-evaluation blocker",
        )
    recorded_revision = plan.get("git_revision")
    if (
        not isinstance(recorded_revision, str)
        or len(recorded_revision) != 40
        or any(character not in "0123456789abcdef" for character in recorded_revision)
    ):
        raise S1V1Error("invalid_manifest", "queue plan lacks a full git revision")
    gpu_runtime = plan.get("gpu_runtime")
    if not isinstance(gpu_runtime, dict):
        raise S1V1Error("invalid_manifest", "queue plan lacks GPU runtime provenance")
    lock_path = gpu_runtime.get("lock_path")
    gpu_index = gpu_runtime.get("index")
    max_idle_memory_mib = gpu_runtime.get("max_idle_memory_mib")
    if (
        not isinstance(lock_path, str)
        or not isinstance(gpu_index, int)
        or isinstance(gpu_index, bool)
        or not isinstance(max_idle_memory_mib, int)
        or isinstance(max_idle_memory_mib, bool)
    ):
        raise S1V1Error("invalid_manifest", "queue plan has invalid GPU runtime provenance")
    if plan != _plan(
        inputs,
        recorded_revision,
        gpu_lock_path=Path(lock_path),
        gpu_index=gpu_index,
        max_idle_memory_mib=max_idle_memory_mib,
    ):
        raise S1V1Error("nonmatching_output_manifest", "completed queue plan no longer matches")
    verify_frozen_inputs(inputs)
    outputs: dict[str, object] = {}
    for arm in inputs.arms:
        completed = _completed_output(
            manifest,
            inputs=inputs,
            arm=arm,
            expected_git_revision=recorded_revision,
        )
        if completed is None:
            raise S1V1Error("partial_queue_output", f"arm did not complete: {arm.name}")
        outputs[arm.name] = completed
    if manifest.get("outputs") != outputs:
        raise S1V1Error("queue_output_manifest_mismatch", "queue output bindings differ")
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run or arm-level resume the two-arm queue")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--gpu-lock", type=Path, default=DEFAULT_GPU_LOCK_PATH)
    run.add_argument("--gpu-index", type=int, default=DEFAULT_GPU_INDEX)
    run.add_argument("--max-idle-memory-mib", type=int, default=DEFAULT_MAX_IDLE_MEMORY_MIB)
    verify = subparsers.add_parser("verify", help="replay a completed queue without GPU access")
    verify.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run_s1_v1(
                args.output_root,
                gpu_lock_path=args.gpu_lock,
                gpu_index=args.gpu_index,
                max_idle_memory_mib=args.max_idle_memory_mib,
            )
        else:
            verify_s1_v1(args.output_root)
    except S1V1Error as error:
        print(f"s1-v1 failed [{error.reason_code}]: {error}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GPU_LOCK_PATH",
    "METHOD_VERSION",
    "PRODUCTION_INPUTS",
    "EncoderArm",
    "FrozenFile",
    "S1V1Error",
    "S1V1Inputs",
    "main",
    "probe_idle_gpu",
    "run_s1_v1",
    "verify_frozen_inputs",
    "verify_s1_v1",
]

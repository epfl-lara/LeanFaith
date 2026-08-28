"""Track T-S1/S2 trainer for the packed M1 same-claim cross-encoder."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.eval.metrics import compute_classification_metrics
from leanfaith.models.m1_cross_encoder import build_m1_cross_encoder_module

MANIFEST_FILENAME = "run_manifest.json"
BEST_CHECKPOINT_FILENAME = "best.safetensors"
LAST_CHECKPOINT_FILENAME = "last.safetensors"
_TRACKED_PACKAGES = ("torch", "transformers", "safetensors", "pydantic")
_VALIDATION_GROUP_FRACTION = 0.10


class TrainerConfig(BaseModel):
    """Configuration recorded verbatim for one S1/S2 training run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records_jsonl: Path
    val_records_jsonl: Path | None = None
    encoder_init_dir: Path
    tokenizer_dir: Path
    out_dir: Path
    seq_len: int = Field(default=1024, gt=0)
    epochs: int = Field(default=2, gt=0)
    batch_size: int = Field(default=8, gt=0)
    grad_accum: int = Field(default=1, gt=0)
    lr: float = Field(default=2e-5, gt=0.0)
    weight_decay: float = Field(default=0.01, ge=0.0)
    warmup_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    seed: int = Field(default=0, ge=0)
    device: str = Field(default="cuda", min_length=1)
    bf16: bool = True
    early_stop_metric: Literal["auprc", "balanced_accuracy", "accuracy"] = "auprc"
    swap_orientation: Literal["augment", "average", "off"] = "augment"
    class_balance: Literal["weighted", "sampled", "off"] = "weighted"
    label_smoothing: float = Field(default=0.0, ge=0.0, lt=1.0)
    max_records: int | None = Field(default=None, gt=0)
    init_state_safetensors: Path | None = None
    holdout_families: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_tokenizer_dir(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(cast(Mapping[str, object], value))
        if data.get("tokenizer_dir") is None and data.get("encoder_init_dir") is not None:
            data["tokenizer_dir"] = data["encoder_init_dir"]
        return data

    @field_validator("holdout_families")
    @classmethod
    def _valid_holdout_families(cls, value: list[str]) -> list[str]:
        if any(not family.strip() for family in value):
            raise ValueError("holdout_families must contain non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("holdout_families must not contain duplicates")
        return value


class TrainingRecord(BaseModel):
    """Frozen input row emitted by corpus2; unknown provenance fields are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)

    record_id: str = Field(min_length=1)
    reference_headless: str = Field(min_length=1)
    candidate_headless: str = Field(min_length=1)
    label: StrictBool
    group_key: str = Field(min_length=1)
    family: str | None = None
    source: str | None = None
    weight: float | None = Field(default=None, gt=0.0)

    @field_validator("record_id", "reference_headless", "candidate_headless", "group_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be blank")
        return value


@dataclasses.dataclass(frozen=True, slots=True)
class MetricReport:
    loss: float
    auprc: float
    balanced_accuracy: float
    accuracy: float
    swap_disagreement: float
    record_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class TrainerResult:
    out_dir: Path
    manifest_path: Path
    best_checkpoint: Path
    last_checkpoint: Path
    best_epoch: int
    best_metric: float
    initial_train_loss: float
    final_train_loss: float
    history: tuple[dict[str, object], ...]


def load_records(path: Path, max_records: int | None = None) -> list[TrainingRecord]:
    """Load validated JSONL rows, preserving file order and useful line errors."""

    records: list[TrainingRecord] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = TrainingRecord.model_validate_json(raw)
            except (ValidationError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid training record: {exc}") from exc
            if record.record_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate record_id {record.record_id!r}")
            seen.add(record.record_id)
            records.append(record)
            if max_records is not None and len(records) >= max_records:
                break
    if not records:
        raise ValueError(f"{path}: no records found")
    return records


def orientation_for_record(record: TrainingRecord, *, seed: int, epoch: int) -> bool:
    """Return a stable pseudo-random swap decision for one record and epoch."""

    payload = f"{seed}\0{epoch}\0{record.record_id}".encode()
    return bool(hashlib.sha256(payload).digest()[0] & 1)


def compute_class_weights(records: Sequence[TrainingRecord]) -> dict[bool, float]:
    """Return inverse-frequency weights normalized to mean one over rows."""

    counts = Counter(record.label for record in records)
    if counts[False] == 0 or counts[True] == 0:
        raise ValueError("training records must contain both labels")
    total = len(records)
    return {label: total / (2.0 * counts[label]) for label in (False, True)}


def _epoch_indices(
    records: Sequence[TrainingRecord], *, mode: str, seed: int, epoch: int
) -> list[int]:
    rng = random.Random((seed << 32) + epoch)
    if mode != "sampled":
        indices = list(range(len(records)))
        rng.shuffle(indices)
        return indices
    by_label = {
        label: [i for i, row in enumerate(records) if row.label is label] for label in (False, True)
    }
    if not by_label[False] or not by_label[True]:
        raise ValueError("balanced sampling requires both labels")
    target = max(len(by_label[False]), len(by_label[True]))
    sampled_indices: list[int] = []
    for label in (False, True):
        sampled_indices.extend(by_label[label])
        sampled_indices.extend(
            rng.choice(by_label[label]) for _ in range(target - len(by_label[label]))
        )
    rng.shuffle(sampled_indices)
    return sampled_indices


def _split_records(
    config: TrainerConfig, records: Sequence[TrainingRecord]
) -> tuple[list[TrainingRecord], list[TrainingRecord], list[TrainingRecord]]:
    holdout_names = set(config.holdout_families)
    holdout = [row for row in records if row.family in holdout_names]
    eligible = [row for row in records if row.family not in holdout_names]
    if config.val_records_jsonl is not None:
        val_rows = load_records(config.val_records_jsonl)
        holdout.extend(row for row in val_rows if row.family in holdout_names)
        validation = [row for row in val_rows if row.family not in holdout_names]
        train = eligible
    else:
        groups = sorted({row.group_key for row in eligible})
        if len(groups) < 2:
            raise ValueError("group-based validation split requires at least two eligible groups")
        random.Random(config.seed).shuffle(groups)
        val_count = min(len(groups) - 1, max(1, round(len(groups) * _VALIDATION_GROUP_FRACTION)))
        val_groups = set(groups[:val_count])
        train = [row for row in eligible if row.group_key not in val_groups]
        validation = [row for row in eligible if row.group_key in val_groups]
    if not train or not validation:
        raise ValueError("training and validation subsets must both be non-empty")
    if config.holdout_families and not holdout:
        raise ValueError("holdout_families matched no records")
    if {row.group_key for row in train} & {row.group_key for row in validation}:
        raise ValueError("training and validation group_key values overlap")
    all_ids = [row.record_id for row in (*train, *validation, *holdout)]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("record_id values overlap across train/validation/holdout inputs")
    compute_class_weights(train)
    return train, validation, holdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _package_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "unknown"
    return versions


def _texts(rows: Sequence[TrainingRecord], *, swap: bool) -> list[str]:
    return [
        pack_pair(
            row.candidate_headless if swap else row.reference_headless,
            row.reference_headless if swap else row.candidate_headless,
        )
        for row in rows
    ]


def _tensorize(
    tokenizer: Any, texts: Sequence[str], *, seq_len: int, device: Any
) -> dict[str, Any]:
    if any(len(tokenizer.encode(text, add_special_tokens=True)) > seq_len for text in texts):
        raise ValueError("packed pair exceeds seq_len; trainer does not silently truncate")
    packed = tokenizer(
        list(texts), padding=True, truncation=True, max_length=seq_len, return_tensors="pt"
    )
    if not {"input_ids", "attention_mask"}.issubset(packed):
        raise ValueError("tokenizer output lacks input_ids or attention_mask")
    return {name: packed[name].to(device) for name in ("input_ids", "attention_mask")}


def _forward_logits(model: Any, batch: Mapping[str, Any]) -> Any:
    output = cast(Mapping[str, Any], model(**batch))
    logits = output.get("logits")
    if logits is None or logits.ndim != 1:
        raise ValueError("M1 model returned incompatible logits")
    return logits


def _batch_logits(
    model: Any,
    tokenizer: Any,
    rows: Sequence[TrainingRecord],
    *,
    config: TrainerConfig,
    device: Any,
    epoch: int,
    training: bool,
) -> tuple[Any, Any, Any]:
    canonical = _texts(rows, swap=False)
    swapped = _texts(rows, swap=True)
    if training and config.swap_orientation != "average":
        chosen = canonical
        if config.swap_orientation == "augment":
            chosen = [
                swapped[i]
                if orientation_for_record(row, seed=config.seed, epoch=epoch)
                else canonical[i]
                for i, row in enumerate(rows)
            ]
        logits = _forward_logits(
            model, _tensorize(tokenizer, chosen, seq_len=config.seq_len, device=device)
        )
        return logits, logits, logits
    joined = canonical + swapped
    both = _forward_logits(
        model, _tensorize(tokenizer, joined, seq_len=config.seq_len, device=device)
    )
    forward, reverse = both[: len(rows)], both[len(rows) :]
    selected = (forward + reverse) / 2.0 if config.swap_orientation == "average" else forward
    return selected, forward, reverse


def _loss(
    logits: Any,
    rows: Sequence[TrainingRecord],
    *,
    class_weights: Mapping[bool, float],
    smoothing: float,
    torch: Any,
) -> Any:
    labels = torch.tensor([float(row.label) for row in rows], device=logits.device)
    targets = labels * (1.0 - smoothing) + 0.5 * smoothing
    weights = torch.tensor(
        [(row.weight or 1.0) * class_weights[row.label] for row in rows],
        device=logits.device,
    )
    losses = torch.nn.functional.binary_cross_entropy_with_logits(logits, targets, reduction="none")
    return (losses * weights).sum() / weights.sum()


def _evaluate(
    model: Any,
    tokenizer: Any,
    rows: Sequence[TrainingRecord],
    *,
    config: TrainerConfig,
    device: Any,
) -> MetricReport:
    import torch

    model.eval()
    labels: list[bool] = []
    probabilities: list[float] = []
    disagreements: list[float] = []
    loss_sum = 0.0
    with torch.no_grad():
        for start in range(0, len(rows), config.batch_size):
            batch_rows = rows[start : start + config.batch_size]
            with torch.autocast(
                device_type=device.type,
                dtype=torch.bfloat16,
                enabled=config.bf16 and device.type == "cuda",
            ):
                selected, forward, reverse = _batch_logits(
                    model,
                    tokenizer,
                    batch_rows,
                    config=config,
                    device=device,
                    epoch=0,
                    training=False,
                )
                batch_loss = _loss(
                    selected,
                    batch_rows,
                    class_weights={False: 1.0, True: 1.0},
                    smoothing=config.label_smoothing,
                    torch=torch,
                )
            labels.extend(row.label for row in batch_rows)
            probabilities.extend(float(value) for value in torch.sigmoid(selected).float().cpu())
            disagreements.extend(
                float(value)
                for value in (torch.sigmoid(forward) - torch.sigmoid(reverse)).abs().float().cpu()
            )
            loss_sum += float(batch_loss) * len(batch_rows)
    metrics = compute_classification_metrics(labels, probabilities, threshold=0.5)
    return MetricReport(
        loss=loss_sum / len(rows),
        auprc=metrics["auprc"],
        balanced_accuracy=metrics["balanced_accuracy"],
        accuracy=metrics["accuracy"],
        swap_disagreement=sum(disagreements) / len(disagreements),
        record_count=len(rows),
    )


def _save_state(model: Any, path: Path) -> None:
    from safetensors.torch import save_file

    state = {
        name: tensor.detach().cpu().contiguous() for name, tensor in model.state_dict().items()
    }
    save_file(state, path)


def run_trainer(config: TrainerConfig) -> TrainerResult:
    """Train M1, select the best validation checkpoint, and write a run manifest."""

    import torch
    import transformers
    from safetensors.torch import load_file

    started_at = datetime.now(tz=UTC).isoformat()
    wall_start = time.monotonic()
    torch.manual_seed(config.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(config.seed)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("config.device requests CUDA but torch reports no CUDA device")

    records = load_records(config.records_jsonl, config.max_records)
    train_rows, val_rows, holdout_rows = _split_records(config, records)
    hf = cast(Any, transformers)
    tokenizer = hf.AutoTokenizer.from_pretrained(
        str(config.tokenizer_dir), local_files_only=True, trust_remote_code=False
    )
    encoder = hf.AutoModel.from_pretrained(
        str(config.encoder_init_dir), local_files_only=True, trust_remote_code=False
    )
    if hasattr(encoder.config, "reference_compile"):
        encoder.config.reference_compile = False
    model = cast(
        Any,
        build_m1_cross_encoder_module(encoder=encoder, hidden_size=int(encoder.config.hidden_size)),
    )
    if config.init_state_safetensors is not None:
        model.load_state_dict(load_file(config.init_state_safetensors), strict=True)
    model.to(device)

    no_decay: list[torch.nn.Parameter] = []
    decay: list[torch.nn.Parameter] = []
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        (no_decay if name.endswith("bias") or parameter.ndim <= 1 else decay).append(parameter)
    optimizer = torch.optim.AdamW(
        [
            {"params": decay, "weight_decay": config.weight_decay},
            {"params": no_decay, "weight_decay": 0.0},
        ],
        lr=config.lr,
    )
    base_indices = _epoch_indices(train_rows, mode=config.class_balance, seed=config.seed, epoch=0)
    steps_per_epoch = math.ceil(len(base_indices) / (config.batch_size * config.grad_accum))
    total_steps = steps_per_epoch * config.epochs
    warmup_steps = round(total_steps * config.warmup_ratio)
    scheduler = hf.get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps
    )
    balance_weights = (
        compute_class_weights(train_rows)
        if config.class_balance == "weighted"
        else {False: 1.0, True: 1.0}
    )

    config.out_dir.mkdir(parents=True, exist_ok=True)
    encoder.config.save_pretrained(str(config.out_dir))
    tokenizer.save_pretrained(str(config.out_dir))
    initial_train = _evaluate(model, tokenizer, train_rows, config=config, device=device)
    history: list[dict[str, object]] = []
    best_metric = -math.inf
    best_epoch = 0
    optimizer_steps = 0
    for epoch in range(config.epochs):
        model.train()
        indices = _epoch_indices(
            train_rows, mode=config.class_balance, seed=config.seed, epoch=epoch
        )
        microbatches = [
            indices[start : start + config.batch_size]
            for start in range(0, len(indices), config.batch_size)
        ]
        for window_start in range(0, len(microbatches), config.grad_accum):
            window = microbatches[window_start : window_start + config.grad_accum]
            optimizer.zero_grad(set_to_none=True)
            for batch_indices in window:
                batch_rows = [train_rows[index] for index in batch_indices]
                with torch.autocast(
                    device_type=device.type,
                    dtype=torch.bfloat16,
                    enabled=config.bf16 and device.type == "cuda",
                ):
                    logits, _, _ = _batch_logits(
                        model,
                        tokenizer,
                        batch_rows,
                        config=config,
                        device=device,
                        epoch=epoch,
                        training=True,
                    )
                    loss = _loss(
                        logits,
                        batch_rows,
                        class_weights=balance_weights,
                        smoothing=config.label_smoothing,
                        torch=torch,
                    ) / len(window)
                loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer_steps += 1
        train_report = _evaluate(model, tokenizer, train_rows, config=config, device=device)
        val_report = _evaluate(model, tokenizer, val_rows, config=config, device=device)
        holdout_report = (
            _evaluate(model, tokenizer, holdout_rows, config=config, device=device)
            if holdout_rows
            else None
        )
        entry: dict[str, object] = {
            "epoch": epoch + 1,
            "train": dataclasses.asdict(train_report),
            "validation": dataclasses.asdict(val_report),
            "holdout": dataclasses.asdict(holdout_report) if holdout_report else None,
        }
        history.append(entry)
        metric_value = float(getattr(val_report, config.early_stop_metric))
        if metric_value > best_metric:
            best_metric = metric_value
            best_epoch = epoch + 1
            _save_state(model, config.out_dir / BEST_CHECKPOINT_FILENAME)
        print(
            f"[trainer] epoch {epoch + 1}/{config.epochs} train-loss {train_report.loss:.4f} "
            f"val-{config.early_stop_metric} {metric_value:.4f}",
            flush=True,
        )

    _save_state(model, config.out_dir / LAST_CHECKPOINT_FILENAME)
    final_train = cast(dict[str, object], history[-1]["train"])
    input_shas = {"records_jsonl": _sha256_file(config.records_jsonl)}
    if config.val_records_jsonl is not None:
        input_shas["val_records_jsonl"] = _sha256_file(config.val_records_jsonl)
    if config.init_state_safetensors is not None:
        input_shas["init_state_safetensors"] = _sha256_file(config.init_state_safetensors)
    manifest: dict[str, object] = {
        "kind": "m1_sft_run",
        "config": config.model_dump(mode="json"),
        "git_revision": _git_revision(),
        "input_sha256": input_shas,
        "package_versions": _package_versions(),
        "record_counts": {
            "train": len(train_rows),
            "validation": len(val_rows),
            "holdout": len(holdout_rows),
        },
        "optimizer_steps": optimizer_steps,
        "warmup_steps": warmup_steps,
        "initial_train": dataclasses.asdict(initial_train),
        "history": history,
        "best": {
            "epoch": best_epoch,
            "metric": config.early_stop_metric,
            "value": best_metric,
            "checkpoint": BEST_CHECKPOINT_FILENAME,
        },
        "last_checkpoint": LAST_CHECKPOINT_FILENAME,
        "checkpoint_sha256": {
            "best": _sha256_file(config.out_dir / BEST_CHECKPOINT_FILENAME),
            "last": _sha256_file(config.out_dir / LAST_CHECKPOINT_FILENAME),
        },
        "started_at": started_at,
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "wall_time_seconds": time.monotonic() - wall_start,
    }
    manifest_path = config.out_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return TrainerResult(
        out_dir=config.out_dir,
        manifest_path=manifest_path,
        best_checkpoint=config.out_dir / BEST_CHECKPOINT_FILENAME,
        last_checkpoint=config.out_dir / LAST_CHECKPOINT_FILENAME,
        best_epoch=best_epoch,
        best_metric=best_metric,
        initial_train_loss=initial_train.loss,
        final_train_loss=cast(float, final_train["loss"]),
        history=tuple(history),
    )


__all__ = [
    "BEST_CHECKPOINT_FILENAME",
    "LAST_CHECKPOINT_FILENAME",
    "MANIFEST_FILENAME",
    "MetricReport",
    "TrainerConfig",
    "TrainerResult",
    "TrainingRecord",
    "compute_class_weights",
    "load_records",
    "orientation_for_record",
    "run_trainer",
]

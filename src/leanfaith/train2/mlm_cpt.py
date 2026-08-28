"""Minimal MLM continued-pretraining runner for ModernBERT on Lean text.

Track T-S0(a) deliverable (PLAN.md): a plain runner with no attestation or
replay machinery.  One ordinary run manifest (config, seed, git revision,
input-file SHA-256, package versions) is written next to the checkpoint, and
a held-out slice (the last K input records) gets a masked-LM eval before and
after training so short runs can show a real loss delta.

The tokenizer is FROZEN: this runner never resizes embeddings and never adds
tokens.  The output directory is itself a loadable snapshot
(``model.save_pretrained`` with safetensors + ``tokenizer.save_pretrained``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

TEXT_FIELD_CANDIDATES: tuple[str, ...] = ("text", "content", "source_text")
_TRACKED_PACKAGES: tuple[str, ...] = ("torch", "transformers", "safetensors", "pydantic")
MANIFEST_FILENAME = "run_manifest.json"


class MlmCptConfig(BaseModel):
    """Configuration for one MLM continued-pretraining run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_jsonl: Path
    snapshot_dir: Path
    out_dir: Path
    seq_len: int = Field(default=1024, gt=0)
    mlm_probability: float = Field(default=0.30, gt=0.0, lt=1.0)
    batch_size: int = Field(default=8, gt=0)
    grad_accum: int = Field(default=1, gt=0)
    lr: float = Field(default=3e-5, gt=0.0)
    max_steps: int | None = Field(default=None, gt=0)
    epochs: int | None = Field(default=None, gt=0)
    warmup_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    log_every: int = Field(default=10, gt=0)
    holdout_records: int = Field(default=64, gt=0)
    seed: int = Field(default=0, ge=0)
    device: str = "cuda"
    bf16: bool = True

    @model_validator(mode="after")
    def _exactly_one_duration(self) -> MlmCptConfig:
        if (self.max_steps is None) == (self.epochs is None):
            raise ValueError("set exactly one of max_steps or epochs")
        return self


@dataclasses.dataclass(frozen=True)
class MaskedLmEval:
    """Masked-LM metrics on the held-out slice (fixed masks via fixed seed)."""

    loss: float
    masked_token_accuracy: float
    masked_token_count: int


@dataclasses.dataclass(frozen=True)
class MlmCptResult:
    """Summary of one completed run; full details live in the run manifest."""

    out_dir: Path
    manifest_path: Path
    optimizer_steps: int
    text_field: str
    train_records: int
    holdout_records: int
    eval_before: MaskedLmEval
    eval_after: MaskedLmEval
    logged_losses: tuple[tuple[int, float], ...]


def detect_text_field(record: Mapping[str, object]) -> str:
    """Return the first candidate field holding a non-empty string."""

    for name in TEXT_FIELD_CANDIDATES:
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return name
    raise ValueError(
        f"no usable text field among {TEXT_FIELD_CANDIDATES} "
        f"in record with keys {sorted(str(key) for key in record)}"
    )


def load_text_records(path: Path) -> tuple[list[str], str]:
    """Read a JSONL corpus; the text field is detected from the first record."""

    texts: list[str] = []
    field: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: JSONL record is not an object")
            typed = cast(dict[str, object], record)
            if field is None:
                field = detect_text_field(typed)
            value = typed.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}:{line_number}: field {field!r} is missing or empty")
            texts.append(value)
    if field is None:
        raise ValueError(f"{path}: no records found")
    return texts, field


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


def _cycle(loader: Any) -> Iterator[Any]:
    while True:
        yield from loader


def _evaluate_masked_lm(
    model: Any,
    encodings: Sequence[Mapping[str, Any]],
    collator: Any,
    *,
    batch_size: int,
    device: Any,
    seed: int,
    bf16: bool,
) -> MaskedLmEval:
    """Exact per-masked-token loss + accuracy; the seed fixes the masks."""

    import torch

    torch.manual_seed(seed)
    model.eval()
    loss_sum = 0.0
    masked = 0
    correct = 0
    with torch.no_grad():
        for start in range(0, len(encodings), batch_size):
            rows = [dict(item) for item in encodings[start : start + batch_size]]
            batch = {name: value.to(device) for name, value in collator(rows).items()}
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=bf16):
                output = model(**batch)
            labels = batch["labels"]
            mask = labels != -100
            count = int(mask.sum().item())
            if count == 0:
                continue
            logits = output.logits[mask].float()
            targets = labels[mask]
            loss_sum += float(
                torch.nn.functional.cross_entropy(logits, targets, reduction="sum").item()
            )
            correct += int((logits.argmax(dim=-1) == targets).sum().item())
            masked += count
    if masked == 0:
        raise RuntimeError("held-out masked-LM eval produced no masked tokens")
    return MaskedLmEval(
        loss=loss_sum / masked,
        masked_token_accuracy=correct / masked,
        masked_token_count=masked,
    )


def run_mlm_cpt(config: MlmCptConfig) -> MlmCptResult:
    """Continued MLM pretraining from a local snapshot; standard HF pattern."""

    import torch
    import transformers
    from torch.utils.data import DataLoader

    hf = cast(Any, transformers)
    started_at = datetime.now(tz=UTC).isoformat()

    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("config.device requests CUDA but torch reports no CUDA device")
    use_bf16 = config.bf16

    texts, text_field = load_text_records(config.input_jsonl)
    if len(texts) <= config.holdout_records:
        raise ValueError(
            f"{len(texts)} records cannot cover a {config.holdout_records}-record holdout"
        )
    train_texts = texts[: -config.holdout_records]
    holdout_texts = texts[-config.holdout_records :]

    tokenizer = hf.AutoTokenizer.from_pretrained(
        str(config.snapshot_dir), local_files_only=True, trust_remote_code=False
    )
    model = hf.AutoModelForMaskedLM.from_pretrained(
        str(config.snapshot_dir), local_files_only=True, trust_remote_code=False
    )
    # Frozen tokenizer: never resize embeddings, never add tokens.
    embedding_rows = int(model.get_input_embeddings().weight.shape[0])
    if len(tokenizer) > embedding_rows:
        raise RuntimeError(
            f"tokenizer has {len(tokenizer)} tokens but the model embeds only "
            f"{embedding_rows}; the tokenizer is frozen and embeddings are never resized"
        )
    if hasattr(model.config, "reference_compile"):
        # Skip ModernBERT's optional torch.compile warmup; irrelevant for
        # correctness and dominant in short runs.
        model.config.reference_compile = False
    model.to(device)

    def encode(batch_texts: list[str]) -> list[dict[str, Any]]:
        encoded = tokenizer(batch_texts, truncation=True, max_length=config.seq_len)
        return [{"input_ids": ids} for ids in encoded["input_ids"]]

    train_encodings = encode(train_texts)
    holdout_encodings = encode(holdout_texts)
    collator = hf.DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm=True, mlm_probability=config.mlm_probability
    )

    steps_per_epoch = math.ceil(len(train_encodings) / (config.batch_size * config.grad_accum))
    if config.max_steps is not None:
        total_steps = config.max_steps
    else:
        assert config.epochs is not None  # enforced by the config validator
        total_steps = steps_per_epoch * config.epochs
    warmup_steps = round(total_steps * config.warmup_ratio)

    eval_kwargs: dict[str, Any] = {
        "batch_size": config.batch_size,
        "device": device,
        "seed": config.seed,
        "bf16": use_bf16,
    }
    eval_before = _evaluate_masked_lm(model, holdout_encodings, collator, **eval_kwargs)
    print(
        f"[mlm_cpt] eval before: loss {eval_before.loss:.4f} "
        f"masked-acc {eval_before.masked_token_accuracy:.4f} "
        f"({eval_before.masked_token_count} masked tokens)",
        flush=True,
    )

    torch.manual_seed(config.seed)
    generator = torch.Generator().manual_seed(config.seed)
    loader = DataLoader(
        cast(Any, train_encodings),
        batch_size=config.batch_size,
        shuffle=True,
        generator=generator,
        collate_fn=collator,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.lr)
    scheduler = hf.get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps
    )

    model.train()
    logged: list[tuple[int, float]] = []
    batches = _cycle(loader)
    for step in range(1, total_steps + 1):
        optimizer.zero_grad(set_to_none=True)
        step_loss = 0.0
        for _ in range(config.grad_accum):
            batch = {name: value.to(device) for name, value in next(batches).items()}
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_bf16):
                output = model(**batch)
            loss = output.loss / config.grad_accum
            loss.backward()
            step_loss += float(loss.detach().item())
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()
        if step % config.log_every == 0 or step == total_steps:
            print(f"[mlm_cpt] step {step}/{total_steps} loss {step_loss:.4f}", flush=True)
            logged.append((step, step_loss))

    eval_after = _evaluate_masked_lm(model, holdout_encodings, collator, **eval_kwargs)
    print(
        f"[mlm_cpt] eval after: loss {eval_after.loss:.4f} "
        f"masked-acc {eval_after.masked_token_accuracy:.4f} "
        f"({eval_after.masked_token_count} masked tokens)",
        flush=True,
    )

    config.out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(config.out_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(config.out_dir))

    manifest: dict[str, Any] = {
        "kind": "mlm_cpt_run",
        "config": json.loads(config.model_dump_json()),
        "seed": config.seed,
        "git_revision": _git_revision(),
        "input_jsonl_sha256": _sha256_file(config.input_jsonl),
        "package_versions": _package_versions(),
        "text_field": text_field,
        "record_counts": {"train": len(train_texts), "holdout": len(holdout_texts)},
        "optimizer_steps": total_steps,
        "warmup_steps": warmup_steps,
        "device": str(device),
        "bf16": use_bf16,
        "eval_before": dataclasses.asdict(eval_before),
        "eval_after": dataclasses.asdict(eval_after),
        "logged_losses": logged,
        "started_at": started_at,
        "finished_at": datetime.now(tz=UTC).isoformat(),
    }
    manifest_path = config.out_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return MlmCptResult(
        out_dir=config.out_dir,
        manifest_path=manifest_path,
        optimizer_steps=total_steps,
        text_field=text_field,
        train_records=len(train_texts),
        holdout_records=len(holdout_texts),
        eval_before=eval_before,
        eval_after=eval_after,
        logged_losses=tuple(logged),
    )


__all__ = [
    "MANIFEST_FILENAME",
    "TEXT_FIELD_CANDIDATES",
    "MaskedLmEval",
    "MlmCptConfig",
    "MlmCptResult",
    "detect_text_field",
    "load_text_records",
    "run_mlm_cpt",
]

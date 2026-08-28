"""Smoke test for the Track T-S0 MLM runner (short run, reload, M1 init).

Builds a tiny JSONL of short Lean records (sampled from the curated CPT
corpus when readable, synthesized otherwise), runs a few dozen optimizer
steps, then verifies:

1. the held-out masked-LM loss decreased,
2. the output directory reloads via ``AutoModelForMaskedLM.from_pretrained``,
3. the adapted encoder initializes the downstream M1 packed cross-encoder
   (``build_m1_cross_encoder_module``) and survives one forward pass.

Usage::

    python -m leanfaith.train2.smoke --workdir /path/to/scratch \
        [--device cuda|cpu] [--steps 30] [--seq-len 256]
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, cast

from leanfaith.train2.mlm_cpt import MlmCptConfig, MlmCptResult, run_mlm_cpt

DEFAULT_SNAPSHOT = Path(
    "/storage/milikic/models/hub/models--answerdotai--ModernBERT-base"
    "/snapshots/8949b909ec900327062f0ebf497f51aef5e6f0c8"
)
DEFAULT_CORPUS = Path(
    "/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl"
)
SMOKE_RECORDS = 200


def _synthetic_records(count: int) -> list[str]:
    rows: list[str] = []
    for index in range(count):
        text = (
            f"theorem add_shift_comm_{index} (a b : Nat) : "
            f"a + b + {index} = b + a + {index} := by\n"
            f"  simpa [Nat.add_comm, Nat.add_left_comm] using "
            f"congrArg (fun n => n + {index}) (Nat.add_comm a b)\n"
        )
        rows.append(json.dumps({"text": text}))
    return rows


def build_smoke_jsonl(
    source: Path,
    destination: Path,
    *,
    records: int = SMOKE_RECORDS,
    min_chars: int = 120,
    max_chars: int = 1500,
    scan_limit: int = 20000,
) -> tuple[int, str]:
    """Write a tiny corpus of short Lean-ish records; returns (count, origin)."""

    rows: list[str] = []
    origin = "sampled"
    try:
        with source.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= scan_limit or len(rows) >= records:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                text = cast(dict[str, object], record).get("text")
                if isinstance(text, str) and min_chars <= len(text) <= max_chars:
                    rows.append(json.dumps({"text": text}, ensure_ascii=False))
    except OSError:
        rows = []
    if len(rows) < records:
        rows = _synthetic_records(records)
        origin = "synthetic"
    destination.write_text("\n".join(rows[:records]) + "\n", encoding="utf-8")
    return len(rows[:records]), origin


def _check_reload_and_m1(out_dir: Path, *, seq_len: int) -> dict[str, Any]:
    """Reload the CPT output snapshot and drive one M1 forward pass from it."""

    import torch
    import transformers

    from leanfaith.models.m1_cross_encoder import build_m1_cross_encoder_module

    hf = cast(Any, transformers)
    reloaded = hf.AutoModelForMaskedLM.from_pretrained(
        str(out_dir), local_files_only=True, trust_remote_code=False
    )
    vocab_rows = int(reloaded.get_input_embeddings().weight.shape[0])

    tokenizer = hf.AutoTokenizer.from_pretrained(
        str(out_dir), local_files_only=True, trust_remote_code=False
    )
    encoder = hf.AutoModel.from_pretrained(
        str(out_dir), local_files_only=True, trust_remote_code=False
    )
    hidden_size = int(encoder.config.hidden_size)
    module = cast(Any, build_m1_cross_encoder_module(encoder=encoder, hidden_size=hidden_size))
    packed = (
        "[REFERENCE]\n"
        "theorem left_id (n : Nat) : 0 + n = n\n"
        "[CANDIDATE]\n"
        "theorem right_id (n : Nat) : n + 0 = n"
    )
    batch = tokenizer(
        [packed], padding=True, truncation=True, max_length=seq_len, return_tensors="pt"
    )
    module.eval()
    with torch.no_grad():
        output = module(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
    probability = float(output["probabilities"][0].item())
    pooled_width = int(output["pooled_pair_embeddings"].shape[-1])
    if not (0.0 <= probability <= 1.0):
        raise RuntimeError(f"M1 forward produced a non-probability: {probability}")
    if pooled_width != hidden_size:
        raise RuntimeError(f"M1 pooled width {pooled_width} != hidden_size {hidden_size}")
    return {
        "reload_ok": True,
        "reloaded_vocab_rows": vocab_rows,
        "m1_init_ok": True,
        "m1_hidden_size": hidden_size,
        "m1_forward_probability": probability,
    }


def run_smoke(
    *,
    workdir: Path,
    device: str,
    steps: int,
    seq_len: int,
    snapshot_dir: Path = DEFAULT_SNAPSHOT,
    corpus: Path = DEFAULT_CORPUS,
) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    smoke_jsonl = workdir / "smoke_corpus.jsonl"
    record_count, origin = build_smoke_jsonl(corpus, smoke_jsonl)
    print(f"[smoke] corpus: {record_count} records ({origin}) -> {smoke_jsonl}", flush=True)

    config = MlmCptConfig(
        input_jsonl=smoke_jsonl,
        snapshot_dir=snapshot_dir,
        out_dir=workdir / "mlm_cpt_smoke_out",
        seq_len=seq_len,
        mlm_probability=0.30,
        batch_size=8,
        grad_accum=1,
        lr=5e-5,
        max_steps=steps,
        log_every=5,
        holdout_records=32,
        seed=0,
        device=device,
        bf16=device.startswith("cuda"),
    )
    result: MlmCptResult = run_mlm_cpt(config)

    if result.eval_after.loss >= result.eval_before.loss:
        raise RuntimeError(
            "held-out masked-LM loss did not decrease: "
            f"{result.eval_before.loss:.4f} -> {result.eval_after.loss:.4f}"
        )
    checks = _check_reload_and_m1(result.out_dir, seq_len=seq_len)

    summary: dict[str, Any] = {
        "device": device,
        "steps": result.optimizer_steps,
        "records": record_count,
        "corpus_origin": origin,
        "text_field": result.text_field,
        "eval_before": {
            "loss": result.eval_before.loss,
            "masked_token_accuracy": result.eval_before.masked_token_accuracy,
        },
        "eval_after": {
            "loss": result.eval_after.loss,
            "masked_token_accuracy": result.eval_after.masked_token_accuracy,
        },
        "loss_decreased": True,
        "out_dir": str(result.out_dir),
        "manifest": str(result.manifest_path),
        **checks,
    }
    print("[smoke] PASS " + json.dumps(summary, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--seq-len", type=int, default=256)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="mlm-cpt-smoke-"))
    run_smoke(
        workdir=workdir,
        device=args.device,
        steps=args.steps,
        seq_len=args.seq_len,
        snapshot_dir=args.snapshot_dir,
        corpus=args.corpus,
    )


if __name__ == "__main__":
    main()

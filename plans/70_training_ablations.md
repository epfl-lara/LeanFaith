# TRAIN — cross-attention model preparation and later ablations

> **Task ID:** TRAIN
> **Status:** deferred
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** versioned datasets and EVAL v2; architecture smokes may run earlier
> **Next gate:** verify/extract the existing M2 matcher into frozen directional and symmetric modes
> **Compute class:** local RTX 4090 for smokes/10K throughput; A100/H100 for full training
> **Lean budget:** zero
> **Local staging root:** `/storage/milikic/leanfaith/value_first/training_v1/`
> **HF destination:** model destinations are chosen per ablation after dataset freezes

## Objective

Prepare one simple architecture and an ablation matrix while data collection proceeds. Do not start
full training from this task setup. The purpose is to make later experiments comparable: determine
whether CPT1, CPT2, staged SFT1/SFT2, or a merged SFT mixture actually helps.

## Frozen initial architecture

Backbones:

- primary: Ettin encoder 150M;
- control: ModernBERT base/encoder;
- other architecture families are deferred.

Encode theorem/reference and body/candidate separately with one shared encoder. Add exactly two
decoder-style cross-attention matcher blocks (8 heads, residual/LayerNorm, `D → 4D → D` GELU FFN,
no causal mask and no extra self-attention), followed by a binary logit head and
binary cross-entropy-with-logits loss. This is classification with decoder-style cross-attention,
not an autoregressive text decoder.

### CPT2 directional mode

The proof body queries theorem/context memory through both matcher blocks. Masked-mean pool the
unchanged theorem encoding and matched body, combine `[r, c, |r-c|, r*c]`, and predict proof
validity. All encoder/matcher/head weights train.

### SFT symmetric mode

At each matcher layer, synchronously cross-update both streams using shared parameters. Masked-mean
pool, combine `[a+b, |a-b|, a*b]`, and predict semantic consistency. The output must be numerically
swap invariant, avoiding doubled inference.

Transfer order: CPT1 encoder → CPT2 encoder+matcher (new validity head) → SFT encoder+matcher (drop
CPT2 head, add equivalence head). If a phase is skipped, initialize missing modules from one
recorded seed. Optional SFT2-only validity/relation/faithfulness heads may attach to pooled features
with a small auxiliary weight (initially 0.25), but are not required by SFT1 or the core model.

## Existing implementation to reuse

- `src/leanfaith/models/m2_bidirectional_matcher.py`: existing two-layer synchronous matcher and
  symmetric head.
- `src/leanfaith/models/m0_dual_encoder.py`: separate-side tokenization/collation patterns.
- `src/leanfaith/train2/trainer.py`: group splits, weighted BCE, checkpoint selection, AdamW/BF16,
  and manifests. Packed M1-specific pair paths must not define the new interface.

Keep frozen replay behavior intact. Prefer extracting/reimplementing a tested `CrossMatchBlock` in
a versioned production module with directional/symmetric task modes rather than mutating historical
M2 semantics. Generalize hard-coded backbone verification to pinned Ettin/ModernBERT revisions.

## Ablation matrix

Freeze data/token budgets, seeds, selection metrics, and compute before comparisons:

1. stock encoder → SFT;
2. CPT1 → SFT;
3. CPT2 → SFT;
4. CPT1 → CPT2 → SFT;
5. SFT1 then SFT2 versus SFT1+SFT2 mixed from the start;
6. SFT1 only, SFT2 only, and both;
7. Ettin primary versus ModernBERT control;
8. optional SFT2 auxiliary heads on/off after the core result.

Model/threshold/config selection uses EVAL validation only. The chosen final model is evaluated on
unchanged EVAL test and compared with stored baselines. Dataset phases remain individually
addressable; never destroy an ablation by silently pre-merging all rows.

## Scope and ownership

**Allowed before data freeze:** architecture refactor behind new config, unit tests, tiny synthetic
forward/backward smoke, checkpoint key-transfer manifest, tokenizer audit, and 10K-row throughput/
memory planning on already approved smoke data.

**Deferred:** full CPT/SFT runs, gold-based model selection, large model publication, or expanding
to other architectures.

**Writable paths:** this brief; `src/leanfaith/models/value_first/`;
`src/leanfaith/train_value_first/`; `configs/models/value_first/`;
`configs/training_value_first/`; `tests/unit/train_value_first/`; the staging root. Existing M2,
train2, dependency/project config, dataset outputs, and other task paths are read-only references;
request coordinator changes.

## Lean-efficiency plan

Lean is the bottleneck and training has a zero-Lean budget. Consume already serialized datasets and
cached validity/equivalence metadata. Never invoke Lean from a collator, trainer, validation loop,
or metric callback.

## Execution gates

### One-example architecture smoke

Test both backbones and task modes on tiny data: shapes, empty-mask rejection, independent padding,
gradient flow through both encoder calls, no causal mask, CPT2 directionality, exact SFT swap
invariance at `1e-6`, checkpoint transfer keys, head drop/init, deterministic seed, and parity with
existing M2 on a fixed tiny encoder.

### Throughput/compute pilot

On the local 4090, measure BF16, dynamic separate-side padding, encoder checkpointing, sequence
lengths, batch/accumulation, peak VRAM, tokens/s, and estimated 10K/full-stage wall time. Request
A100/H100 hardware with an explicit duration/checkpoint plan before full training.

### Training authorization

Start only after required dataset Hub revisions/hashes, EVAL v2, ablation configs, seeds, budgets,
selection rule, and resume/checkpoint policy are frozen. Each run writes safetensors and a manifest
of loaded/new/discarded keys and data/code revisions.

## Acceptance criteria

- One shared-encoder, separate-input, two-layer cross-attention implementation supports directional
  CPT2 and exactly symmetric SFT without changing historical replay.
- Architecture/transfer tests and a local throughput report pass.
- Ablations preserve separate datasets and comparable token/compute budgets.
- No full training starts before data/eval freeze and explicit compute authorization.
- No training path invokes Lean.

## Session kickoff prompt

```text
Own only TRAIN in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/70_training_ablations.md completely. Update this brief and
claim exact paths. This task is deferred for full training: only prepare and test the frozen
Ettin/ModernBERT shared-encoder plus two-layer decoder-style cross-attention architecture and
ablation configs. Reuse M2 without breaking replay. Lean is the bottleneck and the training Lean
budget is zero. Run tiny architecture/transfer tests and a local 4090 throughput pilot only. Do not
train on unfrozen data or use test labels for selection; request A100/H100 with measured estimates
before any full run.
```

## Coordinator requests

- Authorize full training only after data/evaluation revisions and the ablation budget are frozen.

## Progress log (append-only)

- 2026-08-30 — task brief created as deferred; no model changes or training performed.

# First bidirectional-matcher M2 proxy checkpoint

Date: 2026-08-12

Status: complete, clean-code frozen, independently replayed in a fresh process,
and swap-invariance verified.

This is an **experimental proxy diagnostic**, not a semantic-faithfulness
result. It uses mixed machine targets and is ineligible for scientific model
selection, calibration, evaluation, gate credit, release claims, or FormalRx
comparison.

## Architecture and frozen execution

M2 independently encodes the reference and candidate with a shared
ModernBERT-base encoder, applies two synchronous weight-shared bidirectional
matching layers, and predicts the binary proxy target from commutative pooled
features (`sum`, absolute difference, and product). Only the base reference
encoding is cacheable; candidate-dependent matching is recomputed.

- Code revision: `a46dd41bb022dde0192bcb4080b1be1e07d5d231`
- Artifact ID:
  `experimental-m2-proxy-training:57781e0b92f96cba20b26b9594a78f8e9a48b3d2d2fcb74719d9a0a2249892af`
- Output:
  `/storage/milikic/leanfaith/m2_proxy_training/firsthop_kimi_qwen1125_composition_a46dd41_v1`
- Backbone: `answerdotai/ModernBERT-base` at
  `8949b909ec900327062f0ebf497f51aef5e6f0c8`
- Checkpoint SHA-256:
  `862340ebe6ed7a93a607585571c0461ac50b150a941be3dcdc595bacc77f5d57`
- Training manifest SHA-256:
  `f52074650a983b3e77879ac137f52fa2c597004c2d7a47a6ce0bf63c94cb9d21`

The deterministic ancestry-balanced schedule selected 5,952 proxy records
from 3,904 ancestry components: exactly 2,976 per proxy target, 186 optimizer
steps, and no duplicate oversampling. The run used the expanded 17,181-pair
machine-proxy corpus.

## Proxy diagnostics

| Split | Records | Pseudo-AUPRC | Balanced accuracy | Weighted BCE |
|---|---:|---:|---:|---:|
| train | 5,952 | 0.891884 | 0.753360 | 0.492614 |
| validation | 1,621 | 0.715701 | 0.727464 | 0.648358 |
| test | 1,791 | 0.683699 | 0.725051 | 0.676780 |

These values describe agreement with generated machine-proxy targets. They do
not estimate mathematical faithfulness, and they are not directly comparable
with the earlier M0/M1 numbers because this run uses the expanded corpus and a
slightly different frozen schedule.

## Independent replay and symmetry

An earlier M2 artifact at
`/storage/milikic/leanfaith/m2_proxy_training/firsthop_kimi_qwen1125_composition_3f5b23e_v1`
failed an independent replay because the verifier did not reproduce the exact
split-isolated training-order batching and dynamic padding used by training.
That artifact is quarantined and must not be cited as verified.

Revision `a46dd41` froze one shared diagnostic inference protocol for training
and verification. A fresh-process verifier then reloaded the pinned pretrained
checkpoint, reconstructed the initial model state and exact training schedule,
replayed every prediction batch, recomputed diagnostics, verified the final
checkpoint hash, and passed the registered numerical tolerances.

The separate 64-pair swap audit reported exactly zero difference in equivalence
logits and probabilities under input reversal. Directional relation and
ambiguity heads are intentionally disabled because the current proxy targets
do not support those semantic labels.

# First packed-cross-encoder M1 proxy checkpoint

Date: 2026-08-12

Status: complete, clean-code frozen, independently reviewed, independently
verified, and exact-input verified.

This is an **experimental proxy diagnostic**, not a semantic-faithfulness
result. M1 was trained on the same machine targets as M0 so that the model
interaction could be compared without changing the data. It is ineligible for
scientific model selection, calibration, evaluation, gate credit, release
claims, or FormalRx comparison.

## Architecture and frozen execution

M1 jointly encodes the tagged reference and candidate in one ModernBERT pass,
then applies masked-mean pooling and a binary same-claim proxy head. Unlike M0,
every encoder layer can directly compare tokens across the two statements.

- Code revision: `8d815affa78f38968478d1fe5bb98773c14c89ac`
- Artifact ID:
  `experimental-m1-proxy-training:d3417fdd65586e5c1b5b661d23cc07753040eb9bbc363d6b536addcc23913942`
- Output:
  `/storage/milikic/leanfaith/m1_proxy_training/firsthop_kimi_qwen_composition_8d815af_v1`
- Backbone: `answerdotai/ModernBERT-base` at
  `8949b909ec900327062f0ebf497f51aef5e6f0c8`
- Checkpoint SHA-256:
  `bc426653968b54637ad7e2d88d2b9666d853780f8e2af589e6bb1ff210cd251f`
- Training manifest SHA-256:
  `b8c0ec2850474a624a831f0da218c8510c5f65fe9dc31aeb020d15c5ae448973`

The packed-pair audit covered all 17,031 records. Thirty-five pairs exceeded
the frozen 1,024-token budget (21 train, two validation, and 12 test), leaving
13,612 eligible training records. The deterministic ancestry-balanced schedule
selected 5,920 records across 3,867 ancestry components: exactly 2,960 per
proxy target, 185 optimizer steps, and no duplicate oversampling.

## Proxy diagnostics

| Model / split | Records | Pseudo-AUPRC | Balanced accuracy | Weighted BCE |
|---|---:|---:|---:|---:|
| M0 validation | 1,604 | 0.630011 | 0.695354 | 0.662213 |
| M1 validation | 1,602 | 0.907573 | 0.862714 | 0.415879 |
| M0 test | 1,794 | 0.603207 | 0.686989 | 0.661444 |
| M1 test | 1,782 | 0.887478 | 0.859391 | 0.438165 |

M1 performs much better than M0 on the frozen machine-proxy task. This is
evidence that joint token interaction is useful for the generated
transformation distribution; it is not evidence of equivalent gains on
human-adjudicated mathematical faithfulness.

## Independent verification

After an adversarial review found two verifier gaps, the verifier was hardened
to bind all run fields to the frozen protocol and to reconstruct the exact
prediction identity/target set from the source records and ancestry schedule.
The final checkpoint was retrained from the hardened clean revision.

An independent verifier then reloaded the pinned pretrained checkpoint,
reconstructed the initial model state, replayed packed tokenization and the
5,920-record schedule, checked all 9,304 prediction identities and targets,
recomputed metrics, validated the safetensors file, and matched all code and
input hashes. No discrepancy remained. No model-visible private text is stored
in the training metadata.


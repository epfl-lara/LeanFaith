# First ModernBERT M0 proxy checkpoint

Date: 2026-08-12

Status: complete, clean-code frozen, and independently verified.

This is an **experimental proxy diagnostic**, not a semantic-faithfulness
result. The model was trained against deterministic transformation intentions
and single-Codex-judge proxy targets. It is ineligible for scientific model
selection, calibration, evaluation, gate credit, release claims, or comparison
with FormalRx.

## Frozen inputs and execution

- Code revision: `03f209ba1d672089b123de5e9929b4b4d79d345e`
- Input dataset ID: `experimental_mixed_supervision:886da05a36e8b2125ec63c2ff8b0888b3cea48a3f498bc5b6721d9f358f81f6d`
- Input dataset: `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen_composition_974c476_v1`
- Tokenizer audit ID: `tokenizer_audit:44ac7d103263e6c3202f27c3a8661af4157302033edc7290c5be225601b8bf6b`
- Tokenizer audit: `/storage/milikic/leanfaith/tokenizer_audit_v1/frozen_9e0afc9_v1`
- Selected input length: 1,024 tokens
- Eligible tokenizer candidates: ModernBERT-base and ModernBERT-large
- M0 backbone: `answerdotai/ModernBERT-base` at revision
  `8949b909ec900327062f0ebf497f51aef5e6f0c8`
- Prepared-input artifact: `experimental-m0-proxy-inputs:78d6219e786e18392f353536cb374bb394f80d9d805ccaefde770d04b7d651fd`
- Training artifact: `experimental-m0-proxy-training:6ae357e912c0234239bb8e8e03eb899064b4357e61255571d2a89383567d6916`
- Training output: `/storage/milikic/leanfaith/m0_proxy_training/firsthop_kimi_qwen_composition_03f209b_v1`
- Checkpoint SHA-256: `d23f293a3e35ff76087b7bfd8b900d7fdbcf8313c7e76955c3587bb8a53e1b4f`
- Training-manifest SHA-256: `9a59ff7dd85a455550d3276ea0c0060c44a495337ad4ab37a863732c7dfa680d`

The data-only tokenizer rule rejected the common 512-token budget and selected
1,024. CodeT5+ 220M and DeBERTa-v3-large were therefore ineligible because
their pinned native context is 512; no positional architecture was modified.
The audit does not select a scientific backbone winner.

## Schedule

- 17,031 prepared records; 13,632 train-eligible records
- one long record excluded under the frozen input policy
- 5,920 balanced exposures: 2,960 per proxy target
- 3,868 ancestry components represented
- 185 optimizer steps with effective batch size 32
- one epoch, AdamW, learning rate `1e-5`, weight decay `0.01`
- deterministic float32 CUDA execution on the local RTX 4090
- schedule SHA-256:
  `6496cb228c5b379cb90e41c9c1ae738280d98fcfe6936f8dc695f8ee8fb76956`

## Proxy diagnostics

| Split | Records | Pseudo-AUPRC | Balanced accuracy | Weighted BCE |
|---|---:|---:|---:|---:|
| train | 5,920 | 0.841536 | 0.740203 | 0.674015 |
| validation | 1,604 | 0.630011 | 0.695354 | 0.662213 |
| test | 1,794 | 0.603207 | 0.686989 | 0.661444 |

The learned token representation transfers beyond the training rows on these
machine proxy targets, but the train-to-validation gap is substantial. These
figures do not estimate true same-claim accuracy and must not be used to set
deployment thresholds.

## Verification

The standalone verifier rebound the clean code revision, exact prepared
inputs, tokenizer audit, official checkpoint receipt, schedule, predictions,
metrics, and serialized model state and passed. The output contains a
596,077,512-byte safetensors checkpoint and no pickle weights. The manifest
records private-source content and therefore prohibits redistribution and
external transmission.

This checkpoint supplies the first end-to-end token-model evidence and a
working training baseline. It does not replace `training_gold`,
`selection_gold`, `calibration_gold`, or `final_human_test`.

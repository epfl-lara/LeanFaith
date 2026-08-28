# Refocus day 1 — consolidated results (2026-08-28)

All numbers are golden-**dev** only; `final_test` remains sealed per PLAN.md.
Headline subset = expert-labeled, non-conflicted dev pairs (n=228, prevalence 0.689).
Eval runs: `/storage/milikic/leanfaith/golden/eval_runs/`.

## First real numbers (strict zero-shot track, threshold 0.5)

| model | trained on | bal-acc | 95% CI | acc | AUPRC | ROC-AUC | ECE |
|---|---|---|---|---|---|---|---|
| M1 8d815af (pre-refocus, 1 epoch proxy recipe) | mixed 17,181 proxy | 0.593 | [0.541, 0.645] | 0.482 | 0.791 | 0.610 | 0.396 |
| S1v0 stock encoder | corpus-v0 (13,746) | 0.529 | [0.511, 0.553] | 0.351 | 0.789 | 0.579 | 0.646 |
| S1v0 chunks-CPT encoder | corpus-v0 | 0.522 | [0.502, 0.543] | 0.346 | **0.849** | **0.711** | 0.646 |
| S1v0 statement↔proof-CPT encoder | corpus-v0 | 0.538 | [0.513, 0.562] | 0.368 | 0.788 | 0.583 | 0.628 |

Trivial baselines (headline subset): always-majority accuracy 0.689; identity-match accuracy 0.338.
Published context: GTED metric ≈0.66–0.70 acc; majority-vote-8 LLM ≈0.70 acc / 0.40 κ (ProofNet).

## Findings

1. **Training data is the binding constraint.** All S1v0 arms saturate proxy validation
   (AUPRC ≥ 0.998, swap-disagreement ≤ 0.002) yet transfer WORSE on thresholded golden
   metrics than the older 1-epoch checkpoint. Longer training on the shortcut-riddled
   corpus actively hurts. This is the Track D data-engine thesis, now measured.
2. **S0 encoder adaptation improves ranking transfer.** On identical data, the
   chunks-CPT encoder gains +0.06 AUPRC / +0.13 ROC-AUC over stock — the best ranking
   numbers of any model to date. The statement↔proof-CPT encoder did not beat chunks on
   this corpus; retest once corpus-S2 carries real statement-level variety.
3. **Calibration is uniformly poor** in the strict track (ECE 0.40–0.65); the
   gold-calibrated reporting track (dev-fit temperature) is the designated answer and
   should accompany the next round.

## Assets produced today (all on `main` unless noted)

- Golden partition frozen: 910 expert `final_test` (sealed) / 821 dev / 819 golden_train
  (`data/benchmarks/golden_partition_v1.json` + blocklist). 5,111 canonical pairs from
  EPLA+BEq+GTED+ProofNetVerif with cross-dataset membership merging.
- Eval harness `leanfaith-eval` (byte-parity M1 scoring, abstain-not-truncate,
  bootstrap CIs, final-test seal).
- Encoders: `modernbert_lean_v1_run1` (chunks CPT: masked-LM 0.885→0.480) and
  `modernbert_lean_v2_mixed` (+numina statement↔proof: 0.712→0.197) — /storage/…/cpt/.
- S1 anti-shortcut trainer (`train2/trainer.py`) + corpus-v0 adapter
  (13,746/1,619/1,779; 2 golden-blocklist drops, 35 overlength drops).
- `collect2/` autoformalizer package: live pilot 30/30 across Goedel/Kimina/StepFun;
  3 pilot-found defects fixed with regression tests.
- LLM-transform harness (`corpus2/llm_transforms.py`): codex self-labels trustable
  (10/10), lemex conditional (8/10), claude blocked on CLI login.
- Typed Lean Meta transform engine first slice (`LeanFaith/Meta/TransformEngine.lean`):
  P24+P23 over typed Expr, 9/9 re-elaborating candidates on real mathlib theorems.
- D-0 recovery COMPLETE: Qwen 6,391 Lean-valid / 2,444 invalid; Kimi 6,982 / 2,056;
  0 infrastructure errors (root cause of the old crash: 10GB RLIMIT_AS broke Lean
  thread creation, not the olean). 13,373 unique Lean-valid pairs across proposers,
  0 pair-key overlap. Frozen counts + roots under
  /storage/milikic/leanfaith/lf022_recovery_trackD0_20260828/ and lf022_lean_checks/.
  Formal step-5 merge needs a new reviewed inventory spec (informational dedup: no-op).
- Prunes P1+P2 merged: ~130K LOC removed; suite at baseline (one order-sensitive test).

## Next

1. Gold-calibrated track numbers (temperature on dev) for the four models above.
2. Corpus v1: merge depth-3 pairs + recovered Qwen/Kimi (post D-0) + D-3 codex
   transforms at scale (family assigned per record) + ACE replay; diversity caps.
3. Meta-engine second slice: nested sites, more families (P20/P21/P32), certificates.
4. S1 at scale on cluster GPUs; statement↔proof encoder retest on corpus-S2.

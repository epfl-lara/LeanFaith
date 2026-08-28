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

## Gold-calibrated comparison (temperature + threshold fit on dev)

The fit and the metrics below use the same 228-pair expert dev subset. They are therefore
calibration/selection-set results, not held-out estimates; `final_test` remains sealed. The
temperature minimizes binary NLL and the threshold maximizes balanced accuracy, with ties resolved
by the point in each decision interval closest to 0.5. Ranking metrics are unchanged by the
monotone temperature transform.

| model | track | bal-acc | 95% CI | acc | F1 | AUPRC | ROC-AUC | ECE | NLL | T | threshold |
|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 8d815af | strict | 0.593 | [0.541, 0.645] | 0.482 | 0.443 | 0.791 | 0.610 | 0.396 | 1.159 | — | 0.500000 |
| M1 8d815af | gold-calibrated | **0.607** | [0.555, 0.656] | 0.491 | 0.448 | 0.791 | 0.610 | 0.189 | 0.693 | 1000† | 0.500034 |
| S1v0 stock encoder | strict | 0.529 | [0.511, 0.553] | 0.351 | 0.108 | 0.789 | 0.579 | 0.646 | 4.370 | — | 0.500000 |
| S1v0 stock encoder | gold-calibrated | **0.612** | [0.557, 0.669] | 0.535 | 0.547 | 0.789 | 0.579 | 0.190 | 0.694 | 1000† | 0.498264 |
| S1v0 chunks-CPT encoder | strict | 0.522 | [0.502, 0.543] | 0.346 | 0.108 | 0.849 | 0.711 | 0.646 | 4.541 | — | 0.500000 |
| S1v0 chunks-CPT encoder | gold-calibrated | **0.684** | [0.610, 0.752] | 0.645 | 0.692 | 0.849 | 0.711 | 0.190 | 0.694 | 1000† | 0.498034 |
| S1v0 statement↔proof-CPT encoder | strict | 0.538 | [0.513, 0.562] | 0.368 | 0.163 | 0.788 | 0.583 | 0.628 | 4.027 | — | 0.500000 |
| S1v0 statement↔proof-CPT encoder | gold-calibrated | **0.614** | [0.562, 0.665] | 0.500 | 0.462 | 0.788 | 0.583 | 0.190 | 0.694 | 1000† | 0.498653 |

† All four NLL fits reached the configured positive-temperature upper bound (`T=1000`, inverse
temperature `0.001`). Under the required temperature-only model, the NLL optimum is toward zero
logit scale: probabilities move close to 0.5 while the fitted threshold preserves ranking. This
exposes a large intercept/prevalence mismatch that a single temperature cannot remove; the
remaining ECE ≈0.19 should not be described as well calibrated.

Artifacts are under `/storage/milikic/leanfaith/golden/eval_runs/`:

| run directory | calibrated predictions SHA-256 | calibration SHA-256 | metrics SHA-256 | manifest SHA-256 |
|---|---|---|---|---|
| `dev_m1_bc426653968b_gold_calibrated` | `7637cce03797afbbea4d5dca7910290398bd8f67774edbb8ace9deec445d36fe` | `982696a3b7db9a90cc811b8da695835592cbd733270a71602a3de8694ed3401c` | `1e668166cdb54e7402a895b9de2245c45deabe89fa54de2c24e4075ae075005c` | `4e4c6510c4b1093e7a41b29710da46221ed67cca541e5536ea2725f495464e3e` |
| `dev_s1v0_stock_a55db24b754a_gold_calibrated` | `a5a70a072b401d6c9a8491ace87873fe3e06100562402b053343bd2b2a1102bc` | `878f6858368e87dddebf474604548e85592ed98fe69efac09aeb5fa926100135` | `944fa6cf619f624e0e3a0051f380adb66f792987cc911d4cb2dfbb7455172a85` | `d34caa9ec5e379c4f2b82c6e537f6442d24597b5c1a3fa2178c033d002c3dcce` |
| `dev_s1v0_cpt_chunks_3cb6b43950ae_gold_calibrated` | `f173483f9766b4d6e086265f4324d0d8c23e6b8f661368f3992df4af5f274cdc` | `a8125116a1da21e3f28cb52d27d5c63aa852261e7e26249c4684ed11b36dc1fa` | `1e6fa996bea80338a47970b264590cfe3255d4febfdf21b23e1a2b881d84a184` | `567ff3de4efe59edce0064cece1366e8935d82e569e32a63bf05784a5fefb190` |
| `dev_s1v0_cpt_mixed_f6de1e96a6f0_gold_calibrated` | `e807c55271d30a99da3d37d0b8e031d2de897af5d261fff893504f312f2e635b` | `4821ca28e4df75cd5aaf6871f3c6ca56b63e0bd02a147d026bba728f9c36d405` | `dd0281c191b80e52cf4c7f612d87396c65269d05096da4a590d0a61e5e8e8569` | `3ac32836da903e072253b8737e2f6002a09b276142e75a6608a486ff82f71e1c` |

## Findings

1. **Training data is the binding constraint.** All S1v0 arms saturate proxy validation
   (AUPRC ≥ 0.998, swap-disagreement ≤ 0.002) yet transfer WORSE on thresholded golden
   metrics than the older 1-epoch checkpoint. Longer training on the shortcut-riddled
   corpus actively hurts. This is the Track D data-engine thesis, now measured.
2. **S0 encoder adaptation improves ranking transfer.** On identical data, the
   chunks-CPT encoder gains +0.06 AUPRC / +0.13 ROC-AUC over stock — the best ranking
   numbers of any model to date. The statement↔proof-CPT encoder did not beat chunks on
   this corpus; retest once corpus-S2 carries real statement-level variety.
3. **Threshold transfer, not ranking, caused most S1v0 damage.** Dev-fit thresholds lift
   balanced accuracy to 0.612–0.684, led by chunks-CPT. But every temperature fit runs to
   the `T=1000` boundary and leaves ECE near 0.19: one scalar temperature cannot correct
   the models' large intercept/prevalence mismatch. Keep strict and calibrated tracks side
   by side and treat these same-dev fitted numbers as selection diagnostics.

## Assets produced today (all on `main` unless noted)

- Golden partition frozen: 910 expert `final_test` (sealed) / 821 dev / 819 golden_train
  (`data/benchmarks/golden_partition_v1.json` + blocklist). 5,111 canonical pairs from
  EPLA+BEq+GTED+ProofNetVerif with cross-dataset membership merging.
- Eval harness `leanfaith-eval` (byte-parity M1 scoring, abstain-not-truncate,
  bootstrap CIs, final-test seal).
- Dev-only `leanfaith-eval calibrate` track (temperature NLL fit + balanced-accuracy
  threshold, frozen-partition linkage, strict/calibrated comparison, hashed manifests).
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

1. D-3 codex scale run: 200 statements from the full representation store, with
   transformation family assigned per record and every rewritten statement Lean-checked.
2. Judge the 13,373 recovered Qwen/Kimi pairs (100-pair pilot, then resumable 500 batches).
3. Corpus v1: merge depth-3 pairs + judged Qwen/Kimi (post D-0) + D-3 codex
   transforms at scale (family assigned per record) + ACE replay; diversity caps.
4. S1 at scale on cluster GPUs; statement↔proof encoder retest on corpus-S2.
5. Meta-engine second slice: nested sites, more families (P20/P21/P32), certificates.

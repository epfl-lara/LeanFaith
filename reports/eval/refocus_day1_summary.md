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

## D-3 Codex scale run

The production D-3 run is complete at
`/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1_f88931b/`. It used the full
27,786-row public mathlib representation store, explicit per-record family assignment, Codex
`gpt-5.6-sol` at high reasoning effort, and closed provider stdin. All 200 jobs started exactly
one provider process; all 200 exited zero, parsed, matched the assigned family and intended label,
and required no retry. The model changed 154 statements.

Before spending the 200 calls, the exact frozen source plan was elaborated in reconstructed
source-file context: **200/200 valid** across 164 mathlib files. The final generated-candidate
check found **192 valid / 8 invalid / 0 infrastructure errors**. The strict admission filter
(parsed, assigned family/label matched, changed, Lean-valid, blocklist-clear) emitted **146 unique
trainer records: 65 positive / 81 negative**, with 146 unique ancestry group keys. All 14 assigned
families are represented; lower-yield preserve families were P27 (1), P31 (3), and P36 (5), while
unchanged/inapplicable outputs were excluded rather than relabeled.

| artifact | SHA-256 |
|---|---|
| source-context preflight checks | `70ae6c479f78067f91ee19d5ef3cd1d9c0a42e825e917cf42c10f7271016eaf0` |
| source-context preflight manifest | `2b8496e77636ed7587a07de7006bbbbf9221e40bb2c64581a6641301e3797e4d` |
| production job plan | `26770ee4ec163ea1d9bf6a8e2e3f0bfe84ad04615015c4751669967abb477e39` |
| production generation records | `8503d6307374fb58643c0bbdd382338761332321aa587e0d99590c8862305a74` |
| production Lean checks | `a8a34cd67f8a55b2df3e73ab1796eea51e8edd099a14e5746f0b7b886aa14f23` |
| production trainer records | `95ba0a0ab5d18f560dfa6beeb1b012bbf74c8fdb6d95a3cb99e8179d4e54a532` |
| production run manifest | `4e1dd75ff2c3f6eaec88b73fbd81a7589dcc12398feccf97435c824a3f512075` |

The run is bound to repo revision `f88931b3dadf90dae6c8370cf8f581350e8333ff`, clean mathlib
revision `d568c8c09630de097a046763c17b9ea99f95f950`, the frozen golden-train few-shot partition,
and hashed input/output artifacts. `final_test` remained sealed and no private source was sent.
The next queue item needs no D-3 regeneration: it can judge the 13,373 recovered Qwen/Kimi pairs;
corpus v1 later consumes this run's `trainer_records.jsonl` directly.

## Recovered Qwen/Kimi single-pass judge

The production recovered-pair judge is complete at
`/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/`. It used the blinded
`lean_pair_blinded_v2` prompt, Codex `gpt-5.6-sol` at medium reasoning effort, closed provider
stdin, one AB orientation by default, and a BA call only after a parse failure, ambiguous or
uncertain verdict, or confidence below 0.75. All source pairs were public and blocklist-clear;
`final_test` remained sealed and no private source was transmitted.

The deterministic 100-pair pilot resolved 100/100 (2 same / 98 different) and escalated 0/100.
The resumed 500-pair batches then processed the complete frozen 13,373-row plan. Final results:
**13,367 resolved / 6 fail-closed unresolved**, **307 same / 13,060 different**, and **10
escalations (0.0748%)**. Qwen contributed 6,391 judgments (196 same / 6,193 different / 2
unresolved); Kimi contributed 6,982 (111 / 6,867 / 4). The provider ledger contains 13,383
requests: 13,378 completed semantic calls and 5 parse failures, with **0 process failures,
timeouts, interruptions, incomplete journals, requestless attempts, or retries**. Four escalated
records resolved from the reversed orientation; six disagreements or invalid semantic payloads
remain null rather than being guessed.

Every judgment and all 28 batch summaries replayed from immutable raw provider artifacts. The
resolved trainer projection has 13,367 rows (6,389 Qwen / 6,978 Kimi), and a deterministic,
stratified 150-pair audit sample is frozen for later cross-model checking.

| artifact | SHA-256 |
|---|---|
| pair plan | `1746aa6b95476712f858db196138f5f18a938126b90e7de18881fb5c72056fe4` |
| judgments | `2a6ef8c170a20e38047b3fbe6d1b842fb51abb0d0049552aa3f4bfac57b06025` |
| trainer records | `5de1f904904da6fa204a446e65c58d137a59a6a21d5afa15eb1ad24dbf3bf2f1` |
| attempt ledger | `f04391257e0bb7a060f247745537a95232f58388cb153a4546ab7fdd8f9fdb22` |
| response artifact set | `66098547ff86144b5fca1aeca0fee43e8fc1d727c8ac9885733e44aecea3d638` |
| audit sample (150) | `43fa3514b68a89b67e632381bf188d9307af5870f0817f83e21ebb35d4fc7b69` |
| audit sample key | `e3cd5ab4f368090548059f61cdb3c2d43a47ee74c0d99bda8307a66a6dcc7eeb` |
| final manifest | `19a9d814823245f300c9c386514c9f4281322b0939d51a23ab13228df9cc0d1b` |
| run manifest | `de71cac87293f733dc7c0f8501427dc07b5c4c479221dcc9fef1bfa14c09d257` |

Corpus v1 can consume `outputs/trainer_records.jsonl` directly; the six unresolved judgments are
retained only in the judgment/ledger artifacts and are excluded from training.

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
- D-3 Codex scale COMPLETE: 200 provider calls, 192 Lean-valid outputs, and 146 strict
  trainer-schema records at
  `/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1_f88931b/`.
- Recovered-pair judge COMPLETE: 13,373/13,373 processed, 13,367 resolved trainer records,
  10 escalations, 6 fail-closed null labels, and a 150-pair audit sample at
  `/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/`.
- Prunes P1+P2 merged: ~130K LOC removed; suite at baseline (one order-sensitive test).

## Next

1. Corpus v1: merge corpus-v0 + depth-3 pairs + judged Qwen/Kimi + D-3 Codex transforms;
   screen, unordered-near-dedup, ancestry-safe split, 10% family cap, and lexical canary.
2. S1 two-arm retrain from chunks-CPT and mixed-CPT on corpus v1, followed by dev-only strict
   and calibrated evaluation once the literal final-test seal has a safe dev-only input path.
3. Meta-engine second slice: nested sites, P20/P21, type hashes, batch driver, and yield probe.

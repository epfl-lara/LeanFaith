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
| S1v1 chunks-CPT encoder | strict | not run‡ | — | — | — | — | — | — | — | — | — |
| S1v1 chunks-CPT encoder | gold-calibrated | not run‡ | — | — | — | — | — | — | — | — | — |
| S1v1 statement↔proof-CPT encoder | strict | not run‡ | — | — | — | — | — | — | — | — | — |
| S1v1 statement↔proof-CPT encoder | gold-calibrated | not run‡ | — | — | — | — | — | — | — | — | — |

† All four NLL fits reached the configured positive-temperature upper bound (`T=1000`, inverse
temperature `0.001`). Under the required temperature-only model, the NLL optimum is toward zero
logit scale: probabilities move close to 0.5 while the fitted threshold preserves ranking. This
exposes a large intercept/prevalence mismatch that a single temperature cannot remove; the
remaining ECE ≈0.19 should not be described as well calibrated.

‡ The corpus-v1 checkpoints were trained, but their golden-dev rows are deliberately blank. The
only available pair-text artifact mixes `dev` with sealed `final_test`, and the current evaluator
opens that mixed file before partition filtering. No trusted hash-bound dev-only text export was
available, so the mixed artifact was not opened and neither strict nor calibrated scoring was
attempted.

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

## Corpus v1

The production corpus-v1 merge is complete at
`/storage/milikic/leanfaith/corpus2/v1_ed41471/`, bound to implementation commit
`ed41471f00bebb4be2c55cfd782075cfce95a0dd`. It strictly rehashed and joined 34,688 inputs:
17,144 corpus-v0 rows, 4,031 depth-3 rows, 13,367 resolved recovered-pair judgments, and 146 D-3
records. Both packed orientations were screened at 1,024 tokens and both statement sides were
screened against the golden blocklist; `final_test` remained sealed.

Screening removed 19 overlength pairs and one near-identical pair. Unordered near-signature
deduplication produced 32,505 pair identities and quarantined eight label-conflict groups. A rich
pre-cap union graph over ancestry and shared statement identities exposed 13 components crossing
frozen v0 splits; all 83 pairs in those components were quarantined rather than moving an anchor
or relying on a later cap side effect. The rebuilt graph has no cross-split ancestry or statement
identity and contains 17,306 components (9,578 active).

The deterministic fixed-point family cap then removed 9,000 pairs in ten rounds and retained
**23,414 rows: 5,050 same / 18,364 different**. The three largest stored family memberships are
2,341 each, exactly `floor(23,414 / 10)`, so every family is at or below 10%. Splits are ancestry
safe: **train 18,760 / validation 2,166 / test 2,488**. Exclusive source composition is 14,604
v0-only, 3,220 depth-only, 764 depth+v0, 4,206 recovered-only, 474 recovered+v0, and 146 D-3.

The deterministic swap-averaged bag-of-token logistic canary reaches **0.700 validation / 0.680
test balanced accuracy**, below the 0.80 shortcut target. The corpus contains 13,829
private-derived rows, so its manifest correctly sets private content true and redistribution,
external transmission, and release eligibility false.

| artifact | SHA-256 |
|---|---|
| corpus manifest | `22386b7127c80fab6ce70df722ecc155ee3a3520971515ebefee6cb438a20a01` |
| train records | `51ad67e42d5d350be0219ff26142e24ac1b7f8dfbfc652a1355430e46f5d6c4b` |
| validation records | `a5939fee4df3363fec1c3285623ca18509c549fbf65e73f2ec9a741af5505470` |
| test records | `7424eb1afa8f6bbb28bbfebdc3bb16b082c2dbfe327b11e93fdf990ce220d917` |
| provenance | `cac85660e8803e151864b7f723fe6a06c4b578539f76db0ef1594607773ff979` |
| components | `118def6a5324bec761c77e8f5785dc8dd3e8f3b6a0774eef3c4f98f8e6f39de3` |
| exclusions | `3966b82960c0ee19bbe859df9b9c6f433cf57ad78368b3dab66bf1d6f9130e18` |
| lexical canary | `f56724e50215f7d89db46726601b681277763e41c029f90568072f7ba3558cd9` |
| run config | `526c97c0510a9ad98b9a65bdc81bf0c40968e9df37ccefd749f0a8b439dc639d` |

## S1 corpus-v1 two-arm retrain

The production Queue-5 run is complete at
`/storage/milikic/leanfaith/s1_v1_7e6ef0d/`, bound to implementation commit
`7e6ef0de7913688b695ab20ddf0ad5a5e79a8c36`. It reverified the frozen corpus-v1,
tokenizer, and both encoder inputs before loading a model, acquired the shared RTX 4090 lock,
recorded an idle `nvidia-smi` preflight (1 MiB used, 0% utilization, no compute processes), and
ran the chunks-CPT arm followed by the statement↔proof-CPT arm. Both completed on attempt 1 in
about 12 minutes total, with 1,174 optimizer steps and 117 warmup steps each.

These are corpus-v1 **local validation** results, not golden-dev results:

| arm | best epoch | validation bal-acc | validation AUPRC | validation acc | validation loss | swap disagreement | wall time |
|---|---:|---:|---:|---:|---:|---:|---:|
| chunks-CPT | 2 | **0.9832** | **0.9913** | 0.9898 | 0.04085 | 0.00377 | 359.6 s |
| statement↔proof-CPT | 2 | 0.9811 | 0.9885 | **0.9903** | 0.04257 | 0.00471 | 364.4 s |

Golden-dev strict and calibrated evaluation is **blocked by the literal seal, not by a model or
trainer failure**. Scoring requires pair text, but the only available canonical text artifact
mixes `dev` and sealed `final_test`, and the evaluator loads the whole artifact before filtering.
The run manifest records reason code
`literal_seal_missing_trusted_dev_only_text_artifact`, `evaluation_attempted=false`, and
`mixed_canonical_file_opened=false`. Consequently there is no valid basis yet to claim either
checkpoint beats the prior golden-dev thresholds of 0.593 balanced accuracy or 0.849 AUPRC. A
trusted, hash-bound 821-pair dev-only export is the required input to fill the four blank table
rows above.

| artifact | SHA-256 |
|---|---|
| queue manifest | `93c126cad7bcec1923b42c935b5e138afded1dde40544f9a5247112dc8aeb650` |
| GPU preflight | `4a75e7696f6679488b9e616236f8f225545e688bcd64135805563f54c9e23a63` |
| chunks-CPT trainer manifest | `ed94dba87f59d8dbdfefd466ac0a1ab546515dae0f02a64f454a48b525966a52` |
| chunks-CPT best checkpoint | `41a3afae202e23a5327e11e99e138e4065160677f8fe8a2c81dc9f6cfcafaf4b` |
| statement↔proof-CPT trainer manifest | `66544e5f6796261573d06c6ada59c21a030acee96235cd3c50e7a493c6103e14` |
| statement↔proof-CPT best checkpoint | `d034937ccd981fe487b18c48060c33517050632648835cb41bad8e4ab1754880` |
| tmux log | `41ebf7f6f29a590144ba4d1e80fed2db804b5a8a0121a04d4a4103a8d62313d2` |

## Meta-engine slice 2

Meta-engine slice 2 is complete in `LeanFaith/Meta/TransformEngine.lean`, with the resumable
production runner in `leanfaith.corpus2.meta_slice2`. The production root is
`/storage/milikic/leanfaith/meta_engine_slice2_6ace45e/`, bound to implementation commit
`6ace45ec78b064ec952a5528d3dc72bb26a0038c`, mathlib revision
`d568c8c09630de097a046763c17b9ea99f95f950`, runner SHA-256
`4e8d87706902255d07d308634add5eeb32b55e0b1ff74748d984ac443561f2c7`, and engine SHA-256
`33a9b8449c6ea16f3b7b106c5e0487e9508b3b4bac8b5fd9937aa86051c82e67`.
The frozen selector chose exactly 500 unique public mathlib declarations from the 27,786-row
representation store (SHA-256
`7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7`); the ordered-name SHA-256 is
`1230b5bab24c2a55a4d3991f838aca8dab35adb75577c7eddd34d17b2f86f76c`.
No private source was used or externally transmitted, and `final_test` remained sealed.

The engine now traverses nested proposition subexpressions with stable paths, emits P20 exact
delta-unfold candidates with independently reconstructed inverse-fold certificates, performs
exact single-step P21 beta/zeta rewrites, and applies nested P23/P24 schema rewrites. Candidate
records bind exact SHA-256 hashes of source and candidate pretty text. The batch runner uses
immutable per-attempt artifacts, process-group cleanup, deterministic midpoint replay, and a
size-scaled timeout policy: primary attempts use 30 seconds/name with a 180-second floor; audit
attempts use 5 seconds/certificate with a 120-second floor; both have a 900-second ceiling.

The 7 h 18 m production run accounted for all 500 declarations: **393 complete**, **93
fail-closed `sourceTextRejected`** because their pretty source text did not roundtrip to the same
typed expression, and **14 explicit `externalTimeout`** terminals at the 180-second singleton
limit. No Lean `error`, `notProp`, or `notfound` terminal occurred. The 393 yielding declarations
produced **16,138 accepted candidates**, with 15,350 (95.1%) at nested sites:

| family | accepted candidates | operation detail |
|---|---:|---|
| P20 | 7,813 | exact unfold; inverse fold independently certified |
| P21 | 8,078 | 4,039 beta-introduce + 4,039 zeta-introduce |
| P23 | 102 | 93 curry + 9 uncurry |
| P24 | 145 | adjacent binder swap |

Observed P20 yield is unfold-only: fold is the independently verified inverse certificate, not
a global fold-search output. Observed P21 yield is introduce-only; no beta/zeta-eliminate
candidate survived on this sample. These yields are lower bounds because 93 declarations closed
before transformation and 14 reached the singleton timeout.

The engine discovered 75,920 raw candidates. Validation rejected 55,689 and deterministic
deduplication rejected 4,093; the remaining 16,138 were all independently reconstructed from
their declaration, family, operation, and site certificate. The audit verified
**16,138/16,138 (100%)**, with zero audit timeout, failure, or waiver. The run contains 307
immutable attempts: primary 71 accepted + 60 timeout/bisect + 14 timeout/terminal, and audit 162
accepted; all 307 process journals end with `group_gone=true` and none was abandoned. The
standalone verifier replayed selection, both attempt trees, byte-exact aggregates, all hashes,
and complete audit coverage successfully.

| artifact | SHA-256 |
|---|---|
| production manifest | `9e2425f17a44fa2005d2856c290b2f551a19e46c8a10bf0ae9888875ab311fe0` |
| summary | `497e5a17a7f8875ed2241aef4d4a26a84992073c07626766a458d61a93908093` |
| selected declaration names | `1230b5bab24c2a55a4d3991f838aca8dab35adb75577c7eddd34d17b2f86f76c` |
| primary aggregate | `61acf7436e03025a173249360915833dcbba7527d1b2504e10235445922a59f8` |
| independent-audit aggregate | `ae77075722c1942e018a0402b8e8700d0d86e6ee6484dd35168cec81e92e6957` |
| tmux log | `14e86d0a3f7be52d793c89f6150bb278a0ee8a57f39566c1ab42d8c7057f0b5c` |

Failed development runs remain untouched for provenance. The first run at
`/storage/milikic/leanfaith/meta_engine_slice2_85cddb2/` exposed cumulative Lean heartbeats; the
monolithic v2 run at `meta_engine_slice2_717f057/` hit its exact 7,200-second outer timeout; and
the fixed-timeout resumable v3 run at `meta_engine_slice2_f444709/` was cleanly interrupted after
showing that a 900-second timeout at every bisection level wastes an hour per pathological
declaration (failure-manifest SHA-256
`ecf49ac54b74049b3eea5957ab938ced6ba398d7b594ccb184a7b37c34f3c5c0`). The v3 process journal
proves its interrupted group was removed; the earlier roots predate per-attempt journals, and a
current process scan is clean. No failed-run partial stdout was admitted to the production
aggregate.

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
- Typed Lean Meta transform engine slice 2 COMPLETE: nested sites, P20/P21, exact type-text
  hashes, resumable batch execution, and independent reconstruction audit. The exact public
  500-name probe emitted 16,138 candidates and audited 16,138/16,138 at
  `/storage/milikic/leanfaith/meta_engine_slice2_6ace45e/`.
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
- Corpus v1 COMPLETE: 23,414 ancestry/shared-statement-safe rows with every stored family at or
  below 10% and lexical-canary balanced accuracy 0.700/0.680 at
  `/storage/milikic/leanfaith/corpus2/v1_ed41471/`.
- S1 corpus-v1 retrain COMPLETE: chunks-CPT and statement↔proof-CPT arms finished on attempt 1;
  local validation balanced accuracy/AUPRC are 0.983/0.991 and 0.981/0.989. Golden-dev scoring is
  explicitly blocked pending a trusted dev-only text export; the mixed canonical artifact was not
  opened. Frozen root: `/storage/milikic/leanfaith/s1_v1_7e6ef0d/`.
- Prunes P1+P2 merged: ~130K LOC removed; suite at baseline (one order-sensitive test).

## Next

1. P4 prune: retire the superseded `lf022` inventory modules and now-deletable `local_hf` path
   under the reviewed replacement constraints in `PLAN.md`.
2. Fill the four S1v1 golden-dev rows only after a trusted, hash-bound dev-only text export exists;
   do not open the mixed canonical artifact to obtain it.

# Refocus day 1 — consolidated results (2026-08-28)

All reported selection and scoring is golden-**dev** only; no `final_test` row was selected or
scored. Literal container-access provenance is qualified by the erratum below.
Headline subset = expert-labeled, non-conflicted dev pairs with at least one non-ProofNetVerif
membership (n=228, prevalence 0.689).
Eval runs: `/storage/milikic/leanfaith/golden/eval_runs/`.

> **Literal-seal provenance erratum (2026-08-29):** the four strict dev runs predate the refocus
> goal and were produced by an evaluator that decoded the mixed canonical pair container before
> selecting `dev`. Queue 1 calibration itself opened only the resulting dev prediction artifacts.
> During Queue 2, the D-3 loader likewise decoded that mixed container before selecting six
> `golden_train` few-shots. No `final_test` row was selected, scored, retained in a prompt, or
> transmitted, and no private `sft_classic` content was transmitted, but D-3 is not compliant
> with the objective's literal no-read rule. Immutable historical manifests were not rewritten.
> The additive erratum is
> `/storage/milikic/leanfaith/compliance_errata/literal_final_test_seal_2026_08_29_v1.json`
> (SHA-256 `f612f929b2954a69053b446f4d4cd2f6935786dba7cba27eeda55038d759dd88`).
> Current consumers fail closed on the mixed path/hash and require a complete, hash-bound,
> split-only export. None existed at audit time; follow-up commit `f8d7069` added the sanctioned
> exporter and used its dev-only output to score S1v1 without opening the mixed container.

## Current benchmark and training scope

- The official frozen benchmark is Golden Partition v1: 5,111 canonical pairs from 5,497 raw
  memberships — EPLA/ASSESS 1,247, BEq Human Equivalence 200, GTED 298, and ProofNetVerif 3,752.
  Frozen splits are dev 821, `final_test` 910, golden_train 819, and quarantine 2,561. Current
  model comparisons are the dev proxy described above, not a held-out final estimate;
  `final_test` remains sealed and unscored. All BEq groups are in `final_test`, while PNV-only
  records are excluded from the headline.
- S1v1 names a two-arm experiment, not one checkpoint: ModernBERT-base initialized from either
  chunks-CPT or statement↔proof-CPT, then trained with the same corpus-v1 recipe. Corpus v1 has
  23,414 pairs (18,760 train / 2,166 validation / 2,488 corpus test; 5,050 same / 18,364
  different). It contains 13,829 private-derived rows, including 11,115 training rows, so S1v1
  is not the required public-only headline checkpoint. Public Numina statement↔proof data occurs
  only in the mixed CPT arm, not as S1v1 supervised pair rows. The chunks-CPT source contains
  467,207 screened public Lean training-stream rows + 2,346 validation rows; the mixed CPT source
  contains 464,871 Lean chunks + 32,860 statement↔proof rows + 32,580 signature views, plus a
  separate 2,664-row validation slice.
- Integrated benchmark sources are EPLA, BEq, GTED, and ProofNetVerif. ACE remains planned for a
  later corpus after certificate replay; Lean Workbook has only the 30-output autoformalizer
  pilot and is absent from corpus v1. The broader legacy benchmark registry is not the current
  benchmark definition and has not been integrated into this golden evaluation.

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
calibration/selection-set results, not held-out estimates. The calibration command opened only
dev prediction artifacts; the upstream strict-run container provenance is disclosed above. The
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
| S1v1 chunks-CPT encoder (corpus v1) | strict | 0.576 | [0.544, 0.611] | 0.421 | 0.283 | 0.828 | 0.650 | 0.578 | 3.755 | — | 0.500000 |
| S1v1 chunks-CPT encoder (corpus v1) | gold-calibrated | 0.659 | [0.596, 0.718] | 0.610 | 0.651 | 0.828 | 0.650 | 0.190 | 0.694 | 1000† | 0.498336 |
| S1v1 statement↔proof-CPT encoder (corpus v1) | strict | 0.566 | [0.535, 0.605] | 0.408 | 0.254 | 0.823 | 0.666 | 0.575 | 3.039 | — | 0.500000 |
| S1v1 statement↔proof-CPT encoder (corpus v1) | gold-calibrated | **0.669** | [0.604, 0.737] | **0.693** | **0.767** | 0.823 | 0.666 | 0.190 | 0.694 | 1000† | 0.498414 |

† All six NLL fits reached the configured positive-temperature upper bound (`T=1000`, inverse
temperature `0.001`). Under the required temperature-only model, the NLL optimum is toward zero
logit scale: probabilities move close to 0.5 while the fitted threshold preserves ranking. This
exposes a large intercept/prevalence mismatch that a single temperature cannot remove; the
remaining ECE ≈0.19 should not be described as well calibrated.

‡ RESOLVED 2026-08-29: `leanfaith-eval export-golden-splits` (the sanctioned one-time reader
inside the freeze machinery) emitted complete hash-bound split-only files at
`/storage/milikic/leanfaith/golden/canonical/splits_v1/`; the S1v1 rows above were scored through
the hardened split-only evaluate path. The original Queue-5 manifest remains immutable and
correctly records that scoring was blocked before this exporter existed.

Artifacts are under `/storage/milikic/leanfaith/golden/eval_runs/`:

| run directory | calibrated predictions SHA-256 | calibration SHA-256 | metrics SHA-256 | manifest SHA-256 |
|---|---|---|---|---|
| `dev_m1_bc426653968b_gold_calibrated` | `7637cce03797afbbea4d5dca7910290398bd8f67774edbb8ace9deec445d36fe` | `982696a3b7db9a90cc811b8da695835592cbd733270a71602a3de8694ed3401c` | `1e668166cdb54e7402a895b9de2245c45deabe89fa54de2c24e4075ae075005c` | `4e4c6510c4b1093e7a41b29710da46221ed67cca541e5536ea2725f495464e3e` |
| `dev_s1v0_stock_a55db24b754a_gold_calibrated` | `a5a70a072b401d6c9a8491ace87873fe3e06100562402b053343bd2b2a1102bc` | `878f6858368e87dddebf474604548e85592ed98fe69efac09aeb5fa926100135` | `944fa6cf619f624e0e3a0051f380adb66f792987cc911d4cb2dfbb7455172a85` | `d34caa9ec5e379c4f2b82c6e537f6442d24597b5c1a3fa2178c033d002c3dcce` |
| `dev_s1v0_cpt_chunks_3cb6b43950ae_gold_calibrated` | `f173483f9766b4d6e086265f4324d0d8c23e6b8f661368f3992df4af5f274cdc` | `a8125116a1da21e3f28cb52d27d5c63aa852261e7e26249c4684ed11b36dc1fa` | `1e6fa996bea80338a47970b264590cfe3255d4febfdf21b23e1a2b881d84a184` | `567ff3de4efe59edce0064cece1366e8935d82e569e32a63bf05784a5fefb190` |
| `dev_s1v0_cpt_mixed_f6de1e96a6f0_gold_calibrated` | `e807c55271d30a99da3d37d0b8e031d2de897af5d261fff893504f312f2e635b` | `4821ca28e4df75cd5aaf6871f3c6ca56b63e0bd02a147d026bba728f9c36d405` | `dd0281c191b80e52cf4c7f612d87396c65269d05096da4a590d0a61e5e8e8569` | `3ac32836da903e072253b8737e2f6002a09b276142e75a6608a486ff82f71e1c` |
| `dev_s1v1_cpt_chunks_41a3afae202e_gold_calibrated` | `faa24d054d9661516ae794a2011f3b33a71d39a0254315e5ac58e4fc6762525d` | `11be20e0ba9290908db0c64e0c96b7001cb72e89fd76c847a67a765e3fd7fb6d` | `7d471109abd73219c3db5c0e8bc2ecfea0ef6cc6c89677c0221f9875eb7e909b` | `b55e8ae8262c1c2d7402b95ebedb6de7ceb19e1c5714b0741cd743b0709b9420` |
| `dev_s1v1_cpt_mixed_d034937ccd98_gold_calibrated` | `0e3f55dcb2669482fb47fa7d928e1c6691a4567ba38621d7d126225cdec30249` | `4a932382d589c7c817194c766f70e8d71aa6d2743a783ca652c5b217bdf98783` | `4e76d93dca3f9ac42b39444bf47140e2af613a59817ab5ea3e6ef71e4f9c2aad` | `6684dd0473ff0d1b8405a1752b5f3c0da74aa635d226038531c8b5f4d9934f2c` |

## Findings

1. **Training data is the binding constraint.** All S1v0 arms saturate proxy validation
   (AUPRC ≥ 0.998, swap-disagreement ≤ 0.002) yet transfer WORSE on thresholded golden
   metrics than the older 1-epoch checkpoint. Longer training on the shortcut-riddled
   corpus actively hurts. This is the Track D data-engine thesis, now measured.
2. **S0 encoder adaptation improves ranking transfer.** On identical data, the
   chunks-CPT encoder gains +0.06 AUPRC / +0.13 ROC-AUC over stock — the best ranking
   numbers of any model to date. The statement↔proof-CPT encoder did not beat chunks on
   this corpus; retest once corpus-S2 carries real statement-level variety.
3a. **Corpus v1 verdict (2026-08-29):** thresholded transfer improved strict (0.522→0.576
   bal-acc, ECE 0.646→0.578) but calibrated bal-acc/AUPRC did not beat S1v0-chunks (0.659 vs
   0.684; 0.828 vs 0.849) — the judge flooded v1 with negatives (21.6% positive), so positive
   scarcity is now the binding constraint. Two genuine wins: the statement↔proof encoder
   OVERTAKES chunks on corpus v1 (0.669 vs 0.659 calibrated; best F1 0.767 and best raw
   accuracy 0.693 of any model — first to reach the majority baseline), flipping the v0
   ordering exactly as the signature-CPT hypothesis predicted once training data carries real
   statement-level structure. All CIs overlap on n=228 — treat ordering as directional.
   Next levers: the 16,138 already-audited Meta-engine positives and a prevalence/bias-corrected
   head (every temperature fit hits the boundary). All 146 current D-3 rows are already in corpus
   v1, so they must be retained rather than re-imported; further D-3 scale is a later generation
   decision, not part of the immediate repair build. NOTE: corpus v1 contains private-derived
   rows — the headline checkpoint still requires a public-only corpus variant per PLAN
   data-scope policy.
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
revision `d568c8c09630de097a046763c17b9ea99f95f950`, the frozen golden-train few-shot selection,
and hashed input/output artifacts. Its six prompts contained only selected `golden_train`
examples and no private source was sent. However, the local loader decoded the mixed canonical
container before filtering, so the run is not literal no-read compliant; see the erratum above.
No D-3 regeneration was required for corpus v1: the completed recovered-pair judge processed the
13,373 Qwen/Kimi pairs, and corpus v1 consumed this run's `trainer_records.jsonl` directly.

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

These corpus-v1 **local validation** results describe checkpoint selection; the separate
golden-dev follow-up below describes transfer:

| arm | best epoch | validation bal-acc | validation AUPRC | validation acc | validation loss | swap disagreement | wall time |
|---|---:|---:|---:|---:|---:|---:|---:|
| chunks-CPT | 2 | **0.9832** | **0.9913** | 0.9898 | 0.04085 | 0.00377 | 359.6 s |
| statement↔proof-CPT | 2 | 0.9811 | 0.9885 | **0.9903** | 0.04257 | 0.00471 | 364.4 s |

At the time the training queue closed, golden-dev evaluation correctly failed closed: the only
pair-text artifact mixed `dev` with sealed `final_test`. The immutable queue manifest therefore
records reason code `literal_seal_missing_trusted_dev_only_text_artifact`,
`evaluation_attempted=false`, and `mixed_canonical_file_opened=false`.

Follow-up commit `f8d7069` resolved that operational blocker without weakening the seal. The
sanctioned freeze exporter emitted complete hash-bound files for all four partitions, and the
hardened evaluator consumed only the 821-pair dev export. On the 228-pair expert headline subset,
chunks-CPT scored strict/calibrated balanced accuracy 0.576/0.659 with AUPRC 0.828;
statement↔proof-CPT scored 0.566/0.669 with AUPRC 0.823. The mixed arm leads S1v1 calibrated
balanced accuracy/F1, while chunks leads ranking AUPRC. Neither beats the prior S1v0 chunks-CPT
marks of 0.684 calibrated balanced accuracy or 0.849 AUPRC, and all CIs overlap; this is a
directional dev result, not a final held-out claim.

| artifact | SHA-256 |
|---|---|
| queue manifest | `93c126cad7bcec1923b42c935b5e138afded1dde40544f9a5247112dc8aeb650` |
| GPU preflight | `4a75e7696f6679488b9e616236f8f225545e688bcd64135805563f54c9e23a63` |
| chunks-CPT trainer manifest | `ed94dba87f59d8dbdfefd466ac0a1ab546515dae0f02a64f454a48b525966a52` |
| chunks-CPT best checkpoint | `41a3afae202e23a5327e11e99e138e4065160677f8fe8a2c81dc9f6cfcafaf4b` |
| statement↔proof-CPT trainer manifest | `66544e5f6796261573d06c6ada59c21a030acee96235cd3c50e7a493c6103e14` |
| statement↔proof-CPT best checkpoint | `d034937ccd981fe487b18c48060c33517050632648835cb41bad8e4ab1754880` |
| tmux log | `41ebf7f6f29a590144ba4d1e80fed2db804b5a8a0121a04d4a4103a8d62313d2` |
| sanctioned dev split | `7687baf621178621ab4b62525b17841e88a38a8db7c73da9841e357028ba9d37` |
| dev split manifest | `036aeaa4c6143b1ea41b88532e7b02a1ef05693147a662f4e936ca460ac6bec4` |
| split-export run manifest | `264106774074060f7a60054e601dcec288d88aad5834fd8aed50adb736283f19` |
| chunks-CPT strict metrics | `83c4d79c38a16cc3557e2834144c9c7b89456fa9f4673bf89b51879966f62a95` |
| chunks-CPT calibrated metrics | `7d471109abd73219c3db5c0e8bc2ecfea0ef6cc6c89677c0221f9875eb7e909b` |
| statement↔proof-CPT strict metrics | `533baf2f96c455bbcad8260d83c5b6355bf1899b0e50ee0aadcc6973b7ff02a1` |
| statement↔proof-CPT calibrated metrics | `4e76d93dca3f9ac42b39444bf47140e2af613a59817ab5ea3e6ef71e4f9c2aad` |

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

## P4 prune (`2400623`)

P4 is complete after the 17,873-variant recovery, recovered-pair judging, and corpus-v1 merge
removed the last need for the old point-in-time inventory builders. The prune removed the two
LF-022 inventory producers, `generation/local_hf.py` (replaced by the live-piloted `collect2/`
package), and the completed Kimi-v4 selection/requalification/eligibility/prefix-QA/historical
replay chain. Six associated CLI commands, one script, and eleven obsolete test files were also
removed. The generic LF-022 executor and its Qwen/GLM/DeepSeek route-qualification verification
remain because retained execution code still imports them.

Exact code/test/script delta: **24 files, +4/−14,825 lines**. Verification: deleted-module import
sweep empty; collection clean; 148 focused collect2/executor/consumer tests green; all **2,511
unit tests green**; Ruff green; strict mypy green across 238 source files. This deletion never
opened a golden pair-text artifact; `final_test` remains sealed and unscored.

## S1 public-repair contract and one-row smoke (`259b6b9`)

The additive repair contract now binds the exact public projection of corpus v1: **9,585 rows**
(2,351 positive / 7,234 negative; train 7,645 / validation 942 / test 998). It verifies the
trainer/provenance join and public release flags row by row, and explicitly confirms that all 146
D-3 records are already present. It also freezes the eleven input hashes, the completed
16,138-candidate Meta pool, the 16,138 independent audit rows, and the PLAN/catalog-v2 diversity
caps. This is a new versioned path; frozen corpus-v1 semantics were not changed.

Before any full materialization, the Meta attempt/audit tree replayed successfully in 5.1 seconds
without invoking Lean. Candidate 0 — a P20 `unfold:DFunLike.coe` transform of
`MeasureTheory.IsFundamentalDomain.measure_ne_zero` — was joined on the five-field candidate key to
its exact `verified=true` independent-site-reconstruction audit, screened against the golden
blocklist, and emitted as one trainer row plus one repair-provenance row. The smoke manifest
records `lean_reexecution=false`, `external_calls=false`, and `final_test_accessed=false`.

Frozen root:
`/storage/milikic/leanfaith/corpus2/s1_public_repair_smoke_v1_22386b7_9e2425f/`.

| artifact | SHA-256 |
|---|---|
| smoke manifest | `32f825b94d77ad578372537dfdc45a10c8a9dfbdeaeb9559ace3ae6687feaf49` |
| trainer row | `9a8c712a53626baacac2d2abe54138b4d0990f044825a05bf389c3b7240d4c0a` |
| repair provenance row | `ca81836dd7ff0a132c4ed04f7593575a9335b13294c7731007c4f30211bf10bc` |

Verification: six new contract/conversion tests plus all 13 corpus-v1 builder tests are green;
collection is clean; Ruff and format are green; strict mypy is green across 239 source files.

## S1 public-repair full diagnostic (`f4ab970`)

The full builder reused the frozen public projection and all 16,138 audited Meta candidates; it
did not rerun Lean. It applied the contract caps to new admissions (four direct rewrites per
declaration; family 8%; mechanism 15%; exact template 2%; exact P20 lemma 0.5%) and capped the
negative-heavy recovered-judge source at 20% of the final corpus while explicitly protecting all
146 already-admitted D-3 rows. Pair deduplication, golden-blocklist screening, anchored
union-find splits, both-orientation 1,024-token screening, and the lexical canary were replayed.

The first attempt stopped before materialization because the reused Meta provenance IDs were not
in the lexicographic order required by the corpus-v1 candidate schema. The ordering was fixed and
covered; attempt 2 completed. This was a schema-ordering failure, not a Lean or data failure, and
no partial corpus root was admitted.

Final diagnostic counts: **7,488 public/releasable rows**, 3,354 positive / 4,134 negative
(44.8% positive), split train 5,940 / validation 770 / test 778. Source memberships are public
v0 4,541; recovered judged 1,497; Meta 1,086; deterministic depth-3 586; D-3 146. Six Meta pairs
were overlength; 14,560 candidates were removed by the four-per-declaration ancestry cap and
3,669 pairs by the fixed-point source/family/mechanism/template caps. There are zero private rows.

All structural gates pass, but the training gate correctly fails: the lexical-only canary reaches
**0.853 validation / 0.841 test balanced accuracy**, above the required `<0.72` ceiling.
Therefore this is a diagnostic corpus, not an authorized training input. Training was not
launched. The measured next problem is no longer positive prevalence; it is public
source/transformation shortcut leakage, requiring source-matched certified negatives on the same
mathlib declaration distribution as the Meta positives.

Frozen root: `/storage/milikic/leanfaith/corpus2/s1_public_repair_v1_22386b7_9e2425f/`.

| artifact | SHA-256 |
|---|---|
| corpus manifest | `3d72e9923f08242b44d3e4f012d6e8c6aec8cf12783ef67becaf8bddb1b01a85` |
| selection summary | `17c3ae06b961a0248cd71f92a02402163661b58459837638606e0a8e94202634` |
| lexical canary | `ae937c145e2ce476789e2900b6ad78aff759085ce593baa11d02cbdc6fc945bf` |
| completed tmux log | `44dfbd2e405b833d223befd12050ff320be2c725d783e668c954e83e26d33e75` |

Verification: six full-build selection/cap tests plus the six repair-contract tests and all 13
corpus-v1 builder tests are green (25 focused tests); collection is clean; Ruff/format are green;
strict mypy is green across 240 source files. Independent artifact replay verifies output hashes,
trainer/provenance joins, public policy, D-3 retention, ancestry/statement split isolation, all
stored cap memberships, and `final_test_accessed=false`.

## S1 public N19 one-declaration certificate pilot (`117add3`)

The next queue was deliberately reduced to one theorem before any new scale run. The pilot reused
the exact public source row from the positive repair smoke,
`MeasureTheory.IsFundamentalDomain.measure_ne_zero`, retained its mathlib-declaration ancestry
group, and emitted the whole-claim negation as one label-false N19 / `N-PROOF` row. A bounded Lean
driver then proved the source claim from the pinned mathlib theorem and proved the negated
candidate false by applying it to that source certificate.

Lean compiled exactly one driver in **2.632 seconds** with a 120-second hard timeout. Yield was
1/1, exit status was 0, stderr was empty, and both axiom reports contained only `propext`,
`Classical.choice`, and `Quot.sound`; no `sorry`, `admit`, `native_decide`, or non-kernel external
evidence was admitted. The run made no external calls and did not access `final_test`. Its tmux
pane remains preserved as `lf_n19_pilot` with exit status 0.

This result validates only the certificate and trainer-projection pathway. One pair cannot
estimate a corpus-level canary effect, and whole-claim negation is itself an easy lexical
template. The manifest therefore freezes `canary_effect=not_estimable_from_one_pair`,
`scale_authorized=false`, and `training_authorized=false`. The next evidence-bearing decision is
a small multi-declaration pilot, with N19 capped and less trivial source-matched `N-SEP`/`N-PROOF`
families prioritized; another 500-declaration run remains unauthorized.

Frozen root:
`/storage/milikic/leanfaith/corpus2/s1_public_negative_n19_smoke_v1_32f825b_d568c8c/`.

| artifact | SHA-256 |
|---|---|
| manifest | `8e1f39d795f3479a09c5d0e440f1be0c38d0978a5f6c2cb1cdf2f2dd27c5dd27` |
| trainer row | `69fa2f9289c40182fe0f5eb2878aad8f0e333a7a0279e42730116e1d90c6106a` |
| N-PROOF certificate | `fc779cb643695db655b67a9a2ceb688b810f88333424610706bfa0ffd47d1b6e` |
| Lean stdout / axiom audit | `fa1be7006ae086ea1ab9e3a49642588fe4f10c8c130daf3086f6c5abc9aa98f3` |
| process record | `1a9b434e3e977a99dcd38f36f153f55df954b296d444e228398df4a149fc2ead` |

Verification: independent artifact replay is green; seven new unit tests cover deterministic
projection, golden blocking, atomic/idempotent materialization, compiler failure, forbidden axiom
evidence, and mutation detection. The 32 focused repair/builder tests, collection, Ruff/format,
and strict mypy across 236 source files are green.

## Typed N21/N22 96-declaration separator pilot (`99c4431`)

The official small negative pilot froze **96** already-admitted Meta-positive mathlib declaration
groups before execution: 72 train, 12 validation, and 12 test. Its versioned Lean engine admitted
only typed root-body And/Or/Iff mutations, attached an exact two-atom truth-table separator under
the declared abstract-schema contract, retained at most one mutation per declaration, and required
independent reconstruction of every selected candidate. The preregistered gates required at least
24 total certified rows, at least four validation and four test rows, N22 ≥60%, N21 ≤40%, no
operation above 40%, at least 0.01 absolute full-canary improvement on both splits, and a paired
shortcut canary below 0.72.

Primary Lean completed in **13.127 seconds** and emitted 39 candidates. The deterministic family
and operation chooser admitted 13 rows—5 N21 and 8 N22—with no blocklist, duplicate, or overlength
exclusions. Independent Lean reconstruction then verified **13/13** in **3.655 seconds**. Both Lean
processes exited 0 with empty stderr under the pinned mathlib revision and 120-second per-stage
timeouts; no external call or `final_test` access occurred.

The pilot correctly failed its advancement gate:

- Yield was 11 train / **0 validation** / 2 test, below 24 total and 4/4 diagnostics.
- Family mix passed (N22 61.5%, N21 38.5%), but `iffToImp` occupied 7/13 = **53.8%**, above the
  40% operation cap.
- The augmented full canary was 0.8445 validation / 0.8307 test versus 0.8527 / 0.8410 baseline:
  improvements of **0.0082 / 0.0104**, so validation missed the required 0.01.
- The paired shortcut canary was not estimable because no validation mutation yielded.

Therefore `pilot_gate_passed=false`, `scale_authorized=false`, and
`training_authorized=false`. Increasing the sample size with the same root-only generator is not
an acceptable repair: it would preserve the measured applicability and template-concentration
failure. The next bounded implementation target is a new version that traverses the full
propositional conclusion skeleton, proves nested-site root influence by an exact full-skeleton
truth table, and first passes one nested-declaration generation/reconstruction smoke.

Frozen root:
`/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_pilot_v1_3d72e99_d568c8c/`.

| artifact | SHA-256 |
|---|---|
| manifest | `78054484ddabdf0da24988dc8651c4d194b52b66f792d3f788b39cb2a75bfa4a` |
| summary | `487b1038f5e5f0e389831f5720bde68f3dcb7cdae908de698f4c2cd183e6f19d` |
| selected candidates | `2fdd8430fa071b43b58828ca08e3fe338e96e148dd08ffdfd6319eaa17ff7718` |
| trainer rows | `ed3de29d24296eeb8958637130438663b16a2e496c2a90d1e7ecb1ee654e24f5` |
| N-SEP certificates | `936252c3432d0990d567bff2634ca23065126a85935229e736f4f79518299ab2` |
| augmented canary | `61ebd18c09ae75e3782ac6b28724bc281c74b799917148196656648ec1962f73` |
| primary process | `93bafee797ba4a4e0d8713a5a707517f56f9577c20d831652d6579f9766bb3bd` |
| audit process | `4e5f61ae261b2083c98297d3d2a611a687a945ac72e599fd85d309d85878b5c0` |

Verification: independent artifact replay reselects the 96 source groups, revalidates all input and
output hashes, reparses the primary and audit streams, reconstructs trainer/certificate rows, and
recomputes all three canary decisions without rerunning Lean. Six new offline tests plus the prior
32 focused repair tests are green; the Lean engine compiles directly; collection, Ruff/format,
and strict mypy across 237 source files are green.

## Full-skeleton nested N21/N22 smoke (`0c8919a`)

The versioned v2 engine closes the measured nested And/Or/Iff applicability gap without modifying
the frozen root-only engine. It strips expression metadata, traverses the complete And/Or/Iff/Not
post-telescope body skeleton, deduplicates atomic propositions, caps each truth table at eight
atoms / 256 valuations, and exhaustively evaluates every valuation before retaining a nested
mutation with a separating root value. Each candidate binds its logical site path, full source and
candidate skeletons, atom hashes, valuation-space size, separating valuation, and re-elaborated
whole-type hash. The audit command independently regenerates the declaration/family/operation tuple
and requires the exact candidate hash and separator contract. The fixed-sample pilot below exposed
that unrestricted telescope opening consumes source implication arrows; the earlier “full
conclusion” wording is therefore narrowed here to the exact implemented post-telescope scope.

The smoke reused ordinal 13 of the frozen 96-declaration selection, the public train-split theorem
`NonUnitalStarSubalgebra.mem_prod`. Primary Lean completed in **4.745 seconds** and emitted five
typed candidates. The preregistered target was the nested N22 operation
`andToOr:/root-body/right`, changing the full skeleton from `(A0 ↔ (A1 ∧ A2))` to
`(A0 ↔ (A1 ∨ A2))` over all eight valuations. It re-elaborated with candidate SHA-256
`70896b1701362535e27f29a064a8afcf015cb4fe00206ef9c9db3c59dac07bd1`.
Independent Lean reconstruction completed in **3.851 seconds** and reproduced that exact hash.
Both stages exited 0 with empty stderr under 120-second hard timeouts.

All smoke gates pass, but the decision is deliberately narrow:
`same_fixed_96_pilot_rerun_authorized=true`, while `sample_size_increase_authorized=false`,
`scale_authorized=false`, and `training_authorized=false`. The run used only the selected public
train declaration, made no external call, and did not access `final_test`.

Frozen root:
`/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_nested_smoke_v1_7805448_d568c8c_exhaustive/`.

| artifact | SHA-256 |
|---|---|
| manifest | `fd86cc34789944ef285d8877ade408b326b9ecdf82a449a858122172004a549b` |
| summary | `1144de6c266ff325c5bc81fb4ed2a74ad74c53b288bb76f4fd5d7532200daf1f` |
| selected candidate | `adccadbe0d601081a62941ace1e9b0edd4bcc222a89ccba7c57e9a4541672292` |
| primary process | `cc9bf699bd2b2cab785cbfd2178d8d13ae560457c847098e072cfe19445b9676` |
| audit process | `761da0271743052bdbbd04b4b2a1aa42a2d5885ad2a8b591e80a416dfc617ede` |

Verification: independent artifact replay revalidates the frozen root-pilot manifest and
selection row, all input/output hashes, exact generated drivers, primary candidate inventory,
selected nested operation, audit stream, summary, privacy boundary, and no-training decision.
Eight new offline tests are green; the v2 Lean engine compiles directly; the 33 focused public
repair/negative tests, collection, Ruff/format, and strict mypy across 238 source files are green.

## Full-skeleton v2 fixed 96-declaration pilot (`861e010`)

The v2 runner reused the exact v1 selection artifact byte-for-byte: 72 train, 12 validation, and
12 test declarations. It kept every threshold unchanged, bound the passed nested-smoke manifest,
and interpreted the operation cap by logical operation kind so unique nested paths could not evade
the template-diversity gate. Primary Lean completed in **25.872 seconds**, emitted 136 typed
candidates, and covered all 96 declarations. The frozen one-per-declaration chooser admitted 96
rows with zero blocklist, duplicate, near-identical, or overlength exclusions. Independent Lean
reconstruction then verified **96/96** in **9.372 seconds**; both Lean stages exited 0 with empty
stderr under 120-second hard timeouts.

The new engine fixed yield but exposed a different shortcut:

- Yield passed at 72 train / 12 validation / 12 test, and independent audit passed 96/96.
- Family and operation diversity failed: N21 `negateAtom` occupied **87/96 = 90.6%**, while N22
  supplied only 9/96. The full emitted pool contained 116 N21 and only 20 N22 candidates, with N22
  available for 17 declarations.
- The full-corpus canary improved enough: baseline 0.8527/0.8410 validation/test fell to
  0.8309/0.8164, absolute improvements of **0.0217/0.0246**.
- The paired canary still exposed the template: **0.875 validation / 0.667 test**, so validation
  failed the `<0.72` ceiling.

The dominant root negation is explained by a concrete implementation boundary. The engine calls
Lean's unrestricted `forallTelescope`, which opens nondependent proposition binders along with
ordinary parameters. Consequently, source implications are removed before the Boolean parser
runs; v2 is exhaustive over the remaining And/Or/Iff/Not body, not over implication nodes. The
next bounded repair is implication-aware telescope handling plus multiple N22 implication
operations, followed by a distribution-feasibility check on the same 96 names before any canary.

Therefore `pilot_gate_passed=false`, `scale_authorized=false`,
`sample_size_increase_authorized=false`, and `training_authorized=false`. No corpus rebuild,
training, external call, or `final_test` access occurred.

Frozen root:
`/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_pilot_v2_3d72e99_d568c8c/`.

| artifact | SHA-256 |
|---|---|
| manifest | `4fd2c6a769d28d24322f7cedbfc5a2a01ef9edec5e2686eed74add1b914dbe44` |
| summary | `e9fd7fc3ee11b5b23b06ddc85dea2e0f45f25ddbe4952aa965f515ef597f1727` |
| selected candidates | `76e0271d262cf0f217bbc3e0ed63f5d8c3f0b0ba5669e780eba21ada270bdf93` |
| trainer rows | `14790f63e753de274757fa4fe29e2fd745f1800e17b5c32e123402b8847076db` |
| N-SEP certificates | `84571655e42989c4b1d8a8a2e309eeae6737cff62703b2dfd680bc6895eceb92` |
| augmented canary | `d1aaa0c0a094d241b63267b12013cf1e8c6bd528a0aff6ae941c3ced48f3df26` |
| paired canary | `a2f03d5155da89b42ee217011c5f1986e3cb3c999e192b21b6a5b4f55dd46dc6` |
| primary process | `6b3e5ca327d209c671f19c387417f6b0ed1819ed5be47a105a96a5a18b2ad5b8` |
| audit process | `c508c9fa161f6fb60be04af22128f6f692ddfba92650dcf329c7b05a6ea7c704` |

Verification: a fresh artifact replay revalidates the v1 base runner, passed nested smoke, exact
selection, all input/output hashes, generated drivers, 136-candidate primary stream, chooser,
projection, 96-row audit stream, and all three canary decisions without rerunning Lean. Seven new
offline tests plus the prior 33 focused public-repair/negative tests are green; collection,
Ruff/format, the v2 Lean compile, and strict mypy across 239 source files are green.

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
  (10/10), lemex conditional (8/10), claude blocked on CLI login at pilot time.
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
  The old formal step-5 inventory path was superseded by the completed recovered-pair judge and
  corpus-v1 merge.
- D-3 Codex scale COMPLETE: 200 provider calls, 192 Lean-valid outputs, and 146 strict
  trainer-schema records at
  `/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1_f88931b/`; numerical output is
  retained, but literal no-read provenance is noncompliant and recorded in the erratum above.
- Recovered-pair judge COMPLETE: 13,373/13,373 processed, 13,367 resolved trainer records,
  10 escalations, 6 fail-closed null labels, and a 150-pair audit sample at
  `/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/`.
- Corpus v1 COMPLETE: 23,414 ancestry/shared-statement-safe rows with every stored family at or
  below 10% and lexical-canary balanced accuracy 0.700/0.680 at
  `/storage/milikic/leanfaith/corpus2/v1_ed41471/`.
- S1 corpus-v1 retrain COMPLETE: chunks-CPT and statement↔proof-CPT arms finished on attempt 1;
  local validation balanced accuracy/AUPRC are 0.983/0.991 and 0.981/0.989. Follow-up `f8d7069`
  exported the hash-bound dev split and completed strict + calibrated golden-dev scoring through
  the hardened split-only path. The mixed canonical artifact was not opened. Frozen training
  root: `/storage/milikic/leanfaith/s1_v1_7e6ef0d/`; eval roots under
  `/storage/milikic/leanfaith/golden/eval_runs/dev_s1v1_*`.
- Prunes P1+P2+P4 complete: ~145K LOC removed. P4 removed 14,825 lines; all 2,511 unit tests,
  Ruff, and strict mypy are green.
- S1 public-repair contract + one-row live smoke complete: exact public baseline and Meta audit
  pool bound; one audited candidate projected without rerunning Lean or touching `final_test`.
- Full 7,488-row public-repair diagnostic materialized and replay-verified; structural gates pass,
  but its 0.853/0.841 lexical canary fails the `<0.72` training gate, so no retrain was launched.
- One-declaration public N19 / `N-PROOF` pilot complete: 1/1 kernel-certified in 2.632 seconds on
  the exact positive-smoke mathlib source; it validates the certificate path but explicitly does
  not authorize scale or training because one pair has no measurable canary effect.
- Typed N21/N22 96-declaration pilot complete and failed closed: 13/13 selected candidates passed
  independent Lean audit, but yield, operation diversity, validation improvement, and paired-canary
  coverage failed. Scale and training remain unauthorized.
- Full-skeleton N21/N22 one-declaration smoke complete: five typed candidates generated on the
  exact frozen train theorem; the required nested N22 candidate passed exhaustive eight-valuation
  root separation and exact independent reconstruction. Only the same fixed 96-declaration rerun
  is authorized; sample-size increase, scale, and training remain unauthorized.
- Full-skeleton v2 fixed 96-declaration rerun complete and failed closed: yield and full-canary
  improvement pass, but 87/96 selected rows are the same N21 root-negation template and the paired
  validation canary is 0.875. The result identifies implication-aware skeleton parsing—not a larger
  sample—as the next bounded repair.

## Next

1. Add an implication-aware v3 Boolean-skeleton engine with at least two separated N22 implication
   operations, then prove one implication declaration through generation and independent audit.
2. On the same 72/12/12 names, require a deterministic subset satisfying all yield/family/operation
   constraints before fitting canaries. Stop if infeasible; do not enlarge to 500 declarations.
3. Only after feasibility passes, rerun the unchanged full-canary and paired-canary gates.
4. Rebuild a new versioned public corpus only after the small pilot passes. Require all current
   gates and lexical-canary balanced accuracy `<0.72`; do not train the failed 7,488-row
   diagnostic or relax the gate.
5. After that gate passes, smoke one batch/checkpoint, retrain both S1 arms, and score only golden
   dev against S1v0 chunks-CPT (0.849 AUPRC / 0.684 calibrated balanced accuracy).
6. Run Track T-B Stage A separately after its registry and gap/cloze diagnostics are frozen.
   Keep `final_test` sealed and unscored.

# EVAL v2 baseline status — 2026-08-30

## Frozen evaluation split

The evaluation-only split is frozen independently of REPR and contains every one of the 5,111
canonical pairs: 2,555 validation and 2,556 test, with all 591 groups assigned wholly to one side.
There is no training view and no gold export for training. The split assignment content SHA-256 is
`13c392d1775d4f6df3e495644bada88730ac197015ca9e4cea31950d77917ec7`; the pinned canonical
input SHA-256 is `5f26c9b1b126e8bc9fe714f3c17fe68ad1d9b3aac60b19d80fdb4993ac8ed4e1`.

The primary semantic population is `expert_human && !label_conflict`, exactly 1,227 rows on each
split. Auxiliary validity is exactly 1,305 rows on each split. Expert/conflict counts are
1,250/23 on validation and 1,251/24 on test. An independent verifier checked exact IDs, raw text,
group disjointness, counts, hashes, deterministic rebuild identity, and absence of training-named
artifacts. Any later `goal_v1.0` configuration must join onto these frozen pair IDs without changing
the assignment hash.

## Complete current baselines

The table reports primary-semantic coverage, balanced accuracy, and AUROC. Every run also stores
accuracy, precision, recall, F1, AUPRC, Brier, NLL, ECE, descriptive slices, and 1,000 group-level
bootstrap samples. Thresholds are fixed or derived from validation only; test labels configured
nothing.

| Baseline | Validation coverage | Val balanced acc. | Val AUROC | Test coverage | Test balanced acc. | Test AUROC | Run ID |
|---|---:|---:|---:|---:|---:|---:|---|
| Validation-primary majority | 100.00% | 0.5000 | 0.5000 | 100.00% | 0.5000 | 0.5000 | `majority_class_v1--b65da1f45b763265` |
| Exact raw-headless identity | 100.00% | 0.5143 | 0.5143 | 100.00% | 0.5087 | 0.5087 | `identity_string_equality_v1--e1d84cb83f218c71` |
| Candidate typecheck validity | 92.91% | 0.5123 | 0.5123 | 91.28% | 0.5380 | 0.5380 | `lean_candidate_typecheck_v1--92f8da35ad3add05` |
| DefEq | 88.83% | 0.5547 | 0.5547 | 87.61% | 0.5400 | 0.5400 | `lean_defeq_v1--a4c5d8df68433982` |

The majority constant is the 0.796251 validation-primary prevalence, not mixed-provenance
prevalence. Typecheck abstains when the canonical reference does not elaborate. DefEq abstains when
either side does not elaborate or normalized headless reconstruction is a flagged fallback;
unknown/error is never converted to semantic false.

The shared Lean artifact is
`lean_features/full-5111-raw--c589110fc0dba865/features.jsonl`, SHA-256
`ebf010e807507894153991782d6aa081e884b44487c655ba29431009df453d09`. It used mathlib revision
`d568c8c09630de097a046763c17b9ea99f95f950`, Lean `v4.31.0-rc1`, LeanInteract `0.11.4`, REPL
`v1.3.17`, synchronous elaboration, and one persistent worker. The full pass executed 5,803 new
unique typechecks plus 4,100 new DefEq requests, reused 262 pilot cache entries, had zero
infrastructure failures, took 462.84 seconds, and peaked at 17.78 GiB descendant RSS. A repeated
invocation resumed the identical artifact without Lean calls.

## Historical checkpoint adapters

Six frozen checkpoint prediction sets from the historical 821-row dev partition were preserved as
selective-coverage baselines. The old rows map to 411 validation and 410 test rows in v2; every
uncovered row is an abstention. On the primary population this is 167/1,227 validation (13.61%) and
192/1,227 test (15.65%), so these scores are not comparable to full-coverage results without the
coverage qualifier.

| Historical checkpoint | Val balanced acc. | Val AUROC | Test balanced acc. | Test AUROC | Run ID |
|---|---:|---:|---:|---:|---|
| M1 `bc426653968b` | 0.6085 | 0.6059 | 0.6262 | 0.7123 | `historical_m1_bc426653968b_v1--c45e82e1c8fbaa29` |
| S1 v0 CPT chunks | 0.5041 | 0.7040 | 0.6104 | 0.7937 | `historical_s1v0_cpt_chunks_v1--a15c71c402440465` |
| S1 v0 CPT mixed | 0.5161 | 0.6545 | 0.6319 | 0.6844 | `historical_s1v0_cpt_mixed_v1--406edfc11bbc7dd2` |
| S1 v0 stock | 0.5280 | 0.5952 | 0.5859 | 0.6810 | `historical_s1v0_stock_v1--5d8b81a6d9b8320b` |
| S1 v1 CPT chunks | 0.5481 | 0.6728 | 0.6411 | 0.7584 | `historical_s1v1_cpt_chunks_v1--83f0823e25853830` |
| S1 v1 CPT mixed | 0.5361 | 0.6432 | 0.6350 | 0.7525 | `historical_s1v1_cpt_mixed_v1--9ae2be1eabdbc021` |

## Measured pilots and blockers

- **BEqL/BEq+: compute-blocked before scale.** The adapter pins LeanInteract commit
  `976edd7d38a99e1ea4c2dfabeb8ad98baffca3c8` and released implementation SHA-256
  `3981daf6b342b9959c183a55df53818f57cf83dba8c67376105ec2d76d9a80c6`. The one-example
  preflight passed, but `exact?` terminated Lean with `failed to create thread` under the required
  24,576 MiB hard limit. The error remains infrastructure-unknown in
  `beq_plus_features/one-example--bfa1d28396b03161`; no score or scale run was fabricated. The
  original BEq implementation is additionally model-assisted and needs a separately pinned proof
  model/API placement.
- **GTED/ASSESS: environment-blocked.** Primary repositories are pinned locally at GTED
  `98636fcf0d860f95766dc22ee53afe0606fbbccd` and ASSESS
  `bc7933547d8a6d1aaee41ccf56d68bc1f0fc575d`. Their released operator-tree extractors require a
  configured Lean 4.9/mathlib workspace and custom extractor dependencies; this host has only the
  pinned EVAL Lean 4.31 environment. Their published evaluator also searches thresholds on its
  benchmark labels, which cannot be repeated on EVAL v2 test. Provide a pinned 4.9 environment so
  raw similarity can be extracted, then select any threshold on validation only.
- **Codex/Claude/Lemex: API-budget blocked after successful pilots.** One identical human-negative
  validation pair was correctly called non-equivalent by Codex `gpt-5.6-sol` xhigh (0.01; 11.62 s;
  21,617 tokens), Claude `claude-fable-5` xhigh (0.03; 6.72 s; $0.10583), and Lemex
  `moonshotai/Kimi-K2.7-Code` medium (0.00; 9.97 s; 4,652 tokens). Artifacts are
  `llm_judge_pilots/pilot--89d0672c166ece4a` and `pilot--adec43d57ac024f8`. Linear single-pass
  projections are 110.5M Codex tokens, 23.8M Lemex tokens, and $540.90 Claude cost, before retries.
  Full three-model runs and predefined votes wait for an explicit API budget/placement decision.
- **ConsistencyCheck/ReForm:** representation and comparison context only. It adds no row to the
  5,111 Lean-to-Lean benchmark and is not reported as a canonical baseline.
- **`goal_v1.0`:** waits only for the REPR artifact, not for split membership. Its future join must
  bind the same assignment content hash above.

## Revisions and next actions

Repository revision recorded by all new artifacts is
`8747d381ec826d5f540f2c3798d302f9ec074d43`, with EVAL-owned dirty additions explicitly marked.
The old sealed v1 workflow and reports were not edited.

Private Hub publication was verified by downloading the exact committed revisions and checking
every file against the uploaded `CHECKSUMS.sha256`:

- `Lemmy00/leanfaith-eval-v2` at `14472a6047a17c4d9fdcbd91ed191694665d004b` (private),
  including exactly 2,555 validation and 2,556 test rows;
- `Lemmy00/leanfaith-eval-results-v2` at
  `158e4b5a9ff2254df20d99d159cf3416e4d21ac3` (private), including ten baseline runs, shared
  Lean features, source/config bytes, and measured blocker pilots.

Next actions, in order:

1. Approve/provide a larger-memory Lean placement for the BEqL/BEq+ 100-row pilot.
2. Provide the pinned Lean 4.9 extractor environment for GTED/ASSESS.
3. Approve an API budget and concurrency plan for the three full LLM passes; then materialize all
   predefined vote combinations from their frozen predictions without new model calls.
4. When REPR freezes, add the `goal_v1.0` view by pair-ID join and verify unchanged split hashes.

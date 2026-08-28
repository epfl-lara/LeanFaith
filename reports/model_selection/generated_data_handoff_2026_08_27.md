# Generated-data handoff

Date: 2026-08-27

This note records what the long-running LF-022 Qwen and Kimi jobs produced,
where their durable artifacts are stored, and what remains before any record
can be used for training. It supplements the older
`current_data_ledger_2026_08_12.md`; it does not retroactively change that
ledger's frozen counts.

## What was run

Two public-source-only proposer batches attempted the same frozen 9,207-task
generation workload:

| Proposer | Frozen tasks | Terminal tasks | Provisional variants | Other terminal outcomes |
|---|---:|---:|---:|---|
| Qwen3.5-397B | 9,207 | 9,207 | 8,835 | 175 provider exhausted; 194 transport unknown; 3 parse failed |
| Kimi-K2 | 9,207 | 9,198 | 9,038 | 107 provider exhausted; 1 transport unknown; 52 parse failed |
| **Total provisional variants** | 18,414 scheduled | 18,405 terminal | **17,873** | 532 non-variant terminals; 9 Kimi tasks nonterminal |

These are generated candidate theorem statements paired with their public
mathlib sources. They are **provisional data**, not semantic labels, silver,
gold, or training-ready examples. Generation did not use private
`sft_classic` content.

## Qwen artifact locations

Frozen execution worktree and task outputs:

```text
/localhome/milikic/LeanFaith-rcp-5e672b9/
  data/lf022_execution/tasks/
  data/lf022_qwen3_scientific_5e672b9/prefix_9207/batch/
```

Important files:

```text
/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_qwen3_scientific_5e672b9/prefix_9207/batch/batch_manifest.json
/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_qwen3_scientific_5e672b9/prefix_9207/batch/journal/
/storage/milikic/leanfaith/lf022_qwen_recycling/prefix_9207_v1/launcher.log
/storage/milikic/leanfaith/lf022_qwen_recycling/prefix_9207_v1/launcher.exit
```

The complete post-generation reconciliation and exact terminal selector are:

```text
/storage/milikic/leanfaith/lf022_postgen_reconciliations/
  qwen3_5_397b_full9207_v3/
  b190ef9b9019c0b323dc0e3e51c8049e30c83689d8895f215417cc94ed78434e/
    reconciliation.json
    terminal_selector.json
```

Key SHA-256 values:

```text
batch_manifest.json  b40557e0dc179bc6e9132242da62f7bd4d7afef782b3cea7c25c199ed7c5cf98
reconciliation.json  1051628a8bf7582f9d6ca6146119cbab27e09883e1d68410bcb4e76360167253
terminal_selector.json  61804a254f3381aeb8a27c99a8f26c83e84aa8eb7e10b64962d0c93d12d49a8f
```

The Qwen generation job completed successfully on 2026-08-20. Its
reconciliation partitions all 9,207 tasks exactly and records 8,835
`provisional_variants_created` terminals.

## Kimi artifact locations

Frozen execution worktree and task outputs:

```text
/localhome/milikic/LeanFaith-kimi-641d13d/
  data/lf022_execution/tasks/
  data/lf022_kimi_v4_scientific_641d13d/full_9207/batch/
```

Important files:

```text
/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_kimi_v4_scientific_641d13d/full_9207/batch/batch_manifest.json
/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_kimi_v4_scientific_641d13d/full_9207/batch/journal/
/storage/milikic/leanfaith/lf022_kimi_v4_full_9207_v1/pipeline.log
/storage/milikic/leanfaith/lf022_kimi_v4_full_9207_v1/pipeline.exit
```

The Kimi batch-manifest SHA-256 is:

```text
09204ebb1e4b8caeb3e99cacde6e0f59146da38ef30562ec8a20f302145b3ed3
```

Kimi stopped with exit status 2 after writing 9,198 terminal records. Nine
frozen tasks still need explicit terminal resolution. The 9,038 already
persisted provisional variants remain durable and replayable; no complete Kimi
reconciliation or full Lean-check artifact exists yet.

## Lean-validation status and invalid artifact warning

The first complete Qwen Lean-check invocation must **not** be used as candidate
validity evidence. It is preserved here for audit history:

```text
/storage/milikic/leanfaith/lf022_lean_checks/
  qwen3_5_397b_full9207_v3/
  a6a1eeb6945cebc1c174b20ef4c1e169cf36d50bb8fc6fcb8abca9d66286c5c7/
```

That run recorded all 8,835 candidates as infrastructure crashes because Lean
could not read:

```text
Lean/Elab/Tactic/Basic.olean.private
```

It therefore establishes **zero** valid or invalid theorem judgments. On
2026-08-27, a fresh isolated one-candidate diagnostic reached Lean normally and
returned an ordinary `invalid` result with no infrastructure error. This shows
that the original global crash is no longer reproduced, but the full Qwen
validation still has to be rerun into a new content-addressed output root.

The failed check manifest is retained only as negative operational evidence:

```text
manifest SHA-256  58b006f0fbfa253f28c47dfaef2c058de61be396161846a26ea39936f46b24e4
outcome_counts    {"infrastructure_error": 8835}
training_eligible false
evaluation_eligible false
```

## Existing deterministic and proxy data

The generated LLM candidates are additional to the existing deterministic
assets:

| Artifact | Count | Location |
|---|---:|---|
| mixed proxy corpus | 17,181 unique pairs | `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen1125_composition_f7b398af_v1/` |
| corrected deterministic depth-three audit | 4,031 unique provisional pairs | `/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/frontier_084859ee_five_families_v2/` |
| model-adjudicated training silver available before these scale jobs | 9 records | documented in `current_data_ledger_2026_08_12.md` |

The 4,031 depth-three records are not yet merged into the 17,181-pair proxy
corpus. Counts must not be added to a training inventory until the explicit
merge, deduplication, ancestry grouping, and split replay complete.

## Required next processing

1. Resolve the nine nonterminal Kimi tasks and freeze a complete reconciliation.
2. Rerun Qwen Lean validation against the verified terminal selector into a
   new output root; never overwrite the failed audit artifact.
3. Lean-check Kimi's persisted provisional variants after reconciliation.
4. Freeze exact valid, invalid, infrastructure-failure, and duplicate counts
   per proposer.
5. Deduplicate by pair identity, theorem ancestry, and normalized Lean
   representation before combining sources.
6. Apply the registered Sol+Fable weak-supervision route in bounded resumable
   batches. Agreement may yield training-only silver; disagreement remains
   `REVIEW`. It does not create human gold.
7. Build ancestry-connected train/selection partitions before model training.

Until steps 1--5 complete, the exact training-ready count for the new Qwen and
Kimi data is unknown. The authoritative current gross count is 17,873
provisional variants.


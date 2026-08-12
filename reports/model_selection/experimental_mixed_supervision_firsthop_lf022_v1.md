# Experimental mixed supervision: first-hop deterministic + LF-022

**Frozen:** 2026-08-12  
**Code revision:** `085210096065519381f50b8607fc9a070cb4b09b`  
**Dataset ID:**
`experimental_mixed_supervision:ffeb5a5baf4862e35a399eac764535be51a78b72062cd767de8a541efd965a06`

This is the first immutable corpus that combines the complete selectable
deterministic first-hop projection with completed Lean-valid Kimi and Qwen
LF-022 Codex audits. It is explicitly **proxy supervision** for smoke training,
learning curves, and diagnostics. It creates no semantic, silver, or human-gold
labels and is not eligible for confirmatory model selection or evaluation.

## Frozen inventory

| Measure | Count |
|---|---:|
| Retained records | 10,837 |
| Ancestry-connected components | 5,791 |
| Train | 8,789 |
| Validation | 1,017 |
| Test | 1,031 |
| Same-claim proxy | 3,857 |
| Not-same-claim proxy | 6,980 |
| Deterministic first-hop signals | 10,336 |
| Retained Codex single-judge signals | 510 |
| Adapter exclusions | 666 |

The retained Codex signals contain 420 Qwen-proposed pairs and 90
Kimi-proposed pairs. Their proxy targets are 25 same-claim and 485
not-same-claim. Of the 1,176 completed Lean-valid audit judgments presented to
the adapter, 664 were excluded because the verdict/relation and directional
implication fields were internally inconsistent, and two were excluded because
the judge requested expert review. The filter was not weakened to inflate the
dataset.

The source mix is 7,000 internal-only `sft_classic` records and 3,837 public
mathlib records. Private-source flags and release restrictions remain attached
to every relevant record. Global exact-pair deduplication, conflicting-target
quarantine, active-benchmark screening, and connected-component split
assignment run before freezing.

## Immutable artifacts

Root:
`/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen_0852100_v1`

| Artifact | SHA-256 |
|---|---|
| `manifest.json` | `4088092cc8f0ceffe5cd8a3beb848c5e6671be4ec4242e74799c8734c93521b5` |
| `summary.json` | `b90a8823a68b83c9c51bc6852617996370379431511b4eb0f4cfbf8dd3d88956` |
| `records.jsonl` | `83daef5ecc1c2657a2c60170a618255a31fe91a8b9ba2e78fd9be3bdefeace4a` |
| `excluded.jsonl` | `5bfe3fa5158403d29c1f397bfa3a631d1a37d078924a3472794454d6e241b66f` |
| `split_assignments.jsonl` | `466146a7080cefea83d82f0bfb0c97e035b9153d813d296649673e28e3e19aa4` |

The independent verifier passed with external-input verification enabled. A
second freeze against the same directory returned `replayed=true` with the
same dataset ID and bytes. The full repository test suite, Ruff, formatting,
and strict source mypy checks passed at the frozen code revision.

## Next admission boundary

Completed second-hop transformation outputs are intentionally absent because
their chain-level receipt, cycle removal, and global uniqueness postprocessing
are not final. They will enter a separately versioned corpus only after those
checks pass. Newly generated Qwen variants similarly require exact batch
reconciliation, Lean checking, and completed judgment verification before a
later freeze.

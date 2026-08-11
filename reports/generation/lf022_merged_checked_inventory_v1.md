# LF-022 merged checked inventory v1

Captured at `2026-08-11T17:32:10Z` from commit
`6130d965be23afe73d637bae5130759b2dfbb7e8`.

## Result

The four completed LF-022 checker partitions have now been replayed against
their exact selectors, check manifests, source artifact lines, and available
Codex audit artifacts, then deduplicated by the canonical source-theorem plus
candidate-code key.

| Stage | Gross observations | Unique pair keys |
|---|---:|---:|
| Generated and checked | 1,967 | 1,502 |
| Lean-valid | 1,439 | 1,106 |
| Codex-audited | 1,412 | 1,080 |

There are 26 unique Lean-valid pairs without an audit. The 465 duplicate
observations are cross-partition repeats, not additional data units. Repeated
audits disagree on 3 same-claim verdicts, 32 relation verdicts, 20 directional
implication tuples, and 51 complete core tuples. Mechanical Lean outcomes have
zero conflicts.

This inventory is deliberately **audit-only**. It creates no semantic label,
silver or gold record, training/evaluation eligibility, readiness claim, or
gate credit. In particular, the 1,080 Codex-audited pairs are not automatically
accepted as training labels.

## Immutable artifact

Root:
`/storage/milikic/leanfaith/lf022_merged_inventory/commit_6130d96_v1`

| File | SHA-256 |
|---|---|
| `manifest.json` | `60f3aee4292e414caa8d601216443848df74efcb2d9d47fa94fbfaecce58dc3f` |
| `observations.jsonl` | `a3d481f1a48265f3573e8284fae837e10fe34530775a20f5cc1f21adcf3443ef` |
| `pairs.jsonl` | `b9f48b379d04bd30e8afd058c86cca350c5c56b6503dbcb7a0f12480c7120d42` |

The inventory ID is
`lf022_merged_checked_inventory:2a0b0f94b3a448b0939d622686b9cfb5380f8ddacc8b29421bb9a4cf75ecc691`.
Running the same pinned command a second time produced an exact immutable
replay.

The machine-readable companion is
`reports/generation/lf022_merged_checked_inventory_v1.json`.

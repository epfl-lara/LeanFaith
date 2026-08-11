# Deterministic-v2 P14 unique-pair inventory v1

Captured at `2026-08-11T18:18:07Z` from commit
`61a6b688da8a2fc347d34312cd18f05afdc34e54`.

## Result

The new postprocessor replayed the complete P14 depth-two chain artifact,
bound it to the exact 3,941-row seed set, and emitted one immutable record per
`(original theorem, final Lean code)` pair while preserving all chain lineage.

| Classification relative to the original theorem | Count |
|---|---:|
| Alpha-novel final statement | 555 |
| Returned to the original alpha identity | 1,333 |
| Total unique raw pairs | 1,888 |

All 1,888 raw pair keys are unique. The 1,333 alpha-identity returns are
reversible cycles and are explicitly not counted as novel data. The useful
alpha-novel subset contains 294 P14→P14, 29 P15→P14, 11 P16→P14, 1 P17→P14,
and 220 P18→P14 chains. Exact source-byte return is not reported because the
historical source and candidate artifacts use different content-hash domains.

Every record remains provisional and audit-only. The inventory creates zero
semantic labels, promotions, training/evaluation eligibility, or gate credit.

## Immutable artifact

Root:
`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/audit/unique_pairs_p14_recovered_61a6b68_v2`

| File | SHA-256 |
|---|---|
| `manifest.json` | `b1529761ff2b5603b8184a2a6c072b03033db10cef2958da4ce9cb1d1cc1ebfe` |
| `unique_pairs.jsonl` | `6bbb532af8660199228cb02b7e61a2828fd1603463605240665286aba22ee924` |

The set ID is
`detcomp_unique_pair_set:3c7ccbc5803ad09c4ccb456e596e709c2b2dbc3d92665ccc49b5d61ab850f1e2`.
Running the same pinned command a second time produced an exact immutable
replay. The pinned postprocessor uses no-follow, descriptor-relative input
reads and output publication, and its adversarial race regressions passed
before this artifact was materialized.

This schema-2 artifact supersedes the unreferenced schema-1 development output
created before the source-content-hash-domain defect and symlink checks were
corrected. The retired source-content-return metric is absent from every
schema-2 record and manifest.

The machine-readable companion is
`reports/generation/deterministic_v2_p14_unique_pair_inventory_v1.json`.

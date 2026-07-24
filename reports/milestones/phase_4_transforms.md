# Phase 4 — Deterministic generation and promotion

**Status:** generation mechanics complete; Gate 4G evidence finalized  
**Date:** 2026-07-23

## Completed implementation milestones

- LF-016 established the typed transformation protocol, fail-closed registry,
  audit lineage, and promotion boundary.
- LF-017 implemented P01, P02, and P04-lite with Lean-backed positive-family
  validation. Its outputs remain provisional.
- LF-018 implemented N01, N02, N03, N07, and N10 and persisted a Lean-backed
  five-family pre-scale slice. Its pairs remain unresolved and N10 preserves
  dual ancestry.

The authoritative item-level evidence is recorded in:

- `reports/milestones/lf_016_transform_protocol.md`;
- `reports/milestones/lf_017_scoped_positives.md`;
- `reports/milestones/lf_018_scoped_negatives.md`.

No positive or negative transformation family has been promoted.

## Integrated generation replay

LF-019 completed one integrated smoke-only replay across all eight active
families. It re-elaborated every candidate, preserved complete
attempt→draft→audit→variant→pair lineage, linked transformation evidence to
every pair, preserved N10's two ancestries, isolated a malformed source,
built a leakage-free connected split, exercised tiny model/prediction
plumbing, and proved that smoke artifacts cannot enter releases, calibration,
model selection, or scientific tables.

The two immutable clean-checkout runs are:

- Run A `run_20260723T182820Z_6d2692a8`, report SHA-256
  `656a1133a9f83a4fb09b133d31cd8a69aa950213b010d7a67248500638d83d7c`;
- Run B `run_20260723T182826Z_467c0f50`, report SHA-256
  `1a7bbe4fdb412496e16abb8bb69ecc798bc41b1ffdcbd00a7cdf7445547639d0`.

Both bind semantic fingerprint
`3e3e73419c0f30ab33534aadd1aa385d61aa1177d00f278c96738d620eda91de`,
code-tree hash
`be56a7c15a9d201f83b95044dece36371b0231ee39b9c77a0a945214e9c466c1`,
and code-bundle SHA-256
`2c0aee7d7f39dbdaafdbf84be339280d3d8cfb119f2fcd01ffcf681ea4268962`.

Only P01 received the narrowly scoped `smoke_alpha_certificate` provisional
resolution. Every other pair remained unresolved. No intended relation became
a label, and neither run created gold labels or promotions.

## Gate status

- **Gate 4G — generation:** closes from the bound LF-019 replay and canonical
  fail-closed gate report.
- **Gate 4A — positive gold promotion:** open pending the later blinded human
  audit and registered statistical criteria.
- **Gate 4B — negative supervised promotion:** open pending one accepted
  evidence route per promoted item and the later audit prerequisites.

The accepted registry hash is
`8a5316dacba064d9b3b13e12dfd46cd707445ecc520101a9374463f336f6466f`.
The canonical `reports/gates/gate_4g.json` binds these reports, manifests, the
source bundle, and both milestone documents.

Gate 4A remains open. Gate 4B remains open. Their later statistical and
supervised promotion requirements are not weakened by Gate 4G.

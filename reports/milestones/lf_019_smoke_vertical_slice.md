# LF-019 — Integrated smoke vertical slice

**Status:** complete; accepted smoke-only replay  
**Date:** 2026-07-23  
**Scope:** smoke-only end-to-end plumbing across all eight active deterministic
transformation families

## Intended outcome

LF-019 is the integrated fixture-scale proof that the already implemented
transformation families can traverse the persistent data path without turning
mechanical intentions into scientific labels:

```text
fixture source
→ theorem and representation records
→ attempt, draft, audit, variant, and pair lineage
→ pair-linked transformation evidence
→ P01-only smoke resolution
→ ancestry-connected smoke split
→ tiny nonproduction classifier
→ canonical predictions and plumbing metrics
→ manifests and release guards
```

The run must exercise P01, P02, P04-lite, N01, N02, N03, N07, and N10. It
must use at least ten accepted source statements, preserve an explicit expected
failure, and preserve both source ancestries for N10.

## Smoke-only semantic boundary

Every artifact carries:

```yaml
artifact_class: smoke
release_eligible: false
model_selection_eligible: false
```

Only a mechanically verified P01 alpha-renaming pair may receive:

```text
quality_tier = provisional
resolution_method = smoke_alpha_certificate
same_claim = true
```

P02, P04-lite, and all five negative-family pairs remain unresolved. Their
intended relations and any `near_miss` markers are provenance only. LF-019
must create no gold labels, negative semantic labels, transformation-family
promotions, calibration inputs, model-selection inputs, or scientific-table
inputs.

The tiny model is a plumbing fixture, not a research baseline. It emits
canonical probability records routed to REVIEW and reports structural counts
only; it does not report accuracy, F1, AUPRC, calibration, or any other
scientific quality metric.

## Configuration

The smoke slice is controlled by:

- `configs/transformations/lf019_positive_fixtures_v1.yaml`;
- `configs/transformations/lf018_pre_scale_v1.yaml`;
- `configs/transformations/lf019_smoke_v1.yaml`;
- the active transformation registry and all eight family rule configs.

The current effective registry snapshot must be revalidated and frozen before
the accepted run. Historical LF-016/LF-017/LF-018 reports are not silently
rewritten to bind a newer registry hash.

## Required acceptance evidence

LF-019 is accepted only when a persisted replay pair and its reports
mechanically demonstrate:

1. the current registry snapshot is frozen and the active family inventory is
   exact;
2. all eight active families execute and disabled-family dispatch is rejected;
3. at least ten fixture sources are accepted;
4. every accepted candidate is re-elaborated;
5. attempt→draft→audit→variant→pair lineage is complete;
6. every pair has validation/audit status and linked transformation evidence;
7. N10 preserves both source ancestries;
8. only P01 uses the smoke resolution and every other semantic target remains
   unresolved;
9. zero intention-to-label inference, gold labels, or promotions;
10. zero protected-benchmark overlap;
11. zero ancestry-connected split leakage;
12. batch-failure isolation and deterministic semantic replay pass;
13. release, calibration, model-selection, and scientific-table guards reject
    the smoke artifacts; and
14. a clean-checkout execution reproduces the accepted semantic artifact
    fingerprint.

## Accepted replay

The accepted command was:

```bash
leanfaith generate-deterministic \
  --run-smoke-vertical-slice \
  --code-bundle artifacts/code_bundles/lf019/code_bundle_2c0aee7d7f39dbdaafdbf84be339280d3d8cfb119f2fcd01ffcf681ea4268962.tar.gz
```

It ran from a materialized clean checkout whose code-tree hash is
`be56a7c15a9d201f83b95044dece36371b0231ee39b9c77a0a945214e9c466c1`.
The source-bundle SHA-256 is
`2c0aee7d7f39dbdaafdbf84be339280d3d8cfb119f2fcd01ffcf681ea4268962`.

Run A:

- run ID: `run_20260723T182820Z_6d2692a8`;
- audit report SHA-256:
  `656a1133a9f83a4fb09b133d31cd8a69aa950213b010d7a67248500638d83d7c`;
- run-manifest SHA-256:
  `f00743b1cec6cc7a9016e38f8577258ec8afae35addb64af835f281ab4de1b50`;
- output-manifest SHA-256:
  `12ba896cd67959dbfd6cb9b57af749e87aa3b0963a9cd41c11e8254da0075444`;
- artifact-catalog SHA-256:
  `76a390c1a1da819007804548f90986df216c51c2f3cf73196625c5de5a3057d7`.

Run B:

- run ID: `run_20260723T182826Z_467c0f50`;
- audit report SHA-256:
  `1a7bbe4fdb412496e16abb8bb69ecc798bc41b1ffdcbd00a7cdf7445547639d0`;
- run-manifest SHA-256:
  `23c604c4c6868078adf7f03094253ffc6af6334f3ef55623f958f87ea8aaa4ca`;
- output-manifest SHA-256:
  `a18c807c765080f3041db30a4b2cfd168d9539676f5e56b56220c69e886b9fd1`;
- artifact-catalog SHA-256:
  `2a025bdc8d76e1f1cfd0e3fe21f4e82f656f098b8c506d3f426605167ef296f3`.

Both runs produced the identical semantic fingerprint
`3e3e73419c0f30ab33534aadd1aa385d61aa1177d00f278c96738d620eda91de`.
Run A is the independent baseline and therefore does not self-claim replay.
Run B binds Run A's fingerprint, passes replay, and records
`lf019_accepted=true`.

Each run accepted ten source statements, isolated one expected malformed
source, executed all eight active families, persisted eight candidate pairs
and eight linked audit-evidence records, and emitted exactly one provisional
P01 smoke label. All 21 mechanical checks passed in Run B. The split manifest
was byte-identical across runs, no protected benchmark overlap was found, no
gold label or promotion was created, and every smoke artifact was rejected
for release, calibration, model selection, and scientific tables.

The implementation gate completed with 979 passing tests, clean Ruff and
formatting checks, strict package mypy, a passing doctor, and a successful
fixture `lake build`.

Gate 4G closes from this bound replay and the fail-closed finalizer.

Gate 4A remains open.

Gate 4B remains open.

LF-020 is authorized only after the canonical Gate-4G report binds this
milestone and both immutable runs.

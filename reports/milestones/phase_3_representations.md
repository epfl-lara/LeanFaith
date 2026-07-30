# Phase 3 - Multi-view representations (Revision 4.1)

> **Current representation version:** The report below is the preserved
> 2026-07-18 `repr_v2` Gate-3 closure. The current `repr_v3` implementation
> subsequently passed a fresh revalidation on the same immutable 10,000-record
> denominator on 2026-07-30. See
> `reports/milestones/phase_3_repr_v3_revalidation.md` and
> `reports/gates/gate_3_repr_v3.json`. No historical artifact was rewritten or
> relabelled.

**Updated:** 2026-07-18
**Decision:** **PASS**
**Authorization:** Gate 3 is closed. LF-016 remains blocked only by the
post-gate benchmark-signature and overlap freeze required before generation.

Gate 3 validates deterministic, proof-free, multi-view representations on the
exact denominator frozen by Gate 2. The run uses 5,000 mathlib and 5,000
`sft_classic` theorem statements without post-freeze filtering.

## Frozen inputs and execution identity

- Gate-2 report SHA-256:
  `4874318bc44092ddb3afb906b10576adcbdecdebca20324080ae29db454c7638`.
- Frozen manifest SHA-256:
  `19f5c38ea15bbc72c97fe73be6f4a50d5491e3e27cdf024cc05889d4eb1471e3`.
- Frozen theorem partition SHA-256:
  `8eb75ffa0b9233c5a91492fa181f604e3c098a6f3970799bcb0406f8b517f09e`.
- Archived code bundle SHA-256:
  `8b8caafd3bc70810183204b91e1f14f7e54c5dcb0b874cf438e0b46966f15900`.
- Code-tree hash:
  `e965800ceb8d05b565a729a9993a4ff14eb59bab7b4e6a71430f2e0d5722f411`.
- Representation config hash:
  `4f7a64b22600db3a3d84132bf1cb48559d365295d2c6edbb711f0c87479d4572`.
- Context hash:
  `b69bafa2af918b2d452466964e81c7a9e7783f2f44ecdfdd2da0587c52f8c63d`.
- Environment hash:
  `e447ac3a773b0d29ec75b51bcfa5318158399e9fe7459a650f9d0bfef9986298`.

The archived source bundle binds the dirty working tree used for these
historical `repr_v2` runs,
including tracked and untracked source/configuration inputs. Later report-only
edits do not change the executed bundle.

## Scale results

Both Run A and Run B represented all 10,000 frozen inputs. Coverage is measured
against the immutable denominator, both overall and per source.

| View | mathlib | `sft_classic` | Overall | Required | Result |
|---|---:|---:|---:|---:|---|
| `raw_proof_stripped` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | 100% | pass |
| `headless` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | 100% | pass |
| `signature_pp` | 4,981/5,000 | 4,998/5,000 | 9,979/10,000 | >=99% | pass |
| `signature_explicit` | 4,981/5,000 | 4,998/5,000 | 9,979/10,000 | >=99% | pass |
| `semantic_atoms` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | >=99% | pass |
| `operator_tree` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | >=98% | pass |

The 21 `signature_pp` and 21 `signature_explicit` failures are persisted as 42
explicit per-view failure records. No missing view is removed from a
denominator. The mechanical audit has `mechanical_pass=true`; its embedded
`gate_pass=false` records that manual collision review was still pending when
that file was created. The separate collision-closure artifact completes that
requirement with `gate_pass=true`.

## Determinism and invariance

- Run A and Run B use the same frozen inputs, code bundle, configuration,
  context, and environment.
- Semantic replay compared 10,000 records on each side with zero errors and
  `ok=true`; operational timestamps and run IDs are intentionally excluded.
- Binder-normalized alpha fingerprints succeeded on all 10,000 records.
- The property-only renamer passed 1,000/1,000 alpha-renaming cases.
- The name-based versus inline path passed 500/500 mathlib comparisons.
- Representation IDs and semantic content hashes replay exactly.

Run A hashes: manifest
`4149ab65eb5fa2e1eaa613a49d0311560d201ca1554c705c3de446833dd7a8f5`,
records `0fa2e40b4504e647a9abbcf0c4745b29bf701839c99e36ea10c22091022381bf`,
failures `099d233608b8b65e53fc31595225b08aba0035324ceeb688953a9ed7eba6cddb`.

Run B hashes: manifest
`71e43b4a3b6601d6dbc93e436c0687bb7af1e78e1d3c521a40e1f7662489d54b`,
records `81ab8da2d0986e1202c0fa532e049ce061d78b237c1baa789bb4861b31051a3d`,
failures `099d233608b8b65e53fc31595225b08aba0035324ceeb688953a9ed7eba6cddb`.

Audit hashes:

- Run A mechanical:
  `2ee5fd465bf0528c08b60f79709a7c050efae23a84f59a2b7f6fc165dbdf947b`;
- Run B mechanical:
  `f9bc9549b53e64b687ffeb1e3e0feafa2c62724053b05c5179ebbb2b5901e46e`;
- A/B semantic replay:
  `8b0facd7ca5ee09cbeadae0320a7d97d6f69461aa25cb6fb43c77770623fccbb`;
- alpha invariance:
  `97974a834dd6af1803318892b7a437cb614b15fc85327b28a91b95909e8218ae`;
- cross-path comparison:
  `a1c7fe57c636be530a346337b1c56a097bc1c5e586841ae7695b819c0dbef317`.

## Collision and proof-leakage audits

There are zero cryptographic collisions and zero canonical-alpha collisions.
The audit enumerated 152 expected lossy-view clusters: 141 from
`semantic_atoms` projection and 11 from `signature_pp` erasure. All 152 were
reviewed and assigned deterministic reason codes; no representation defect was
found. See `reports/representation_collisions_mvp.md`.

- Manual reviews SHA-256:
  `a3f1511b22017855282f5189adf0554bdfdf8ed0d6a062a7e729c54b2b0762ce`.
- Collision closure SHA-256:
  `d5bf43ff0219016ecea34c43f8fd982aa17671803f5ef80e35823aad83221785`.

Proof-leakage checks pass with zero errors. Model-visible views are derived
from proof-stripped declarations/types; the unique proof sentinel is absent,
and identical signatures with different proof bodies produce identical views.

## Verification

- Focused Gate-3 suite: 89 tests passed; log SHA-256
  `6692934017b7c49d882f90bacacdf46a1fb73798aa5057b4c7edfb9fb4d213af`.
- Ruff: pass; SHA-256
  `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`.
- Ruff format check: pass; SHA-256
  `1b2a949c4425b6097242b1e28a12a597d352c04edc11185a9a3012a5f542e9d6`.
- Strict mypy: 59 source files, zero issues; SHA-256
  `960745a059df2e6bf5556b518f6404c7707247bf3da084d48c3008145eb95a5d`.
- Doctor/LeanInteract/toolchain/fixture checks: all pass; SHA-256
  `02cc002a4005d97e81e3177558d9377859b03c4f1c22abc2cd38d7dc391373fc`.

The successful closure profile used one worker, sequential Run A then Run B,
500-record chunks, and a 49,152 MiB per-REPL memory limit. This records the
actual reproducible run; it is not a universal hardware prescription.

## Decision

All Gate-3 fixed-regression, frozen-denominator, per-source coverage,
failure-persistence, alpha-invariance, collision, proof-leakage, cross-path,
and deterministic-replay requirements pass. Gate 3 is closed.

The benchmark representation-signature and overlap freeze is a distinct
post-Gate-3 prerequisite in the ordered plan. It remains in progress and keeps
LF-016 unauthorized until its own artifact is complete; it does not reopen or
weaken Gate 3.

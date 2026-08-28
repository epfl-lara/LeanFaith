# P18 v1.0 root-equality-symmetry pre-scale yield audit

**Audit date:** 2026-08-11

**Disposition:** source-only opportunity scan completed; no scale
materialization was launched. These counts create no semantic label,
promotion, gate credit, evaluation credit, or training credit.

P18 swaps the two complete spans of one surface-root Lean equality. Its
source prefilter requires the surface declaration and elaborated expression
tree to agree on an exact root `Eq α lhs rhs` after the declaration's visible
binders. The sides must be distinct. Comments, quoted terms, macros, scoped
term syntax, parser ambiguity, and tree mismatch fail closed.

## Frozen input bindings

| Source | Records | Theorem SHA-256 | Representation SHA-256 |
|---|---:|---|---|
| internal-only frozen `sft_classic` Gate-3 subset | 5,000 | `3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30` | `c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24` |
| public frozen LF-022 mathlib extraction | 27,786 | `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7` | `c799f54c60d3eb3f45a0fa473231ba991e871b7de440c65b037436721037e505` |

Additional immutable bindings:

- private subset manifest SHA-256: `e0600a5b5b8dc20d2983e66daef78aef18cd0cc9c652414a35263ea55f0ac43f`;
- public extraction manifest SHA-256: `b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06`;
- P18 profile file SHA-256: `8e46d3576eae20656ddd919c484b8fd80960e70036b71ccdb728092da1afb3cc`;
- P18 effective profile hash: `ec63234893919272b41249db390dc2b1b214ead5d343747b07c2951a55975302`;
- P18 version-addendum SHA-256: `8154b90040d3b4e9dfce6f84642bae9b83c19f4bb932956e8f4d4d0be53e491d`;
- frozen base-v2 portfolio effective hash: `f48e2dfd4555e71dfd07518330f33d222894fa935fb81b6c9e7678a8a1a66594`.

## Exact matcher results

| Measure | Private 5,000 | Public 27,786 |
|---|---:|---:|
| one surface-root `=` before strict safety checks | 2,444 | 12,850 |
| surface and elaborated tree are exact root equality before safety exclusions | 1,516 | 955 |
| exact-tree cases excluded for comments or quoted terms | 563 | 110 |
| exact-tree cases excluded for scoped operators | 9 | 32 |
| exact-tree cases excluded for scoped term syntax or macros | 11 | 30 |
| **strict P18 v1.0 opportunities** | **933** | **783** |

Thus the source-only upper bound is **1,716 potential P18 variants**. Each
source can emit at most one deterministic draft, and every emitted draft must
still re-elaborate in the same Lean context and pass exact inverse, expected
operator-tree, recomputed alpha-fingerprint, and recomputed semantic-atom
audits. The opportunity count is not an accepted-pair count.

## Difference from the preliminary read-only estimate

The preliminary probe estimated 1,489 private and 865 public candidates. It
was a broad surface/tree heuristic, not the versioned P18 matcher. The
authoritative implementation independently found 1,516 private and 955
public exact-tree shapes before strict safety exclusions, then rejected 583
private and 172 public exact-tree shapes because the declaration contained a
comment/quoted term, a scoped operator, or scoped/macro term syntax. The final
933/783 counts therefore differ from the preliminary 1,489/865 estimate by
-556/-82 respectively.

The comparison is diagnostic rather than a claim that the old estimate's
individual records can be reconstructed: its heuristic was not an immutable
family profile. The new counts are bound to the exact profile, addendum, and
frozen partitions above.

## Evidence boundary

P18 is an additive versioned E2 family. It does not modify the frozen v2
portfolio or the P14-P17 profile bytes. A separate live LeanInteract smoke
proved `(lhs = rhs) ↔ (rhs = lhs)` without `sorry`, re-elaborated the swapped
candidate, and produced one clean provisional materialization. Scale
execution remains blocked on root review.

All future records from this profile must retain:

```text
resolved_label_count = 0
promoted_item_count = 0
training_eligible = false
quality_tier = provisional  # only after a clean mechanical audit
```

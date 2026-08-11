# N18 v1.0 root-equality-polarity pre-scale yield audit

**Audit date:** 2026-08-11

**Disposition:** source-only opportunity scan completed; no scale materialization
was launched. These counts create no semantic label, promotion, gate credit,
evaluation credit, or training credit.

N18 changes exactly one root equality-polarity glyph, `=` to `≠` or `≠` to
`=`. Its source prefilter requires exactly one equality-polarity glyph anywhere
in the conclusion and requires the surface glyph to agree with an elaborated
root `Eq α lhs rhs` or `Ne α lhs rhs` after the declaration's visible
binders. The operands must be structurally distinct. Comments, quoted terms,
macros, scoped term syntax, additional equality glyphs, and parser/tree
disagreement fail closed.

## Frozen input bindings

| Source | Records | Theorem SHA-256 | Representation SHA-256 |
|---|---:|---|---|
| internal-only frozen `sft_classic` Gate-3 subset | 5,000 | `3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30` | `c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24` |
| public frozen LF-022 mathlib extraction | 27,786 | `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7` | `c799f54c60d3eb3f45a0fa473231ba991e871b7de440c65b037436721037e505` |

Additional immutable bindings:

- private subset manifest SHA-256: `e0600a5b5b8dc20d2983e66daef78aef18cd0cc9c652414a35263ea55f0ac43f`;
- public extraction manifest SHA-256: `b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06`;
- N18 profile file SHA-256: `cfd227a152c0eba830beee19a990bb7f189a3f2468012980b70eb9a17b525653`;
- N18 effective profile hash: `30d6f4b48c2f90faa867838f95a3cf547b292e8b3b04ae2b8142b79729a7dc87`;
- N18 version-addendum SHA-256: `d57453dcc4cc6fb8218ee4847c3b5e9239a678734d35399e35ea2eca7d601012`;
- frozen base-v2 portfolio effective hash: `f48e2dfd4555e71dfd07518330f33d222894fa935fb81b6c9e7678a8a1a66594`.

## Exact matcher results

| Direction | Private 5,000 | Public 27,786 |
|---|---:|---:|
| root `Eq` / surface `=` to root `Ne` / surface `≠` | 925 | 770 |
| root `Ne` / surface `≠` to root `Eq` / surface `=` | 35 | 17 |
| **strict N18 v1.0 opportunities** | **960** | **787** |

The source-only upper bound is **1,747 potential N18 variants**. Each source
can emit at most one draft. Every emitted draft must still re-elaborate in the
same Lean context and pass inverse replay, exact root-head/operand checking,
recomputed alpha fingerprint, and the exact one-token semantic-atom delta
`const:Eq` to `const:Ne` or its inverse. The opportunity count is not an
accepted-pair count.

## Evidence boundary

N18 is an additive D0 family bound by a separate versioned addendum. It does
not modify the frozen v2 portfolio or N11--N17 profile bytes. Live
LeanInteract tests materialize and audit clean examples in both directions
and a complex-operand case (`x + 1 = Nat.succ y`). These smokes prove only the
mechanical path; they do not prove that a generated candidate is semantically
non-equivalent for every source theorem.

All future records from this profile must retain:

```text
intended_relation = near_miss        # generation provenance only
intended_error_types = [E10, E26]    # generation provenance only
resolved_label_count = 0
promoted_item_count = 0
training_eligible = false
quality_tier = provisional           # only after a clean mechanical audit
```

# LF-033 P17 pre-scale yield audit

**Implementation commit:** `a2b881b75359d24f071e6f6d6db0057f50858baf`  
**Disposition:** mechanically verified zero-yield control; no scale run authorized

The read-only audit called `enumerate_p17_sites` on every frozen
representation. It did not elaborate candidates, write variants, create
labels, promote a family, or make any item training-eligible.

| Source | Frozen representation partition SHA-256 | Records | `pack_two` | `unpack_pair` |
|---|---|---:|---:|---:|
| public mathlib | `c799f54c60d3eb3f45a0fa473231ba991e871b7de440c65b037436721037e505` | 27,786 | 0 | 0 |
| private qualification | `3483a62ea061548b7bda4d0e7afe074e871ee45a9e988aa9dfd67d01c65176f5` | 1,841 | 0 | 0 |

P17 v1 deliberately requires final singleton proof binders whose domains are
distinct direct references to earlier variables explicitly declared as
`Prop`. The prepared corpora contain no source satisfying that entire closed
shape. The rule remains useful as an executable correctness/control family,
but it contributes no pairs to the dataset and must not be presented as a
data-producing result.

Do not widen P17 v1 using surface syntax guesses. A higher-coverage successor
requires separately versioned Lean Meta evidence that each selected binder
domain is a proposition, followed by the same exact dependency, candidate
elaboration, tree-certificate, inverse-replay, and provisional-only audits.

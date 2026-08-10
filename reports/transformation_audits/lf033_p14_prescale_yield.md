# LF-033 P14 pre-scale yield audit

**Audit date:** 2026-08-10

**Disposition:** implemented as experimental E2 evidence; scale execution remains provisional-only

P14 swaps one adjacent pair of independent, explicit data binders and then
checks the candidate in the same Lean context.  The source and candidate must
also satisfy one unique exact transformation of their full elaborated
outer-forall trees, including any section or auto-implicit binders that are not
visible in the declaration text.  The inverse transformation must recover the
source exactly.

The prefilter is intentionally broader than final acceptance.  It only
identifies records worth sending through LeanInteract and the structural
certificate; it does not create a semantic label.

| Source | Frozen records | P14-prefilter applicable | Grouped swap | Singleton swap |
|---|---:|---:|---:|---:|
| public mathlib | 27,786 | 1,441 | 832 | 609 |
| private qualification | 1,841 | 618 | 301 | 317 |

The counts above are candidate opportunities, not accepted pairs.  Each source
produces at most one deterministic draft for a fixed seed, and all drafts still
require same-context elaboration plus the exact tree, inverse, fingerprint,
semantic-atom, and representation audits.

## Exact corpus checks

Three non-fixture corpus examples were checked end to end:

| Source | Shape | Result |
|---|---|---|
| private `thm:a44b00f0ddc3bf182647f029ddc943f2ba4af1fc3528c86ce1f768cd58d796cd` | grouped `(a b : ℝ)` | clean provisional variant |
| public `thm:bb7c167519d8af364b605042c2de04ad6ce57427844648545e35538d0f877809` | grouped `(g₁ g₂ : G)` with one hidden outer binder | clean provisional variant |
| private `thm:014f51088eb32636677c9b04587697c8685874dcbf7c928aff556e6a70ef8fc5` | singleton `(eaten : ℕ) (left : ℕ)` | clean provisional variant |

A separate 20-candidate private-corpus smoke used LeanInteract pool mode with
two workers:

| Terminal result | Count |
|---|---:|
| clean provisional variant | 12 |
| quarantined: elaborated tree match not unique | 7 |
| candidate did not elaborate | 1 |

This smoke completed in 13.04 seconds.  The eight rejected items demonstrate
the intended fail-closed boundary: syntactically plausible swaps are not
retained when elaboration or the unique structural certificate is uncertain.

## Evidence and credit boundary

P14 remains E2 evidence only.  It creates no resolved F1 label, promotes no
transformation family, and contributes no training credit.  The implementation
records actual overlap with P02 binder regrouping, recomputes stored alpha
fingerprints from operator trees, verifies the inverse residual hash, excludes
proof binders, and requires both selected values to occur in the residual
claim.

A larger materialization run may proceed after the currently serialized
representation and deterministic jobs finish.  Its output must remain in the
experimental P14 candidate pool until a separately approved promotion audit.

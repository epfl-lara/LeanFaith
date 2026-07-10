# Semantic policy v1

**Version:** `semantic_policy_v1` · **Status:** approved for pilot annotation
· **Source of truth:** PLAN.md §3; this file makes the §3.6 edge cases
decidable. Annotators and code may not invent decisions beyond this file; an
uncovered case routes to review (`same_claim=null`, `resolution_outcome=
unresolved`) and, if recurring, triggers a policy revision between rounds.

## 1. Primary target

F1 ("same claim") asks whether two Lean statements express the same intended
mathematical claim — not whether both are true, provable, or mutually
derivable in a rich library (PLAN.md §3.1–§3.2). F0 (definitional/
representation equivalence) and F2 (truth-level relation) are stored
separately and never overwrite F1.

A same-claim positive **may** differ in: theorem/binder names, formatting and
whitespace, harmless parenthesization/grouping, explicitly presented implicit
arguments, approved notation wrappers (the P04-lite table), or another
reversible interface presentation listed here.

A same-claim positive **may not** change: substantive domains, dependencies,
hypotheses, quantifiers and their order, operators, constants, literals,
bounds, casts that change content, typeclass/structure assumptions, or
conclusion strength.

## 2. Edge-case decisions (§3.6)

Each case states the decision, the canonical label fields, and an example.

### 2.1 Extra unused universally quantified variables — NOT same claim

The theorem interface is part of the claim. `∀ n, n + 0 = n` vs
`∀ n m, n + 0 = n`: `same_claim=false`, `relation=incomparable_near_miss`,
`error_types=[E21]` (Appendix C.5), even though truth conditions agree
(F2 may be true).

### 2.2 Redundant but mathematically meaningful hypotheses — NOT same claim

Adding a derivable-but-substantive hypothesis (e.g. also assuming `0 < n`
where the claim already implies it) changes claim strength presentation:
`same_claim=false`, `relation=B_stronger`-side accordingly, `E02`.
Exception: a hypothesis that is *literally required for elaboration* (e.g. a
typeclass instance the notation needs) is interface, not content.

### 2.3 Vacuous implication and inconsistent assumptions — NOT same claim

If a candidate's hypotheses are unsatisfiable or make the conclusion vacuous
where the source's are not: `same_claim=false`, `E03`. Directional relations
may NOT be certified through ex falso/vacuity (PLAN.md §16.3); such evidence
is rejected and the relation stays `unknown` unless humanly resolved.

### 2.4 Subtype / set / typeclass reformulations — same claim only when the
translation is the standard reversible one

`(x : {n : Nat // 0 < n})` vs `(x : Nat) (hx : 0 < x)`: same claim (approved
reversible presentation) provided every use site translates accordingly.
Changing to a materially different structure (`Finset` vs `Set`, monoid vs
group assumptions): `same_claim=false`, `E08`/`E06`.

### 2.5 Coercions and domain embeddings — same claim only when content-neutral

An explicit `(↑n : ℤ)` where the source states the claim over `ℕ` embedded in
`ℤ` in the standard way is cosmetic (E29) *only if* the quantified domain is
unchanged. Quantifying over a different domain and coercing back is
`same_claim=false`, `E06`/`E15` (Appendix C.6 pattern).

### 2.6 Theorem-interface generalization/specialization — NOT same claim

`∀ x : ℝ, 0 ≤ x^2` vs `∀ x : ℚ, 0 ≤ x^2`: `same_claim=false`; relation
`A_stronger`/`B_stronger` only under a recorded binder alignment (§16.3 mode
2) or expert judgment; otherwise `incomparable_near_miss` with `E19`/`E20`.

### 2.7 Answer-only versus full theorem statements — NOT same claim

A candidate that states only the extracted answer (`answer = 42`) for a
problem whose intended formalization is a full theorem: `same_claim=false`,
`E18`/`E26`. NL–Lean faithfulness requires the intended objects, hypotheses,
and conclusion (PLAN.md §3.4).

### 2.8 Simplification to reflexivity or `True` — NOT same claim

Any collapse to a trivial proposition (`True`, `x = x`, `0 = 0`) is semantic
erasure: `same_claim=false`, `relation=incomparable_near_miss`, `E25`
(Appendix C.2/C.3). F2 may simultaneously be true; that never lifts F1.

### 2.9 Notation expansion versus abstraction change

Expanding whitelisted notation to its direct definitional form (the
`replacement_table_v1`/P04-lite list) is same-claim (E29 at most). Replacing
a defined concept by its unfolded internal representation in a way that
changes the stated abstraction level (e.g. `Continuous f` → epsilon-delta
spelled out) is `same_claim=false`, `E09`/`E26`, unless the policy table
explicitly lists that pair as reversible.

### 2.10 Reference defects and genuinely ambiguous NL

If the trusted reference appears wrong: keep the item, set `E27`, route to
review (C.7); the reference is evidence, not ground truth (§3.4). If the NL
is genuinely ambiguous after context: terminal ambiguity (C.8) with `E28`,
`quality_tier=gold_human`, masked from binary metrics (§3.5).

## 3. Directional relations

`A_stronger`/`B_stronger` are claim-level relations under an explicit
binder/hypothesis alignment (§3.3, §16.3). Closed-proposition mutual
provability, vacuity, or ex falso never certify them. Failed proof search is
never evidence of nonimplication (§0.4).

## 4. Review routing

`cannot_assess_yet`/`uncertain` → UNRESOLVED route (tier `unknown`,
`requires_adjudication=true`). Terminal `ambiguous` is reserved for
irresolvable-by-policy items and requires an adjudicated `gold_human` or
benchmark-defined label (§3.5, §14.2).

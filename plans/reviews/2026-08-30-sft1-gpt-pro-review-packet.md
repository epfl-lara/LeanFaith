# SFT1 revision 0.3.0 — GPT Pro review packet

> **Review target:** SFT1 deterministic theorem-equivalence transform strategy and implementation
> readiness
>
> **Repository state:** review branch only; no transform implementation, Lean execution, dataset
> generation, 10K pilot, scale, or publication is authorized
>
> **Proposal:** revision 0.3.0, awaiting user approval

## 1. What LeanFaith is trying to build

LeanFaith trains a model to judge whether two Lean theorem statements express the same intended
mathematical claim. SFT1 is the large, cheap, deterministic supervision stage: it should generate
millions of useful theorem-statement pairs without an LLM label for every row and without spawning
one Lean process per row.

The desired value/cost tradeoff is:

- exact typed transformations and operation-local evidence create labels automatically;
- every retained row is checked and its evidence replayed inside bounded persistent Lean Meta
  workers;
- cheap source filtering, applicability discovery, sampling, hashing, and deduplication happen
  before expensive typed validation;
- scale comes from many diverse roots and mechanisms, not repeated lexical templates;
- SFT2 later adds smaller, more expensive LLM-generated and judged data.

The core row is:

```text
reference: goal_v1 string
candidate: goal_v1 string
label: bool
```

Rich provenance, certificates, truth evidence, transform chains, and compilation/rendering receipts
remain in keyed sidecars.

## 2. Current proposal in one page

Revision 0.3.0 is deliberately a non-executable proposal:

- 46 exact operation records replace umbrella family approval;
- 30 hash-bound starter-bank entries describe proposed schemas, lemmas, procedures, protected
  rubric dimensions, refutation templates, and reserved ideas;
- all operation admissions, executability flags, and label-emission flags are false;
- no row-count promise is authorized; 2–3M is a range to measure, not a minimum;
- the 10K pilot, bulk scale, and publication require later decisions.

Positive lanes:

- `P-DEF`: exact definitional equality;
- `P-SCHEMA`: exact instantiation of a pinned equivalence schema;
- `P-LEMMA`: one pinned local theorem rewrite with explicit context transport;
- `P-REFLECT`: one proof-producing procedure with a replayable local certificate.

Negative lanes:

- `N-RUBRIC`: an exact typed mutation of one protected claim dimension already named by the shared
  consistency rubric, with operation-specific applicability, anti-degeneracy checks, exact-delta
  evidence, and explicit operation plus family/dimension admission. It makes no claim about theorem
  truth.
- `N-PROOF`: a stronger, capped subtype of an admitted N-RUBRIC operation, retaining an exact source
  proof and candidate refutation.
- `D0` is only a structural delta and can never create a binary label.
- candidate truth evidence is separately `proved|refuted|unknown` and cannot determine the label.

Composition is intentionally narrow:

```text
positive := P | P P | P P P
negative := N | P N | P P N
```

There is exactly one negative, it is final, its protected site is recomputed after positive hops,
and inverse/mechanism/site/text/Expr/render cycles reject the chain.

## 3. Files that must be inspected

Please inspect the code and contracts, not only this summary:

1. `plans/30_sft1_deterministic.md` — scope, gates, dependencies, execution and approval contract.
2. `configs/transformations/sft1_value_first_v1/PROPOSED_TRANSFORM_AUDIT.md` — full family and
   operation audit, expected value, applicability, caps, cost, and dispositions.
3. `configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml` — authoritative
   exact operation registry and all machine-readable invariants.
4. `configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml` — design-frozen banks and
   reserved entries.
5. `src/leanfaith/sft1/composition_policy.py` and
   `tests/unit/sft1/test_composition_policy.py` — strict loader and 63 fail-closed invariants.
6. `TRANSFORM_CATALOG_V2.md`, `src/leanfaith/transforms/`, and `LeanFaith/Meta/` — historical and
   current implementation evidence. Do not assume that a family described in the proposal is
   implemented merely because an older family with a similar name exists.
7. `plans/00_shared_contracts.md` — shared representation, label, efficiency, release, and
   contamination rules.
8. `plans/02_goal_v1.md` and `LeanFaith/Meta/GoalV1.lean` — representation dependency. The reviewed
   predecessor is explicitly non-consumable by SFT1; a replacement freeze is in progress.

## 4. Known review history and current opinions

### Codex review

Our present view is that revision 0.3.0 is substantially more scientifically defensible than the
earlier polarity-only plan. The strongest corrections are:

- exact operation admission instead of family-wide approval;
- N-RUBRIC plus capped N-PROOF rather than pretending every useful negative can be refuted;
- no D0 or failed-search labels;
- claim-erasure firewalls for theorem-backed and reflective positives;
- negative-last composition with typed-site rediscovery;
- deterministic global duplicate/conflicting-label handling;
- 100% retained-row typed validation and certificate replay in persistent Meta;
- one success plus one adversarial rejection per operation/project, then about 100 eligible roots
  per operation, before even requesting a 10K pilot.

We do **not** yet claim that the chosen set is diverse enough, that every proposed operation is
worth implementing, or that the projected millions of rows represent millions of independent
training signals. That is the central purpose of this review.

### Claude Fable 5 maximum-reasoning review

Claude independently agreed that the earlier REPR freeze was mechanically real but unusable by
SFT1 because it lacked a direct closed-`Expr` renderer. It also found:

- P23's historical use of `Name.anonymous` produces `[anonymous]` locals that the representation
  rejects;
- the universe naming profile must be shared across REPR, SFT1, SFT2, and evaluation;
- the representation validator had unmeasured real-goal false rejections;
- copying rendering logic, creating temporary endpoint declarations, using `sorry`, or
  pretty-printing and re-elaborating candidates are unacceptable workarounds.

Revision 0.3.0 now treats all of these as hard pre-gate dependencies. The policy plans a
task-owned hygienic P23 implementation, but neither that implementation nor the replacement REPR
API exists on this review branch.

### Important boundary

The review should distinguish:

1. whether the *catalog and policy* are scientifically well chosen;
2. whether the *existing repository code* already implements each operation correctly;
3. whether the *planned implementation contract* is sufficient to make an unimplemented operation
   safe;
4. what evidence must be observed at one-example, 100-root, and later 10K gates.

## 5. Questions the review must answer

### A. Scientific coverage and diversity

1. Does the executable-candidate subset cover a sufficiently broad range of semantic phenomena, or
   is it overconcentrated in local logical rewrites, binder permutations, and small algebraic
   changes?
2. Build a coverage matrix across at least:
   - quantifier scope/order/dependency;
   - hypotheses and guards;
   - conclusion strength and polarity;
   - existence and uniqueness;
   - equality, order, set, function, relation, divisibility, and algebraic structure;
   - dependent types, subtypes, coercions, typeclasses, universes, and bundled structures;
   - finite/combinatorial indexing and boundary conditions;
   - theorem-backed local equivalences and reflective normalization;
   - constructive versus classical regimes.
3. Identify important blind spots and distinguish genuinely high-value additions from families that
   merely add volume or lexical variety.
4. Which mechanisms are likely to teach robust equivalence rather than transform-ID recognition?

### B. Exact operation audit

Review every one of the 46 exact operation entries. Do not omit diagnostic or proof-of-concept
entries. For each, state:

- keep, modify, diagnostic-only, proof-of-concept, defer, split, or reject;
- whether its evidence class really establishes the proposed F1 label;
- missing applicability or anti-degeneracy conditions;
- dependency, axiom, constructive/classical, coercion, instance, universe, and binder risks;
- cancellation/composition risks;
- likely semantic value and shortcut risk;
- whether the proposed cap is sensible;
- whether current code implements it, partially supports it, contradicts it, or does not exist.

Pay special attention to P01, P20–P24, P32–P42, N19, N25–N32, and the separate synthetic N28 track.

### C. Implementation readiness

1. Map proposal operations to actual code paths and tests. Flag every mismatch between prose/YAML
   and implementation.
2. Evaluate whether the planned certificates are kernel-replayable and sufficiently local; reject
   any route that can smuggle in an endpoint proof or whole-claim theorem.
3. Evaluate whether the strict loader checks the important scientific invariants or merely schema
   consistency.
4. Identify which operations need:
   - a one-example implementation proof;
   - a 100-root applicability study;
   - a redesign before coding;
   - a literature-backed schema/lemma bank;
   - a stronger independent audit.
5. Confirm whether persistent Meta workers, candidate pre-sampling, 100% retained-row replay, cache
   keys, journals, and time budgets are operationally plausible without per-row process spawn.

### D. Negative-label validity

1. Is N-RUBRIC a defensible deterministic F1 label basis when it makes no F2 truth claim?
2. Are the protected dimensions and operation/family admission requirements precise enough to
   avoid “different syntax implies different claim”?
3. Which N-RUBRIC operations still require an N-PROOF or a finite model before they are trustworthy?
4. Are N-PROOF caps appropriate, or would they create a truth/refutability shortcut?
5. Which negative operations may create invalid, vacuous, definitionally equal, or irrelevant
   candidates despite their current guards?

### E. Data value, shortcuts, and scale

1. Estimate, with explicit assumptions:
   - eligible roots by source;
   - raw candidates;
   - typed-valid candidates;
   - evidence-valid candidates;
   - distinct rendered pairs;
   - high-value retained pairs.
2. Is a 2–3M core realistic, useful, and sufficiently independent? Recommend a more defensible range
   if not.
3. Identify family, mechanism, operation, bank/template, lemma/procedure, source, token-overlap,
   length-ratio, edit-distance, and ancestry caps that should change.
4. Evaluate the proposed shortcut gates: candidate/reference-only balanced accuracy below 0.60 and
   paired family/mechanism/template-held-out balanced accuracy below 0.65, with confidence bounds.
5. Recommend additional canaries or held-out structures needed before scale.

### F. Literature comparison

Use primary sources wherever possible. Compare relevant formal-equivalence, autoformalization,
mutation-testing, theorem-proving, proof-repair, and contrastive-data systems. For each transferred
idea, distinguish whether the literature provides:

- a sound positive label;
- a sound negative label;
- only a candidate mutation;
- or only an audit/evaluation method.

Do not copy a mutation family without explaining the Lean-specific certificate required here.

## 6. Required response format

Return:

1. **Executive verdict:** approve revision 0.3.0 for bounded implementation, approve with named
   changes, or reject pending named evidence.
2. **Critical blockers:** ordered by severity.
3. **Coverage/diversity matrix:** covered, weakly covered, and missing phenomena.
4. **Complete 46-operation decision table.**
5. **Implementation reality table:** existing, partial, contradictory, planned, or absent, with
   exact code references.
6. **Missing high-value families:** full evidence/applicability/certificate/cost/cap specification.
7. **Composition and cancellation corrections.**
8. **Data-quality and realistic-volume estimate.**
9. **Ranked implementation roadmap** maximizing:

   ```text
   unique high-quality pairs × semantic value × label reliability
   --------------------------------------------------------------
                    Lean and engineering cost
   ```

10. **Exact approval wording** or the exact changes required before approval.

## 7. Review rules

Be skeptical and technically precise. In particular, do not:

- equate typechecking with semantic equivalence;
- equate a structural delta with a changed intended claim;
- equate candidate truth or refutability with F1 equivalence;
- use failed proof search as evidence;
- assume empty types are inhabited;
- ignore vacuous hypotheses or unreachable transformed sites;
- count swapped orientation as new data;
- recommend unrestricted `simp`, theorem search, or normalization;
- recommend full-corpus compilation or one Lean process per row;
- silently treat planned operations as implemented;
- recommend an LLM judgment for every SFT1 row.

The goal is not to maximize the number of transform IDs. The goal is a defensible catalog whose
labels, semantic coverage, implementation plan, composition behavior, data value, and Lean cost are
strong enough to justify large-scale deterministic supervision.

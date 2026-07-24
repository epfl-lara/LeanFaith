# LeanFaith Lean–Lean annotation guidelines v1

**Status:** frozen guideline for the LF-021 real-output prevalence campaign  
**Task:** reference-aware Lean–Lean same-claim and claim-relation annotation  
**Applies to frame:** `lf021_extended_prevalence_frame_v1:c4d248631456c45a05a6c4d929cda6a69c6b91e05e5e65ed4a8ca8eff0367312`  
**Frame SHA-256:** `a07b352030a2c51fa51ebcabc00a3c1d1ecf2041318feabfe57a4c70fc365069`

This guideline governs human semantic annotation of the 240 frozen LF-021
real-output frame items. Each item represents one unique
`(problem_group, alpha_identity_fingerprint)` claim. Retained invocation
multiplicity does not create additional human-label units.

The task is to compare the mathematical claims expressed by two elaborating,
proof-stripped Lean statements:

- **A:** the registered reference statement;
- **B:** the candidate autoformalization.

The primary question is whether A and B express the same intended mathematical
claim. This is an F1 claim-faithfulness judgment. It is not merely a check that
both statements compile, are provable, or are logically related as closed
propositions.

The frame initially contains operational `REVIEW` placeholders with
`resolution_outcome=unresolved`. Those fields are not labels, votes, hints, or
evidence and must not be shown as such to annotators.

## 1. Allowed annotation inputs

The blinded interface may show only what is needed to understand the pair:

- proof-stripped A and B, labeled consistently;
- their normalized or explicitly pretty-printed signatures;
- the minimum pinned imports, namespaces, local notation, and type information
  needed to interpret the signatures;
- a neutral indication that a displayed signature was successfully elaborated,
  or a notice that a required display artifact is unavailable.

Annotators may inspect definitions of constants appearing in A or B when the
meaning cannot otherwise be determined. Such lookups must occur in the pinned
Lean environment and be recorded in the rationale or annotation metadata.

The following information is prohibited until all raw labels for the item are
locked:

- generator family, model name, model size, checkpoint, or provider;
- prompt, decoding settings, reasoning trace, or generated explanation;
- tranche, invocation ID, pool, source proxy, sampling stratum, or multiplicity;
- model scores, structural similarity scores, metric predictions, or confidence;
- transformation or mutation intention, error-category request, or provenance;
- LLM-judge votes, symbolic-search outcomes, prior human votes, or adjudication;
- split assignment, benchmark status, acceptance decision, or expected label.

Project administrators may retain this metadata for sampling and later
analysis, but it must not be present in the annotation payload or UI. Annotators
must not attempt to identify the generator from style, search external logs, or
consult another annotator before submitting their independent label.

## 2. What “same claim” means

Choose `same_claim` only when A and B preserve the same substantive:

- mathematical objects and their roles;
- domains, codomains, types, subtypes, and typeclass assumptions;
- quantifiers and their dependency/order when order matters;
- hypotheses and side conditions;
- operators, predicates, constants, bounds, indices, and casts;
- conclusion and its direction or strength.

Harmless presentation differences do not change the claim. Examples include
alpha-renaming, formatting, grouped versus ungrouped binders, transparent
notation expansion, or reordering independent binders when all dependencies
and roles are preserved.

Do not choose `same_claim` merely because:

- both statements compile;
- both propositions are true or have proofs in the current library;
- automated search proves both directions between the closed propositions;
- the candidate resembles the reference textually;
- the candidate is a familiar theorem;
- a difference looks small.

A substantive missing, added, weakened, strengthened, or altered component
usually makes the pair `not_same_claim`, even when the candidate remains a true
and useful theorem.

## 3. Required annotation fields

Each independent annotation records:

```text
same_claim:
  same_claim | not_same_claim | ambiguous | cannot_assess_yet

relation:
  equivalent | A_stronger | B_stronger |
  incomparable | unrelated | ambiguous | null

confidence:
  1 | 2 | 3 | 4 | 5

rationale:
  concise semantic justification

reference_issue:
  none | suspected | definite
```

The rationale is mandatory for `not_same_claim`, `ambiguous`, and
`cannot_assess_yet`. It should identify the decisive mathematical agreement,
difference, ambiguity, or missing information. A rationale may cite syntax,
but must explain its semantic consequence.

`reference_issue` is independent of the relation decision. A suspected or
defective reference does not automatically make a pair ambiguous. Compare the
claims that A and B actually express, then flag the reference issue separately.

E01–E30 error codes may be collected as optional analysis metadata. They do
not determine the same-claim or relation label and are not required for this
prevalence task.

## 4. Canonical terminal outcomes

Only the following terminal semantic relations are permitted.

### `equivalent`

Use with `same_claim=same_claim`. A and B express the same intended claim under
this policy. Truth-level equivalence alone is insufficient.

### `A_stronger`

Use with `same_claim=not_same_claim` when A makes the strictly stronger
claim after aligning corresponding objects, binders, hypotheses, and roles.
Informally, A supports B at the claim level but the reverse claim fails or
requires additional content.

Typical indicators include a stronger conclusion, a broader quantified
domain, or fewer restrictive assumptions in A. These are indicators, not
automatic rules; dependencies and mathematical intent must be checked.

### `B_stronger`

Use with `same_claim=not_same_claim` for the reverse direction: B makes the
strictly stronger claim after role alignment. For example, a candidate that
removes a necessary hypothesis or strengthens the conclusion may be
`B_stronger`, even if it happens to be provable in the current environment.

### `incomparable`

Use with `same_claim=not_same_claim` when A and B are materially related but
neither is an accepted restatement or one-way strengthening of the other.
Examples include changing one substantive operator while preserving the rest
of the theorem, or changing different aspects in opposing directions.

`near_miss` is provenance or analysis metadata, not a terminal relation.

### `unrelated`

Use with `same_claim=not_same_claim` when B does not express the substantive
claim of A and there is no useful claim-level strengthening relation. Shared
vocabulary or a shared domain does not by itself make statements comparable.

### `ambiguous`

Use with `same_claim=ambiguous` only when the mathematical relationship is
genuinely underdetermined after inspecting all permitted context and applying
this guideline. This is a terminal semantic outcome:

```text
same_claim = null
resolution_outcome = ambiguous
relation = ambiguous
requires_adjudication = false
```

Terminal ambiguity is not a synonym for low confidence, lack of time,
unfamiliar mathematics, a missing UI artifact that can be recovered, or
annotator disagreement. Those conditions use the unresolved workflow below.

## 5. Unresolved workflow is not ambiguity

Choose `cannot_assess_yet` when a reliable decision requires recoverable
context, a domain expert, a policy ruling, or another technical check. It is a
request for adjudication, not a semantic class:

```text
same_claim = null
resolution_outcome = unresolved
relation = null
quality_tier = unknown
requires_adjudication = true
decision = REVIEW
```

The rationale must state exactly what is missing or disputed. `unknown` must
never be serialized as a semantic relation. Evidence records may independently
use an `unknown` status; that does not create a relation label.

An adjudicator may convert an unresolved item to a terminal outcome after the
missing context or expertise is supplied. If the information is intrinsically
insufficient even after that process, the adjudicator may choose terminal
`ambiguous` and must explain why no determinate semantic decision is possible.

## 6. Compilation and formal evidence

Compilation and elaboration are admission checks only. They show that Lean
accepted a statement in the pinned environment; they do not show that it is
faithful to A.

The following rules are absolute:

1. A failed proof search is never evidence of non-equivalence, non-implication,
   incomparability, or unfaithfulness.
2. A timeout, crash, unsupported tactic, missing counterexample, or other
   infrastructure outcome is never a semantic label.
3. Successful proof search between closed theorem propositions establishes, at
   most, registered F2 evidence. It does not by itself establish F1 same-claim
   faithfulness or a directional claim relation.
4. A definitional-equality result is auxiliary F0 evidence. Promotion to F1 is
   governed by semantic policy, not by the checker alone.
5. The absence of a proof or counterexample leaves the corresponding evidence
   unknown.

Raw annotators do not see proof-search or model-generated evidence. If an
adjudicator requests formal evidence, both the successful result and its exact
scope must be recorded. Failure of that request has no negative meaning.

## 7. Decision procedure

For each pair:

1. Read the complete proof-stripped signatures and the permitted context.
2. Identify corresponding objects, binders, domains, hypotheses, dependencies,
   and conclusions.
3. Ignore names and cosmetic presentation.
4. Compare every substantive component, including implicit type/domain changes,
   coercions, quantifier dependencies, bounds, and side conditions.
5. If all substantive content is preserved, choose
   `same_claim + equivalent`.
6. Otherwise determine whether A is stronger, B is stronger, the claims are
   related but incomparable, or unrelated.
7. Use terminal `ambiguous` only for genuine semantic underdetermination.
8. Use `cannot_assess_yet + relation=null` for any recoverable uncertainty.
9. Assign confidence and write the required rationale.
10. Flag a suspected reference problem without using it as a shortcut to the
    relation label.

Do not force a directional label solely from surface patterns such as “more
hypotheses” or “larger domain.” Determine strength only after aligning the
mathematical roles. If no reliable one-way relation is supported, use
`incomparable`, `unrelated`, or unresolved review as appropriate.

## 8. Independent annotation

Every frame item receives two independent expert annotations.

- Assignment order is randomized without exposing sampling strata.
- The two annotators must not discuss the item before both records are locked.
- Neither annotator sees the other vote, rationale, confidence, or identity.
- Submitted raw records are immutable and preserved even after adjudication.
- Administrative corrections create append-only superseding records; they do
  not overwrite the original decision.

Items are routed to adjudication when:

- same-claim answers differ;
- terminal relations differ;
- either annotator selects `cannot_assess_yet`;
- either annotator reports a definite reference issue;
- either annotator has confidence 1 or 2;
- a versioned policy trigger requires review.

Agreement is computed on the independent pre-adjudication records. An
adjudicated label must never be substituted into raw agreement statistics.

## 9. Adjudication

The adjudicator receives the same blinded semantic payload, then the two locked
raw annotations and their rationales. Generator/model identity, scores,
sampling stratum, intended label, and LLM judgments remain prohibited.

The adjudicator must:

1. reproduce or recover any missing permitted context;
2. identify the exact source of disagreement;
3. apply this versioned guideline rather than majority vote;
4. select one canonical terminal outcome or retain the unresolved route;
5. record an adjudication rationale and any permitted evidence consulted;
6. preserve both independent annotations and link them to the new resolution.

If the disagreement exposes a missing semantic-policy rule, the item remains
unresolved until that rule is adopted. Guideline changes occur only between
annotation rounds, receive a new version, and trigger re-review of every
affected earlier item. Frozen labels are not silently reinterpreted.

## 10. Quality and release checks

Before prevalence estimation or Gate-5 use:

- every one of the 240 frozen frame items has an attempted annotation;
- every terminal record uses one canonical relation spelling;
- `same_claim` and `relation` combinations satisfy Sections 3–5;
- unresolved records remain visible as nonresponse and `REVIEW`;
- terminal ambiguity is reported separately and is not coerced to faithful or
  unfaithful in the primary three-way estimate;
- both independent raw labels and any adjudication record are preserved;
- prohibited metadata did not enter the annotation payload;
- no compilation, proof-search failure, or LLM agreement created a human label.

Human annotation does not retroactively change the frozen frame, its inclusion
probabilities, or its multiplicities. It supplies genuine semantic outcomes for
the already frozen sampling units.

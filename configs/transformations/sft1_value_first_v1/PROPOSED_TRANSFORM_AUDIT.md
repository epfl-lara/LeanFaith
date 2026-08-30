# SFT1 value-first transform audit and proposal

> **Status:** revision 0.3.0 proposal only; awaiting the exact user decision in
> `plans/30_sft1_deterministic.md`
>
> **Audit date:** 2026-08-30
>
> **Revision:** 0.3.0, superseding but not approving revision 0.2.0
>
> **Authorization:** strict policy loading and non-Lean invariant tests only
>
> **Lean/data status:** no Lean run, no transform implemented, and no row generated

## 1. Executive verdict

The supplied independent review is accepted as design input. Its central scientific point remains
the boundary for SFT1: “same intended claim” is not kernel-decidable. A deterministic label is
defensible only when an exact, closed, elaborated proposition pair is related by an admitted
transform-local mathematical paraphrase or by an admitted protected-claim mutation, under a
recorded representation and logic regime.

Revision 0.3.0 makes seven material corrections:

1. The old N-PROOF-only negative core becomes two explicit lanes: operation-specific **N-RUBRIC**
   and a stronger capped **N-PROOF** subtype. Generic D0 is never label evidence.
2. Both endpoints are canonical closed `Expr` values rendered by direct calls to the same pending
   `renderClosedProp` API in one persistent Meta request. Rendering never copies the renderer or
   options, declares a theorem/axiom, uses `sorry`, synthesizes an endpoint proof, surface-renders
   or pretty-prints/re-elaborates a candidate, or compiles model-facing `goal_v1` text.
3. Every P-LEMMA/P-REFLECT operation carries four claim-erasure guards.
4. An exact 46-operation registry replaces family-wide executable approval. Every operation has its
   own admission object and remains non-executable.
5. The required positive and negative starter banks are design-frozen and hash-bound before
   implementation; unused entries are explicitly reserved.
6. “No per-pair Lean” is replaced by “no per-row process spawn”: cheap sampling happens first, but
   every retained row receives bounded typed Meta validation and evidence replay.
7. A zero-Lean root census precedes row commitments. The 2–3M range is a band to measure, not a
   minimum or promise; 500K×8 is 4M, and 5M requires at least 625K roots.

The proposal does not authorize implementation, Lean, the one-example gates, the approximately
100-root gates, the 10K pilot, data generation, scale, publication, or any row count.

## 2. Operational evidence contract

| Code | Exact meaning in revision 0.3.0 | Label role |
| --- | --- | --- |
| P-DEF | Exact closed propositions are definitionally equal under pinned transparency/options, with no unresolved term or universe metavariables and no endpoint proof | Positive only after exact-operation admission |
| P-SCHEMA | One pinned universally valid schema is instantiated exactly, including all side conditions and its audited dependency closure | Positive only after exact-operation admission |
| P-LEMMA | One frozen theorem/lemma rewrites one typed occurrence through an explicit context; arguments, instances, direction, path, dependency closure, and replayable proof are retained | Positive only after exact-operation admission and all four claim-erasure guards |
| P-REFLECT | One pinned proof-producing procedure yields a kernel-replayable local equality/iff certificate transported through one exact context | Positive only after exact-operation admission and all four claim-erasure guards |
| N-RUBRIC | One exact typed mutation changes a protected dimension named by the shared consistency rubric, with operation-specific applicability, anti-degeneracy checks, exact-delta reconstruction, and operation/family admission | Negative F1 label basis; makes no F2 or truth claim |
| N-PROOF | Stronger capped subtype of one admitted N-RUBRIC operation, retaining an exact source proof and exact candidate refutation for the same closed pair | Negative F1 label basis; candidate truth evidence is `refuted` |
| D0 | Type-safe structural delta only | Candidate/diagnostic evidence, never a label |
| F2-DIR | One implication/specialization/weakening direction only | Auxiliary relation, never a label |

Candidate truth evidence is separately recorded as exactly `proved`, `refuted`, or `unknown`.
It cannot determine the F1 label, select a lane, or turn failed proof search into evidence.

Positive certificates have the review-required dependency firewall: no source/candidate declaration,
endpoint proof, unrelated theorem proving a whole endpoint, unrestricted theorem search, or
unrestricted simplifier trace. The exact allowed schema/lemma/procedure closure, transparency,
logic regime, and axiom profile are recorded.

Every certificate binds the complete closed telescope: universes, ordered locals, binder kinds,
local definitions, implicit/instance binders, coercions, synthesized instances, hypotheses, and
target.

## 3. Representation and source applicability

The REPR freeze at commit `cbc933c3623d81ba649a1f9c5107ad404389d69f` and spec hash
`073d92c8e1fcc5cb7a3a9bf325d047e9b2d52149504977086de46abf6f84ef52` was reviewed, but is now
recorded only as a superseded predecessor. It is not consumable by SFT1, its hashes are not live
execution dependencies, and the loader no longer verifies it as the active renderer.

Implementation and every Lean gate are hard-blocked until REPR publishes a new coherent freeze
exposing `LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String`, with replacement
commit/spec/config, Lean/Python, canonical-universe-profile, render-context, and real-goal coverage
hashes. Those replacement fields remain null/pending.

The exact function must be called directly for reference and candidate in the same persistent Meta
request. It must reject Expr/universe metavariables, free variables, loose bvars, ill-typed values,
non-propositions, and anonymous binders exposed as named locals in the rendered outer Pi telescope;
structural anonymous Pis rendered as arrows are not named-telescope residue. SFT1 may not copy the
renderer/options, declare an endpoint, synthesize a proof, use `sorry`, surface-render,
pretty-print/re-elaborate a candidate, or compile `goal_v1` text. `goal_v1` is output, not Lean
input.

The replacement freeze must publish a `renderer_api_hash` over exactly its replacement commit,
Lean renderer path/hash, namespace, and signature. Universe-profile and render-context hashes stay
independent so their provenance cannot be hidden inside the code hash.

The two Exprs must use the same canonical universe profile selected by the replacement REPR freeze.
The current engine's local `u_i` naming is not admissible unless it exactly equals that shared,
hash-bound profile used by REPR, SFT1, EVAL, and SFT2.

Source applicability begins with a zero-Lean census:

| Source | Zero-Lean eligibility evidence | Later closed-Expr route | Ineligible when |
| --- | --- | --- | --- |
| Mathlib/Physlib/CSLib imported constants | pinned declaration inventories, revisions, licenses, projects/imports, exact and near-duplicate clusters | obtain `ConstantInfo.type`, deterministically instantiate universes, require closed/type-correct/`Prop` | constant/context unavailable, unapproved dependencies, unresolved universe profile, non-`Prop` |
| `compiler_data` signatures | pinned source files/metadata, final signature extraction, exact compile context, license/revision, duplicates | in one persistent `TermElabM`, elaborate the complete telescope/result directly to a closed Prop Expr; never declare a theorem | missing exact context, elaboration leaves metavariables/open terms, non-`Prop`, unapproved dependencies |

The census reports raw/eligible counts, clusters, source/domain/signature strata, import-context
coverage, and expected route before any row commitment.

## 4. Claim-erasure guards

Every exact operation whose evidence class is P-LEMMA or P-REFLECT must reject:

1. a lemma/procedure that proves, reuses, or replaces the complete claim;
2. a reflexive result or collapse to `True`/`False`;
3. deleting a hypothesis by rewriting it to `True`;
4. reflective normalization of the root relation or whole claim.

The guard result, selected proper occurrence, before/after subterm hashes, context transport, and
adversarial rejection code are replay evidence. These guards apply to P32, P33, P34, P36, P39,
P41, and P42 registry entries; no family-level exception exists.

## 5. Exact admitted-design preserving registry

All entries below remain `executable: false`, `label_emission_authorized: false`, and
`admission.approved: false`. “Candidate” and “proof of concept” describe the proposed next
implementation stage, not current authorization.

| Exact operation ID | Status / evidence / mechanism | Typed applicability and composition safety | Expected value | Lean cost / exact cap |
| --- | --- | --- | --- | --- |
| P01_ALPHA_RENAME_SINGLE_V1 | candidate; P-DEF; presentation alpha | one capture-free explicit binder rename; one hop only; sole exception may repeat alpha fingerprint once but never Expr/render/text/inverse | high coverage, low standalone signal | C1; 0.5%, one/root |
| P02_REGROUP_BINDERS_V1 | **diagnostic**; P-DEF; binder presentation | adjacent identical binder kinds/types and dependency graph; retain only distinct render for diagnostics | low-medium | C1; 0.2%, no label |
| P11_BOUNDED_FORALL_EXPAND_V1 | **diagnostic**; P-SCHEMA; bounded-quantifier presentation | exact guard, order, instance, binder, and body; reject overlap with guard-removal negatives | medium diagnostic value | C1–C2; 0.2%, no label |
| P14_SWAP_INDEPENDENT_DATA_BINDERS_V1 | candidate; P-SCHEMA; binder permutation | adjacent explicit data binders with exact mutual independence; one swap/inverse token | high | C1–C2; 2%, one/root |
| P15_SWAP_IFF_SIDES_V1 | candidate; P-SCHEMA; logical symmetry | exact distinct Iff sides; once per chain | high | C1–C2; 2%, one/root |
| P16_REASSOC_AND_LEFT_V1 | candidate; P-SCHEMA; logical reassociation | exact three-node And tree, atom order preserved; reject AC cycles/overlap | high structural, lower yield | C1–C2; 2%, one/root |
| P18_SYMMETRIZE_EQUALITY_V1 | candidate; P-SCHEMA; equality symmetry | exact distinct Eq operands; once per chain; protected from overlapping negative mutation | high | C1–C2; 2%, one/root |
| P20_FOLD_SET_NONEMPTY_V1 | candidate; P-DEF; frozen definition fold | exact transparent `Set.Nonempty` body and arguments, unique inverse; no whole-claim collapse | medium-high | C1; 1.5%, one/root |
| P20_UNFOLD_SET_NONEMPTY_V1 | candidate; P-DEF; frozen definition unfold | exact `Set.Nonempty` application and arguments; no proof/opaque unfolding | medium-high | C1; 1.5%, one/root |
| P21_BETA_INTRO_V1 | **diagnostic introduction**; P-DEF | uniquely reconstructible redex, immediate reduction equals source; reject padding/render collapse | low | C1; 0.1%, no label |
| P21_BETA_REDUCE_V1 | candidate reduction; P-DEF | explicit beta redex, closed argument, capture-free substitution; one definitional mechanism | medium | C1; 1.5%, one/root |
| P21_ZETA_INTRO_V1 | **diagnostic introduction**; P-DEF | uniquely reconstructible used let; reject unused-let padding/render collapse | low | C1; 0.1%, no label |
| P21_ZETA_REDUCE_V1 | candidate reduction; P-DEF | exact local let, closed value, capture-free zeta substitution | medium | C1; 1.5%, one/root |
| P22_ETA_REDUCE_EXPLICIT_FUN_V1 | candidate; P-DEF; eta reduction | explicit nondependent lambda; final bound variable only as last argument; no introduction | medium | C1; 1%, one/root |
| P23_CURRY_PROP_PAIR_V1 | candidate; P-SCHEMA; proposition packaging | two adjacent independent Prop binders and proof-independent continuation; task-owned implementation must use deterministic capture-free `Name.mkSimple` names `h`, then smallest fresh `h_<n>`; reject `Name.anonymous` and `[anonymous]`; excludes P12/P17 | high | C1–C2; 2%, one/root; hygiene regression required |
| P24_SWAP_INDEPENDENT_PROP_BINDERS_V1 | candidate; P-SCHEMA; proof-binder permutation | adjacent mutually proof-independent Prop binders; one swap | high | C1–C2; 2%, one/root |
| P28_DECOMPOSE_IFF_V1 | candidate; P-SCHEMA; logical decomposition | exact Iff to fixed-order conjunction of implications; no P15 at same site/cycle | high | C1–C2; 1.5%, one/root |
| P32_ADD_ASSOC_LOCAL_V1 | candidate; P-LEMMA; frozen AC bank | one proper additive associativity occurrence with exact carrier/instances; four erasure guards; no P34/P35/P42 overlap | high | C2; 1%, one/root |
| P32_ADD_COMM_LOCAL_V1 | candidate; P-LEMMA; frozen AC bank | one proper additive commutativity occurrence with distinct operands; guards/cycle checks | high | C2; 1%, one/root |
| P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1 | proof of concept; P-LEMMA; hypothesis transport | equality proof local retained; continuation transport is nondependent; four guards; no same-local P39/permutation | very high, riskier | C2; 0.5%, one/root |
| P34_NAT_SUCC_ADD_ONE_LOCAL_V1 | candidate; P-LEMMA; frozen semantic-rewrite bank | one proper Nat successor occurrence, exact theorem/instances/context; four guards | medium-high | C2; 1%, one/root |
| P35_SET_INTER_MEMBERSHIP_V1 | candidate; P-SCHEMA; frozen membership bank | exact Set-intersection membership with identical arguments; one local site | high in set roots | C2; 1%, one/root |
| P36_SET_EXTENTIONALITY_V1 | proof of concept; P-LEMMA; extensionality | nonreflexive same-carrier root Set equality to elementwise iff; four guards; propext recorded | very high, regime-sensitive | C2–C3; 0.5%, one/root |
| P38_EXISTS_SUBTYPE_NONEMPTY_V1 | proof of concept; P-SCHEMA; subtype packaging | exact Exists/Nonempty-subtype schema, proof-independent projection; no P40/P41 overlap | high | C2; 0.5%, one/root |
| P39_HYP_SET_INTER_REWRITE_V1 | **proof of concept**; P-LEMMA; frozen hypothesis bank | exact proof-independent hypothesis transport; all guards; no same-local P23/P24/P33 | very high | C2; 0.5%, one/root |
| P40_EXISTS_UNIQUE_EXPAND_V1 | proof of concept; P-SCHEMA; uniqueness expansion | exact ExistsUnique predicate/equality orientation; no P38/P41 at introduced existential | very high | C2; 0.5%, one/root |
| P41_SUBTYPE_FORALL_GUARD_V1 | **proof of concept**; P-LEMMA; frozen subtype bank | body depends only on coerced value, never proof field; all guards | very high | C2; 0.5%, one/root |
| P42_RING_POLYNOMIAL_LOCAL_V1 | **proof of concept**; P-REFLECT; frozen procedure bank | proper local supported polynomial subterm; retained proof; all guards; root-relation normalization forbidden | very high | C2–C3; 0.5%, one/root |

P23 explicitly supersedes the anonymous-binder construction in the read-only shared
`LeanFaith/Meta/TransformEngine.lean`. A live probe produced `[anonymous] : True✝`, and the frozen
validator rejected it. Future approved work must live in `LeanFaith/Meta/SFT1/TransformEngine.lean`,
allocate generated binder names deterministically against the complete telescope, inspect Expr
binder names before rendering, and pass a shared-API regression with no `[anonymous]` output. The
existing shared file remains untouched.

### Preserving families not admitted as exact operations

| Family/legacy range | Typed applicability finding | Composition safety | Expected value | Lean cost / disposition |
| --- | --- | --- | --- | --- |
| P00/P03 | disabled fixture / unused ID; no typed candidate | not composable | none | C0 inventory only; no operation |
| P04–P10 | notation, names, implicits, coercions, ascriptions, projections, and constructors often vanish under canonical rendering | no chain use until distinct closed-Expr render and inverse/site rules exist | low historical clean yield | C0–C1 diagnostic; deferred |
| P12/P13/P17 | proof-arrow, surface eta, and hypothesis packing overlap the typed P23/P22 designs | excluded to prevent mechanism and inverse overlap | superseded | C0 inventory; historical fixtures only |
| P19 | umbrella β/δ/ι/ζ/η normal-form choice is not one exact applicability predicate | unsafe with P20–P22 and internally cycle-prone | medium if split | C1 diagnostic; redesign as exact non-overlapping operations |
| P25 | neutral wrapper/presentation lacks a non-padding typed criterion | obvious wrapper cues and cancellation risk; not composable | low | C1 diagnostic; direct-only if ever specified |
| P26/P27/P29 | material implication, contraposition, and double-negation/De Morgan rules depend on constructive, decidable, or classical regimes | no composition until each regime, side condition, and inverse token is exact | potentially high but regime-sensitive | C2 redesign with frozen schemas; no current operation |
| P30/P31 | quantifier distribution/extraction needs exact nonempty-domain and dependency premises | unsafe with binder/guard operations until protected sites and premises are explicit | high on eligible roots | C2 redesign as individual schemas; no umbrella admission |
| P37 | instance/coercion transport is not implied by proof irrelevance | cannot compose without definitional identity or explicit equality/Subsingleton evidence | uncertain | C2–C3 conditional future work |

## 6. Exact admitted-design negative registry

Every natural negative family admitted in this registry has an N-RUBRIC operation and an optional
lower-cap N-PROOF sibling. The N-PROOF pointer, mutation site, rubric dimension, pair hashes, and
candidate refutation must match exactly.

| Exact operation ID | Lane / protected dimension | Applicability and anti-degeneracy | Value | Lean cost / exact cap |
| --- | --- | --- | --- | --- |
| N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1 | N-RUBRIC; shared-rubric negation mistakes | complete closed proposition only; reject existing outer negation, True/False defeq, same render; no F2/truth claim | medium, strong cue risk | C1–C2; 1% |
| N19_NEGATE_CLOSED_CLAIM_PROOF_V1 | N-PROOF subtype | exact source proof plus refutation of exact negated candidate; no endpoint declaration | stronger evidence, capped | C2; 0.5% |
| N25_TOGGLE_EQ_NE_RUBRIC_V1 | N-RUBRIC; shared-rubric negation mistakes | one protected typed Eq/Ne; reject defeq operands, unreachable/vacuous site, same render | very high | C1–C2; 1.5% |
| N25_TOGGLE_EQ_NE_PROOF_V1 | N-PROOF subtype | complete telescope assignment, retained hypotheses, source proof, candidate refutation | very high | C3; 0.75% |
| N26_INCREMENT_BOUND_RUBRIC_V1 | N-RUBRIC; shared-rubric edge cases | one normalized protected literal, unchanged type, unique +1 delta; reject irrelevant/unreachable literal | highest | C1–C2; 1.5% |
| N26_INCREMENT_BOUND_PROOF_V1 | N-PROOF subtype | exact boundary witness, source proof, candidate refutation; no timeout/search-failure label | highest | C3; 1% |
| N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1 | **proof of concept**; witness dependency | exact ∀∃→∃∀ bvar remap; two distinguishable inputs, body depends on witness; protect binders/domain/body | highest but rare | C2–C3; 0.75% |
| N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1 | **proof of concept** N-PROOF | complete finite cases prove source and refute uniform-witness candidate | highest | C3; 0.3% |
| N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1 | **proof of concept**; existence/uniqueness | exact Exists→ExistsUnique predicate; two distinguishable candidate witnesses; reject subsingleton | very high | C2–C3; 0.5% |
| N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1 | **proof of concept** N-PROOF | exact source existence proof plus two satisfying disequal witnesses refuting uniqueness | very high | C3; 0.25% |
| N31_DROP_REQUIRED_GUARD_RUBRIC_V1 | **priority-1 proof of concept**; required guard | exact one-local deletion/bvar reindex; reject unused/redundant guard, True body, unreachable context | highest priority/value | C2–C3; 1% |
| N31_DROP_REQUIRED_GUARD_PROOF_V1 | **priority-1 proof of concept** N-PROOF | complete values/hypotheses plus source proof and unguarded-candidate refutation | highest priority/value | C3; 0.5% |
| N32_SWAP_ROLE_ORDER_RUBRIC_V1 | **proof of concept**; shared-rubric converse mistakes | reverse only distinct same-typed arguments to the same protected binary relation; reject symmetric/commutative heads and all function-composition reorderings; no P32/P34/P42 overlap | very high | C2–C3; 0.5% |
| N32_SWAP_ROLE_ORDER_PROOF_V1 | **proof of concept** N-PROOF | exact source proof and reversed-relation-argument candidate refutation with nonsymmetry evidence | very high | C3; 0.25% |
| N28_FINITE_ARITHMETIC_RUBRIC_V1 | **separate synthetic proof of concept**; shared-rubric edge cases | frozen generated Nat template, exactly one protected +1 mutation, all other Expr nodes equal; isolated ancestry | medium-high, template risk | C2; 0.25% |
| N28_FINITE_ARITHMETIC_PROOF_V1 | separate synthetic N-PROOF | frozen exact source proof/refutation template; template-held-out gate | stronger, capped | C2–C3; 0.1% |
| N28_FINITE_SET_RUBRIC_V1 | **separate synthetic proof of concept**; shared-rubric negation mistakes | frozen finite-set template, exact Eq→Ne protected delta, all other Expr nodes equal; isolated ancestry | medium-high | C2; 0.25% |
| N28_FINITE_SET_PROOF_V1 | separate synthetic N-PROOF | frozen exact source proof/refutation template; template-held-out gate | stronger, capped | C2–C3; 0.1% |

N28 has its own split and cap denominator and cannot be used to satisfy natural-root yield or
balance. N29–N32 remain proof-of-concept; N31 is attempted first.

### Breaking families not admitted as exact label operations

| Family/legacy range | Typed applicability finding | Composition safety | Expected value | Lean cost / disposition |
| --- | --- | --- | --- | --- |
| N01/N02/N03/N07/N10–N18 | historical typed/surface mutation proves only D0 or direction; degeneracy, vacuity, symmetry, and unrealized separators remain | never enter a model-facing chain or supply a label | diagnostic discovery value only | C0–C2 preserved evidence; no label operation |
| N20 | complete-claim false conjunction can be certified, but local-target use is unsound under unreachable hypotheses | no composition; complete-claim form is an obvious shortcut | low | C1–C2 diagnostic only |
| N21/N22 | Boolean-skeleton pilots did not ground arbitrary atoms in exact theorem models | no registry entry, composition, or one/100 gate work | low until grounded redesign | prior C1–C2 evidence only; redesign-only |
| N23/N24 | strengthening/weakening supplies only F2 direction without a concrete separator | auxiliary relation only; never a negative terminal | useful sidecar signal | C1–C2 sidecar only |
| N27 | domain/type drift lacks exact embeddings, coercion/instance audit, and a separating element | cannot compose until all transport and separator evidence is closed | potentially high | C3 conditional N-PROOF redesign |
| Generic separator/model search | abstract truth tables or failed search do not realize the closed Lean pair | discovery cannot enter a chain or select a label | discovery only | C0–C3 D0 until exact translation and replay exist |

## 7. Frozen banks and exact admission

The design bank `starter_banks_v0_3_0.yaml` contains 30 entries:

- P20 definition anchors;
- P32 AC lemma anchors;
- P34 semantic-rewrite anchors;
- P35 membership schemas;
- P39 hypothesis schemas;
- P41 subtype-quantifier schemas;
- P42 proof-producing reflective procedures;
- seven shared-rubric negative model entries spanning six distinct dimensions;
- seven exact N-PROOF templates; and
- two separate N28 synthetic templates.

Each entry has a canonical anchor-spec hash and a registry binding. Six broader starter ideas
(`Function.Bijective`, multiplication AC, set-image composition, union membership, subtype
existential, and similar unused orientations) remain `reserved_unadmitted`; their presence in a
bank cannot authorize them. Lean-resolved hashes are intentionally null and must be committed
before execution.

Approval is exact-operation-local. A family status, polarity, bank membership, or successful proof
cannot approve neighboring operations. Diagnostic and unresolved entries can never emit rows.
Nine separate pending family-and-rubric-dimension admission records bind the natural negative
families and both N28 synthetic dimensions to their exact N-RUBRIC/N-PROOF member IDs. All nine are
false; both that record and the exact operation admission must be true in a future approved freeze.

## 8. Composition safety

The exact grammar is:

```text
positive_row := P | P P | P P P
negative_row := N | P N | P P N
```

`P` is an admitted positive operation. `N` is one admitted N-RUBRIC operation or its capped
N-PROOF subtype. A positive row has zero negative operations; a negative row has exactly one.
Nothing follows N. At most three total operations occur.

Every hop starts from the current typed Expr and uniquely rediscovers a site. Selected sites are
pairwise disjoint. Mechanism superclasses and inverse tokens do not repeat. Repeated text, closed
Expr, render, or selected-site-lineage hashes reject the chain. P01 alone may repeat the alpha
fingerprint once across its sole hop; that exception never permits a second P01 or any other cycle.

The exact P20 fold/unfold, P21 beta/zeta introduction/reduction, and P22 eta-reduction operations
are one named mutual-exclusion group with at most one member per chain. The loader checks the exact
seven-member set, so the one-definitional-mechanism rule cannot drift into an umbrella family flag.

Safe proposal examples:

- P01 alpha rename → P34 local successor rewrite at a disjoint site;
- P24 independent proof-binder swap → N26 exact bound mutation, with N26 evidence built against the
  swapped intermediate;
- P39 hypothesis transport → P34 target rewrite at a rediscovered disjoint occurrence.

Rejected examples:

- N25 → P18, because a positive follows a negative;
- P15 → P15, because the inverse/cycle token repeats;
- any P20 → P21 chain, because the exact seven-operation definitional group permits at most one
  member anywhere in a chain;
- P33/P39/P41 claim-erasing use of a whole-claim lemma or hypothesis-to-True rewrite;
- P42 normalization of the root relation;
- P14/P24 touching a binder protected by N29;
- any D0 legacy negative in a model-facing chain.

## 9. Value, cost, sampling, and caps

Lean cost classes assume one initialized persistent project/toolchain environment:

- **C0:** zero-Lean census, source parsing/filtering, joins, deduplication, and candidate sampling;
- **C1:** typed local construction, closure/`inferType`/`isProp`/`isDefEq`, hashing, and render;
- **C2:** one pinned schema/lemma/procedure certificate and replay;
- **C3:** bounded witness/finite-case/side-condition construction plus replay.

C0 work and deterministic pre-validation candidate sampling happen first. Sampling is seedless:
sort by the hash of source closed Expr, operation ID, selected-site lineage, and candidate closed
Expr, then take the operation-budget prefix. C1–C3 work then runs for **every retained row**, not a
sample. No row spawns a process. Persistent workers enforce per-op heartbeat/soft/hard budgets,
retry only infrastructure failures, journal by root, and measure Lean-seconds and sidecar bytes per
retained pair.

Global maxima are: compiler_data roots 20%; any source 40%; family 8%; mechanism 12%;
presentation/definitional combined 10%; exact operation 2%; bank/template 0.5%;
lemma/procedure 0.25%; ordered composition template 0.5%; eight retained pairs/root. Exact smaller
operation caps in the registry win. Natural and synthetic denominators are separate, and caps never
force fill or class balance.

Retention uses a stable row-hash total order before deduplication and finite caps. The order is
source eligibility, operation applicability, root blocklist, pre-validation sampling, typed Meta
validation/replay, post-transform blocklist, stable ordering, canonical-unordered-pair
duplicate/conflict classification, same-label minimum-row-hash retention or whole-class
conflicting-label rejection, per-root, operation, bank/template, lemma/procedure, family, mechanism,
source, and joint source×polarity caps, deterministic training orientation swap, then a final
model-facing duplicate/conflict assertion. An assertion failure prevents shard commit or refill.

The stable row hash canonically binds source/root identity, both closed-Expr hashes, operation-chain
and selected-site lineage hashes, label, evidence/certificate payload, renderer, REPR spec,
universe-profile, and render-context hashes. The orientation-invariant pair class hashes the two
rendered-output hashes in sorted order.

Both evaluation screens bind
`data/benchmarks/golden_blocklist_v1.json` at SHA-256
`8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7`; matches are excluded and
counted.

Cache keys bind both closed Expr hashes and builders, canonical universes, Lean/project/toolchain,
imports/options/instances, operation and computed registry-entry hashes, anchor and resolved-bank
hashes, evidence/certificate payload hash, transparency, axiom profile, validator/evidence versions,
replacement commit, renderer API, REPR spec, render-context ID/hash, and policy hash. Successes and
deterministic terminal failures are reusable.

## 10. Representation pre-gate and shortcut controls

Before one-example Lean, the replacement REPR freeze must be pinned and its real-goal coverage
receipt must pass. Each reference/candidate closed Expr pair must render by direct shared-API calls
in the same persistent request; required-distinct outputs must differ and each output must contain
exactly one turnstile. Reject all Expr/render residue involving mvars, universe mvars, fvars, loose
bvars, anonymous names exposed as rendered outer-telescope locals, ill-typed values, or non-Props.

Representation failures are counted by source, family, exact operation, polarity, and exact failure
class. Stable IDs/sidecars bind both closed-Expr hashes, renderer/spec hashes, canonical-universe-
profile ID/hash, replacement commit, render-context ID/hash, and both rendered-output hashes. P23
additionally requires its no-anonymous-binder regression. Inline schema/lemma/procedure resolutions
and per-operation/per-project fixture bundles must be hash-frozen before the one-example gate.

The gate sequence is:

1. Complete the zero-Lean root census and source-eligibility matrix.
2. After exact user approval, the new coherent REPR replacement freeze, the additive shared label
   contract merge, and all representation pre-gate checks, require one success and one adversarial
   rejection for every exact operation in every eligible project. Zero yield needs a census-backed
   policy revision, not a silent waiver.
3. Only after that matrix passes, process approximately 100 eligible roots per operation with 100%
   retained-certificate replay.
4. Stop, report, and request a separate user decision for the 10K pilot.

Any later 10K pilot must enforce:

- candidate-only and reference-only balanced accuracy <0.60;
- paired family-, mechanism-, and template-held-out balanced accuracy <0.65;
- 95% stratified-cluster-bootstrap confidence bounds, with the upper bound below the threshold;
- deterministic 50% training-only orientation swap after caps and the orientation-invariant
  duplicate/conflict screen, followed by the final model-facing assertion;
- intact ancestry and near-duplicate clusters;
- root-level and post-transform evaluation blocklist screens;
- global model-facing duplicate and conflicting-label rejection;
- joint source×polarity stratification without forced examples; and
- 100% retained-certificate replay in persistent Meta.

The zero-Lean census and pilots must measure whether a 2–3M planning band is realistic. That range
is not a minimum or commitment. At eight rows/root, 500K roots cap at 4M and 5M requires at least
625K roots. The 10K pilot, all scale, publication, and every target remain explicitly gated.

## 11. Coordinator requests

- REPR must publish a new coherent freeze, superseding the non-consumable reviewed `cbc933…`
  predecessor, with committed `renderClosedProp (e : Expr) : MetaM String`, replacement
  commit/spec/config and Lean/Python hashes, canonical-universe-profile ID/hash and naming contract,
  canonical renderer-API hash and hash basis, render-context identity, a passed hash-bound real-goal
  coverage regression, and a direct reference/candidate same-persistent-request test. The API must
  fail closed on open, metavariable, exposed anonymous-telescope-binder, ill-typed, and non-Prop
  Exprs. SFT1 will not copy rendering code/options,
  declare endpoints, synthesize proofs, surface-render/re-elaborate candidates, use `sorry`, or
  compile goal text.
- `plans/00_shared_contracts.md` must be updated additively so exact row evidence plus admission of
  the exact operation/family/rubric dimension creates an SFT1 label; polarity multiplication, D0,
  F2 direction, failed search, and candidate provability do not. It must record N-RUBRIC and capped
  N-PROOF lanes and the separate `proved|refuted|unknown` truth field.

This SFT1 session does not own either coordinator path.

## 12. Exact approval requested

> Approve SFT1 proposal revision 0.3.0 solely for task-owned implementation and the one-success plus one-adversarial-rejection per exact operation and eligible project gate followed by the approximately 100 eligible roots per operation gate, all to begin only after SFT1 pins a new coherent REPR freeze exposing `LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String` with its spec, Lean/Python, canonical renderer-API, canonical-universe-profile, render-context, and passed real-goal-coverage hashes and the coordinator merges the additive shared SFT1 label rule; the reviewed `cbc933c3623d81ba649a1f9c5107ad404389d69f` predecessor is not consumable, and the 10K pilot, bulk generation or scale, publication, and every production root or pair-count commitment beyond those two gates remain unapproved.

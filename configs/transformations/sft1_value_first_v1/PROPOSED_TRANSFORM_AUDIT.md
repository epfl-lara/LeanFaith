# SFT1 value-first transform audit and proposal

> **Status:** policy revision 0.3.1 is not gate- or production-approved; the corrected bounded REPR
> integration artifact `v0.3.1` is complete, the bounded implementation envelope is
> user-authorized, proposed Wave 1 gate admission remains pending, and no exact operation, F1 label,
> or row is production-admitted
>
> **Audit date:** 2026-08-30
>
> **Revision:** policy 0.3.1, superseding policy 0.3.0 and distinct from the REPR gate's `v0.3.1`
>
> **Authorization:** strict policy/Lean-free tests and a bounded implementation envelope only;
> selected-wave gate admission, implementation readiness, production admission, row emission, and
> scale remain separate states below
>
> **Lean/data status:** direct-Expr representation gate passed 6/6 and is frozen under receipt
> `f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78`; no transform
> one-example was run and no training row was generated

## 1. Executive verdict

The supplied independent review is accepted as design input. Its central scientific point remains
the boundary for SFT1: “same intended claim” is not kernel-decidable. A deterministic label is
defensible only when an exact, closed, elaborated proposition pair is related by an admitted
transform-local mathematical paraphrase or by an admitted protected-claim mutation, under a
recorded representation and logic regime.

Policy revision 0.3.1 preserves the seven scientific corrections from 0.3.0 and adds the
wave/admission, promotion, drop-receipt, failure-taxonomy, and Git-replay corrections below:

1. The old N-PROOF-only negative core becomes two explicit lanes: operation-specific **N-RUBRIC**
   and a stronger capped **N-PROOF** subtype. Generic D0 is never label evidence.
2. Both endpoints are canonical closed `Expr` values emitted through the approved frozen
   `render_closed_expr_in_session` / `emitClosedProp` route in one persistent Meta request.
   Rendering never copies the renderer or options, declares a theorem/axiom, uses `sorry`,
   synthesizes an endpoint proof, surface-renders or pretty-prints/re-elaborates a candidate, or
   compiles model-facing `goal_v1` text.
3. Every P-LEMMA/P-REFLECT operation carries four claim-erasure guards.
4. An exact 46-operation registry replaces family-wide executable approval. A selected wave, not
   the entire registry, defines the current binding/readiness barrier; every operation remains
   non-executable until its exact gate admission.
5. The required positive and negative starter banks are design-frozen and hash-bound before
   implementation; unused entries are explicitly reserved.
6. “No per-pair Lean” is replaced by “no per-row process spawn”: cheap sampling happens first, but
   every retained row receives bounded typed Meta validation and evidence replay.
7. A zero-Lean root census precedes row commitments. The 2–3M range is a band to measure, not a
   minimum or promise; 500K×8 is 4M, and 5M requires at least 625K roots.

The passed representation gate establishes only that the frozen REPR route can serialize these six
closed reference/candidate pairs without forbidden residue surviving. It did not live-inject both
forbidden strings as adversarial rejection probes, and it is not transform evidence or approval of
policy revision 0.3.1. The bounded implementation envelope is authorized, but Wave 1 gate admission
and readiness remain false. The current freeze permits zero production negatives. The 10K pilot,
training-row emission, bulk scale, publication, and every production row count remain unauthorized.

The policy tracks five non-implying states:

| State | Meaning | Current value |
| --- | --- | --- |
| bounded implementation authorization | scope ceiling for task-owned implementation and bounded gates after admission/readiness; no work may start early and the present revision remains Lean-free | authorized, not actionable |
| implementation readiness | global prerequisites plus resolved execution bindings for every operation in the selected wave | false |
| gate admission | one user decision naming the selected wave, operations/projects, and negative family/dimension gate admissions | false; proposed Wave 1 pending |
| production admission | post-measurement promotion of exact operation versions, projects, caps, and negative dimensions | false for all operations |
| row emission / scale | permission for a model-facing pilot, then any later bulk run or publication | false |

Proposed Wave 1 contains exactly `P01_ALPHA_RENAME_SINGLE_V1`, `P15_SWAP_IFF_SIDES_V1`,
`P18_SYMMETRIZE_EQUALITY_V1`, `P21_BETA_REDUCE_V1`,
`N31_DROP_REQUIRED_GUARD_RUBRIC_V1`, and `N31_DROP_REQUIRED_GUARD_PROOF_V1`, each across its four
registered eligible projects. It has 24 operation-project combinations, 48 success/rejection
fixtures, and approximately 600 roots. The all-46 alternative would have 156 combinations, 312
fixtures, and approximately 4,600 roots. Only current-wave bindings block current-wave readiness;
the other 40 operations remain fail-closed without blocking Wave 1.

## 2. Operational evidence contract

| Code | Exact meaning in policy revision 0.3.1 | Label role |
| --- | --- | --- |
| P-DEF | Exact closed propositions are definitionally equal under pinned transparency/options, with no unresolved term or universe metavariables and no endpoint proof | Positive gate evidence after gate admission; model-facing label only after production admission plus row emission |
| P-SCHEMA | One pinned universally valid schema is instantiated exactly, including all side conditions and its audited dependency closure | Positive gate evidence after gate admission; model-facing label only after production admission plus row emission |
| P-LEMMA | One frozen theorem/lemma rewrites one typed occurrence through an explicit context; arguments, instances, direction, path, dependency closure, and replayable proof are retained | Same staged admission plus all four claim-erasure guards |
| P-REFLECT | One pinned proof-producing procedure yields a kernel-replayable local equality/iff certificate transported through one exact context | Same staged admission plus all four claim-erasure guards |
| N-RUBRIC | One exact typed mutation changes a protected dimension named by the shared consistency rubric, with operation-specific applicability, anti-degeneracy checks, exact-delta reconstruction, and operation/family admission | Negative F1 gate evidence; model-facing label only after production admission and row emission; no F2/truth claim |
| N-PROOF | Stronger capped subtype of one admitted N-RUBRIC operation, retaining an exact source proof and exact candidate refutation for the same closed pair | Same staged admission; candidate truth evidence is `refuted` |
| D0 | Type-safe structural delta only | Candidate/diagnostic evidence, never a label |
| F2-DIR | One implication/specialization/weakening direction only | Auxiliary relation, never a label |

Candidate truth evidence is separately recorded as exactly `proved`, `refuted`, or `unknown`.
It cannot determine the F1 label, select a lane, or turn failed proof search into evidence.

`RowEvidenceReceipt` validates dropped receipts as strictly as retained receipts. The exact terminal
reason determines the coherent allowed states of reference validity, candidate validity, F0/defeq,
F1 certificate, truth, optional F2, and final disposition. Evidence-class/F1 direction invariants
still hold after a drop. Every N-PROOF receipt, including a drop, obeys its exact source-proof and
candidate-refutation field discipline: a certified receipt requires both hashes and `refuted`
candidate truth, while an uncertified receipt forbids both hashes. A terminal reason contradicted
by any axis or proof field is invalid policy data, not a transform rejection.

The root-level blocklist rejects before a candidate receipt exists. Within `RowEvidenceReceipt`,
`candidate_closed_prop_invalid` requires failed candidate validation, unknown F0/truth, no F2, and
uncertified F1; `no_op_dropped`/`cancellation_dropped` require definitionally equal F0 and
uncertified F1; post-transform `blocklist_dropped`, `f1_relation_uncertified`,
`vacuity_rejected`, and `empty_domain_rejected` require a valid candidate and uncertified F1; and
duplicate/split-cluster drops require a class-consistent completed F1 certificate. Retain requires
that same completed certificate.

Positive certificates have the review-required dependency firewall: no source/candidate declaration,
endpoint proof, unrelated theorem proving a whole endpoint, unrestricted theorem search, or
unrestricted simplifier trace. The exact allowed schema/lemma/procedure closure, transparency,
logic regime, and axiom profile are recorded.

Every certificate binds the complete closed telescope: universes, ordered locals, binder kinds,
local definitions, implicit/instance binders, coercions, synthesized instances, hypotheses, and
target.

### Adversarial-review disposition before transform implementation

The external review is adopted where it sharpens executable evidence. The typed policy now binds
separate reference-valid, candidate-valid, F0/defeq, F1-certificate, candidate-truth, optional
F2-direction, and final retain/drop results; an exact certificate checker and dispatch target per
operation; binder/domain and empty-domain profiles; environment/normalization fingerprints;
correlation/effective-diversity groups; production eligibility distinct from bounded
proof-of-concept gate eligibility; exact gate counters; and a certificate/provenance-residue screen
on the early 100-root model-facing surface.
Every concrete execution binding defaults to unresolved and fail-closed. Resolved dispatch,
checker, anchor, applicability-bank, and fixture hashes are readiness requirements for operations
selected into the current wave rather than unverified design claims; unresolved unselected
operations do not block that wave.

Open semantic predicates must become closed checkers or banks before use. In particular, N26 is
limited to mechanically claim-relevant boundary contexts (for example, checked `Fin n` or
`Finset.range n` bounds) and excludes generic exponent, index, or upper-bound edits; N31 gets a
closed required-guard bank for exact nonzero, positivity, nonnegativity, membership, and index-bound
shapes and remains the highest-priority negative proof of concept; and N32 N-RUBRIC admits only
exact role-sensitive `Nat`/`Int` `LT`/`LE` heads, excluding `Eq`, `Iff`, arbitrary relations, and
failed symmetry search.

One review recommendation is rejected as target-changing: N-RUBRIC does not require a kernel proof
of `¬(A ↔ B)`, a false candidate, or a countermodel. Those are stronger F2/N-PROOF facts. A true but
claim-different proposition can be a correct F1 negative, while a whole-proposition `Iff` proof is
not alone sufficient for a positive F1 label because unrelated already-proved endpoints can imply
each other vacuously. Exact transform-local F1 evidence and the dependency firewall remain
mandatory.

### Negative-operation promotion

The current freeze permits **zero production negatives**. An N-RUBRIC or N-PROOF operation is
promoted from proof-of-concept to production-eligible only when:

1. an explicit wave gate-admission decision names its exact ID/version, registered projects,
   protected family/rubric dimension, lane, cap, and wave;
2. the wave passes the actual one-positive/one-negative serialized smoke and the selected-wave
   conformance matrix, including one live success and one expected adversarial rejection for that
   operation in every eligible project;
3. the operation passes its measured approximately-100-eligible-root gate with 100% retained-
   certificate replay and reports applicability, anti-degeneracy/exact-delta outcomes,
   candidate-truth distribution, terminal classes, duplicate/conflict and cache/replay behavior,
   source/project/family/mechanism/template/polarity yield, Lean-seconds, sidecar bytes, RSS, and
   shortcut/surface-residue plus held-out balanced-accuracy diagnostics with confidence bounds; and
4. after that measured report, the user records a separate production-promotion decision naming
   the exact operation/version, projects, family/dimension admission, lane, frozen receipt and
   implementation hashes, axiom profile, and cap. No sibling operation or lane is promoted by
   implication.

The approximately-100-root report is the measured promotion basis; a successful fixture is not.
Production eligibility still does not authorize row emission, a 10K pilot, scale, or publication.
It is represented by the conjunction of transition to `implementation_candidate` and an exact
production-admission record; neither half suffices. N-PROOF additionally requires production
admission of its parent N-RUBRIC operation, and its cap may not exceed the parent's cap.
The promotion record must say, in substance: “Promote exactly `<operation IDs and versions>` under
`<selected-wave measured receipt>` to production-eligible for the named projects,
family/dimensions, lanes, axiom profiles, and caps; do not authorize row emission, a 10K pilot,
scale, publication, or any row-count commitment.”

## 3. Representation and source applicability

The consumable REPR freeze is `176a783842c5a73b84413dfa8347670608b615d9` (implementation
`93cd9cf9d4848827f2bacad57a35c3d7f01500f7`). Its spec, config, Lean, injected-helper,
Python, implementation-set, semantic, API, universe-profile, and render-context hashes are pinned
in the typed policy. The older `cbc933…` freeze remains a non-consumable superseded predecessor.

Reference and candidate are alive together in one `run_meta do` request handled by
`render_closed_expr_in_session`. SFT1 calls `LeanFaith.GoalV1.emitClosedProp` exactly once per
explicitly unrolled endpoint; the frozen emitter owns rendering and payload construction. Complete
sidecars persist, while only `sidecar.core_text()` is model-facing. SFT1 may not copy the
renderer/options, declare candidate theorem/axiom endpoints, synthesize a proof, use `sorry`,
surface-render, pretty-print/re-elaborate a candidate, or compile/re-elaborate `goal_v1` text.

The API rejects Expr/universe metavariables, free variables, loose bvars, ill-typed values,
non-propositions, and anonymous binders that would become unsupported rendered locals. It preserves
nondependent explicit structural arrows. Final SFT1 output maps `[anonymous]` to
`anonymous_binder_name` and `⋯` to `forbidden_rendered_placeholder` and rejects both.
Both endpoints use `goal_v1_first_occurrence_u_i_v1` under the exact frozen universe hash; local
canonicalization may not diverge.

SFT1 independently passed all six real-goal direct-Expr cases in authoritative additive
`attempt_009`: 6/6 pairs and 12/12 endpoints rendered, required-distinct outputs differed, every
output contained exactly one turnstile, complete sidecars were retained, and no `[anonymous]` or
`⋯` residue reached `goal_v1`. These clean successes prove only that no forbidden residue survived;
they were not live adversarial rejection probes injecting either literal. Lean-free behavioral tests
cover the two exact failure mappings. The gate consumed 21.546 measured Lean-seconds and persisted
119,895 sidecar bytes. The frozen regression ID is
`sft1_repr_six_real_goal_direct_expr_v0_3_1`. Its
[`repr_six_goal_gate_receipt_v0_3_1.json`](repr_six_goal_gate_receipt_v0_3_1.json) binds semantic
receipt hash `f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78` and checked-in
receipt-file SHA-256 `ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8`.

The small repo-relative [`repr_six_goal_evidence_v0_3_1/`](repr_six_goal_evidence_v0_3_1/)
bundle makes attempt 009 independently reviewable from Git. Its manifest hash-binds all six case
files and their canonical evidence/request hashes to the receipt, execution/helper preimages,
timings, sidecar-byte total, exact residue mappings, and limited clean-success claim. Its raw file
SHA-256 is `aeb44673d45ce3bb31923fec7ab402c40aefdefa108302deed0b9f0fe0246d46`;
each case file has its own raw hash, and canonical minified JSON reproduces the original attempt-009
receipt evidence hashes. The original `/storage/milikic/.../attempt_009` evidence remains
supplemental provenance, not the only replay path.

The exact executed preimage
[`repr_six_goal_gate_execution_v0_3_1.yaml`](repr_six_goal_gate_execution_v0_3_1.yaml) has raw
file SHA-256 `82f22c08082e26424e1a55627b707d341e7fa84f72348cfed0b007b0526505ff`
and effective canonical hash
`dfc7037ee8d5a340b82b237fa14ef1f3d9c2752bf64e91d34846d9570fac5747`. The frozen post-pass
binding [`repr_six_goal_gate_v0_3_1.yaml`](repr_six_goal_gate_v0_3_1.yaml) has raw file SHA-256
`5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91` and effective canonical
hash `7404e31935ab35b9c3270bf46654936121944a7e8f55fb91da4f1e047f59c0ad`. The reviewed helper
file and injected-preamble hashes remain `c87b9c5065a41f51e7cbdcdcc98f14fedc6a015054c40a0cfe4367ad63330129`
and `bd0e3ef6b5e5c50bf07b31771e2a2ca0da131323d10d8571994bdd24a922981a`.

The correction binds the canonical-gold source file and extracted formal statement at
`a0c4d102a0ea4d2923cca85129c6cda054a11b1854462eed3d7e71e555b703ea` and
`8b0061199a23b47539e6f30df775109d5c6776ea1c2206f452d5a9d48240aa7e`, respectively, and uses
minimal source-faithful compile contexts. Only the two actual big-operator cases open
`BigOperators`; canonical gold opens no notation scope, the compiler case imports only
`Init.Sym.Lemmas`, and Physlib/CSLib use their exact imports. The CSLib `TimeM` notation remains
closed and its fully qualified type passes without weakening the validator.

The previously passing `attempt_008` v0.3.0 config and receipt remain preserved as superseded
evidence, not the active dependency; v0.3.1 adds the exact execution-config preimage,
canonical-gold source pins, and minimal context bindings. Attempts before 008 failed closed and
received no success receipt. They exposed a misnamed Meta helper API, the need to fully qualify
`Lean.Sym.Int.lt_eq_true`, canonical-gold binder/delimiter failures, and unsupported legacy
big-operator syntax in the ConsistencyCheck fixture. Every gate case still has
`production_admission: false`, so neither receipt admits P01 or any other operation.

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

## 5. Exact proposed preserving registry

All entries below remain `executable: false`, `label_emission_authorized: false`, and
`production_admission: false`. The six proposed Wave 1 operations alone are
`pending_gate_admission`; the other 40 are `not_selected`. “Candidate” and “proof of concept”
describe a design stage, not gate or production authorization.

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

## 6. Exact proposed negative registry

Every natural negative family proposed in this registry has an N-RUBRIC operation and an optional
lower-cap N-PROOF sibling. Proposal membership is not gate or production admission. The N-PROOF
pointer, mutation site, rubric dimension, pair hashes, and candidate refutation must match exactly.

| Exact operation ID | Lane / protected dimension | Applicability and anti-degeneracy | Value | Lean cost / exact cap |
| --- | --- | --- | --- | --- |
| N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1 | N-RUBRIC; shared-rubric negation mistakes | complete closed proposition only; reject existing outer negation, True/False defeq, same render; no F2/truth claim | medium, strong cue risk | C1–C2; 1% |
| N19_NEGATE_CLOSED_CLAIM_PROOF_V1 | N-PROOF subtype | exact source proof plus refutation of exact negated candidate; no endpoint declaration | stronger evidence, capped | C2; 0.5% |
| N25_TOGGLE_EQ_NE_RUBRIC_V1 | N-RUBRIC; shared-rubric negation mistakes | one protected typed Eq/Ne; reject defeq operands, unreachable/vacuous site, same render | very high | C1–C2; 1.5% |
| N25_TOGGLE_EQ_NE_PROOF_V1 | N-PROOF subtype | complete telescope assignment, retained hypotheses, source proof, candidate refutation | very high | C3; 0.75% |
| N26_INCREMENT_BOUND_RUBRIC_V1 | N-RUBRIC; shared-rubric edge cases | one protected boundary in the closed mechanically claim-relevant bank (including checked `Fin n`/`Finset.range n` contexts), unchanged type, unique +1 delta; reject generic exponent/index/upper-bound edits and irrelevant/unreachable sites | highest | C1–C2; 1.5% |
| N26_INCREMENT_BOUND_PROOF_V1 | N-PROOF subtype | same admitted boundary site plus exact boundary witness, source proof, and candidate refutation; no timeout/search-failure label | highest | C3; 1% |
| N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1 | **proof of concept**; witness dependency | exact ∀∃→∃∀ bvar remap; two distinguishable inputs, body depends on witness; protect binders/domain/body | highest but rare | C2–C3; 0.75% |
| N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1 | **proof of concept** N-PROOF | complete finite cases prove source and refute uniform-witness candidate | highest | C3; 0.3% |
| N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1 | **proof of concept**; existence/uniqueness | exact Exists→ExistsUnique predicate; two distinguishable candidate witnesses; reject subsingleton | very high | C2–C3; 0.5% |
| N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1 | **proof of concept** N-PROOF | exact source existence proof plus two satisfying disequal witnesses refuting uniqueness | very high | C3; 0.25% |
| N31_DROP_REQUIRED_GUARD_RUBRIC_V1 | **priority-1 proof of concept**; required guard | exact one-local deletion/bvar reindex for a frozen nonzero, positivity, nonnegativity, membership, or index-bound guard; reject arbitrary Prop, unused/redundant guard, True body, and unreachable context | highest priority/value | C2–C3; 1% |
| N31_DROP_REQUIRED_GUARD_PROOF_V1 | **priority-1 proof of concept** N-PROOF | complete values/hypotheses plus source proof and unguarded-candidate refutation | highest priority/value | C3; 0.5% |
| N32_SWAP_ROLE_ORDER_RUBRIC_V1 | **proof of concept**; shared-rubric converse mistakes | reverse distinct same-typed arguments only under admitted `Nat`/`Int` `LT`/`LE` heads; reject `Eq`, `Iff`, arbitrary/symmetric heads, failed symmetry search, and function-composition reorderings; no P32/P34/P42 overlap | very high | C2–C3; 0.5% |
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

Admission is exact-operation-local and stage-specific. A family status, polarity, bank membership,
or successful proof cannot admit neighboring operations. Diagnostic and unresolved entries can
never emit rows. Nine family-and-rubric-dimension records bind the natural negative families and
both N28 synthetic dimensions to their exact N-RUBRIC/N-PROOF member IDs. Only the N31 required-
domain-guard record is selected into proposed Wave 1 and awaits that wave's single gate-admission
decision; the other eight are `not_selected`. All nine production admissions are false. Matching
family/dimension and exact-operation decisions are both required at the gate and production stages.

Resolved dispatch, checker, anchor, closed-bank, and fixture bindings are readiness requirements
only for current-wave operations. An unselected operation remains unresolved and fail-closed but
does not block a higher-confidence selected wave. This removes the old all-46 execution barrier
without weakening any individual operation.

## 8. Composition safety

The exact grammar is:

```text
positive_row := P | P P | P P P
negative_row := N | P N | P P N
```

For model-facing rows, `P` is a production-admitted positive operation and `N` is one
production-admitted N-RUBRIC operation or its separately admitted capped N-PROOF subtype; row
emission must also be authorized. Bounded gate artifacts use the same grammar with gate-admitted
operations without acquiring production or emission status. A positive row has zero negative
operations; a negative row has exactly one. Nothing follows N. At most three total operations occur.

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
source eligibility, operation applicability, root blocklist, pre-validation sampling, typed
candidate construction/closure/type/render validation, post-transform blocklist, typed F1 evidence
validation/replay, stable ordering, canonical-unordered-pair
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

The approved REPR freeze remains byte-pinned, and the independent SFT1 six-real-goal direct-Expr
receipt has passed the representation-specific checks. Each reference/candidate pair was emitted
once per endpoint through the frozen route in the same request; required-distinct outputs differed,
and each output contained exactly one turnstile. These successful endpoints prove that no forbidden
residue survived; they do not constitute live adversarial rejection probes for both forbidden
strings. The frozen route rejects Expr/render residue
involving mvars, universe mvars, fvars, loose bvars, unsupported anonymous rendered locals,
`[anonymous]`, `⋯`, ill-typed values, or non-Props, while preserving nondependent explicit
structural arrows. The exact taxonomy maps `[anonymous]` to `anonymous_binder_name` and `⋯` to
`forbidden_rendered_placeholder`; Lean-free behavioral tests inject both literals separately.

Representation failures are counted by source, family, exact operation, polarity, and exact failure
class. Stable IDs/sidecars bind both closed-Expr hashes, renderer/spec hashes, canonical-universe-
profile ID/hash, implementation/freeze commits, all renderer implementation hashes, fixed-preamble
and compile-context hashes, and both rendered-output hashes. P23 additionally requires its
`Name.mkSimple` collision/no-anonymous-binder regression before any wave selecting P23. Inline
schema/lemma/procedure resolutions and per-operation/per-project fixture bundles must be
hash-frozen for every selected-wave operation before that wave's smoke; unresolved unselected
operations remain fail-closed without blocking the wave.

The remaining gate sequence is:

1. Complete the zero-Lean root census and source-eligibility matrix.
2. **Completed:** freeze and pin the 6/6 six-real-goal SFT1 receipt described above. This is a
   representation dependency result only.
3. Strict-load and test the exact result schema, wave policy, production-versus-gate admissions,
   drop-receipt rules, counters, and residue screens. Obtain the additive shared label contract
   merge and complete the zero-Lean census. Resolve dispatch, certificate checkers,
   binder/domain/environment profiles, anchors/banks, and fixtures only for the selected wave. The
   bounded implementation envelope is authorized, but proposed Wave 1 still needs the exact user
   gate-admission decision below before any Lean or transform execution.
4. After Wave 1 readiness and gate admission, serialize one actual positive and one actual negative
   example end to end, including each final core projection, complete sidecar, content-hash manifest
   link, stable ancestry/operation IDs, durable journal, cache replay, and second-attempt duplicate
   suppression. The positive operation is exactly `P01_ALPHA_RENAME_SINGLE_V1`; the negative is
   exactly `N31_DROP_REQUIRED_GUARD_RUBRIC_V1`. After the census, choose each root seedlessly by the
   minimum stable eligible-root hash for its bound operation. Both certificates replay 100%; counts
   alone cannot substitute for these bindings, and the outputs are gate artifacts, not training-row
   emission.
5. Only after that two-example smoke passes, run the selected-wave conformance matrix: one live
   success and one expected adversarial rejection for each selected operation-project combination.
   Proposed Wave 1 has 24 combinations and 48 fixtures. Contradictory drop receipts fail the schema;
   zero yield needs a census-backed policy revision, not a silent waiver.
6. Only after conformance passes, process approximately 100 eligible roots per selected operation
   with 100% retained-certificate replay. Wave 1 is approximately 600 roots. The superseded all-46
   barrier would have cost 156 combinations, 312 fixtures, and approximately 4,600 roots.
7. Stop and report. Record any exact operation-production promotions supported by the measurements;
   only then request a separate user decision for the 10K pilot.

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

## 11. Coordinator dependencies

- REPR dependency delivery and SFT1's independent direct-Expr coverage gate are resolved by the
  approved `176a783…` freeze and authoritative passed receipt `f62b68e…`. This closes
  representation consumability only; attempt 008 remains preserved superseded evidence.
- `plans/00_shared_contracts.md` must be updated additively so exact row evidence plus production
  admission of the exact operation/family/rubric dimension and separate row-emission authorization
  creates an SFT1 model-facing label; gate admission creates bounded evidence only. Polarity
  multiplication, D0, F2 direction, failed search, and candidate provability do not. It must record
  N-RUBRIC and capped N-PROOF lanes and the separate `proved|refuted|unknown` truth field.
- The task-owned typed policy/loader now encodes and invariant-tests the exact machine-schema,
  certificate partitions, binder/domain/environment profiles, correlation, eligibility, counter,
  residue, anchor, fixture, wave, and admission requirements in section 10. Concrete bindings need
  resolve only for selected-wave operations; unselected entries remain fail-closed.

This SFT1 session does not own the shared-contract coordinator path. Policy revision 0.3.1 leaves
`plans/00_shared_contracts.md` untouched and retains the additive change only as this coordinator
request.

## 12. Exact current and future decisions

The one exact current decision is:

> Approve SFT1 Wave 1 gate admission for P01_ALPHA_RENAME_SINGLE_V1, P15_SWAP_IFF_SIDES_V1, P18_SYMMETRIZE_EQUALITY_V1, P21_BETA_REDUCE_V1, N31_DROP_REQUIRED_GUARD_RUBRIC_V1, and N31_DROP_REQUIRED_GUARD_PROOF_V1 across their registered eligible projects, including gate admission of the N31 required_domain_guard family/dimension for those two negative operations, solely for bounded implementation, the one-positive/one-negative end-to-end smoke, the selected-wave conformance matrix, and the approximately-100-roots-per-operation gate; do not grant production admission, row emission, a 10K pilot, scale, publication, or any row-count commitment.

This decision is not recorded by the bounded implementation authorization or by the older GPT Pro
snapshot. After the wave gates, record exact operation-production promotions separately. Only then
request exactly:

> Approve only the measured 10K SFT1 pilot described in the completed one-positive/one-negative smoke, selected-wave conformance, and approximately-100-roots-per-operation report; do not approve bulk generation, scale, publication, or any production root or pair-count commitment.
